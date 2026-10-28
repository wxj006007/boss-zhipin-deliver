"""Read one live JD, wait for the agent's decision, then send before reading another.

Run through run_account.py immediate. No model API or automatic semantic approval.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.parse import urlparse
import uuid

from scripts import _live_driver as drv, webbridge_client as wb
from scripts import phase_stream as ps
from scripts.execution_context import metadata, fingerprint
from scripts.pipeline_batch import kw_url
from scripts.runtime import atomic_write_json, resolve_workdir
from scripts.hr_activity import record_skip


# Bounded absorption of HR-activity-filtered cards inside one `next` call.
# Those cards are never reviewable, so bouncing the caller once per card only
# adds process/IO overhead; each skip is still recorded individually.
MAX_ABSORBED_SKIPS = 10


class Immediate:
    def __init__(self):
        self.root = Path(resolve_workdir())
        self.path = self.root / 'immediate_state.json'
        self.profile_key = fingerprint(drv.load_profile_cached())
        self.run = metadata().get('run', 'single')

    def load(self):
        if not self.path.exists():
            raise ValueError('先运行 immediate start <关键词>')
        state = json.loads(self.path.read_text(encoding='utf-8'))
        if state['run'] != self.run or state['profile_key'] != self.profile_key:
            raise ValueError('本轮运行或画像已变化，请重新 immediate start')
        return state

    def save(self, state):
        atomic_write_json(self.path, state)

    def event(self, stage, started=None, **fields):
        row = dict(at=datetime.now(timezone.utc).isoformat(), stage=stage, **metadata(), **fields)
        if started is not None:
            row['elapsed_s'] = round(time.monotonic() - started, 3)
        with (self.root / 'immediate_events.jsonl').open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')

    def guard(self):
        progress = wb.ProgressManager(str(self.root / 'stream_progress.json')).load()
        if progress.data.get('uncertain') or drv.walk_state().get('delivery_halted'):
            raise ValueError('有待核对发送或停止状态；先核对，不能通过新流程绕过去重或停止')
        return progress

    def page(self, state):
        current = wb.evaluate('location.href') or ''
        # Full query equality preserves city, type, scale and pagination, not just keyword.
        actual, expected = urlparse(current), urlparse(state['url'])
        from urllib.parse import parse_qs
        if (actual.hostname != expected.hostname or actual.path != expected.path
                or parse_qs(actual.query) != parse_qs(expected.query)):
            raise ValueError('当前页面已偏离本轮搜索条件，请先核对；不自动重新导航')
        anomaly, reason = wb.detect_page_anomaly(check_blank=False)
        if anomaly:
            raise wb.AbortScriptError(reason)
        ps.CURRENT_WORK_URL['url'] = current

    def start(self, keyword):
        self.guard()
        previous = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        if previous.get('run') == self.run and previous.get('pending'):
            raise ValueError('当前还有待审核岗位，先 decide 后再切关键词')
        started = time.monotonic()
        url = kw_url(keyword)
        wb.navigate(url, wait_range=(5, 7), expect_url_contains='query=', label=f'逐岗-{keyword}')
        state = dict(run=self.run, profile_key=self.profile_key, keyword=keyword, url=url,
                     pending=None, handled=previous.get('handled', {}) if
                     previous.get('run') == self.run and previous.get('profile_key') == self.profile_key else {})
        self.page(state)
        self.save(state)
        self.event('start', started, keyword=keyword)
        return {'started': True, 'keyword': keyword, 'next': 'immediate next'}

    def next(self):
        progress = self.guard()
        state = self.load()
        if state.get('pending'):
            pending = state['pending']
            # A crash after successful send but before state commit must not resend.
            if pending['job']['jobId'] not in progress.delivered_ids:
                return dict(pending, awaiting_decision=True)
            state['handled'][pending['job']['jobId']] = 'delivered'
            state['pending'] = None
            self.save(state)
        self.page(state)
        started = time.monotonic()
        skipped = 0
        loading = None
        for scroll in range(3):
            jobs = wb.extract_jobs()
            for job in jobs:
                jid = job.get('jobId')
                if not jid or jid in state['handled'] or progress.is_done(jid):
                    continue
                if not drv.mechanical_ok(job, drv.load_profile_cached())['ok']:
                    state['handled'][jid] = 'mechanical_skip'
                    self.save(state)
                    continue
                # Only open cards in the freshly read DOM. Do not process a saved pool.
                if not ps.card_exists(jid):
                    state['handled'][jid] = 'not_present'
                    self.save(state)
                    continue
                opened = drv.cmd_open(jid)
                if not opened.get('panel_ok') or not str(opened.get('detail') or '').strip():
                    state['handled'][jid] = 'read_failed'
                    self.save(state)
                    self.event('read_failed', started, jobId=jid)
                    return {'read_failed': True, 'jobId': jid, 'detail': opened}
                activity = opened.get('hr_activity') or {}
                if activity.get('allowed') is False:
                    # Activity-filtered cards are not reviewable jobs. Keep the
                    # per-card record, but absorb a bounded number inside this
                    # call instead of bouncing the caller for every one.
                    record_skip(str(self.root), job, activity, 'immediate_read')
                    state['handled'][jid] = 'activity_skip'
                    self.save(state)
                    self.event('activity_skip', started, jobId=jid)
                    skipped += 1
                    if skipped >= MAX_ABSORBED_SKIPS:
                        self.event('activity_skip_break', started, absorbed=skipped)
                        return {'activity_skipped': True, 'jobId': jid,
                                'reason': activity.get('reason'), 'absorbed': skipped,
                                'hint': '本次已吸收多张活跃度不符卡片，可继续 next'}
                    continue
                pending = dict(job=job, detail=opened['detail'], hr_activity=activity,
                               review_id=uuid.uuid4().hex, read_at=time.time(),
                               activity_skipped_count=skipped)
                state['pending'] = pending
                self.save(state)
                self.event('read', started, jobId=jid, absorbed_activity_skips=skipped)
                return dict(pending, awaiting_decision=True)
            self.save(state)
            # A terminal response can contain the last batch. Only report EOF
            # after scanning its DOM, not immediately after the scroll request.
            if loading and loading.get('confirmed_end'):
                self.event('exhausted', started, keyword=state['keyword'],
                           absorbed_activity_skips=skipped, loading=loading)
                return {'view_exhausted': True, 'search_exhausted': True,
                        'activity_skipped_count': skipped, 'loading': loading,
                        'hint': '当前搜索列表已明确返回无更多岗位；不代表网站全部供给已穷尽'}
            if scroll < 2:
                loading = ps.scroll_jobs_list(times=1)
                if loading.get('status') in ('loading', 'stalled'):
                    self.event('load_deferred', started, keyword=state['keyword'], loading=loading)
                    return {'next_deferred': '列表加载未完成或暂未增长，不能认定已读尽',
                            'view_exhausted': False, 'loading': loading,
                            'activity_skipped_count': skipped,
                            'hint': '保留当前页面，可重试 next；若反复停滞则检查连接或页面加载状态'}
        self.event('scan_limit', started, keyword=state['keyword'], loading=loading)
        return {'next_deferred': '本次扫描达到批次上限，尚未确认到底',
                'view_exhausted': False, 'activity_skipped_count': skipped,
                'hint': '继续在原页面调用 next，不因本次批次上限重新搜索或判定耗尽'}

    def decide(self, decision):
        progress = self.guard()
        state = self.load()
        pending = state.get('pending')
        if not pending or decision.get('jobId') != pending['job']['jobId'] or decision.get('review_id') != pending['review_id']:
            raise ValueError('决策必须对应当前待审核岗位和 review_id，禁止跨岗位或旧审核发送')
        action = decision.get('action')
        if action not in ('approve', 'reject') or not str(decision.get('reason') or '').strip():
            raise ValueError('需要 action=approve/reject 和具体审核 reason')
        if action == 'approve':
            greeting = drv.validate_greeting(decision.get('greeting'))
        job, jid = pending['job'], pending['job']['jobId']
        started = time.monotonic()
        self.event('decision', jobId=jid, action=action, reason=decision['reason'],
                   review_wait_s=round(time.time() - pending['read_at'], 3))
        if action == 'reject':
            result = {'rejected': True, 'jobId': jid}
            status = 'rejected'
        elif progress.is_done(jid):
            result, status = {'already_done': True, 'jobId': jid}, 'already_done'
        else:
            self.page(state)
            result = drv.cmd_deliver(jid, job.get('company', ''), job.get('salary', ''),
                                    job.get('industry', ''), decision.get('direction', ''),
                                    greeting=greeting, reviewed_detail=pending['detail'])
            if result.get('review_required'):
                # Keep this job eligible, but force a fresh JD and review token.
                state['pending'] = None
                self.save(state)
                self.event('review_required', started, jobId=jid)
                return result
            status = ('delivered' if result.get('delivered_ok') else
                      'uncertain' if result.get('uncertain') else 'deferred')
        state['handled'][jid] = status
        state['pending'] = None
        self.save(state)
        self.event('result', started, jobId=jid, status=status, result=result)
        return dict(result, jobId=jid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('start').add_argument('keyword')
    sub.add_parser('next')
    sub.add_parser('decide').add_argument('file')
    sub.add_parser('step').add_argument('file')
    sub.add_parser('status')
    args = parser.parse_args()
    flow = Immediate()
    try:
        if args.command == 'start':
            result = flow.start(args.keyword)
        elif args.command == 'next':
            result = flow.next()
        elif args.command == 'decide':
            result = flow.decide(json.loads(Path(args.file).read_text(encoding='utf-8-sig')))
        elif args.command == 'step':
            # One process per job: send the decision made from the JD just read,
            # then read the next job. Semantics are unchanged (the agent still
            # decides from the pending JD, one pending job at a time); this only
            # halves process/IO overhead per job.
            decided = flow.decide(json.loads(Path(args.file).read_text(encoding='utf-8-sig')))
            try:
                following = flow.next()
            except (ValueError, OSError, wb.AbortScriptError) as error:
                following = {'next_deferred': str(error)}
            result = {'decided': decided, 'following': following}
        else:
            result = flow.load()
        print(json.dumps({'ok': True, 'data': result}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, wb.AbortScriptError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
