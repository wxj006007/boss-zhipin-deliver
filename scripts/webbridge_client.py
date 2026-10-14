#!/usr/bin/env python3
"""
WebBridge HTTP 客户端 v4 — 共享模块

所有 BOSS 脚本通过此模块与 Kimi WebBridge 通信。
提供 decrypt_salary、extract_jobs、handle_popup 等 Boss直聘专用工具函数。

核心设计:
  - 全局单一 SESSION，所有脚本复用同一个标签页
  - api() 自带 3 次重试
  - extract_jobs() 内置 Vue $props.data 提取 + DOM 兜底（v4 新增）
  - handle_popup() 处理"好"/"确定"弹窗（含第120份投递弹窗）
  - ProgressManager 管理断点续传

v4 变更 (2026-07-30):
  - extract_jobs() 增加 DOM 兜底：Vue __vue__ 私有 API 失效时，
    改用已验证的 DOM 选择器 (.job-name/.boss-name/.company-location) 提取
  - clean_pid_files() 将 ~/.trae-cn/*.pid 改为 ~/.workbuddy/*.pid
  - register_signal_guard() 增加 SIGINT（Windows 亦可捕获）

用法:
    from scripts.webbridge_client import (
        api, evaluate, navigate, extract_jobs,
        decrypt_salary, parse_salary_max, handle_popup,
        scroll_page, ProgressManager, SESSION
    )
"""

import json
import http.client
import time
import re
import random
import os
import signal
import difflib
import glob
import subprocess
from contextvars import ContextVar
from datetime import datetime
try:
    from .runtime import atomic_write_json
except ImportError:
    from runtime import atomic_write_json
from scripts.accounts import context, assert_route, assert_run, assert_workdir, metadata
from scripts.hr_activity import ACTIVITY_HELPER_JS

# ═══════════════════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════════════════

_ACCOUNT = context() or {}
DEFAULT_HOST = _ACCOUNT.get('host', "127.0.0.1")
DEFAULT_PORT = _ACCOUNT.get('port', 10086)
_ROUTE = ContextVar('webbridge_route', default=None)

# ★ 全局唯一 SESSION — 所有脚本共用一个标签页
#   禁止在各脚本中自定义 SESSION 名
SESSION = _ACCOUNT.get('session', "boss-main")

DEFAULT_TIMEOUT = 60
MAX_RETRIES = 3
RETRY_DELAY = 3

# ═══════════════════════════════════════════════════════════
# Boss直聘薪资字体解密
# ═══════════════════════════════════════════════════════════

def decrypt_salary(text):
    """解密 Boss直聘薪资字体加密（U+E031~U+E03A → 数字 0~9）"""
    if not text:
        return ""
    result = []
    for ch in text:
        cp = ord(ch)
        if 0xE031 <= cp <= 0xE03A:
            result.append(str(cp - 0xE031))
        else:
            result.append(ch)
    return ''.join(result)


def parse_salary_max(value):
    """Monthly salary bound in K; unknown/day/hour rates return 0."""
    try:
        from .runtime import salary_range_k
    except ImportError:
        from runtime import salary_range_k
    return salary_range_k(value)[1] or 0


# ═══════════════════════════════════════════════════════════
# 模块级 API 函数（自带重试）
# ═══════════════════════════════════════════════════════════

def api(action, args=None, session=SESSION, host=None, port=None, timeout=DEFAULT_TIMEOUT, retries=MAX_RETRIES):
    """发送 WebBridge API 请求，返回解析后的 dict。自带 MAX_RETRIES 次重试。"""
    if args is None:
        args = {}
    route = _ROUTE.get() or (DEFAULT_HOST, DEFAULT_PORT, SESSION)
    host, port = host or route[0], port or route[1]
    session = route[2] if session == SESSION else session
    assert_route(host, port, session)
    last_error = None
    for attempt in range(retries):
        conn = None
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            body = json.dumps({"action": action, "args": args, "session": session}).encode("utf-8")
            conn.request("POST", "/command", body=body, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = json.loads(resp.read().decode("utf-8"))
            return data
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(RETRY_DELAY)
        finally:
            if conn is not None:
                conn.close()
    raise last_error


def evaluate(code, session=SESSION):
    """执行 JS 并返回字符串结果"""
    r = api("evaluate", {"code": code}, session=session)
    return r.get("data", {}).get("value", "")


def evaluate_once(code, session=SESSION):
    """Execute a mutation once. A lost response must never replay a send."""
    r = api("evaluate", {"code": code}, session=session, retries=1)
    if r.get("ok") is not True or "value" not in r.get("data", {}):
        raise RuntimeError("WebBridge 操作未确认，请回读页面；不自动重试")
    return r["data"]["value"]


class PageLandingError(Exception):
    """导航落点校验失败：以为打开页面A，实际打开的是页面B。"""
    pass


def assert_landing(session=SESSION, expect_url_contains=None,
                   require_texts=None, forbid_texts=None, label="页面"):
    """导航后【必须】回读 DOM 确认落点，杜绝"以为打开A实际B"。

    参数:
      expect_url_contains: location.href 必须包含此串（确认到了预期路由）
      require_texts:       body.innerText 必须命中其中【任一】（确认内容渲染对了）
      forbid_texts:        body.innerText 不得命中其中【任一】（命中即说明落错页）
    任一不满足即抛 PageLandingError。
    """
    actual_url = evaluate("location.href", session=session) or ""
    body = evaluate("document.body.innerText", session=session) or ""
    if expect_url_contains and expect_url_contains not in actual_url:
        raise PageLandingError(
            f"[落点校验失败·{label}] URL 应含 '{expect_url_contains}'，实际 '{actual_url}'")
    if forbid_texts:
        hit = [t for t in forbid_texts if t in body]
        if hit:
            raise PageLandingError(
                f"[落点校验失败·{label}] 命中禁止文本 {hit}（落错页），URL='{actual_url}'")
    if require_texts:
        if not any(t in body for t in require_texts):
            raise PageLandingError(
                f"[落点校验失败·{label}] 期望含 {require_texts} 任一但未命中，URL='{actual_url}'")
    return True


def navigate(url, wait_range=(4, 6), session=SESSION,
             expect_url_contains=None, require_texts=None, forbid_texts=None, label="页面"):
    """导航到 URL 并等待 SPA 渲染。

    ⚠ 防御性：只要传入 expect_url_contains / require_texts / forbid_texts 中任一，
      导航后会自动回读 DOM 调用 assert_landing 校验落点，失败抛 PageLandingError。
      调用方【必须】为关键导航声明期望——不声明即视为"未确认落点"的隐患。
    """
    r = api("navigate", {"url": url}, session=session)
    time.sleep(random.uniform(*wait_range))
    if expect_url_contains or require_texts or forbid_texts:
        assert_landing(session=session, expect_url_contains=expect_url_contains,
                       require_texts=require_texts, forbid_texts=forbid_texts, label=label)
    return r


def click(selector, session=SESSION):
    """通过 CSS 选择器点击元素"""
    return api("click", {"selector": selector}, session=session).get("data", {}).get("success", False)


def scroll_page(session=SESSION):
    """滚动到页面底部，触发懒加载"""
    evaluate("(function(){window.scrollTo(0, document.body.scrollHeight);return document.body.scrollHeight;})()", session=session)


# ═══════════════════════════════════════════════════════════
# 页面反馈检测 / 异常检测 / 标签页独占锁
# （v4.1 新增 — 落实"每步操作后必须回读页面确认反馈/跳转，
#   发现页面异常立即停脚本"的硬性纪律）
# ═══════════════════════════════════════════════════════════

class AbortScriptError(Exception):
    """页面/环境级异常：必须立即停止【整个】脚本（不是跳过单个岗位）。

    与 PageLandingError 的区别：
      - PageLandingError 仅表示"某次导航落点不对"，由调用方决定跳过/重试。
      - AbortScriptError 表示"环境坏了"（登录墙/风控/空白页/标签被劫持/
        达到每日上限等），必须终止整批，等待人工介入。
    """


# 登录墙 / 安全验证 —— 命中即认为会话失效，需人工重登
_ANOMALY_LOGIN = [
    "请先登录", "扫码登录", "登录已过期", "请登录后", "BOSS直聘登录",
    "账号未登录", "登录状态已失效",
]

# 风控 / 反爬 / 账号风险 —— 命中即停，避免被封
_ANOMALY_RISK = [
    "操作过于频繁", "系统检测到您的账号", "账号存在风险", "行为异常",
    "请进行安全验证", "风险校验", "暂时无法操作", "请稍后再试", "验证码",
    "网络环境异常", "被限制",
]


def snapshot_page(session=SESSION):
    """只读快照：返回 (url, body_text, body_len)，【不导航】。供监督与异常检测。"""
    url = evaluate("window.location.href", session=session) or ""
    body = evaluate("document.body.innerText", session=session) or ""
    return url, body, len(body)


def detect_page_anomaly(session=SESSION, allow_login_wall=True, check_blank=True):
    """检测【页面/环境级】异常（环境坏了，必须停脚本）。

    返回 (is_anomaly, detail)。
    参数 allow_login_wall: 某些场景登录是预期内（如刚启动），可由调用方关掉该项。
    参数 check_blank: 是否把"空白/内容过少"判为异常。wait_for_feedback 轮询时
       传 False —— 点击/导航过程中页面会瞬时空白(跳转中)，属正常过渡，不应误判
       为异常中止整批；只有登录墙/风控/安全验证才是真异常，仍立即中止。
    """
    url, body, blen = snapshot_page(session=session)
    # 空白页 / 渲染失败（仅 check_blank 时判异常；过渡态空白由调用方容忍）
    if check_blank and blen < 60:
        return True, f"页面空白或内容过少(body_len={blen})，可能渲染失败/被重定向"
    # URL 跳登录
    if "passport" in url or ("login" in url and "zhipin.com" in url):
        return True, f"页面跳转到登录页: {url[:90]}"
    if allow_login_wall:
        for p in _ANOMALY_LOGIN:
            if p in body:
                return True, f"登录墙/安全验证命中: '{p}'"
    for p in _ANOMALY_RISK:
        if p in body:
            return True, f"风控/异常命中: '{p}'"
    return False, ""


def wait_for_feedback(expected_texts, timeout=8, poll=0.5,
                      anomaly_check=True, forbid_texts=None, session=SESSION):
    """操作（点击/导航）后，【轮询 DOM 等待预期反馈出现】，确认动作生效。

    这是落实"检测到页面有相应的反馈或者跳转后才进行下一步"的核心门控：
      - 命中 expected_texts 任一 → 返回该文本（反馈已确认）。
      - 命中 forbid_texts 任一 → 抛 AbortScriptError（出现了不该出现的异常态）。
      - 若 anomaly_check 且发现页面级异常 → 抛 AbortScriptError。
      - 超时仍未命中 → 返回 None（交由调用方判断，通常记为 unknown/fail 并跳过该岗位）。

    注意：本函数【不抛 PageLandingError】，而是抛 AbortScriptError 以便整批停止。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if anomaly_check:
            # 容忍过渡态空白(check_blank=False)：登录墙/风控/安全验证仍立即中止
            is_anom, detail = detect_page_anomaly(session=session, check_blank=False)
            if is_anom:
                raise AbortScriptError(f"等待反馈时检测到页面异常: {detail}")
        body = evaluate("document.body.innerText", session=session) or ""
        if forbid_texts:
            hit = [t for t in forbid_texts if t in body]
            if hit:
                raise AbortScriptError(f"等待反馈时出现禁止态 {hit}（页面异常）")
        for t in expected_texts:
            if t in body:
                return t
        time.sleep(poll)
    return None


def _list_other_boss_script_pids():
    """枚举除自己外、正在运行的其他 boss-zhipin-deliver 脚本进程 pid。

    仅靠进程名不够，必须看命令行含 'boss-zhipin-deliver/scripts/phase'，
    避免误杀用户其它 python 程序。
    """
    me = os.getpid()
    try:
        ps = (r"Get-CimInstance Win32_Process -Filter \"Name='python.exe'\""
              r" | Where-Object { $_.CommandLine -like '*boss-zhipin-deliver/scripts/phase*'"
              r" -and $_.ProcessId -ne %d }"
              r" | Select-Object -ExpandProperty ProcessId") % me
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout
        return [int(x.strip()) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def kill_other_boss_scripts():
    """启动前强制清场：杀掉其它正在运行的 boss 脚本（防止抢同一标签页）。

    返回被杀死的 pid 列表。
    """
    if assert_run():
        raise RuntimeError('账户模式通过串行占用管理任务，禁止清场其他进程')
    killed = []
    for pid in _list_other_boss_script_pids():
        try:
            r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                killed.append(pid)
        except Exception:
            pass
    return killed


# 标签页独占锁文件（记录当前占用脚本的 pid，供监督者读取"谁在控制 tab"）
_TAB_LOCK_PATH = os.path.expanduser(
    "~/.workbuddy/skills/boss-zhipin-deliver/.tab_lock")


def acquire_tab_lock(script_name="unknown"):
    """占用标签页：先杀其它 boss 脚本保证单例，再写锁文件。

    返回被清场的 pid 列表（供日志汇报）。
    """
    killed = kill_other_boss_scripts()
    info = {
        "pid": os.getpid(),
        "script": script_name,
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        with open(_TAB_LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False)
    except Exception:
        pass
    return killed


def release_tab_lock():
    try:
        if os.path.exists(_TAB_LOCK_PATH):
            os.remove(_TAB_LOCK_PATH)
    except Exception:
        pass


def read_tab_lock():
    """监督者读取：当前谁在控制标签页。返回 dict 或 None。"""
    try:
        if os.path.exists(_TAB_LOCK_PATH):
            with open(_TAB_LOCK_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        return None
    return None


# ═══════════════════════════════════════════════════════════
# Boss直聘 专用工具函数
# ═══════════════════════════════════════════════════════════

# Vue $props.data 提取（首选，字段最完整）
_EXTRACT_JOBS_VUE_JS = r"""
(function(){
    ACTIVITY_HELPER
    var cards = document.querySelectorAll('.job-card-wrap');
    var jobs = [];
    var seen = new Set();
    for (var i = 0; i < cards.length; i++) {
        try {
            var d = cards[i].__vue__.$props.data;
            if (!d || !d.encryptJobId) continue;
            var jid = d.encryptJobId;
            if (seen.has(jid)) continue;
            seen.add(jid);
            jobs.push({
                title: d.jobName || "",
                salary: d.salaryDesc || "",
                city: d.cityName || "",
                company: d.brandName || "",
                industry: d.brandIndustry || "",
                scale: d.brandScaleName || "",
                stage: d.brandStageName || "",
                online: !!d.bossOnline,
                hr_activity: bossActivity(cards[i], d, 'card'),
                welfare: (d.welfareList || []).join("|"),
                degree: d.jobDegree || "",
                experience: d.jobExperience || "",
                jobId: jid,
            });
        } catch(e) {}
    }
    return JSON.stringify({total: jobs.length, list: jobs});
})()
""".replace('ACTIVITY_HELPER', ACTIVITY_HELPER_JS)

# DOM 兜底提取（v4 新增）：Vue 私有 API 失效时改用已验证选择器
#   公司名 → .boss-name  （不是 .company-name）
#   地区   → .company-location（不是 .job-area）
#   标题   → a.job-name（含 href /job_detail/{jobId}.html）
_EXTRACT_JOBS_DOM_JS = r"""
(function(){
    ACTIVITY_HELPER
    var links = document.querySelectorAll('a[href*="job_detail"]');
    var jobs = [];
    var seen = {};
    for (var i = 0; i < links.length; i++) {
        var a = links[i];
        var title = (a.innerText || '').trim().split('\n')[0];
        var href = a.getAttribute('href') || '';
        var m = href.match(/\/job_detail\/([^.?]+)/);
        var jid = m ? m[1] : '';
        if (!jid || seen[jid]) continue;
        seen[jid] = 1;
        var card = a.closest('.job-card-wrap') || a.closest('li') || a.parentElement;
        var salary = '', company = '', location = '';
        if (card) {
            var salEl = card.querySelector('.job-salary') || card.querySelector('[class*="salary"]');
            salary = salEl ? (salEl.innerText || '').trim() : '';
            var compEl = card.querySelector('.boss-name');
            company = compEl ? (compEl.innerText || '').trim() : '';
            var locEl = card.querySelector('.company-location');
            location = locEl ? (locEl.innerText || '').trim() : '';
        }
        jobs.push({
            title: title, salary: salary, city: location, company: company,
            industry: '', scale: '', stage: '', online: false,
            hr_activity: bossActivity(card, {}, 'card_dom'),
            welfare: '', degree: '', experience: '', jobId: jid, href: href,
        });
    }
    return JSON.stringify({total: jobs.length, list: jobs});
})()
""".replace('ACTIVITY_HELPER', ACTIVITY_HELPER_JS)


def _parse_jobs(raw):
    """解析 extract_jobs 的 JS 返回（JSON 或空）"""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data.get("list", [])
    except (json.JSONDecodeError, TypeError):
        return []


def extract_jobs(session=SESSION):
    """从当前页面提取所有岗位卡片。

    策略 (v4): 先尝试 Vue $props.data（字段最完整），
    若返回为空（Vue 私有 API 失效/页面结构变化），回退到 DOM 选择器提取。
    薪资已解密，并按 jobId 去重。
    """
    jobs = _parse_jobs(evaluate(_EXTRACT_JOBS_VUE_JS, session=session))
    if not jobs:
        jobs = _parse_jobs(evaluate(_EXTRACT_JOBS_DOM_JS, session=session))

    # 按 jobId 去重（同一卡片可能同时被两种提取命中）
    merged = {}
    for j in jobs:
        jid = j.get('jobId', '')
        if jid and jid not in merged:
            merged[jid] = j
    for j in merged.values():
        j['salary'] = decrypt_salary(j.get('salary', ''))
    return list(merged.values())


_POPUP_SELECTORS = [
    '.btn-sure',
    '.dialog-container .btn',
    '.greet-pop .btn',
    '.dialog-confirm', '.layer-btn-confirm', '.btn-confirm',
    '.dialog-ok', '.modal-confirm', '.layui-layer-btn0',
]


def handle_popup(session=SESSION):
    """检测并处理弹窗（特别是第120份时的"好"弹窗）"""
    time.sleep(1.5)

    result = evaluate("""
    (function(){
        var prioritySelectors = %s;
        for (var j = 0; j < prioritySelectors.length; j++) {
            var btn = document.querySelector(prioritySelectors[j]);
            if (btn) {
                var rect = btn.getBoundingClientRect();
                var style = window.getComputedStyle(btn);
                if (rect.width > 0 && rect.height > 0 && style.display !== 'none') {
                    btn.click();
                    return 'clicked: ' + prioritySelectors[j];
                }
            }
        }
        var btns = document.querySelectorAll('button, a');
        for (var i = 0; i < btns.length; i++) {
            var el = btns[i];
            var text = (el.textContent || '').trim();
            if (text === '好' || text === '确定' || text === '我知道了' || text === '继续沟通') {
                var rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    el.click();
                    return 'clicked: ' + text + ' (' + el.tagName + '.' + el.className + ')';
                }
            }
        }
        return 'no_popup';
    })()
    """ % json.dumps(_POPUP_SELECTORS), session=session)

    if result and result != 'no_popup':
        time.sleep(1)
        still_visible = evaluate("""
        (function(){
            var dialog = document.querySelector('.dialog-wrap.greet-pop, .dialog-container');
            if (!dialog) return false;
            var rect = dialog.getBoundingClientRect();
            var style = window.getComputedStyle(dialog);
            return (rect.width > 0 && rect.height > 0 && style.display !== 'none');
        })()
        """, session=session)
        if still_visible == 'true':
            evaluate("""
            (function(){
                var btn = document.querySelector('.btn-sure');
                if (btn) {
                    var rect = btn.getBoundingClientRect();
                    var x = rect.left + rect.width / 2;
                    var y = rect.top + rect.height / 2;
                    ['mousedown', 'mouseup', 'click'].forEach(function(type) {
                        var evt = new MouseEvent(type, {
                            bubbles: true, cancelable: true, view: window,
                            clientX: x, clientY: y
                        });
                        btn.dispatchEvent(evt);
                    });
                    return 'mouse_event_clicked';
                }
                return 'no_btn_sure';
            })()
            """, session=session)
            time.sleep(1)
            return result + ' + retry_mouse'

    return result


# ═══════════════════════════════════════════════════════════
# 进度管理（断点续传）
# ═══════════════════════════════════════════════════════════

class ProgressManager:
    """投递进度管理器 — 支持断点续传

    用法:
        pm = ProgressManager()
        pm.load()
        if pm.is_done(job_id):
            continue
        pm.add_delivered(job_id, job)
        pm.save()
    """

    def __init__(self, filepath="stream_progress.json"):
        self.filepath = filepath
        self.data = {"delivered": [], "failed": [], "blocked": []}

    def load(self):
        assert_workdir(os.path.dirname(os.path.abspath(self.filepath)))
        if os.path.exists(self.filepath):
            with open(self.filepath, 'r', encoding='utf-8') as f:
                self.data = json.load(f)
        ctx = context()
        saved_account = self.data.get('account_context', {}).get('account')
        if ctx and saved_account and saved_account != ctx['account']:
            raise ValueError('投递历史属于另一账户，停止以免混用去重记录')
        # 兼容旧数据：被日上限拦截的岗位曾误记在 failed（reason 含"日上限"），
        # 自愈归位到 blocked —— blocked 不计入 is_done，明日配额重置后可重试
        self.data.setdefault("blocked", [])
        moved = 0
        remaining = []
        for item in self.data.get("failed", []):
            if "日上限" in (item.get("reason") or ""):
                item.setdefault("result", "blocked")
                self.data["blocked"].append(item)
                moved += 1
            else:
                remaining.append(item)
        if moved:
            self.data["failed"] = remaining
        return self

    def save(self):
        assert_workdir(os.path.dirname(os.path.abspath(self.filepath)))
        if metadata():
            self.data['account_context'] = metadata()
        atomic_write_json(self.filepath, self.data)

    @property
    def delivered_ids(self):
        return set(j['jobId'] for j in self.data.get('delivered', []))

    @property
    def failed_ids(self):
        return set(j['jobId'] for j in self.data.get('failed', []))

    @property
    def blocked_ids(self):
        return set(j['jobId'] for j in self.data.get('blocked', []))

    @property
    def all_done_ids(self):
        # blocked(被日上限拦截) 不计入 is_done —— 明日配额重置后应重试，避免丢失有效岗位
        return self.delivered_ids | self.failed_ids | {j.get("jobId") for j in self.data.get("uncertain", [])}

    def is_done(self, job_id):
        return job_id in self.all_done_ids

    def is_blocked(self, job_id):
        return job_id in self.blocked_ids

    def add_delivered(self, job_id, job):
        self.data["uncertain"] = [j for j in self.data.get("uncertain", []) if j.get("jobId") != job_id]
        record = dict(job)
        record.update(jobId=job_id, result="success")
        record.update(metadata())
        rows = self.data.setdefault("delivered", [])
        for previous in rows:
            if previous.get("jobId") == job_id:
                previous.update(record)
                return
        record.setdefault("delivered_at", datetime.now().astimezone().isoformat())
        rows.append(record)

    def add_uncertain(self, job_id, job, reason):
        record = dict(job, jobId=job_id, result="uncertain", reason=reason, verify=False)
        record.update(metadata())
        rows = self.data.setdefault("uncertain", [])
        for previous in rows:
            if previous.get("jobId") == job_id:
                previous.update(record)
                return
        rows.append(record)

    def add_failed(self, job_id, job, reason):
        self.data['failed'].append({
            'jobId': job_id,
            'title': job.get('title', ''),
            'company': job.get('company', ''),
            'result': 'fail',
            'reason': reason,
            **metadata(),
        })

    def add_blocked(self, job_id, job, reason):
        """被日上限拦截（未真正投出）—— 单独分类，区别于真实投递失败。"""
        self.data['blocked'].append({
            'jobId': job_id,
            'title': job.get('title', ''),
            'company': job.get('company', ''),
            'result': 'blocked',
            'reason': reason,
            **metadata(),
        })

    @property
    def delivered_count(self):
        return len(self.data.get('delivered', []))

    @property
    def failed_count(self):
        return len(self.data.get('failed', []))

    @property
    def blocked_count(self):
        return len(self.data.get('blocked', []))


# ═══════════════════════════════════════════════════════════
# 健康检查与 Daemon 管理
# ═══════════════════════════════════════════════════════════

def health_check(host=None, port=None):
    """快速检查 WebBridge daemon 是否可达"""
    try:
        route = _ROUTE.get() or (DEFAULT_HOST, DEFAULT_PORT, SESSION)
        host, port = host or route[0], port or route[1]
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/")
        status = conn.getresponse().status
        conn.close()
        return status == 200 or status == 404
    except Exception:
        return False


def ensure_active_tab(session=SESSION, timeout_wait=10):
    """确保 session 有活跃标签页

    WebBridge active 标志不可靠（navigate 后仍为 False），
    改为：有标签页就尝试 evaluate，失败才 navigate。
    """
    try:
        if not health_check():
            return False

        # 有标签页时直接尝试 evaluate
        r = api("list_tabs", {}, session=session, timeout=5)
        if r.get("ok"):
            tabs_data = r.get("data", {})
            tabs = tabs_data.get("tabs", []) if isinstance(tabs_data, dict) else tabs_data
            if tabs:
                if str(evaluate("1+1", session=session)) == "2":
                    return True

        # 无标签页或不可交互，导航创建
        nav_r = navigate("https://www.zhipin.com", wait_range=(timeout_wait, timeout_wait + 2), session=session)
        if not nav_r.get("ok"):
            return False
        return str(evaluate("1+1", session=session)) == "2"
    except Exception:
        return False


def clean_pid_files():
    """清理 WebBridge / WorkBuddy PID 残留文件"""
    if assert_run():
        raise RuntimeError('账户模式禁止清理公共 PID 文件')
    pid_patterns = [
        os.path.expanduser("~/.kimi-webbridge/daemon.pid"),
        os.path.expanduser("~/.kimi-webbridge*.pid"),
        os.path.expanduser("~/.workbuddy/*.pid"),
    ]
    cleaned = []
    for pattern in pid_patterns:
        for f in glob.glob(pattern):
            if os.path.exists(f):
                try:
                    os.remove(f)
                    cleaned.append(f)
                except Exception:
                    pass
    return cleaned


# ═══════════════════════════════════════════════════════════
# 信号安全中断
# ═══════════════════════════════════════════════════════════

class SafeInterrupt(BaseException):
    pass


def _signal_handler(signum, frame):
    raise SafeInterrupt(f"Received signal {signum}")


def register_signal_guard():
    """注册信号处理器，SIGTERM / SIGINT 触发 SafeInterrupt 以便保存进度。"""
    try:
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError, OSError):
        pass  # 非主线程 / 平台不支持时忽略
    try:
        signal.signal(signal.SIGINT, _signal_handler)
    except (ValueError, AttributeError, OSError):
        pass


# ═══════════════════════════════════════════════════════════
# WebBridge 类
# ═══════════════════════════════════════════════════════════

class WebBridge:
    """Kimi WebBridge 通信封装

    用法:
        wb = WebBridge()
        wb.navigate(url)
        jobs = wb.extract_jobs()
    """

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, session=SESSION, timeout=DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.session = session
        self.timeout = timeout

    def api(self, action, args=None, timeout=None):
        return api(action, args, self.session, self.host, self.port, timeout or self.timeout)

    def health_check(self):
        return health_check(self.host, self.port)

    def evaluate(self, code):
        return self._using(evaluate, code, self.session)

    def _using(self, function, *args):
        token = _ROUTE.set((self.host, self.port, self.session))
        try:
            return function(*args)
        finally:
            _ROUTE.reset(token)

    def navigate(self, url, wait_range=(4, 6)):
        return self._using(navigate, url, wait_range, self.session)

    def click(self, selector):
        return self._using(click, selector, self.session)

    def scroll_page(self):
        return self._using(scroll_page, self.session)

    def snapshot(self):
        return self.api("snapshot", {})

    def screenshot(self, path):
        return self.api("screenshot", {"path": path})

    def list_tabs(self):
        r = self.api("list_tabs", {})
        data = r.get("data", r)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("tabs", data.get("data", []))
        return []

    def close_tab(self, tab_id):
        return self.api("close_tab", {"tabId": tab_id})

    def extract_jobs(self):
        return self._using(extract_jobs, self.session)

    def handle_popup(self):
        return self._using(handle_popup, self.session)

    def ensure_active_tab(self, timeout_wait=10):
        return self._using(ensure_active_tab, self.session, timeout_wait)

    def verify_login(self, username=None):
        """通用登录态检测（v5 泛用化：不再绑定特定用户名）。

        - username 传入具体名字时：兼容旧用法，检测页面是否含该名字（已登录用户）。
        - username 为 None（默认）：检测通用登录态——
            页面不存在登录墙（短信登录/密码登录/扫码登录），
            且存在已登录用户标志（我的/消息 导航 或 用户头像/用户卡片元素）。
        这样任何人（不同姓名）复用技能都无需改代码。
        """
        body = self.evaluate("document.body.innerText") or ""
        if username:
            if username in body:
                return True
            time.sleep(5)
            body = self.evaluate("document.body.innerText") or ""
            return username in body
        # 通用检测：非登录墙 + 存在已登录用户标志
        has_login_wall = ("短信登录" in body) or ("密码登录" in body) or ("扫码登录" in body)
        logged_in = (
            ("我的" in body)
            or ("消息" in body)
            or bool(self.evaluate(
                "!!document.querySelector('.user-avatar, .geek-user, .user-card, .nav-user, .avatar')"))
        )
        if not has_login_wall and logged_in:
            return True
        time.sleep(5)
        body = self.evaluate("document.body.innerText") or ""
        has_login_wall = ("短信登录" in body) or ("密码登录" in body) or ("扫码登录" in body)
        logged_in = ("我的" in body) or ("消息" in body) or bool(
            self.evaluate("!!document.querySelector('.user-avatar, .geek-user, .user-card, .nav-user, .avatar')"))
        return (not has_login_wall) and logged_in

    def body_with_retry(self):
        body = self.evaluate("document.body.innerText") or ""
        if len(body) < 200:
            time.sleep(5)
            body = self.evaluate("document.body.innerText") or ""
        return body

    def find_chat_button_ref(self):
        snap = self.snapshot()
        tree_str = json.dumps(snap, ensure_ascii=False)
        for p in [
            r"'name':\s*'立即沟通'.*?'ref':\s*'(@e\d+)'",
            r"'ref':\s*'(@e\d+)'.*?'name':\s*'立即沟通'",
            r'"name":\s*"立即沟通".*?"ref":\s*"(@e\d+)"',
            r'"ref":\s*"(@e\d+)".*?"name":\s*"立即沟通"',
        ]:
            m = re.search(p, tree_str)
            if m:
                return m.group(1)
        return None


# ═══════════════════════════════════════════════════════════
# 版本信息
# ═══════════════════════════════════════════════════════════

__version__ = "4.0.0"
__updated__ = "2026-07-30"
__changes__ = """
v4.0.0 (2026-07-30):
  - extract_jobs() 增加 DOM 兜底提取（Vue __vue__ 私有 API 失效时自动切换）
  - clean_pid_files() 清理路径改为 ~/.workbuddy/*.pid（迁移自 TRAE ~/.trae-cn）
  - register_signal_guard() 增加 SIGINT 捕获
  - 所有调用脚本改为相对 __file__ 动态解析 SKILL_DIR，摆脱硬编码路径
"""
