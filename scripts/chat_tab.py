"""Keep the job list alive while opening the exact conversation in a sibling tab.

Only use tabs owned by the current WebBridge session. Never infer a recipient
from a name, company, list position, or a constructed chat URL.
"""
import json
import time
from urllib.parse import urlparse

from scripts.webbridge_client import api, evaluate, evaluate_once, SESSION
from scripts.browser_session import verify_identity, guarded_script


def tab_command(action, args=None, session=SESSION):
    # Creating/selecting tabs must not be replayed after a lost response.
    result = api(action, args or {}, session=session, retries=1)
    data = result.get('data', {})
    if result.get('ok') is not True or data.get('success') is not True:
        raise RuntimeError(f'标签页操作未确认：{action}')
    return data


def prepare_chat_tab(context, session=SESSION):
    """Read-only preflight before initiating communication."""
    tabs = tab_command('list_tabs', session=session).get('tabs', [])
    source = [t for t in tabs if t.get('url') == context['url']]
    if len(source) != 1:
        raise ValueError('当前session的原列表标签页不唯一，停止以免切错页面')
    url = evaluate("document.querySelector('#header a[href*=\"/web/geek/chat\"]')?.href || ''", session=session)
    parsed = urlparse(url or '')
    if parsed.scheme != 'https' or parsed.hostname != 'www.zhipin.com' or parsed.path != '/web/geek/chat':
        raise ValueError('未读到页面上的可信消息入口')
    chat = [t for t in tabs if urlparse(t.get('url', '')).hostname == 'www.zhipin.com'
            and urlparse(t.get('url', '')).path == '/web/geek/chat']
    if len(chat) > 1:
        raise ValueError('当前session有多个聊天标签页，请先明确保留哪一个')
    context.update(tab_id=source[0]['tabId'], chat_url=url,
                   chat_tab_id=chat[0]['tabId'] if chat else None)
    return context


STAY_JS = r"""(() => {
    const visible=e=>e.getClientRects().length>0;
    const boxes=[...document.querySelectorAll('.greet-boss-container,.greet-boss-dialog,.dialog-container,.dialog-wrap,[role="dialog"]')].filter(visible);
    const buttons=[...new Set(boxes.flatMap(b=>[...b.querySelectorAll('button,a')]))]
        .filter(e=>visible(e)&&(e.textContent||'').trim()==='留在此页');
    if(buttons.length!==1) return false;
    buttons[0].click(); return true;
})()"""


SELECT_CHAT_JS = r"""(() => {
    const jid=JOB_ID;
    const visible=e=>e.getClientRects().length>0;
    const inputs=[...document.querySelectorAll('#chat-input[contenteditable="true"],textarea.input-area')].filter(visible);
    if(inputs.some(e=>(e.tagName==='TEXTAREA'?e.value:e.innerText).trim()))
        return JSON.stringify({draft:true});
    const box=document.querySelector('.user-list-content');
    if(!box) return JSON.stringify({selected:false});
    const rows=[...box.querySelectorAll('li')].filter(e=>
        e.__vue__?.$props?.source?.encryptJobId===jid);
    if(rows.length>1) return JSON.stringify({ambiguous:true});
    if(rows.length!==1) {
        if(box.scrollTop!==0) {box.scrollTop=0;box.dispatchEvent(new Event('scroll'));}
        return JSON.stringify({selected:false});
    }
    const target=rows[0].querySelector('.friend-content');
    if(!target || !visible(target)) return JSON.stringify({selected:false});
    target.click();
    return JSON.stringify({selected:true});
})()"""


def enter_chat_tab(context, jid, popup, session=SESSION, timeout=15):
    from scripts import phase_stream as ps
    from scripts.list_return import probe_list
    state = probe_list(context, jid, session)
    if not state.get('on_list') or not state.get('same_document'):
        raise RuntimeError('沟通后原列表已变化，停止以免使用错误页面')
    if popup == 'greet_sent':
        if evaluate_once(guarded_script(STAY_JS), session=session) is not True:
            raise RuntimeError('未能确认留在此页按钮，停止')
        deadline = time.monotonic() + timeout
        while True:
            remaining = ps.read_popup_type(session)
            if not remaining:
                break
            if remaining != 'greet_sent':
                raise RuntimeError(f'关闭沟通提示后出现其他弹窗：{remaining}')
            if time.monotonic() >= deadline:
                raise RuntimeError('沟通提示未关闭，停止')
            time.sleep(.25)
    if context.get('chat_tab_id') is None:
        opened = tab_command('navigate', {'url':context['chat_url'], 'newTab':True}, session)
        context['chat_tab_id'] = opened['tabId']
    else:
        tabs = tab_command('list_tabs', session=session).get('tabs', [])
        if not any(t.get('tabId') == context['chat_tab_id'] and
                   urlparse(t.get('url', '')).path == '/web/geek/chat' and
                   urlparse(t.get('url', '')).hostname == 'www.zhipin.com' for t in tabs):
            raise RuntimeError('原聊天标签页已关闭或被导航，停止')
        tab_command('attach_tab', {'tabId':context['chat_tab_id']}, session)
    # A fresh tab may still be loading. Wait for the existing identity reader,
    # then use the page's own exact job ID to select the conversation once.
    deadline = time.monotonic() + timeout
    while True:
        ready = evaluate("location.hostname==='www.zhipin.com' && location.pathname==='/web/geek/chat' && !!document.querySelector('.user-list-content')", session=session)
        if ready:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('聊天标签页未就绪，停止')
        time.sleep(.25)
    verify_identity()
    while time.monotonic() < deadline:
        anomaly, reason = ps.detect_page_anomaly(session=session, check_blank=False)
        if anomaly:
            raise RuntimeError(reason)
        code = SELECT_CHAT_JS.replace('JOB_ID', json.dumps(jid))
        selected = json.loads(evaluate_once(guarded_script(code), session=session) or '{}')
        if selected.get('draft'):
            raise RuntimeError('聊天标签页已有草稿，停止以免切换会话丢失草稿')
        if selected.get('ambiguous'):
            raise RuntimeError('目标岗位对应多个会话，停止')
        if selected.get('selected'):
            return True
        time.sleep(.25)
    raise RuntimeError('未找到岗位ID精确匹配的会话，不按姓名或公司猜测')
