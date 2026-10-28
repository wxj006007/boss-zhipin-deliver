"""Send agent-written greetings through the existing WebBridge session.

No model API: the agent reads the JD and supplies the message as UTF-8 JSON.
"""
import json
import time

from scripts.webbridge_client import evaluate, evaluate_once, SESSION
from scripts.list_return import capture_list, restore_list
from scripts.chat_tab import prepare_chat_tab
from scripts.delivery_tab import (prepare_delivery_tab, click_delivery_contact,
                                  enter_delivery_chat as enter_chat_tab,
                                  DeliveryReviewRequired)
from scripts.browser_session import verify_identity, guarded_script
from scripts.hr_activity import check_current as check_activity, skip_result as activity_skip_result


def validate_greeting(message):
    if not isinstance(message, str) or not message.strip():
        raise ValueError("招呼语必须是非空文本")
    message = message.strip()
    if len(message) > 500 or any(ord(c) < 32 and c not in "\n\t\r" for c in message):
        raise ValueError("招呼语须不超过500字且不含控制字符")
    return message


def load_greeting(path, jid):
    with open(path, encoding="utf-8-sig") as f:
        payload = json.load(f)
    if not isinstance(payload, dict) or payload.get("jobId") != jid:
        raise ValueError("招呼语文件的 jobId 与待投岗位不一致")
    return validate_greeting(payload.get("greeting"))


# The active conversation's job ID and message selectors were inspected on the
# logged-in BOSS page. Never identify a recipient using the left conversation list.
CHAT_JS = r"""
(() => {
    const p = PAYLOAD;
    const visible = e => !!e && e.getClientRects().length > 0 &&
        getComputedStyle(e).visibility !== 'hidden';
    const norm = s => (s || '').replace(/\r\n?/g, '\n').replace(/\u00a0/g, ' ').trim();
    const inputs = [...document.querySelectorAll('#chat-input[contenteditable="true"],textarea.input-area')].filter(visible);
    if (inputs.length !== 1) return JSON.stringify({ready:false});
    const input = inputs[0];
    const root = input.closest('.chat-conversation,.dialog-container,.dialog-wrap,[role="dialog"]');
    if (!root) return JSON.stringify({ready:false});
    const record = root.querySelector('.chat-record');
    const boss = record?.__vue__?.$data?.boss;
    const activeId = boss?.encryptJobId || '';
    // DOM fallback is scoped to the active conversation's job header.
    const header = root.querySelector('.chat-position-content');
    const link = header?.querySelector('a[href*="/job_detail/"]');
    const linkedId = link?.getAttribute('href')?.match(/\/job_detail\/([^./?]+)/)?.[1] || '';
    const target = activeId ? activeId === p.jid : linkedId === p.jid;
    if (!record || record.__vue__?.$data?.loading === true)
        return JSON.stringify({ready:false,target});
    const messages = [...root.querySelectorAll('.message-item.item-myself')].map(el => ({
        id:el.getAttribute('data-mid') || '',
        text:norm(el.querySelector('.text-content')?.innerText),
        sent:!!el.querySelector('.status-delivery,.status-read'),
        failed:!!el.querySelector('.status-error,.status-fail,.status-failed')
    }));
    const value = () => norm(input.tagName === 'TEXTAREA' ? input.value : input.innerText);
    const buttons = [...root.querySelectorAll('button[type="send"],button.btn-send,div.send-message')].filter(visible);
    const sendEnabled = buttons.length === 1 && !buttons[0].disabled &&
        buttons[0].getAttribute('aria-disabled') !== 'true' &&
        !/(^|\s)(disabled|unable)(\s|$)/.test(buttons[0].className);
    if (p.action === 'read') return JSON.stringify({ready:true,target,input:value(),messages,send_enabled:sendEnabled});
    if (!target) return JSON.stringify({ok:false,reason:'当前会话的岗位ID不匹配'});
    if (p.action === 'fill') {
        if (value() && value() !== norm(p.message))
            return JSON.stringify({ok:false,reason:'聊天框已有不同草稿，停止以免覆盖'});
        input.focus();
        if (input.tagName === 'TEXTAREA') {
            Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, p.message);
        } else { input.innerText = p.message; }
        input.dispatchEvent(new Event('input', {bubbles:true}));
        return JSON.stringify({ok:value() === norm(p.message)});
    }
    if (p.action === 'send') {
        if (value() !== norm(p.message)) return JSON.stringify({ok:false,reason:'输入框回读不一致'});
        if (messages.some(m => m.text === norm(p.message)))
            return JSON.stringify({ok:false,reason:'会话已有相同文本，需核对，禁止重发'});
        if (!sendEnabled) return JSON.stringify({ok:false,reason:'发送按钮不可用或不唯一'});
        buttons[0].click();
        return JSON.stringify({ok:true});
    }
    return JSON.stringify({ok:false});
})()
"""


def chat_action(jid, action="read", message="", session=SESSION):
    if action in ('fill', 'send'):
        verify_identity()
    payload = json.dumps({"jid": jid, "action": action, "message": message}, ensure_ascii=True)
    code = CHAT_JS.replace("PAYLOAD", payload, 1)
    if action in ('fill', 'send'):
        code = guarded_script(code)
    run = evaluate if action == "read" else evaluate_once
    return json.loads(run(code, session=session) or "{}")


def continue_chat(session=SESSION):
    """Enter the conversation from the success dialog or the selected JD panel."""
    return evaluate_once(r"""(() => {
        const visible=e=>e.getClientRects().length>0;
        const dialogs=[...document.querySelectorAll('.greet-boss-container,.greet-boss-dialog,.dialog-container,.dialog-wrap,[role="dialog"]')].filter(visible);
        const boxes=dialogs.length ? dialogs : [document.querySelector('.job-detail-box')||document.querySelector('#main .job-detail')];
        const buttons=[...new Set(boxes.filter(Boolean).flatMap(b=>[...b.querySelectorAll('button,a')]))]
            .filter(e=>visible(e)&&(e.textContent||'').trim()==='继续沟通');
        if(buttons.length!==1) return false;
        buttons[0].click(); return true;
    })()""", session=session)


def deliver_greeting(job, message, session=SESSION, timeout=15):
    """Send once and verify the exact outgoing message in the target conversation."""
    from scripts import phase_stream as ps

    message = validate_greeting(message)
    jid = job["jobId"]
    original_url = evaluate("location.href", session=session)
    if "/web/geek/jobs" not in (original_url or "") or not ps._panel_shows_job(jid, "", session):
        return {"delivered_ok": False, "reason": "未处于目标岗位详情，未点击沟通"}

    def uncertain(reason):
        return {"delivered_ok": False, "uncertain": True, "stopped": True, "reason": reason}

    # Unknown dialogs are safe to reject before initiating any communication.
    if ps.read_popup_type(session):
        return {"delivered_ok": False, "stopped": True, "reason": "沟通前有弹窗，请先核对"}
    verify_identity()
    activity = check_activity(jid, session)
    job['hr_activity'] = activity
    if not activity['allowed']:
        return activity_skip_result(activity)
    try:
        list_context = capture_list(session)
        prepare_chat_tab(list_context, session)
    except Exception as error:
        return {"delivered_ok": False, "stopped": True,
                "reason": f"未能记录原列表，尚未点击沟通：{error}"}
    def before_contact_failure(reason, **fields):
        # No contact was initiated. Return to the original document without a
        # reload, retaining the pending review if the caller needs to retry.
        result = dict(delivered_ok=False, reason=reason, **fields)
        try:
            result['list_return'] = restore_list(list_context, jid, session)
            if not result['list_return'].get('preserved'):
                result['stopped'] = True
        except Exception as error:
            result.update(stopped=True, return_error=str(error))
        return result

    try:
        activity = prepare_delivery_tab(list_context, jid, session, timeout)
    except DeliveryReviewRequired as error:
        return before_contact_failure(str(error), review_required=True)
    except (ValueError, RuntimeError) as error:
        return before_contact_failure(str(error), stopped=True)
    job['hr_activity'] = activity
    if not activity['allowed']:
        return before_contact_failure(activity['reason'], skipped=True,
                                      activity_filtered=True, hr_activity=activity)
    try:
        clicked = click_delivery_contact(list_context, jid, session)
    except DeliveryReviewRequired as error:
        return before_contact_failure(str(error), review_required=True)
    if not clicked:
        return before_contact_failure('独立详情页未确认目标沟通按钮，未点击沟通', stopped=True)

    deadline = time.monotonic() + timeout
    continued = False
    state = {}
    while time.monotonic() < deadline:
        anomaly, reason = ps.detect_page_anomaly(session=session, check_blank=False)
        if anomaly:
            return uncertain(reason)
        state = chat_action(jid, session=session)
        if state.get("ready") and state.get("target"):
            break
        # A successful detail-page click can navigate while the old document's
        # fixed elements are still visible. Do not classify that transient DOM
        # as an unknown dialog before giving navigation/editor loading time.
        if not state.get('ready'):
            path = evaluate('location.pathname', session=session)
            if path == '/web/geek/chat':
                time.sleep(.5)
                continue
        popup = ps.read_popup_type(session)
        if popup == "daily_limit":
            return dict(uncertain("沟通上限，需核对本次状态"), daily_limit=True)
        if not continued and (popup == "greet_sent" or
                              (not popup and evaluate("location.pathname.startsWith('/job_detail/') && !![...document.querySelectorAll('.job-banner a.btn-startchat')].find(e=>e.innerText.trim()==='继续沟通')", session=session) is True)):
            continued = enter_chat_tab(list_context, jid, popup, session, timeout)
            # Opening/loading a sibling tab has its own bounded wait; give the
            # active conversation its full readiness timeout after selection.
            deadline = time.monotonic() + timeout
        elif popup == 'unknown' and not state.get('ready'):
            time.sleep(.5)
            # Re-read once after the navigation transition. A persistent unknown
            # dialog still stops; only a confirmed chat route may keep waiting.
            if evaluate('location.pathname', session=session) == '/web/geek/chat':
                continue
            if ps.read_popup_type(session) == 'unknown':
                return uncertain("出现未识别的沟通界面，需人工核对")
        elif popup in ("not_interested", "resume_guide"):
            return uncertain("出现未识别的沟通界面，需人工核对")
        time.sleep(.5)
    else:
        return uncertain("未确认目标岗位的聊天输入框，未发送自定义文本")

    return finish_greeting(job, message, list_context, session, timeout, state)


def finish_greeting(job, message, list_context, session=SESSION, timeout=15, state=None):
    """Send/verify once in a checked conversation, then restore the retained list.

    Also usable after an explicitly audited interruption, without re-clicking
    contact. Callers retain uncertain history until this returns a verified send.
    """
    from scripts import phase_stream as ps
    message = validate_greeting(message)
    normalized_message = message.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    jid = job['jobId']
    verify_identity()
    def uncertain(reason):
        return dict(delivered_ok=False, uncertain=True, stopped=True, reason=reason)
    state = state if state is not None else chat_action(jid, session=session)
    if not state.get('ready') or not state.get('target'):
        return uncertain('目标会话尚未确认，不发送')
    if any(m.get("text") == normalized_message for m in state.get("messages", [])):
        return uncertain("当前会话已有相同招呼语，禁止重复发送")
    before = {m.get("id") for m in state.get("messages", [])}
    filled = chat_action(jid, "fill", message, session)
    if not filled.get("ok"):
        return uncertain(filled.get("reason", "填写失败"))
    # BOSS can enable the editor's send button well after the input event.
    # Only poll read-only state here; the eventual send remains a single mutation.
    time.sleep(2.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        anomaly, reason = ps.detect_page_anomaly(session=session, check_blank=False)
        if anomaly:
            return uncertain(reason)
        state = chat_action(jid, session=session)
        if not state.get("ready") or not state.get("target"):
            return uncertain("等待发送按钮时目标会话未就绪或发生变化，未发送自定义文本")
        if state.get("input") != normalized_message:
            return uncertain("等待发送按钮时输入框文本发生变化，未发送自定义文本")
        if any(m.get("text") == normalized_message for m in state.get("messages", [])):
            return uncertain("等待发送按钮时发现相同消息，禁止重复发送")
        if state.get("send_enabled") is True:
            break
        time.sleep(.5)
    else:
        return uncertain("等待发送按钮启用超时，未点击发送；需核对，不自动重发")
    sent = chat_action(jid, "send", message, session)
    if not sent.get("ok"):
        return uncertain(sent.get("reason", "发送未确认"))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        anomaly, reason = ps.detect_page_anomaly(session=session, check_blank=False)
        if anomaly:
            return uncertain(reason)
        state = chat_action(jid, session=session)
        if not state.get("target"):
            return uncertain("发送后目标会话发生变化")
        matches = [m for m in state.get("messages", []) if m.get("id") and m["id"] not in before
                   and m.get("text") == normalized_message]
        if any(m.get("failed") for m in matches):
            return uncertain("自定义消息出现发送失败标记")
        confirmed = next((m for m in matches if m.get("sent")), None)
        if confirmed and state.get("ready") and state.get("input") == "":
            result = {"delivered_ok": True, "verify": True,
                      "verification_source": "custom_greeting_message", "message_id": confirmed["id"],
                      "source_tab_id": list_context.get('tab_id'),
                      "delivery_tab_id": list_context.get('delivery_tab_id')}
            try:
                restored = restore_list(list_context, jid, session)
                result['list_return'] = restored
                if not restored.get('preserved'):
                    result.update(stopped=True, reason="消息已确认；" + restored['reason'])
            except Exception as error:
                result.update(stopped=True, reason=f"消息已确认，但保留原列表返回失败：{error}")
            return result
        time.sleep(.5)
    return uncertain("未读到自定义消息的送达状态，不自动重发")
