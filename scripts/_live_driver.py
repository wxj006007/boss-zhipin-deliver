#!/usr/bin/env python3
"""Live one-by-one delivery driver (corrected flow: walk, never batch).

DESIGN PRINCIPLE (user-corrected 2026-08-01, v6.5):
  - NEVER open/read multiple JDs before acting. Process ONE card at a time.
  - The AGENT does the direction judgment (surface glance + JD read). The
    script only enforces the two unambiguous MECHANICAL hard gates:
    city match and salary >= min_salary_k. Direction (is this really a hotel
    role?) is decided by the agent, never by a hardcoded token list, so we
    never false-skip a real match whose title lacks a keyword (e.g.
    "度假别墅店长").
  - This is the opposite of the old `scan` command, which opened every
    surface-matching card and returned all JDs in one batch (the "batch
    mindset" the user explicitly rejected).

Commands:
  walk                     (NO keyword) navigate to the BOSS RECOMMEND page
                           (https://www.zhipin.com/web/geek/jobs?ka=header-jobs),
                           reset cursor to top, and present ONLY the NEXT ONE
                           card's surface (v6.9 single-card cursor — NEVER a
                           batch list). THIS IS THE DEFAULT STARTING POINT.
                           Judge that ONE card: surface-hit -> open <jid> ->
                           deliver <jid>; surface-miss -> walk --next.
  walk <keyword>           ⛔ HARD GATED: blocked unless the recommend feed has
                           been EXHAUSTED first (recommend_exhausted flag set by
                           walk --more bottom-detection or walk --exhaust). Used
                           ONLY to backfill thin directions AFTER the recommend
                           feed is fully worked (e.g. recommend had no 茶室/营地
                           -> walk 茶室). Presents the NEXT ONE card's surface.
  walk --more              scroll the list for more cards, then present the next
                           ONE card's surface. ALSO detects the feed-bottom marker
                           and auto-sets recommend_exhausted=true (unlocks keyword
                           search) once the LAST job is reached.
  walk --exhaust          explicit: scroll recommend to the LAST job and set
                           recommend_exhausted=true (unlocks keyword search).
                           Detects bottom marker only — does NOT present cards.
                           May take a while; call with a long timeout.
  walk --next              advance cursor, present the NEXT ONE card's surface
                           (no scroll). Auto-skips mech-fail (city/salary) and
                           already-delivered cards. Surface only — agent opens JD.
  walk --reset             clear walk state.
  reject <jid>             record agent's REJECT of one card (mechanical dedup
                           like delivered — auto-skipped on re-present after a
                           re-render, so the agent never re-judges a card it
                           already declined). Call BEFORE walk --next on a miss.
  open <jid>               open ONE card (scroll+click -> right panel switches
                           JD), verify panel, return JD text for agent to judge.
  deliver <jid> [公司] [薪资] [行业] [方向]   deliver ONE card live, record.
                           **On success resets the walk cursor to TOP** (because
                           delivering re-renders/reflows the recommend list) so
                           the next walk --next re-scans strictly top-to-bottom.
                           Agent loop: reject -> `reject <jid>` then walk --next;
                           approve -> deliver -> walk --next (cursor auto-reset).

AGENT STRATEGY (recommend-FIRST, HARD RULE since v6.8, user-mandated):
  1. ALWAYS start with `walk` (no arg): fast glance of the visible recommend
     cards. Work them ONE BY ONE (surface glance -> open JD -> judge ->
     deliver/skip). NEVER batch-collect the whole feed.
  2. Scroll with `walk --more` to reach more recommend cards. When the LAST job
     is reached, --more auto-detects the bottom marker and sets
     recommend_exhausted=true (the v6.8 "scroll to last job" rule) — this UNLOCKS
     `walk <keyword>`. You may also call `walk --exhaust` to force it.
  3. ONLY after recommend_exhausted is true may you run `walk <keyword>` to
     backfill thin directions. The script BLOCKS any `walk <keyword>` while
     recommend_exhausted is false — never jump to keywords as the first move.

Typical agent loop (faithful to the user's screenshot flow):
  walk                     # start on the recommend page, glance left column
  # agent sees 企宣专员(skip), 酒店销售部经理(looks ok) -> open it
  open <jid2>              # read JD of THIS ONE card
  # agent: APPROVE -> deliver
  deliver <jid2> '{...}'
  walk --next              # advance to next mechanically-passing card, read JD
  # agent: REJECT (西餐厅店长, obviously not hotel) -> walk --next again, etc.
  # ... recommend feed thins -> derive keywords -> walk 文旅 / walk 书店 ...
"""
import sys, os, json, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.phase_stream import (
    deliver_click, _panel_shows_job, evaluate, click, SESSION,
    search_url_for, scroll_jobs_list, RECOMMEND_URL, _try_locate_card,
)
from scripts.webbridge_client import (
    ProgressManager, extract_jobs, navigate, parse_salary_max,
)
from scripts.config_v3 import load_profile
from scripts.greeting import deliver_greeting, load_greeting, validate_greeting
from scripts.runtime import resolve_workdir, atomic_write_json
from scripts.account_browser import verify_identity

WORKDIR = resolve_workdir()
STATE_FILE = os.path.join(WORKDIR, "walk_state.json")


def load_profile_cached():
    try:
        return load_profile()
    except Exception:
        from scripts.accounts import context
        if context():
            raise
        return {}


def profile_city(profile):
    return (profile or {}).get("target_city") or (profile or {}).get("city") or ""


def profile_min_salary(profile):
    return float((profile or {}).get("min_salary_k", 0) or 0)


def mechanical_ok(job, profile):
    """Only the two unambiguous hard gates: city + salary.

    Direction (is this a real hotel/tourism role?) is intentionally NOT decided
    here — that is the agent's job, done after reading the JD. Keeping this
    loose avoids false-skipping genuine matches that lack a keyword in the title.

    City gate is lenient on the recommend feed: BOSS recommend cards sometimes
    omit the city string; only fail when BOTH the target city and the card's
    city are present and mismatch. Empty card city -> leave it to the agent.
    """
    city = profile_city(profile)
    jc = job.get("city", "") or ""
    if city and jc and city not in jc:
        return {"ok": False, "reason": f"city '{jc}' != '{city}'"}
    sal = parse_salary_max(job.get("salary", ""))
    mins = profile_min_salary(profile)
    if mins and sal > 0 and sal < mins:
        return {"ok": False, "reason": f"salary {sal}K < {mins}K"}
    return {"ok": True, "reason": ""}


def walk_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    atomic_write_json(STATE_FILE, s)


# ───────────────────────────────────────────────────────────
# v6.8 硬规则：推荐页必须滚动到最后一个岗位（彻底穷尽）后，才允许关键词搜索
# ───────────────────────────────────────────────────────────
_RECOMMEND_END_MARKERS = ["没有更多了", "已经到底", "到底啦", "没有更多职位", "没有职位了"]


def _recommend_end_marker():
    """检测推荐页是否已滚动到底（出现"没有更多了 / 已经到底"等底部文案）。"""
    try:
        txt = evaluate("""(function(){
            var n = document.querySelector('#main .job-list') || document.querySelector('.job-list');
            var scope = (n && n.innerText) ? n.innerText : (document.body ? document.body.innerText : '');
            return scope || '';
        })()""") or ""
    except Exception:
        return None
    for m in _RECOMMEND_END_MARKERS:
        if m in txt:
            return m
    return None


def _exhaust_recommend_feed(cap=80):
    """反复滚动直到底部，返回 (exhausted, total, marker)。

    适用于「推荐页」与「关键词搜索页」两类会懒加载的列表（用通用底部文案
    检测，对两者都有效）。

    懒加载稳健性修正（2026-08-02，用户实测质疑）：
      ❌ 旧逻辑：先 extract_jobs 计数 → 再滚 1 次 → 仅等 1.4s → 连续 2 次
         计数相等就判"稳定触底"。BOSS 一批加载若耗时 >1.4s，会在加载途中
         被误判为"到底"而提前收尾，导致只翻到几十张就以为到底（实测一般
         搜索页能懒加载到 450+ 张）。
      ✅ 新逻辑：每次**先滚动（2 次）触发加载 → 等 3s 让批次渲染完成 → 再
         计数**；连续 **4 次**计数稳定（每次间隔约 5s）才判触底；并优先以
         底部文案（没有更多了/已经到底…）确认。cap 提到 80 防卡死。
    """
    prev = -1
    stuck = 0
    for _ in range(cap):
        scroll_jobs_list(times=2)   # 先滚动触发懒加载
        time.sleep(3.0)             # 长等待：给 BOSS 一批网络+渲染留足时间
        jobs = extract_jobs()
        cnt = len(jobs)
        marker = _recommend_end_marker()
        if marker:
            return True, cnt, marker
        if cnt == prev:
            stuck += 1
            if stuck >= 4:
                return True, cnt, "stable_no_growth"
        else:
            stuck = 0
            prev = cnt
    return True, (prev if prev >= 0 else len(extract_jobs())), "cap_reached"


def _delivered_jids():
    """已投递 jobId 集合（机械去重，防重复呈现/重复投递）。纯机械、非方向判断。"""
    try:
        with open(os.path.join(WORKDIR, "stream_progress.json"), "r", encoding="utf-8") as f:
            d = json.load(f)
        return set(r.get("jobId") for r in d.get("delivered", []) if r.get("jobId"))
    except Exception:
        return set()


def _present_next():
    """v6.9 核心：一次只呈现"下一张卡"的表面（标题/薪资/城市），绝不整页批量 dump。

    设计动机（用户 2026-08-02 强制）：旧 walk 一次性 extract 出 ~15 张卡整列表返回，
    智能体"攥着整页清单"就跳序挑投（i4→i3→i11）。现改为游标制——智能体任何时刻
    只收到"当前这一张"，从机制上杜绝批量视野与跳序。

    游标推进：last_jid 锚定优先（在"当前 DOM 列表"里定位上次呈现卡的位置再 +1，
    对 BOSS 列表动态重排鲁棒），锚不到退回数字游标 idx。
    机械跳过两类卡（不交给智能体做方向判断）：
      ① 城市/薪资硬门槛不过关（mechanical_ok=False）
      ② 已投递（stream_progress 去重）
    智能体只见这一张：表面命中 → open <jobId> 读JD→判→deliver；不命中 → walk --next 跳过。
    """
    profile = load_profile_cached()
    st = walk_state()
    kw = st.get("keyword", "")
    jt = st.get("job_type", "") or ""
    idx = int(st.get("idx", 0) or 0)
    last_jid = st.get("last_jid", "")
    # 关键词页：若漂离搜索页则回跳（推荐页 kw 为空则无需）
    if kw:
        cur = evaluate("window.location.href") or ""
        if "query=" not in (cur or ""):
            try:
                navigate(search_url_for(kw, profile.get("city_code") or "", jt),
                         wait_range=(5, 7), expect_url_contains="query=",
                         label=f"回到-{kw}")
            except Exception:
                pass
        elif jt and f"jobType={jt}" not in (cur or ""):
            # URL 还在搜索页但丢了筛选（如页面自身刷新回退）→ 补齐 jobType
            try:
                navigate(search_url_for(kw, profile.get("city_code") or "", jt),
                         wait_range=(5, 7), expect_url_contains=f"jobType={jt}",
                         label=f"回补筛选-{kw}")
            except Exception:
                pass
    jobs = extract_jobs()
    if not jobs:
        # 页面可能刚导航完未就绪：回推荐页重试一次，避免静默空输出
        try:
            navigate(RECOMMEND_URL, wait_range=(5, 7),
                     expect_url_contains="header-jobs", label="推荐页重试")
        except Exception:
            pass
        jobs = extract_jobs()
    # 触底检测（v6.8 / 2026-08-02 加固）：推荐页(kw为空)与关键词搜索页都检测
    # 底部文案。推荐页命中→置 recommend_exhausted 解锁关键词搜索；关键词页命中
    # →置 kw_exhausted，给智能体一个"该关键词已翻完"的干净信号（否则只靠
    # view_exhausted 反复 --more 会无限空转）。
    marker = _recommend_end_marker()
    if marker:
        if not kw:
            st["recommend_exhausted"] = True
        else:
            st["kw_exhausted"] = True
        st["recommend_end_marker"] = marker
        save_state(st)
    # 续走起点：last_jid 锚定优先
    start = idx
    if last_jid:
        for p, j in enumerate(jobs):
            if j.get("jobId") == last_jid:
                start = p + 1
                break
    delivered = _delivered_jids()
    rejected = set(st.get("rejected_jids", []))
    skipped = []
    i = start
    while i < len(jobs):
        job = jobs[i]
        jid = job.get("jobId")
        if jid in delivered:
            skipped.append({"i": i, "reason": "已投递(去重)"})
            i += 1
            continue
        if jid in rejected:
            skipped.append({"i": i, "reason": "已判否-跳过"})
            i += 1
            continue
        m = mechanical_ok(job, profile)
        if not m["ok"]:
            skipped.append({"i": i, "reason": m["reason"]})
            i += 1
            continue
        # 呈现这一张（仅这一张）→ 推进游标
        st["idx"] = i + 1
        st["last_jid"] = jid
        save_state(st)
        print(json.dumps({
            "present": i,
            "job": {"jobId": jid, "title": job.get("title"),
                    "company": job.get("company", ""),
                    "salary": job.get("salary"), "city": job.get("city"),
                    "industry": job.get("industry"),
                    "scale": job.get("scale", ""), "stage": job.get("stage", "")},
            "mech": m,
            "auto_skipped_mech": skipped,
            "visible_total": len(jobs),
            "recommend_exhausted": st.get("recommend_exhausted", False),
            "hint": "只判这一张：表面命中→ open <jobId> 读JD→判→deliver；不命中→ walk --next 跳过。勿重复 walk(会回顶部)。"
        }, ensure_ascii=False))
        return
    # 当前可见卡已过完
    st["idx"] = i
    save_state(st)
    print(json.dumps({
        "view_exhausted": True, "keyword": kw, "idx": i,
        "auto_skipped_mech": skipped,
        "recommend_exhausted": st.get("recommend_exhausted", False),
        "kw_exhausted": st.get("kw_exhausted", False),
        "hint": ("该关键词已翻到底(kw_exhausted)，可换下一个 walk <关键词>" if kw and st.get("kw_exhausted")
                 else "当前可见卡已过完：walk --more 滚动懒加载更多（关键词页也适用）；或换 walk <关键词>")
    }, ensure_ascii=False))


def cmd_walk(keyword, job_type="", force=False):
    profile = load_profile_cached()
    if keyword:
        # ⛔ 硬规则（v6.8）：必须先穷尽推荐页（walk 无参 → 滚动到最后一个岗位，
        # 且 state.recommend_exhausted==True），才允许关键词搜索。否则直接拒绝，
        # 不导航、不产出 surface，强制回到"推荐页优先"流程。
        # v6.10：用户可显式 force=True 越过该硬规则（例如推荐页全是社招、完全
        # 不匹配，用户直接指定"后端开发 + 求职类型=实习"入口）。
        st = walk_state()
        if not st.get("recommend_exhausted") and not force:
            print(json.dumps({
                "blocked": True,
                "rule": "recommend-first-hard",
                "reason": "推荐页尚未穷尽，禁止关键词搜索。请先用 `walk`（无参）停在 BOSS 推荐页 "
                          "并滚动到最后一个岗位（自动标记 recommend_exhausted=true），"
                          "逐条判读/投递推荐页岗位后，才允许 `walk <关键词>` 补充稀缺方向。"
                          "（若推荐页内容整体不匹配、需直接走关键词入口，可加 --force 显式越过）",
                "hint": "运行: python scripts/_live_driver.py walk   （无参）",
            }, ensure_ascii=False))
            return None
        # 已穷尽（或用户 --force）→ 放行关键词搜索（保留 recommend_exhausted 状态）
        jt = job_type or st.get("job_type", "") or ""
        url = search_url_for(keyword, profile.get("city_code") or "", jt)
        try:
            navigate(url, wait_range=(6, 8), expect_url_contains="query=",
                     label=f"搜索-{keyword}")
        except Exception as e:
            print("NAV_WARN", repr(e)[:200])
        st["keyword"] = keyword
        st["job_type"] = jt
        st["idx"] = 0
        st["last_jid"] = ""
        save_state(st)
    else:
        # 推荐页优先（默认起点）：无关键词 → 停在 BOSS 推荐页，
        # 仅打印"当前可见"表面清单供智能体扫一眼（fast glance）。
        # ✅ 绝不在这里滚动到底批量采集（v6.8 反模式，已删除）。
        # 逐条判读/投递由智能体 open/deliver 完成；滚动到末尾并标记穷尽
        # 由 walk --more（自动触底检测）或 walk --exhaust（显式）负责。
        cur = (evaluate("window.location.href") or "")
        if "header-jobs" not in (cur or ""):
            try:
                navigate(RECOMMEND_URL, wait_range=(6, 8),
                         expect_url_contains="header-jobs", label="推荐页")
            except Exception as e:
                print("NAV_WARN", repr(e)[:200])
        st = walk_state()
        st["keyword"] = ""
        st["idx"] = 0
        st["last_jid"] = ""
        # 保留 recommend_exhausted（由 --more/--exhaust 置位），不在此重置
        save_state(st)
    # v6.9：不再批量打印整页 surface 列表；只呈现"下一张卡"（一次一张，游标制）。
    _present_next()
    return None


def cmd_open(jid):
    import re
    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(jid or "")):
        raise ValueError("无效岗位ID")
    sel = f'a[href*="job_detail/{jid}"]'
    evaluate(f"""(function(){{var el=document.querySelector('a[href*="job_detail/{jid}"]');if(el)el.scrollIntoView({{block:'center'}});return !!el;}})()""")
    time.sleep(1.0)
    ok = click(sel)
    time.sleep(2.0)
    panel_ok = _panel_shows_job(jid, "")
    located = "ok" if panel_ok else _try_locate_card(jid, "", session=SESSION)
    if not panel_ok:
        panel_ok = located == "ok" and _panel_shows_job(jid, "")
        ok = panel_ok
    if not panel_ok:
        return {"clicked": ok, "panel_ok": False, "detail": "", "title": "",
                "not_found": located == "absent", "location_status": located}
    detail = evaluate("""(function(){var d=document.querySelector('.job-detail-box')||document.querySelector('#main .job-detail');return d?d.innerText:'';})()""")
    title = evaluate("""(()=>{const d=document.querySelector('.job-detail-box')||document.querySelector('#main .job-detail');return d?.querySelector('.job-name')?.innerText.trim()||''})()""")
    return {"clicked": ok, "panel_ok": panel_ok, "detail": detail if panel_ok else "", "title": title if panel_ok else ""}


def cmd_deliver(jid, company="", salary="", industry="", direction="", greeting=None):
    verify_identity()
    """Deliver ONE live card by jid. Job metadata (company/salary/industry/
    direction) is taken from simple positional args (shell-safe, no JSON).

    SAFETY: re-select + verify the panel is actually showing THIS card BEFORE
    clicking 立即沟通 (eliminates the "clicked A but panel showed B" race), then
    scrape the title AFTER the panel is confirmed on this job.

    Usage: deliver <jid> [company] [salary] [industry] [direction]
    direction = 高层投递方向标签（活动策划/文旅运营/HR招聘…），用于收尾汇报页按方向分组
    """
    if greeting is not None:
        greeting = validate_greeting(greeting)
    pm = ProgressManager(os.path.join(WORKDIR, "stream_progress.json")).load()
    if pm.is_done(jid):
        return {"delivered_ok": False, "already_done": True, "reason": "已处理或待核对，避免重复发送"}
    if walk_state().get("delivery_halted"):
        return {"delivered_ok": False, "stopped": True, "reason": "上轮异常已停止；核对后用 walk --reset 恢复"}
    op = cmd_open(jid)  # scroll+click the card, verify panel is on this job
    if not op.get("panel_ok"):
        if op.get("not_found"):
            return {"delivered_ok": False, "not_found": True, "detail": op,
                    "reason": "目标卡片不在当前列表，跳过该岗位"}
        if greeting is not None:
            st = walk_state()
            st["consecutive_failures"] = st.get("consecutive_failures", 0) + 1
            stopped = st["consecutive_failures"] >= 3
            if stopped:
                st["delivery_halted"] = True
            save_state(st)
            return {"delivered_ok": False, "stopped": stopped, "detail": op,
                    "reason": "目标岗位面板未确认，不发送自定义招呼语"}
        # 默认招呼保留 WorkBuddy 的兜底：由 deliver_click 重新定位并核验。
    if greeting is not None and not (op.get("detail") or "").strip():
        return {"delivered_ok": False, "reason": "未读取到当前岗位JD，不发送自定义招呼语"}
    job = {"jobId": jid, "title": op.get("title", ""), "company": company,
           "salary": salary, "industry": industry, "direction": direction, "city": ""}
    if greeting is not None:
        job["greeting"] = greeting
        # Persist before initiating communication, including across process crashes.
        pm.add_uncertain(jid, job, "自定义招呼正在发送；若中断需核对后恢复")
        pm.save()
    try:
        res = (deliver_greeting(job, greeting, session=SESSION) if greeting is not None
               else deliver_click(job, session=SESSION, auto=True))
    except Exception as error:
        pm.add_uncertain(jid, job, f"执行异常，需核对是否已发送：{error}")
        pm.save()
        st = walk_state()
        st["delivery_halted"] = True
        save_state(st)
        raise
    if greeting is not None and not res.get("uncertain"):
        pm.data["uncertain"] = [x for x in pm.data.get("uncertain", []) if x.get("jobId") != jid]
        # Success is committed with add_delivered below; do not clear the crash
        # protection on disk before that write.
        if not res.get("delivered_ok"):
            pm.save()
    if res.get("uncertain"):
        pm.add_uncertain(jid, job, res.get("reason", "送达未确认，需核对"))
        pm.save()
    if res.get("stopped") or res.get("daily_limit") or res.get("wrong_path"):
        st = walk_state()
        st["delivery_halted"] = True
        save_state(st)
    if res.get("delivered_ok"):
        # scrape title AFTER the panel is confirmed on this job.
        # IMPORTANT: scope to the right-hand panel (.job-detail-box) — an
        # unscoped '.job-name' would match the LEFT results-list's first card
        # and record the wrong title (the "clicked A but logged B" trap).
        try:
            t = evaluate(
                "(function(){var b=document.querySelector('.job-detail-box')||"
                "document.querySelector('#main .job-detail');"
                "var e=b?b.querySelector('.job-name'):null;"
                "return e?e.innerText.trim():'';})()") or ""
            if greeting is None and t:
                job["title"] = t
        except Exception:
            pass
        pm = ProgressManager(os.path.join(WORKDIR, "stream_progress.json")).load()
        job["verify"] = res.get("verify") is True
        job["verification_source"] = res.get("verification_source", "")
        if res.get("message_id"):
            job["message_id"] = res["message_id"]
        pm.add_delivered(job.get("jobId"), job)
        pm.save()
        # v6.9.1 修（顺序错乱根因）：投递会触发"立即沟通"弹窗→可能 wrong_path 导航回推荐页；
        # 即使正常路径，BOSS 也会把已投卡片从推荐列表移除并回流重排。若不清游标，下次
        # walk --next 会拿陈旧 idx/last_jid 续跑，漏掉重排后前移的未审卡片（即"跳过第N张跳到后面"）。
        # 故投递成功后强制游标回顶（idx=0, last_jid=""），rejected_jids/recommend_exhausted 保留。
        # 下次 walk --next 从顶部重扫，已投递(去重)+已判否(跳过)自动过滤，只呈现"下一张未审卡片"。
        st = walk_state()
        st["idx"] = 0
        st["last_jid"] = ""
        save_state(st)
    if not res.get("delivered_ok") and not res.get("already_done") and not res.get("not_found"):
        # v6.11.2: not_found（卡片真不在当前页）按 skill 语义是"跳过"，不计入连续失败熔断；
        # 熔断只针对真正的异常（面板错位/误点/弹窗异常）。
        st = walk_state()
        st["consecutive_failures"] = st.get("consecutive_failures", 0) + 1
        if st["consecutive_failures"] >= 3:
            st["delivery_halted"] = True
            res["stopped"] = True
        save_state(st)
    elif res.get("delivered_ok"):
        st = walk_state()
        st["consecutive_failures"] = 0
        save_state(st)
    return res


def cmd_next():
    """v6.9：推进游标并只呈现下一张卡的"表面"（不自动开 JD）。
    表面是否命中由智能体判断；命中再显式 `open <jobId>` 读 JD。"""
    _present_next()


def cmd_reject(jid):
    """记录智能体判定为"不命中"的卡片 jid（纯机械去重用，非方向判断）。

    与已投递去重同理：投递成功会触发重渲染/列表重排，该卡再次进入视野时
    由 _present_next 自动跳过，避免重复呈现与重复判读。下次 walk --next 从顶部
    重扫时跳过，保证严格从上到下且不重复审。"""
    st = walk_state()
    rj = set(st.get("rejected_jids", []))
    rj.add(jid)
    st["rejected_jids"] = list(rj)
    save_state(st)
    print(json.dumps({"rejected": jid, "rejected_count": len(rj)}, ensure_ascii=False))


def cmd_more():
    """滚动列表加载更多（懒加载），然后呈现下一张卡。

    适配「推荐页」与「关键词搜索页」：每次滚 3 次并等待，并先打印一行
    LAZY 进度（grew=本次是否真加载到新卡），便于智能体判断还需继续
    --more 还是已到底；随后由 cmd_next 正常呈现下一张卡（其 JSON 另起一行）。
    """
    before = len(extract_jobs())
    scroll_jobs_list(times=3)
    time.sleep(1.5)
    after = len(extract_jobs())
    grew = after > before
    print("LAZY " + json.dumps({"grew": grew, "before": before,
                                 "after": after}, ensure_ascii=False))
    return cmd_next()


def cmd_exhaust():
    """v6.8 解锁命令：显式把推荐页滚动到最后一个岗位，标记 recommend_exhausted=true。

    ✅ 只探测底部、设置标志 —— 不采集 / 不打印任何岗位列表（绝不批量采集）。
    滚动可能耗时（最多 cap 次），调用方应使用较长超时（如 240000ms）。
    """
    exhausted, total, marker = _exhaust_recommend_feed()
    st = walk_state()
    st["recommend_exhausted"] = bool(exhausted)
    st["recommend_total"] = total
    st["recommend_end_marker"] = marker
    save_state(st)
    print(json.dumps({"exhausted": bool(exhausted), "marker": marker,
                      "total_visible": total,
                      "recommend_exhausted": bool(exhausted),
                      "note": "推荐页已滚到底，关键词搜索已解锁（walk <关键词>）"},
                     ensure_ascii=False))
    return bool(exhausted)


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "walk"
    if action == "open":
        jid = sys.argv[2]
        print(json.dumps(cmd_open(jid), ensure_ascii=False))
    elif action == "deliver":
        import argparse
        parser = argparse.ArgumentParser(description="投递一个岗位，可发送智能体生成的专属招呼语")
        parser.add_argument("jid")
        parser.add_argument("metadata", nargs="*", help="公司 薪资 行业 方向（可选）")
        parser.add_argument("--greeting-file", help="UTF-8 JSON：jobId + greeting")
        args = parser.parse_args(sys.argv[2:])
        if len(args.metadata) > 4:
            parser.error("最多四项岗位元数据")
        metadata = args.metadata + [""] * (4 - len(args.metadata))
        message = load_greeting(args.greeting_file, args.jid) if args.greeting_file else None
        print(json.dumps(cmd_deliver(args.jid, *metadata, greeting=message),
                         ensure_ascii=False))
    elif action == "reject":
        jid = sys.argv[2]
        cmd_reject(jid)
    elif action in ("next", "--next"):
        cmd_next()
    elif action in ("more", "--more"):
        cmd_more()
    elif action in ("reset", "--reset"):
        save_state({})
        print("WALK_STATE_RESET")
    elif action == "walk":
        args = sys.argv[2:]
        if args and args[0] in (
                "--next", "next", "--more", "more", "--exhaust", "exhaust",
                "--reset", "reset"):
            sub = args[0]
            if sub in ("--next", "next"):
                cmd_next()
            elif sub in ("--more", "more"):
                cmd_more()
            elif sub in ("--exhaust", "exhaust"):
                cmd_exhaust()
            else:
                save_state({})
                print("WALK_STATE_RESET")
        else:
            # 支持: walk <kw> [--job-type=1902|实习] [--force]
            kw = ""
            job_type = ""
            force = False
            for a in args:
                if a in ("--force", "force"):
                    force = True
                elif a.startswith("--job-type=") or a.startswith("--jobtype="):
                    job_type = a.split("=", 1)[1].strip()
                elif a in ("--intern", "intern"):
                    job_type = "1902"
                elif not kw:
                    kw = a
            # 中文别名 → 筛选码
            jt_alias = {"实习": "1902", "全职": "1901", "兼职": "1903", "不限": ""}
            job_type = jt_alias.get(job_type, job_type)
            cmd_walk(kw, job_type=job_type, force=force)
    else:
        # any other token is treated as a keyword
        cmd_walk(action)
