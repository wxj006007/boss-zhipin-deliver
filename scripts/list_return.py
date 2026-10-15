"""Return to the retained job list without reloading its search URL.

The browser may restore the original document from its back/forward cache.
Never claim preservation from URL equality alone, and never silently navigate.
"""
import json
import time
import uuid
from scripts.webbridge_client import evaluate, evaluate_once, SESSION


CAPTURE_JS = r"""(() => {
    const token = TOKEN;
    if (location.hostname !== 'www.zhipin.com' || location.pathname !== '/web/geek/jobs')
        return JSON.stringify({ok:false,reason:'不在岗位列表页'});
    const nodes = [...document.querySelectorAll('.job-card-wrap')];
    const ids = nodes.map(e => e.__vue__?.$props?.data?.encryptJobId ||
        e.querySelector('a[href*="/job_detail/"]')?.getAttribute('href')?.match(/\/job_detail\/([^.?/]+)/)?.[1]);
    if (!nodes.length || ids.some(id => !id))
        return JSON.stringify({ok:false,reason:'无法记录当前列表岗位ID'});
    const scrolls = [...new Set([document.scrollingElement, ...nodes.flatMap(e => {
        const result=[]; for(let p=e.parentElement; p; p=p.parentElement)
            if(p.scrollHeight>p.clientHeight+4) result.push(p);
        return result;
    })])].filter(Boolean).map(el => ({el,top:el.scrollTop,left:el.scrollLeft}));
    const context = {token,url:location.href,ids,timeOrigin:performance.timeOrigin,historyLength:history.length};
    window.__bossListReturn = {...context,nodes,scrolls};
    return JSON.stringify({ok:true,...context});
})()"""

PROBE_JS = r"""(() => {
    const c = CONTEXT, p = window.__bossListReturn;
    const sameDocument = !!p && p.token === c.token && performance.timeOrigin === c.timeOrigin;
    const onList = location.href === c.url;
    const visible = e => !!e && e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
    const chatVisible = [...document.querySelectorAll('#chat-input[contenteditable="true"],textarea.input-area')].some(visible);
    const ids = [...document.querySelectorAll('.job-card-wrap')].map(e =>
        e.__vue__?.$props?.data?.encryptJobId ||
        e.querySelector('a[href*="/job_detail/"]')?.getAttribute('href')?.match(/\/job_detail\/([^.?/]+)/)?.[1]).filter(Boolean);
    // A sent job may disappear. All other original jobs must still occur in order.
    const expected = c.ids.filter(id => id !== JOB_ID);
    const remaining = ids.filter(id => expected.includes(id));
    const missing = expected.filter(id => !ids.includes(id));
    return JSON.stringify({url:location.href,referrer:document.referrer,historyLength:history.length,
        on_list:onList,same_document:sameDocument,chat_visible:chatVisible,
        missing_ids:missing,order_preserved:JSON.stringify(expected)===JSON.stringify(remaining),
        before_count:c.ids.length,after_count:ids.length,
        same_nodes:sameDocument && p.nodes.filter((e,i)=>c.ids[i]!==JOB_ID).every(e=>e.isConnected)});
})()"""


def capture_list(session=SESSION):
    result = json.loads(evaluate(CAPTURE_JS.replace('TOKEN', json.dumps(uuid.uuid4().hex)), session=session) or '{}')
    if not result.get('ok'):
        raise ValueError(result.get('reason', '无法记录原列表'))
    return result


def probe_list(context, jid, session=SESSION):
    code = PROBE_JS.replace('CONTEXT', json.dumps(context)).replace('JOB_ID', json.dumps(jid))
    return json.loads(evaluate(code, session=session) or '{}')


def restore_list(context, jid, session=SESSION, timeout=8):
    from urllib.parse import urlparse
    def failed(reason, state):
        return dict(state, preserved=False, reason=reason)

    state = probe_list(context, jid, session)
    if not state.get('on_list'):
        current = urlparse(state.get('url', ''))
        h0, h1 = context['historyLength'], state.get('historyLength')
        direct_history = h1 == h0 + 1 or (h0 >= 50 and h1 == h0)
        # A same-document route retains our marker; a full navigation has a referrer.
        if (current.hostname != 'www.zhipin.com' or current.path != '/web/geek/chat'
                or not direct_history or not (state.get('same_document') or state.get('referrer') == context['url'])):
            return failed('聊天页来源或历史不匹配，未自动后退', state)
        # One mutation only; a lost reply must not cause a second back navigation.
        evaluate_once('(() => { history.back(); return true; })()', session=session)
    deadline = time.monotonic() + timeout
    while True:
        state = probe_list(context, jid, session)
        if state.get('on_list') and state.get('same_document'):
            if state.get('chat_visible'):
                return failed('聊天浮层仍打开，需核对关闭入口；未刷新列表', state)
            if state.get('missing_ids') or not state.get('order_preserved'):
                return failed('原页面已恢复，但剩余岗位发生变化，停止本页遍历', state)
            # Restore existing scroll containers, never reconstruct or inject job cards.
            code = """(() => {const p=window.__bossListReturn;
                if(!p || p.token!==TOKEN) return false;
                for(const s of p.scrolls) if(s.el.isConnected) {s.el.scrollTop=s.top;s.el.scrollLeft=s.left;}
                return true;})()""".replace('TOKEN', json.dumps(context['token']))
            restored = evaluate_once(code, session=session)
            if restored is not True:
                return failed('恢复滚动位置时原页面状态已变化', state)
            return dict(state, preserved=True, method='retained_document', scroll_restored=True)
        if time.monotonic() >= deadline:
            return failed('未恢复原列表文档；未重新打开搜索URL', state)
        time.sleep(.25)
