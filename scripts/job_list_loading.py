"""Background-safe list loading through DOM scroll events, without tab activation."""
import json
import time

from scripts import webbridge_client as wb


STATE_JS = r"""(() => {
 const cards=[...document.querySelectorAll('.job-card-wrap')];
 const list=document.querySelector('.job-list-container,.rec-job-list,#main .job-list,.job-list');
 const ids=[...new Set(cards.map(e=>e.__vue__?.$props?.data?.encryptJobId ||
   e.querySelector('a[href*="/job_detail/"]')?.getAttribute('href')?.match(/\/job_detail\/([^.?/]+)/)?.[1]).filter(Boolean))];
 let vm=cards[0]?.__vue__ || document.querySelector('.page-jobs-main')?.__vue__;
 for(let i=0;vm&&i<10&&vm.$options?.name!=='PageJobs';i++)vm=vm.$parent;
 const data=vm?.$options?.name==='PageJobs'?vm:null;
 const loading=!!(data?.loading || data?.moreLoading);
 const markers=['没有更多了','已经到底','到底啦','没有更多职位','没有职位了'];
 const endNode=list?[...list.querySelectorAll('*')].find(e=>!e.closest('.job-card-wrap') &&
   !e.children.length && e.getClientRects().length>0 && markers.includes(e.innerText?.trim())):null;
 const root=document.scrollingElement||document.documentElement;
 return JSON.stringify({url:location.href,time_origin:performance.timeOrigin,
   on_list:location.hostname==='www.zhipin.com'&&location.pathname==='/web/geek/jobs',
   ready:!!(list||data),ids,count:ids.length,page:data?.pageVo?.page??null,
   has_more:typeof data?.hasMore==='boolean'?data.hasMore:null,loading,
   end_marker:endNode?.innerText?.trim()||'',visibility:document.visibilityState,
   scroll_top:root?.scrollTop,scroll_height:root?.scrollHeight,client_height:root?.clientHeight});
})()"""

SCROLL_JS = r"""(() => {
 const expected=EXPECTED;
 if(location.href!==expected.url || performance.timeOrigin!==expected.time_origin)
   throw new Error('BOSS_LIST_DOCUMENT_CHANGED');
 const list=document.querySelector('.job-list-container,.rec-job-list,#main .job-list,.job-list');
 let root=null;
 for(let p=document.querySelector('.job-card-wrap')?.parentElement||list;p&&p!==document.body;p=p.parentElement){
   if(/^(auto|scroll)$/.test(getComputedStyle(p).overflowY) && p.clientHeight>0 && p.scrollHeight>p.clientHeight+4){root=p;break;}
 }
 const page=document.scrollingElement||document.documentElement;
 root=root||page;
 if(!root)throw new Error('BOSS_LIST_SCROLL_CONTAINER_MISSING');
 root.scrollTop=root.scrollHeight;
 // Hidden tabs may update scrollTop without delivering the native scroll event.
 // Notify the actual container and the page listeners even when already at bottom.
 root.dispatchEvent(new Event('scroll'));
 document.dispatchEvent(new Event('scroll'));
 window.dispatchEvent(new Event('scroll'));
 return JSON.stringify({container:root===page?'document':root.className,top:root.scrollTop});
})()"""


def read_state(session=wb.SESSION):
    try:
        state = json.loads(wb.evaluate(STATE_JS, session=session))
    except (ValueError, TypeError) as error:
        raise wb.AbortScriptError('岗位列表状态读取失败，不能判断已到底') from error
    if (not isinstance(state, dict) or state.get('on_list') is not True
            or not isinstance(state.get('ids'), list) or not state.get('url')
            or not isinstance(state.get('time_origin'), (int, float))):
        raise wb.AbortScriptError('当前不是可核验的岗位列表，停止滚动')
    return state


def is_exhausted(state):
    return bool(state.get('ready') and not state.get('loading') and
                (state.get('has_more') is False or
                 (state.get('has_more') is not True and state.get('end_marker'))))


def summary(state):
    return {key: value for key, value in state.items() if key != 'ids'}


def advance_once(session=wb.SESSION, timeout=8, max_triggers=2):
    """Load one batch or return an explicit non-terminal stall within a deadline."""
    before = read_state(session)
    current = before
    initial_ids = set(before['ids'])
    deadline = time.monotonic() + timeout
    next_trigger = time.monotonic()
    attempts = 0

    def result(status):
        return dict(status=status, confirmed_end=status == 'exhausted', attempts=attempts,
                    new_ids=[jid for jid in current['ids'] if jid not in initial_ids],
                    before=summary(before), after=summary(current))

    while True:
        if (current['url'], current['time_origin']) != (before['url'], before['time_origin']):
            raise wb.AbortScriptError('滚动期间列表文档或搜索条件改变，不继续加载')
        if is_exhausted(current):
            return result('exhausted')  # May include the final batch: caller must scan it.
        if not current.get('loading'):
            if set(current['ids']) - initial_ids:
                return result('loaded')
            if (isinstance(current.get('page'), int) and isinstance(before.get('page'), int)
                    and current['page'] > before['page']):
                return result('advanced')  # A page of duplicate IDs is not proof of EOF.
        now = time.monotonic()
        if now >= deadline:
            return result('loading' if current.get('loading') else 'stalled')
        if current.get('ready') and not current.get('loading') and attempts < max_triggers and now >= next_trigger:
            code = SCROLL_JS.replace('EXPECTED', json.dumps({k: before[k] for k in ('url', 'time_origin')}), 1)
            wb.evaluate_once(code, session=session)
            attempts += 1
            next_trigger = now + 2
        time.sleep(min(.25, max(0, deadline - time.monotonic())))
        current = read_state(session)


def advance(session=wb.SESSION, times=1):
    rounds = []
    for _ in range(max(1, times)):
        outcome = advance_once(session=session)
        rounds.append(outcome)
        if outcome['status'] not in ('loaded', 'advanced'):
            break
    return dict(rounds[-1], rounds=rounds)


def main():
    """Account-runner diagnostic: observe background loading without contacting HR."""
    import argparse
    from pathlib import Path
    import uuid
    from scripts.accounts import assert_run, metadata
    from scripts.account_browser import verify_identity
    from scripts.chat_tab import tab_command
    from scripts.pipeline_batch import kw_url
    from scripts.runtime import atomic_write_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--keyword', help='在独立后台标签页测试此关键词，结束后选回原标签页')
    parser.add_argument('--steps', type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.steps <= 10:
        parser.error('--steps 必须为 1–10')
    account = assert_run()
    if account is None:
        parser.error('请通过 run_account.py scroll-check 显式选择账户')
    original = json.loads(wb.evaluate('JSON.stringify({url:location.href,time_origin:performance.timeOrigin})'))
    tabs = tab_command('list_tabs')['tabs']
    source = [tab for tab in tabs if tab.get('url') == original['url']]
    if len(source) != 1:
        raise wb.AbortScriptError('当前标签页不能唯一定位，不创建诊断标签页')
    report = dict(metadata(), source_tab_id=source[0]['tabId'], steps=[], read_only=True)
    report['foreground_before'] = [t['tabId'] for t in tab_command('list_browser_tabs')['tabs'] if t.get('active')]
    path = Path(account['workdir']) / 'scroll_checks' / (uuid.uuid4().hex + '.json')
    try:
        if args.keyword:
            tab = tab_command('navigate', {'url': kw_url(args.keyword), 'newTab': True})
            report['test_tab_id'] = tab['tabId']
            if tab['tabId'] == source[0]['tabId']:
                raise wb.AbortScriptError('未创建独立诊断标签页')
            time.sleep(3)
        verify_identity()
        initial = read_state()
        report['initial'] = summary(initial)
        print(json.dumps({'initial': report['initial']}, ensure_ascii=False), flush=True)
        if initial['visibility'] != 'hidden':
            raise wb.AbortScriptError('诊断页面并非隐藏状态，不宣称完成后台验证')
        for _ in range(args.steps):
            outcome = advance()
            report['steps'].append(outcome)
            atomic_write_json(path, report)
            print(json.dumps({k: v for k, v in outcome.items() if k not in ('new_ids', 'rounds')}, ensure_ascii=False), flush=True)
            if outcome['status'] not in ('loaded', 'advanced'):
                break
    finally:
        if args.keyword:
            tab_command('attach_tab', {'tabId': source[0]['tabId']})
        restored = json.loads(wb.evaluate('JSON.stringify({url:location.href,time_origin:performance.timeOrigin})'))
        report['source_preserved'] = restored == original
        report['foreground_after'] = [t['tabId'] for t in tab_command('list_browser_tabs')['tabs'] if t.get('active')]
        report['foreground_unchanged'] = report['foreground_after'] == report['foreground_before']
        atomic_write_json(path, report)
    print(json.dumps({'report': str(path), 'source_preserved': report['source_preserved'],
                      'foreground_unchanged': report['foreground_unchanged']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
