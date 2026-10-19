#!/usr/bin/env python3
"""BOSS 直聘投递 · 投递原语库 + 收尾（实时单卡投递由 _live_driver.py 驱动）

架构（v6.9 起）：
  - _live_driver.py 负责「智能体交互层」：walk / open / deliver / reject 单卡游标，
    把每步动作交给浏览器，方向判断（该不该投）由 AI 语义完成，脚本只做两道
    机械硬门槛（城市 + 薪资下限），绝不硬编码岗位类型/行业关键词。
  - 本文件 phase_stream.py 提供「投递共享原语 + 收尾」：
      ① 浏览器操作原语：滚动懒加载、读 JD、弹窗识别/关闭、面板校验、
         推荐页内原地投递 deliver_click、每步 DOM 回读校验；
      ② 收尾：write_summary_page（--mode report，仅生成投递成果汇报 HTML）；
      ③ 独立诊断：run_verify（--mode verify，交叉核对真实沟通列表，读后自动回推荐页）；
      ④ 诊断模式：--diag（只读 dump 浮层 + 弹窗分类 + 日上限检测，不投递）。

设计铁律（与筛选无关，平台级）：
  - 每次投递都回读页面确认「送达」才继续；遇未知弹窗 / 误触 / 日上限 立即整批停手。
  - 弹窗只点白名单关闭键（留在此页/继续沟通/取消/关闭/稍后），绝不点发送/上传类按钮。
  - 连续投递失败熔断（FAIL_BREAKER）：连续 ≥3 次失败/超时即停手。
  - 本文件的默认招呼路径停留岗位列表；greeting.py 的自定义文本路径会进入会话并在核验后返回。

用法（仅收尾/诊断，投递主流程在 _live_driver.py）：
    python scripts/phase_stream.py --workdir <目录> --mode report
    python scripts/phase_stream.py --workdir <目录> --mode verify
    python scripts/phase_stream.py --workdir <目录> --diag
"""
import sys, os, time, json

_HERE = os.path.dirname(os.path.abspath(__file__))
# 兼容 Git Bash 的 /c/... UNIX 风格路径：Windows Python 会把 /c 误算成 C:\c
if len(_HERE) >= 3 and _HERE[0] == "/" and _HERE[1].isalpha() and _HERE[2] == "/":
    _HERE = _HERE[1].upper() + ":" + _HERE[2:]
SKILL_DIR = os.path.dirname(_HERE)
if SKILL_DIR not in sys.path:
    sys.path.insert(0, SKILL_DIR)

from scripts.webbridge_client import (
    SESSION, api, evaluate, extract_jobs, detect_page_anomaly,
    ProgressManager, parse_salary_max, wait_for_feedback, AbortScriptError,
    navigate, snapshot_page, click,
)
from scripts.config_v3 import load_profile
from scripts.account_browser import verify_identity, guarded_script
from scripts.webbridge_client import evaluate_once
from scripts.accounts import metadata, assert_workdir
from scripts.hr_activity import check_current as check_activity, skip_result as activity_skip_result
from scripts.env_check import check_env
from scripts.runtime import atomic_write_text, salary_range_k

RECOMMEND_URL = "https://www.zhipin.com/web/geek/jobs?ka=header-jobs"
# 仅用于 _ensure_recommend 判定当前是否已在 BOSS 工作页（推荐页/搜索页同路径，仅 query 不同）
WORKING_URL = RECOMMEND_URL
# v6.11.2：当前批量分组的工作页 URL（pipeline_batch.goto 每次导航成功后写入）。
# _ensure_recommend 意外跳页时回这里，而不是写死的推荐页。
CURRENT_WORK_URL = {"url": ""}


def search_url_for(query, city_code, job_type=""):
    """构造 BOSS 搜索页 URL。

    job_type（v6.10 新增）：BOSS「求职类型」筛选码——
      ""     不筛选（默认）
      "1901" 全职
      "1902" 实习   ← 投实习岗必须传这个，否则搜索结果被社招岗淹没
      "1903" 兼职
    对应 URL 参数 &jobType=<码>。
    """
    from urllib.parse import quote
    if not query:
        # 无关键词 → 返回 BOSS 推荐页（用户个性化 feed，命中率最高，walk 默认起点）
        return RECOMMEND_URL
    url = f"https://www.zhipin.com/web/geek/jobs?query={quote(query)}&city={city_code}"
    if job_type:
        url += f"&jobType={job_type}"
    return url


def scroll_jobs_list(session=SESSION, times=1):
    """在当前账户后台标签页触发并核验懒加载，不切前台、不改变焦点。

    返回 loaded/advanced/exhausted/loading/stalled；无新增或达到等待上限不等于到底。
    exhausted 可能同时带回最后一批新岗位，调用方须先读它们再结束扫描。
    """
    from scripts.job_list_loading import advance
    return advance(session=session, times=times)


# ───────────────────────────────────────────────────────────
# 读取 JD（点击卡片 → 右侧面板切换 → 抽取 JD 文本）
# ───────────────────────────────────────────────────────────
JD_JS = r"""
(function(){
    function grab(sel){ var el=document.querySelector(sel); return el? el.innerText.trim(): null; }
    var detail = grab('#main .job-detail') || grab('.job-detail') || grab('.detail-content')
                 || grab('.job-detail-box') || grab('div.detail-content');
    if (detail) return '=== JD面板 ===\n' + detail;
    var main = document.querySelector('#main');
    if (main){ var copy=main.cloneNode(true); var list = copy.querySelector('.job-list'); if (list) list.remove();
        return '=== #main(副本已去左列表) ===\n' + copy.textContent.trim(); }
    return '=== body兜底 ===\n' + (document.body.innerText||'').trim();
})()
"""

def read_jd(job, session=SESSION):
    ptype = read_popup_type(session)
    if ptype in ("not_interested", "unknown"):
        raise AbortScriptError(f"读JD前检测到危险弹窗({ptype})，停止待人工介入")
    if ptype:
        safe_close_popup(session)
    jid = job.get("jobId")
    sel = f'a[href*="job_detail/{jid}"]'
    r = click(sel, session=session)
    time.sleep(3)
    return evaluate(JD_JS, session=session) or ""


# ═══════════════════════════════════════════════════════════
# 通用可见浮层扫描（取代固定选择器白名单）
# ═══════════════════════════════════════════════════════════
OVERLAY_SCAN_JS = r"""
function _ovVis(el){ if(!el) return false; var s; try{s=getComputedStyle(el);}catch(e){return true;}
    if(s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
    if(el.offsetParent===null && el.tagName!=='BODY' && el.tagName!=='HTML'){
        var p=s.position; if(p!=='fixed'&&p!=='absolute') return false; }
    return true; }
function _ovIs(el){ var s; try{s=getComputedStyle(el);}catch(e){s={};}
    if(el.getAttribute('role')==='dialog'||el.getAttribute('role')==='alertdialog') return true;
    var cls=(''+(el.className||'')).toLowerCase();
    if(/mask|modal|dialog|popup|overlay|drawer|toast|layer|chat-block|greet|confirm/.test(cls)) return true;
    if(s.position==='fixed'||s.position==='absolute'){
        var t=(el.innerText||'').replace(/\s+/g,' ').trim();
        if(/不感兴趣|上传附件|已向BOSS|上限|沟通次数|确定|取消|发送|继续沟通|留在此页/.test(t)) return true; }
    return false; }
function _ovScan(){ var out=[]; var all=document.querySelectorAll('*');
    for(var i=0;i<all.length;i++){ var el=all[i]; if(!_ovVis(el)) continue; if(!_ovIs(el)) continue;
        var t=(el.innerText||el.textContent||'').replace(/\s+/g,' ').trim(); if(!t||t.length<2) continue;
        var s; try{s=getComputedStyle(el);}catch(e){s={position:'',zIndex:'0'};}
        out.push({tag:el.tagName, cls:(''+(el.className||'')).slice(0,60), pos:s.position, z:(parseInt(s.zIndex,10)||0), text:t.slice(0,200)}); }
    out.sort(function(a,b){ return (b.z-a.z)||(b.text.length-a.text.length); });
    return out.slice(0,10); }
"""

POPUP_STATE_JS = OVERLAY_SCAN_JS + r"""
(function(){
    var out={};
    var gb = document.querySelector('.greet-boss-container')||document.querySelector('.greet-boss-dialog');
    out.greet_open = !!(gb && _ovVis(gb));
    out.greet_text = (gb && _ovVis(gb)) ? (gb.innerText||'').replace(/\s+/g,' ').trim().slice(0,200) : '';
    var overlays = _ovScan();
    if(overlays.length){ var top=overlays[0]; out.popup=true; out.popup_text=top.text; out.popup_cls=top.cls;
        var btns=[]; var b=document.querySelectorAll('button,a,span,[role=button]');
        for(var j=0;j<b.length;j++){var bt=(b[j].textContent||'').trim(); if(bt) btns.push(bt);}
        out.popup_buttons=btns;
    } else { out.popup=false; out.popup_text=''; }
    var bodyTxt=(document.body.innerText||'').replace(/\s+/g,' ');
    var limitKw=['您已达到沟通上限','您今天已与','沟通上限','已与150位','休息一下，明天再来',
                 '达到上限','已达上限','今日沟通次数','沟通次数已','今日已向'];
    out.body_has_limit=false; out.limit_phrase='';
    for(var k=0;k<limitKw.length;k++){ if(bodyTxt.indexOf(limitKw[k])>=0){ out.body_has_limit=true; out.limit_phrase=limitKw[k]; break; } }
    var box=document.querySelector('.job-detail-box')||document.querySelector('#main .job-detail');
        if(!box) return '';
    var els=box.querySelectorAll('button,a');
    for(var i=0;i<els.length;i++){var t=(els[i].textContent||'').trim();
        if(t.indexOf('沟通')>=0||t.indexOf('送达')>=0){out.btn=t;break;}}
    out.overlays=overlays;
    return JSON.stringify(out);
})()
"""

VISIBLE_OVERLAYS_JS = OVERLAY_SCAN_JS + r"""
(function(){
    var ov=_ovScan();
    var bodyTxt=(document.body.innerText||'').replace(/\s+/g,' ');
    var limitKw=['您已达到沟通上限','您今天已与','沟通上限','已与150位','休息一下，明天再来',
                 '达到上限','已达上限','今日沟通次数','沟通次数已','今日已向'];
    var hit=null; for(var k=0;k<limitKw.length;k++){ if(bodyTxt.indexOf(limitKw[k])>=0){ hit=limitKw[k]; break; } }
    return JSON.stringify({overlay_count:ov.length, overlays:ov, body_has_limit:!!hit, limit_phrase:hit||''}, null, 1);
})()
"""

POPUP_ALLOW_CLOSE = ["留在此页", "继续沟通", "取消", "关闭", "我知道了", "稍后", "暂不", "X"]


def read_popup_type(session=SESSION):
    """Read only; limits take precedence over generic acknowledgement buttons."""
    info = json.loads(evaluate(POPUP_STATE_JS, session=session) or "{}")
    txt = (info.get("popup_text") or "") + " || " + (info.get("greet_text") or "")
    if info.get("body_has_limit") or any(x in txt for x in (
            "已达上限", "达到上限", "今日上限", "投递上限", "已达150", "已达 150",
            "次数已达", "沟通上限", "已与150位", "您已达到沟通上限", "休息一下，明天再来")):
        return "daily_limit"
    if not info.get("popup") and not info.get("greet_open"):
        return ""
    if any(x in txt for x in ("选择原因", "减少不合适职位推荐", "不感兴趣")):
        return "not_interested"
    if any(x in txt for x in ("上传附件简历", "在线填写", "拖拽文件")):
        return "resume_guide"
    if any(x in txt for x in ("已向BOSS发送消息", "已发送", "[送达]", "已送达", "留在此页")):
        return "greet_sent"
    if any(x in txt for x in ("我知道了", "继续加油")) or txt.strip(' |') in ("好", "确定"):
        return "tip"
    return "unknown"


def click_popup_button(button_text, session=SESSION):
    """原生点击弹窗内文字匹配的按钮（WebBridge 真实鼠标点击，比合成 .click() 可靠）。

    匹配严格化（修复点错/点空隐患）：
      - 仅限 button / a 元素——弹窗按钮都是真按钮或链接；外层 div/span 的 textContent
        也包含按钮文字，若命中容器会点到"空壳"导致按钮没真正触发。
      - 优先【精确匹配】可见文字；无精确匹配才退回【包含匹配】，且只对叶子节点
        (children.length===0) 匹配，坚决避免命中包裹容器。
    """
    tag = evaluate(r"""(function(){
        var target=%s;
        var boxes=document.querySelectorAll('.greet-boss-container,.greet-boss-dialog,.dialog-wrap,.dialog-container,.dialog-content,.dialog-box,[role=dialog]');
        var all=[];
        for(var b=0;b<boxes.length;b++){var br=boxes[b].getBoundingClientRect();
            if(br.width>0&&br.height>0) all=all.concat(Array.from(boxes[b].querySelectorAll('button,a')));}
        for(var i=0;i<all.length;i++){ var t=(all[i].textContent||'').trim();
            if(t===target){ all[i].id='__popbtn__'; return 'tagged'; } }
        for(var i=0;i<all.length;i++){ var el=all[i];
            if(el.children.length>0) continue;
            var t=(el.textContent||'').trim();
            if(t.indexOf(target)>=0){ el.id='__popbtn__'; return 'tagged'; } }
        return 'NO_BTN';
    })()""" % json.dumps(button_text), session=session)
    if tag != 'tagged':
        return 'NO_BTN:'+button_text
    ok = click('#__popbtn__', session=session)
    evaluate(r"(function(){var e=document.getElementById('__popbtn__'); if(e) e.removeAttribute('id');})()", session=session)
    time.sleep(1.5)
    return ('CLICKED:'+button_text) if ok else 'CLICK_FAIL'


def safe_close_popup(session=SESSION):
    ptype = read_popup_type(session)
    if ptype == "resume_guide":
        r = _click_text("没有附件简历", session)
        if not r.startswith("CLICKED"):
            click_popup_button("X", session)
        return ptype
    if ptype == "greet_sent":
        click_popup_button("留在此页", session)
        return ptype
    if ptype == "not_interested":
        r = click_popup_button("X", session)
        if not r.startswith("CLICKED"):
            click_popup_button("取消", session)
        return ptype
    if ptype == "tip":
        # 第120份等非致命提示弹窗：点掉继续，不计入成功/失败
        for bt in ("我知道了", "知道了", "好", "确定"):
            r = click_popup_button(bt, session)
            if r and r.startswith("CLICKED"):
                break
        return ptype
    if ptype == "daily_limit":
        return ptype
    return ptype


def _click_text(text_sub, session=SESSION):
    """原生点击文字精确匹配的元素（WebBridge 真实鼠标点击）。"""
    tag = evaluate(r"""(function(){
        var t=%s;
        var all=document.querySelectorAll('a,span,div,button,em,i,label,p,[role=button]');
        for(var i=0;i<all.length;i++){ var el=all[i];
            if(el.children.length===0 && (el.textContent||'').trim()===t){ el.id='__txtbtn__'; return 'tagged'; } }
        return 'NO';
    })()""" % json.dumps(text_sub), session=session)
    if tag != 'tagged':
        return 'NO:'+text_sub
    ok = click('#__txtbtn__', session=session)
    evaluate(r"(function(){var e=document.getElementById('__txtbtn__'); if(e) e.removeAttribute('id');})()", session=session)
    time.sleep(1.2)
    return ('CLICKED:'+text_sub) if ok else 'CLICK_FAIL'


def auto_handle_popups(session, max_rounds=6):
    last_type = ""
    for _ in range(max_rounds):
        ptype = read_popup_type(session)
        last_type = ptype
        if ptype == "":
            return json.loads(evaluate(POPUP_STATE_JS, session=session) or "{}"), "clean", ""
        if ptype == "daily_limit":
            return json.loads(evaluate(POPUP_STATE_JS, session=session) or "{}"), "limit", "daily_limit"
        if ptype in ("not_interested", "unknown"):
            return json.loads(evaluate(POPUP_STATE_JS, session=session) or "{}"), "danger", ptype
        safe_close_popup(session)
        time.sleep(0.8)
    return json.loads(evaluate(POPUP_STATE_JS, session=session) or "{}"), "forced", last_type


CLICK_CHAT_JS = r"""
(function(){
    var box = document.querySelector('.job-detail-box') || document.querySelector('#main .job-detail');
    if(!box) return 'NO_BTN';
    var els = box.querySelectorAll('button, a');
    for (var i=0;i<els.length;i++){
        var t=(els[i].textContent||'').trim();
        if (t==='立即沟通'){ els[i].click(); return 'CLICKED:'+els[i].tagName; }
    }
    return 'NO_BTN';
})()
"""

VERIFY_DELIVERED_JS = r"""
(function(){
    var box = document.querySelector('#main .job-detail') || document;
    var els = box.querySelectorAll('button,a');
    var btnText = '';
    for (var i=0;i<els.length;i++){
        var t=(els[i].textContent||'').trim();
        if (t.indexOf('沟通')>=0 || t.indexOf('送达')>=0){ btnText=t; break; }
    }
    var hasDelivery = false;
    var delivered = btnText === '继续沟通';
    return JSON.stringify({delivered:delivered, btn:btnText, hasDelivery:hasDelivery});
})()
"""


def _feedback(label, session=SESSION):
    """每步操作后回读 DOM，打印当前落点/按钮/弹窗类型（反馈机制，杜绝'以为'与'实际'脱节）。"""
    url, body, blen = snapshot_page(session=session)
    btn = ""
    ptype = ""
    try:
        d = json.loads(evaluate(POPUP_STATE_JS, session=session) or "{}")
        btn = d.get("btn", "")
        ptype = read_popup_type(session)
    except Exception:
        pass
    on_chat = "/chat" in url
    print(f"[feedback] {label} | URL={url[:72]} | btn={btn!r} | popup={ptype} | chat={on_chat}")
    return url, btn, ptype, on_chat


def _ensure_recommend(session=SESSION):
    """确保在工作页（推荐页 或 当前分组的关键词搜索页）；若不在则导航过去一次。
    工作页 URL 路径均为 /web/geek/jobs（推荐页与搜索页同路径，仅 query 不同），
    故以该路径判定是否已在工作页；严禁 navigate 到 job_detail 详情 URL。
    v6.11.2 修：关键词批量流程（pipeline_batch）中 WORKING_URL 写死为推荐页是过时状态，
    意外跳走后应回【当前分组的关键词页】（由 pipeline_batch.goto 写入 CURRENT_WORK_URL），
    落点校验用 target 自身的特征（含 query= 即搜索页），不再误报 header-jobs。
    """
    url, _, _ = snapshot_page(session=session)
    if "/web/geek/jobs" not in (url or ""):
        target = CURRENT_WORK_URL.get("url") or WORKING_URL
        navigate(target, wait_range=(3, 5), session=session,
                 expect_url_contains=("query=" if "query=" in target else "header-jobs"),
                 label="工作页")
        time.sleep(1)
    return True


def _select_card_on_recommend(jid, session=SESSION, max_scroll=10):
    """在推荐页【左侧列表内原地】选中 jid 对应卡片：滚动让卡片可见 → WebBridge 原生点击其岗位链接。
    返回 True/False。全程不离开推荐页（绝不 navigate 到 job_detail URL）。
    """
    sel = f'a[href*="job_detail/{jid}"]'
    for attempt in range(max_scroll):
        # 先滚动让该卡片进入视口（原生点击前需可见）
        evaluate(r"""(function(){
            var el=document.querySelector('a[href*="job_detail/%s"]');
            if(el){ el.scrollIntoView({block:'center'}); }
            return !!el;
        })()""" % jid, session=session)
        time.sleep(0.8)
        ok = click(sel, session=session)
        if ok:
            time.sleep(2.0)
            return True
        # 没点到 → 滚动左侧岗位列表加载更多卡片后重试
        evaluate(r"""(function(){
            var el=document.querySelector('a[href*="job_detail/%s"]');
            if(el){
                var p=el.parentElement;
                while(p && p!==document.body){
                    if(p.scrollHeight > p.clientHeight+4 && p.clientHeight>0){ p.scrollTop+=800; return; }
                    p=p.parentElement;
                }
            }
            window.scrollBy(0,800);
        })()""" % jid, session=session)
        time.sleep(1.2)
    return False


def _panel_shows_job(jid, title, session=SESSION):
    """校验右侧面板当前显示的是目标岗位。

    权威依据：右侧面板『立即沟通』按钮的 ka 属性必须含目标 jid。
    ⚠️ 严禁用 `title in document.body.innerText` 兜底——左侧列表本就罗列所有岗位标题，
    该判断永远为真，会掩盖'面板没切到目标岗'的脱钩，导致向错误岗位投递却记成成功。
    """
    ka = evaluate(r"""(function(){
        var box=document.querySelector('.job-detail-box')||document.querySelector('#main .job-detail');
        if(!box) return '';
        var els=box.querySelectorAll('button,a');
        for(var i=0;i<els.length;i++){ var t=(els[i].textContent||'').trim();
            if(t==='立即沟通'){ return els[i].getAttribute('ka')||''; } }
        return '';
    })()""", session=session)
    if ka and jid and jid in ka:
        return True
    return False


def _verify_delivered_on_page(session=SESSION, jid=""):
    """Verify only within the selected detail panel, never another card in the list."""
    payload = evaluate(r"""(function(){
        var box=document.querySelector('.job-detail-box')||document.querySelector('#main .job-detail');
        if(!box) return '{}';
        var jid=%s, els=box.querySelectorAll('button,a');
        for(var i=0;i<els.length;i++){
            var text=(els[i].textContent||'').trim(), ka=els[i].getAttribute('ka')||'';
            if(text==='继续沟通' && (!jid || ka.indexOf(jid)>=0)) return '{"delivered":true}';
        }
        return '{"delivered":false}';
    })()""" % json.dumps(jid), session=session)
    return json.loads(payload or '{}').get('delivered') is True


def card_exists(jid, session=SESSION):
    """当前 DOM 中是否存在目标岗位的卡片链接（用于判断该岗在当前页是否可定位）。"""
    sel = f'a[href*="job_detail/{jid}"]'
    r = evaluate(f'(function(){{return !!document.querySelector({json.dumps(sel)});}})()', session=session)
    return str(r).strip().lower() in ("true", "1", "yes")


def _try_locate_card(jid, title, session=SESSION, max_rounds=12):
    """在【当前已导航到的工作页】上定位并选中目标卡片（含滚动加载更多）。

    返回:
      - 'ok'     : 已点击且右侧面板确已切到目标岗（可继续点'立即沟通'）
      - 'absent' : 滚动加载后卡片仍不在 DOM（该页确实没有此岗 → 应跳过，不中止整批）
      - 'timing' : 卡片在 DOM 但面板始终未切换（SPA 时序异常 → 为安全计也按跳过处理）
    """
    for _r in range(max_rounds):
        if not card_exists(jid, session):
            # 卡片不在当前 DOM → 滚动加载更多后重试
            scroll_jobs_list(session=session)
            time.sleep(1.3)
            if not card_exists(jid, session):
                # 多次滚动后仍无 → 认定该页无此卡
                continue
            continue
        ok_sel = _select_card_on_recommend(jid, session=session)
        if not ok_sel:
            time.sleep(1.0)
            continue
        if _panel_shows_job(jid, title, session=session):
            return "ok"
        time.sleep(1.2)  # 面板未切换 → 等 SPA 渲染后重试
    return "timing" if card_exists(jid, session) else "absent"


def deliver_click(job, session=SESSION, auto=True):
    verify_identity()
    """Send once; count only explicit success feedback. Ambiguous sends stop for review."""
    jid, title = job.get("jobId"), (job.get("title") or "").strip()
    prior = read_popup_type(session)
    if prior == "daily_limit":
        return {"delivered_ok": False, "daily_limit": True, "stopped": True}
    if prior in ("unknown", "not_interested"):
        raise AbortScriptError("遗留异常弹窗，停止投递待人工检查")
    safe_close_popup(session)
    _ensure_recommend(session)
    if _try_locate_card(jid, title, session=session) != "ok":
        return {"delivered_ok": False, "not_found": True, "jid": jid, "reason": "目标卡片未找到或面板未确认"}
    anomaly, reason = detect_page_anomaly(session=session, check_blank=False)
    if anomaly:
        raise AbortScriptError(reason)
    verify_identity()
    activity = check_activity(jid, session)
    job['hr_activity'] = activity
    if not activity['allowed']:
        return activity_skip_result(activity)
    clicked = evaluate_once(guarded_script(CLICK_CHAT_JS), session=session)
    if not str(clicked).startswith("CLICKED:"):
        return {"delivered_ok": False, "reason": "未找到立即沟通按钮"}
    time.sleep(1.5)
    if not auto:
        return json.loads(evaluate(POPUP_STATE_JS, session=session) or "{}")
    deadline = time.time() + 15
    while time.time() < deadline:
        anomaly, reason = detect_page_anomaly(session=session, check_blank=False)
        if anomaly:
            return {"delivered_ok": False, "uncertain": True, "stopped": True, "reason": reason}
        ptype = read_popup_type(session)
        if ptype == "daily_limit":
            return {"delivered_ok": False, "daily_limit": True, "stopped": True}
        if ptype in ("unknown", "not_interested"):
            return {"delivered_ok": False, "uncertain": True, "stopped": True,
                    "reason": "发送后出现异常弹窗，需人工确认；不自动重发"}
        if ptype in ("resume_guide", "tip"):
            safe_close_popup(session)
            time.sleep(.8)
            continue
        if ptype == "greet_sent":
            closed = click_popup_button("留在此页", session)
            # v6.11.1 修（每单必停根因）：点「留在此页」后弹窗淡出/二次提示有延迟，
            # 旧逻辑单次回读 1.2s 会把"淡出中的 greet_sent"当成残留浮层 → stopped=True
            # 导致每投成功一单整批停机。改为轮询等弹窗真正关闭（最多 ~6s）：
            # 期间 greet_sent 残影/tip/resume_guide 顺手点掉；
            # unknown / not_interested / daily_limit 仍立即判定停机（安全语义不降）。
            final_pt = ""
            for _ in range(6):
                time.sleep(1.0)
                final_pt = read_popup_type(session)
                if final_pt == "" or final_pt in ("daily_limit", "unknown", "not_interested"):
                    break
                safe_close_popup(session)
            url, _, _, on_chat = _feedback("发送成功后关闭弹窗", session)
            if on_chat:
                _ensure_recommend(session)
            return {"delivered_ok": True, "verify": True, "verification_source": "success_popup",
                    "wrong_path": on_chat,
                    "stopped": on_chat or bool(final_pt)}
        url, _, _ = snapshot_page(session=session)
        if "/chat" in url:
            _ensure_recommend(session)
            return {"delivered_ok": False, "verify": False, "uncertain": True, "stopped": True,
                    "reason": "页面进入聊天但未读到成功反馈，需人工核对"}
        if _verify_delivered_on_page(session, jid):
            return {"delivered_ok": True, "verify": True, "verification_source": "target_panel"}
        time.sleep(1)
    return {"delivered_ok": False, "verify": False, "uncertain": True, "stopped": True,
            "timeout": True, "reason": "发送后超时且未确认送达，需人工核对；不自动重发"}


def verify_delivered(session=SESSION):
    try:
        d = json.loads(evaluate(VERIFY_DELIVERED_JS, session=session) or "{}")
        ok = d.get("delivered", False)
        detail = f"btn={d.get('btn','')} delivery={d.get('hasDelivery',False)}"
    except Exception:
        ok, detail = False, "verify_error"
    return ok, detail


def verify_against_account(session=SESSION, expected=None):
    """Read the visible communication list; restore the jobs page even on errors."""
    try:
        navigate("https://www.zhipin.com/web/geek/chat", wait_range=(3, 5),
                 session=session, expect_url_contains="chat", label="沟通列表")
        time.sleep(1.5)
        body = evaluate("document.body.innerText", session=session) or ""
        missing = []
        for item in expected or []:
            title, company = (list(item) + ["", ""])[:2] if isinstance(item, (list, tuple)) else (item or "", "")
            key = company or title
            if not key or key not in body:
                missing.append(title or company or "无标识记录")
        return body, missing
    finally:
        navigate(RECOMMEND_URL, wait_range=(3, 5), session=session,
                 expect_url_contains="header-jobs", label="回推荐页")


def run_verify(workdir, session=SESSION):
    """独立诊断模式：仅核对本地已投递记录与真实沟通列表，读后自动回到推荐页。"""
    progress_file = os.path.join(workdir, "stream_progress.json")
    pm = ProgressManager(progress_file)
    pm.load()
    delivered = pm.data.get("delivered", [])
    if not delivered:
        print("[verify] 本地无已投递记录，无需核对")
        return
    items = [(d.get("title", ""), d.get("company", "")) for d in delivered]
    _ensure_recommend(session)  # 起点确保在推荐页
    chat_body, missing = verify_against_account(session=session, expected=items)
    print(f"[verify] 本地已投递 {len(items)} 份，真实沟通列表核对：")
    if missing:
        print(f"[verify] ⚠️ 当前可见沟通列表未匹配（不据此判定投递失败）：{missing}")
    else:
        print("[verify] 当前可见沟通列表匹配到全部公司/标题（不等同逐岗位送达核验）")


# ───────────────────────────────────────────────────────────
# 收尾闭环：投递成果汇报（自包含 HTML）
# ───────────────────────────────────────────────────────────
def write_summary_page(workdir, profile=None):
    assert_workdir(workdir)
    """Generate an escaped, sponsor-free report from saved records without asserting missing verification."""
    import re, datetime
    from html import escape
    def esc(value):
        return escape(str(value if value is not None else ""), quote=True)
    prog = os.path.join(workdir, "stream_progress.json")
    if not os.path.exists(prog):
        return None
    with open(prog, "r", encoding="utf-8") as f:
        data = json.load(f)
    delivered = data.get("delivered", [])

    def dir_of(j):
        return (j.get("direction") or j.get("industry") or "其他").strip() or "其他"

    groups = {}
    for j in delivered:
        groups.setdefault(dir_of(j), []).append(j)
    order = sorted(groups.keys(), key=lambda c: (-len(groups[c]), c))  # 多者在前

    def sal_low(s):
        return salary_range_k(s)[0]

    total = len(delivered)
    prof = profile or {}
    floor = prof.get("min_salary_k")
    city = prof.get("target_city") or "—"
    low = [j for j in delivered if floor and sal_low(j.get("salary", "")) is not None
           and sal_low(j.get("salary", "")) < float(floor)] if floor else []

    rows_html = []
    for cat in order:
        items = groups[cat]
        rows_html.append(f'<h2 class="cat">{esc(cat)} <span class="cnt">×{len(items)}</span></h2>')
        rows_html.append('<table><thead><tr><th>#</th><th>岗位</th><th>公司</th><th>薪资</th><th>行业</th></tr></thead><tbody>')
        for idx, j in enumerate(items, 1):
            rows_html.append(
                f"<tr><td>{idx}</td><td>{esc(j.get('title',''))}</td><td>{esc(j.get('company',''))}</td>"
                f"<td>{esc(j.get('salary',''))}</td><td>{esc(j.get('industry',''))}</td></tr>"
            )
        rows_html.append("</tbody></table>")

    cats_summary = "".join(f"<span class='pill'>{esc(c)}×{len(groups[c])}</span>" for c in order)

    verified = sum(j.get("verify") is True for j in delivered)
    unverified = sum(j.get("verify") is False for j in delivered)
    unknown = total - verified - unverified
    uncertain = len(data.get("uncertain", []))
    activity_skipped = len(data.get('activity_skipped', []))
    verification_summary = f"页面核验已确认 {verified} 份 · 未确认 {unverified} 份 · 未保存核验信息 {unknown} 份"
    if uncertain:
        verification_summary += f" · 另有 {uncertain} 份发送结果待人工核对"
    if activity_skipped:
        verification_summary += f" · 活跃度筛选跳过 {activity_skipped} 条记录（不计失败，可再次评估）"

    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
    floor_txt = f"{floor}K" if floor else "—"
    low_note = (f"<br><b class='warn'>注意：</b>有 {len(low)} 份薪资下限低于门槛 {esc(floor_txt)}，请人工核对。"
                if low else "")
    account = metadata() or data.get('account_context', {})
    account_line = (f"账户：{esc(account.get('account'))} ｜ 浏览器：{esc(account.get('browser'))}"
                    f" ｜ 运行：{esc(account.get('run'))}<br>" if account else '')
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BOSS 投递成果汇报 · 本轮 {total} 份</title>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#eef2f7;color:#222;margin:0;padding:26px 14px 60px}}
.wrap{{max-width:880px;margin:0 auto}}
h1{{font-size:25px;color:#1763c9;margin:0 0 4px}}
.sub{{color:#7a869a;font-size:13.5px;margin-bottom:16px;line-height:1.6}}
.banner{{background:linear-gradient(135deg,#1763c9,#36a3ff);color:#fff;border-radius:16px;padding:22px 26px;margin-bottom:18px;box-shadow:0 8px 22px rgba(23,99,201,.25)}}
.banner .big{{font-size:36px;font-weight:800;letter-spacing:.5px}}
.banner .small{{font-size:13.5px;opacity:.94;margin-top:8px;line-height:1.6}}
.pills{{margin:6px 0 8px;line-height:2.4}}
.pill{{display:inline-block;background:#eaf2ff;color:#1763c9;border:1px solid #cfe2ff;border-radius:20px;padding:4px 12px;font-size:12.5px;margin:3px 6px 3px 0}}
.card{{background:#fff;border-radius:14px;padding:18px 20px;margin:16px 0;box-shadow:0 2px 10px rgba(20,40,80,.06)}}
.cat{{font-size:17px;color:#0f3d7a;margin:18px 0 8px;border-left:4px solid #36a3ff;padding-left:10px}}
.cat .cnt{{color:#36a3ff;font-size:14px;font-weight:600}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden;border:1px solid #eef1f5}}
th,td{{text-align:left;padding:9px 12px;font-size:13px;border-bottom:1px solid #eef1f5}}
th{{background:#f0f5fc;color:#345;font-weight:600}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#f7faff}}
.note{{background:#fff8ec;border:1px solid #ffe2b8;border-radius:12px;padding:14px 18px;font-size:13px;color:#8a5a00;margin-top:18px;line-height:1.8}}
.warn{{color:#c0392b;font-weight:600}}
</style></head><body>
<div class="wrap">
<h1>📊 BOSS 直聘 · 本轮投递成果汇报</h1>
<div class="sub">{account_line}生成时间：{now} ｜ 工作地：{esc(city)} ｜ 薪资门槛：≥{esc(floor_txt)} ｜ 流程：推荐页优先 + 关键词补刀 + AI 语义终审</div>
<div class="banner"><div class="big">投递记录 {total} 份</div><div class="small">{verification_summary}· 覆盖 {len(order)} 个方向</div></div>
<div class="card">
<div class="pills">{cats_summary}</div>
{''.join(rows_html)}
<div class="note">
<b>投递说明：</b>本轮保存 <b>{total}</b> 条投递记录，覆盖 <b>{len(order)}</b> 个方向；{verification_summary}。
以上统计来自本地保存的投递记录；历史记录缺少核验字段时显示为未保存核验信息，不推定已确认送达。{low_note}
</div>
</div>
</div>
</body></html>"""
    html_path = os.path.join(workdir, "投递汇总.html")
    atomic_write_text(html_path, html)
    return html_path


# ───────────────────────────────────────────────────────────
# 使用须知 / 首次运行引导
# ───────────────────────────────────────────────────────────
def print_usage_notice():
    notice = (
        "\n╔══════════════════════════════════════════════════════════════╗\n"
        "║          📋 BOSS 直聘投递技能 · 使用须知（必读）              ║\n"
        "╠══════════════════════════════════════════════════════════════╣\n"
        "║  ✅ 本技能定位：替你完成「机械重复的浏览+投递」，             ║\n"
        "║     最终「该不该投」由 AI 语义判断（基于你填写的求职意向）。      ║\n"
        "║                                                            ║\n"
        "║  ❌ 绝非鼓励无脑海投：                                       ║\n"
        "║     · 请勿高频滥用（建议每日 ≤50 次沟通）                     ║\n"
        "║     · 请勿对明显不匹配的岗位投递，避免污染你的求职画像        ║\n"
        "║     · 请勿刻意绕过平台反滥用机制                             ║\n"
        "║                                                            ║\n"
        "║  ⚙️  使用前必做：在 user_profile.json 填写你的求职方向         ║\n"
        "║     （role_summary / prefer / avoid），让 AI 审核有依据。     ║\n"
        "║                                                            ║\n"
        "║  🛡️  安全机制：每次投递都回读页面确认「送达」才继续；        ║\n"
        "║     遇未知弹窗/误触/日上限 立即整批停手，绝不无脑继续。       ║\n"
        "╚══════════════════════════════════════════════════════════════╝\n"
        "   三步上手：① 填 user_profile.json → ② 准备环境(Chrome/WebBridge/登录) → ③ 运行本技能\n"
    )
    print(notice)


# ───────────────────────────────────────────────────────────
# 平台级常量（与筛选无关，仅为安全护栏）
# ───────────────────────────────────────────────────────────
DAILY_CAP = 150
FAIL_BREAKER = 3

DAILY_LIMIT_JS = r"""
(function(){
    var txt = (document.body.innerText||'');
    var dlg = document.querySelector('.dialog-content,.dialog-wrap,.dialog-box,.dialog-container,[class*=dialog]');
    if (dlg) txt = txt + ' || ' + (dlg.innerText||'');
    var patterns = ['已达上限','达到上限','今日上限','投递上限','已达150','已达 150','今日已投递','次数已达','今日投递','沟通上限','已与150位','您已达到沟通上限','休息一下，明天再来','您今天已与'];
    var hit = null;
    for (var i=0;i<patterns.length;i++){ if (txt.indexOf(patterns[i])>=0){ hit = patterns[i]; break; } }
    return JSON.stringify({hit: hit});
})()
"""


def detect_daily_limit(session=SESSION):
    try:
        d = json.loads(evaluate(DAILY_LIMIT_JS, session=session) or "{}")
        hit = d.get("hit")
        return bool(hit), hit or ""
    except Exception:
        return False, "detect_error"


# ───────────────────────────────────────────────────────────
# 入口（仅收尾/诊断；投递主流程在 _live_driver.py）
# ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="BOSS 直聘投递 · 收尾/诊断（实时投递见 _live_driver.py）")
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--mode", default="report", choices=["verify", "report"])
    ap.add_argument("--diag", action="store_true",
                    help="诊断模式：只读 dump 所有可见浮层 + 弹窗分类 + 日上限检测，不投递")
    a = ap.parse_args()

    profile = load_profile()

    if a.diag:
        print_usage_notice()
        print("[diag] 进入诊断模式（只读，不投递）")
        _env = check_env(verbose=True)
        if not _env.ok:
            print("[diag] ⚠️ 环境异常，诊断结果可能不准确")
        navigate(RECOMMEND_URL, wait_range=(3, 5), session=SESSION,
                 expect_url_contains="header-jobs", label="推荐页")
        time.sleep(3)
        ov = json.loads(evaluate(VISIBLE_OVERLAYS_JS) or "{}")
        ptype = read_popup_type(SESSION)
        is_limit, lp = detect_daily_limit(SESSION)
        full = json.loads(evaluate(POPUP_STATE_JS) or "{}")
        report = {
            "read_popup_type": ptype,
            "detect_daily_limit": {"hit": is_limit, "phrase": lp},
            "visible_overlays": ov,
            "popup_state_full": full,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        diag_file = os.path.join(a.workdir, f"diag_{int(time.time())}.json")
        try:
            json.dump(report, open(diag_file, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"[diag] 报告已写入 {diag_file}")
        except Exception as e:
            print(f"[diag] 写文件失败: {e}")
        sys.exit(0)

    if a.mode == "verify":
        run_verify(a.workdir, session=SESSION)
    elif a.mode == "report":
        # 收尾闭环：投递成果汇报自包含页
        out = write_summary_page(a.workdir, profile=profile)
        if not out:
            print("[report] ⚠️ 未找到 stream_progress.json，无法生成汇报页")
            sys.exit(1)
        print(f"[report] 已生成合并汇报页：{out}")
