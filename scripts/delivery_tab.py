"""Initiate contact in a sibling tab, leaving the reviewed search document alive."""
import json
import time
from urllib.parse import urlparse

from scripts.webbridge_client import evaluate, evaluate_once, SESSION
from scripts.browser_session import verify_identity, guarded_script
from scripts.chat_tab import tab_command, SELECT_CHAT_JS, STAY_JS
from scripts.hr_activity import decide as activity_decision


class DeliveryReviewRequired(ValueError):
    pass


SOURCE_JOB_JS = r"""(() => {
 const jid=JOB_ID;
 const links=[...document.querySelectorAll('.job-card-wrap a[href*="/job_detail/"]')]
   .filter(a=>new URL(a.href).pathname==='/job_detail/'+jid+'.html');
 const hrefs=[...new Set(links.map(a=>a.href))];
 const panel=document.querySelector('.job-detail-box');
 const description=panel?.querySelector('.desc')?.innerText || '';
 const own=document.querySelector('#header .nav-figure,#header .nav-user,#header .user-nav');
 const ids=[];
 for(let el=own,n=0;el&&n<6;el=el.parentElement,n++) {
   const vm=el.__vue__; if(!vm)continue;
   for(const root of [vm.$data||{},vm.$store?.state||{}])
     for(const key of ['userInfo','user','geekInfo']) {
       const u=root[key]?.userInfo||root[key];
       if(u && /^\d+$/.test(String(u.userId||'')))ids.push(String(u.userId));
     }
 }
 const unique=[...new Set(ids)];
 const browser=/Edg\//.test(navigator.userAgent)?'edge':/Chrome\//.test(navigator.userAgent)?'chrome':'unknown';
 return JSON.stringify({href:hrefs.length===1?hrefs[0]:'',
   title:links[0]?.innerText?.trim()||'',description,
   own_uid:unique.length===1?unique[0]:'',browser});
})()"""

# The SSR detail page exposes the signed-in person's numeric uid in _PAGE.
# It is compared with the numeric userId observed in the *verified* list page's
# own user header, never with a recruiter id or a guessed name. No tokens read.
DETAIL_IDENTITY_JS = r"""location.hostname==='www.zhipin.com' &&
 !!document.querySelector('#header .user-nav') &&
 String(window._PAGE?.uid||'')===p.source.own_uid &&
 (/Edg\//.test(navigator.userAgent)?'edge':/Chrome\//.test(navigator.userAgent)?'chrome':'unknown')===p.source.browser"""

DETAIL_JS = r"""(() => {
 const p=PAYLOAD;
 const norm=s=>(s||'').replace(/\s+/g,'');
 if(location.hostname!=='www.zhipin.com' || location.pathname!=='/job_detail/'+p.jid+'.html')
   return JSON.stringify({ready:false,reason:'详情页岗位URL不匹配'});
 if(!(IDENTITY_CHECK))return JSON.stringify({ready:false,identity_mismatch:true});
 const visible=e=>e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
 const buttons=[...document.querySelectorAll('.job-banner a.btn-startchat')].filter(visible);
 if(buttons.length!==1)return JSON.stringify({ready:false,reason:'沟通按钮不唯一或未就绪'});
 const button=buttons[0];
 const redirect=new URL(button.getAttribute('redirect-url')||'',location.origin);
 if(redirect.origin!==location.origin || redirect.pathname!=='/web/geek/chat' || redirect.searchParams.get('jobId')!==p.jid)
   return JSON.stringify({ready:false,target_mismatch:true});
 const title=document.querySelector('.job-banner h1')?.innerText||'';
 const description=document.querySelector('.job-detail .job-sec-text')?.innerText||'';
 if(!title.trim()||!description.trim())return JSON.stringify({ready:false});
 if(norm(title)!==norm(p.source.title)||norm(description)!==norm(p.source.description))
   return JSON.stringify({ready:true,review_required:true,reason:'新标签页JD或标题与已审核岗位不同'});
 const text=button.innerText.trim();
 const activityText=document.querySelector('.job-detail .boss-active-time')?.innerText?.trim()||'';
 const result={ready:true,activity:{online:null,text:activityText,source:'detail',observed_at:new Date().toISOString()}};
 if(text!=='立即沟通'||button.getAttribute('data-isfriend')==='true')
   return JSON.stringify({...result,already_contacted:true});
 if(p.click) {
   if(activityText!==p.activity_text)return JSON.stringify({...result,activity_changed:true});
   button.click();result.clicked=true;
 }
 return JSON.stringify(result);
})()""".replace('IDENTITY_CHECK', DETAIL_IDENTITY_JS)


def read_delivery_detail(context, jid, session=SESSION, click=False):
    payload=dict(jid=jid,source=context['delivery_source'],click=click,
                 activity_text=context.get('delivery_activity_text'))
    code=DETAIL_JS.replace('PAYLOAD',json.dumps(payload,ensure_ascii=True),1)
    return json.loads((evaluate_once if click else evaluate)(code,session=session) or '{}')


def prepare_delivery_tab(context, jid, session=SESSION, timeout=15):
    """Open the observed job link BEFORE contact, then verify account/JD/activity."""
    verify_identity()
    source=json.loads(evaluate(guarded_script(SOURCE_JOB_JS.replace('JOB_ID',json.dumps(jid))),session=session) or '{}')
    url=urlparse(source.get('href',''))
    if (url.scheme!='https' or url.hostname!='www.zhipin.com' or url.username or url.password
            or url.path!='/job_detail/'+jid+'.html' or not source.get('description')
            or not source.get('title') or not source.get('own_uid')):
        raise ValueError('无法从已审核列表读取真实详情链接、JD或账户ID，未点击沟通')
    context['delivery_source']=source
    worker=context.get('chat_tab_id')
    if worker is not None:
        if worker==context['tab_id']:
            raise ValueError('投递标签页不能是原列表标签页')
        tab_command('attach_tab',{'tabId':worker},session)
        verify_identity()
        empty=evaluate("(() => location.pathname==='/web/geek/chat' && !!document.querySelector('.user-list-content') && ![...document.querySelectorAll('#chat-input[contenteditable=true],textarea.input-area')].some(e=>(e.tagName==='TEXTAREA'?e.value:e.innerText).trim()))()",session=session)
        if empty is not True:
            raise ValueError('聊天页已有草稿或页面变化，不导航覆盖')
    opened=tab_command('navigate',{'url':source['href'],'newTab':worker is None},session)
    context['delivery_tab_id']=opened['tabId']
    if opened['tabId']==context['tab_id'] or (worker is not None and opened['tabId']!=worker):
        raise RuntimeError('未在独立投递标签页打开详情，未点击沟通')
    context['chat_tab_id']=opened['tabId']
    deadline=time.monotonic()+timeout
    while True:
        result=read_delivery_detail(context,jid,session)
        if result.get('identity_mismatch') or result.get('target_mismatch'):
            raise ValueError('新标签页账户或沟通按钮岗位不匹配，未点击沟通')
        if result.get('review_required'):
            raise DeliveryReviewRequired(result['reason'])
        if result.get('already_contacted'):
            raise ValueError('详情页显示已沟通过，需核对历史，未重复发起沟通')
        if result.get('ready'):
            context['delivery_activity_text']=result.get('activity',{}).get('text','')
            return activity_decision(result.get('activity',{}))
        if time.monotonic()>=deadline:
            raise ValueError('新标签页详情未就绪，未点击沟通')
        time.sleep(.25)


def click_delivery_contact(context, jid, session=SESSION):
    # Revalidate in the same JS call as the single click, including exact JD.
    result=read_delivery_detail(context,jid,session,click=True)
    if result.get('review_required'):
        raise DeliveryReviewRequired(result['reason'])
    return result.get('clicked') is True


def enter_delivery_chat(context, jid, popup, session=SESSION, timeout=15):
    """If contact showed a dialog, enter chat in the worker, never in the list."""
    from scripts import phase_stream as ps
    if context['delivery_tab_id']==context['tab_id']:
        raise RuntimeError('禁止在原列表标签页进入聊天')
    tab_command('attach_tab',{'tabId':context['delivery_tab_id']},session)
    if popup=='greet_sent':
        payload=json.dumps({'source':context['delivery_source']},ensure_ascii=True)
        code=f"(() => {{const p={payload};if(!({DETAIL_IDENTITY_JS}))throw new Error('BOSS_ACCOUNT_MISMATCH');return {STAY_JS};}})()"
        if evaluate_once(code,session=session) is not True:
            raise RuntimeError('未能确认留在此页按钮，停止')
        deadline=time.monotonic()+timeout
        while ps.read_popup_type(session):
            if time.monotonic()>=deadline:
                raise RuntimeError('沟通提示未关闭，停止')
            time.sleep(.25)
    opened=tab_command('navigate',{'url':context['chat_url']},session)
    if opened['tabId']!=context['delivery_tab_id']:
        raise RuntimeError('聊天页未在独立投递标签页打开')
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        ready=evaluate("location.pathname==='/web/geek/chat' && !!document.querySelector('.user-list-content')",session=session)
        if ready:
            verify_identity()
            selected=json.loads(evaluate_once(guarded_script(SELECT_CHAT_JS.replace('JOB_ID',json.dumps(jid))),session=session) or '{}')
            if selected.get('draft') or selected.get('ambiguous'):
                raise RuntimeError('目标会话不唯一或已有草稿，停止')
            if selected.get('selected'):
                return True
        time.sleep(.25)
    raise RuntimeError('未找到岗位ID精确匹配的会话')
