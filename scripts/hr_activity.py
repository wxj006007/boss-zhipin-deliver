"""User-configured HR activity filtering. Offline is not the same as inactive."""
from datetime import datetime, timedelta, timezone
import json
import os
import re

SHANGHAI = timezone(timedelta(hours=8))

# Shared by the Vue extractor, DOM fallback and selected-job reader. Read only
# dedicated recruiter activity elements, never arbitrary job-description text.
ACTIVITY_HELPER_JS = r"""
function bossActivity(root, data, source) {
    const nodes = root ? [...root.querySelectorAll('.boss-active-time,.boss-online-text')] : [];
    const texts = [...new Set(nodes.map(e => (e.textContent || '').trim()).filter(Boolean))];
    return {online: typeof data?.bossOnline === 'boolean' ? data.bossOnline : null,
            text: texts.length === 1 ? texts[0] : '', source,
            observed_at: new Date().toISOString()};
}
"""

READ_ACTIVITY_JS = r"""(() => {
    const jid = JOB_ID;
    ACTIVITY_HELPER
    const link = [...document.querySelectorAll('a[href*="/job_detail/"]')]
      .find(e => (e.getAttribute('href') || '').split('/job_detail/')[1]?.split(/[.?]/)[0] === jid);
    const card = link?.closest('.job-card-wrap') || link?.closest('li');
    const data = card?.__vue__?.$props?.data || {};
    const result = bossActivity(card, data, 'card');
    const panel = document.querySelector('.job-detail-box') || document.querySelector('#main .job-detail');
    const target = !!panel && [...panel.querySelectorAll('button,a')]
      .some(e => (e.getAttribute('ka') || '').includes(jid));
    if (!target) return JSON.stringify({target_verified:false, activity:result});
    const detail = bossActivity(panel, {}, 'detail');
    if (detail.text) { result.text = detail.text; result.source = 'detail'; }
    return JSON.stringify({target_verified:true, activity:result});
})()""".replace('ACTIVITY_HELPER', ACTIVITY_HELPER_JS)


def policy(profile=None):
    if profile is None:
        from scripts.config_v3 import load_profile
        profile = load_profile()
    rule = profile.get('hr_activity_filter')
    if rule is None:
        return {'mode': 'off', 'unknown': 'allow'}
    if not isinstance(rule, dict) or rule.get('mode') not in ('off', 'today'):
        raise ValueError('hr_activity_filter.mode 必须为 off 或 today')
    if rule.get('unknown', 'allow') not in ('allow', 'skip'):
        raise ValueError('hr_activity_filter.unknown 必须为 allow 或 skip')
    return {'mode': rule['mode'], 'unknown': rule.get('unknown', 'allow')}


def display(activity):
    activity = activity or {}
    return activity.get('text') or ('在线' if activity.get('online') is True else
                                   '当前离线，活跃时间未知' if activity.get('online') is False else '活跃度未知')


def classify(activity, now=None):
    """Return today / outside_today / unknown using China calendar dates."""
    now = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    text = re.sub(r'\s+', '', str((activity or {}).get('text') or ''))
    if re.fullmatch(r'(?:当前|正在)?在线|刚刚(?:活跃|在线)?|(?:今天|今日)(?:\d{1,2}:\d{2})?(?:活跃|在线)?', text):
        return 'today'
    relative = re.fullmatch(r'(\d+)(分钟|小时)(?:前|内)(?:活跃|在线)?', text)
    if relative:
        amount = int(relative[1])
        delta = timedelta(minutes=amount) if relative[2] == '分钟' else timedelta(hours=amount)
        return 'today' if (now - delta).date() == now.date() else 'outside_today'
    date = re.fullmatch(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?(?:活跃|在线)?', text)
    if date:
        try:
            actual = datetime(*map(int, date.groups()), tzinfo=SHANGHAI).date()
            if actual > now.date():
                return 'unknown'
            return 'today' if actual == now.date() else 'outside_today'
        except ValueError:
            return 'unknown'
    if re.fullmatch(r'(?:昨天|昨日|前天)(?:活跃|在线)?|(?:本周|本月|近期|最近|很久以前|很久未)(?:活跃|在线)|'
                    r'(?:近)?\d+(?:天|日|周|月)(?:前|内)?(?:活跃|在线)|不活跃|长期未活跃', text):
        return 'outside_today'
    # A dedicated detail label takes priority over a potentially stale online flag.
    if (activity or {}).get('online') is True:
        return 'today'
    return 'unknown'


def decide(activity, profile=None, now=None):
    rule = policy(profile)
    state = classify(activity, now=now)
    allowed = rule['mode'] == 'off' or state == 'today' or (state == 'unknown' and rule['unknown'] == 'allow')
    if rule['mode'] == 'off':
        reason = '未启用HR活跃度筛选'
    elif state == 'today':
        reason = 'HR在线或今天活跃'
    elif state == 'unknown':
        reason = '活跃度未知，继续按岗位匹配度决定' if allowed else '活跃度未知，按配置跳过'
    else:
        reason = 'HR活跃标签未满足今天活跃要求：' + str((activity or {}).get('text') or '')
    return {'allowed': allowed, 'status': state, 'reason': reason, 'policy': rule,
            'observation': activity or {}}


def check_current(job_id, session, profile=None):
    rule = policy(profile)
    if rule['mode'] == 'off':
        return decide({}, {'hr_activity_filter': rule})
    if not re.fullmatch(r'[A-Za-z0-9_-]+', str(job_id or '')):
        raise ValueError('无效岗位ID')
    from scripts.webbridge_client import evaluate, AbortScriptError
    result = json.loads(evaluate(READ_ACTIVITY_JS.replace('JOB_ID', json.dumps(job_id)), session=session) or '{}')
    # Unknown activity is allowed, but an unknown target or broken read is not.
    if not result.get('target_verified'):
        raise AbortScriptError('活跃度复核时岗位面板不匹配，停止操作')
    return decide(result.get('activity') or {}, {'hr_activity_filter': rule})


def skip_result(check):
    return {'delivered_ok': False, 'skipped': True, 'activity_filtered': True,
            'reason': check['reason'], 'hr_activity': check}


def record_skip(workdir, job, check, stage):
    from scripts.webbridge_client import ProgressManager
    from scripts.accounts import metadata
    pm = ProgressManager(os.path.join(workdir, 'stream_progress.json')).load()
    now = datetime.now(SHANGHAI)
    item = dict(jobId=job['jobId'], title=job.get('title', ''), company=job.get('company', ''),
                reason=check['reason'], hr_activity=check, stage=stage,
                checked_at=now.isoformat(), date=now.date().isoformat(), **metadata())
    rows = pm.data.setdefault('activity_skipped', [])
    # Store the latest observation for this job/run/day, without permanent rejection.
    rows[:] = [r for r in rows if (r.get('jobId'), r.get('run'), r.get('date')) !=
               (item['jobId'], item.get('run'), item['date'])]
    rows.append(item)
    pm.save()
