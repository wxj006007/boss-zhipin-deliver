"""完整核对 BOSS 真实沟通列表与本地投递记录（只读）。

为什么需要它：`phase_stream --mode verify` 只读「当前可见」的会话行（首屏约
31–40 条），站点会话列表是**虚拟滚动**的，行数不随 scrollTop 增长，因此它的
「未匹配」既不等于投递失败，也不能作为完整核对依据。

本脚本的做法：
  1. 进入 /web/geek/chat；
  2. 用**派发 scroll 事件 + 逐步滚动**驱动虚拟列表（仅设 scrollTop 不触发加载，
     必须同时 dispatchEvent('scroll')）；
  3. 每步收集可见会话行，按「HR 姓名 + 公司 + 角色」身份去重，连续多步无新增即停；
  4. 与本地 `stream_progress.json` 的 delivered 逐条比对：
     - A 会话存在且招呼语是最后一条消息（含固定开头标记）
     - B 会话存在（HR 已回复，招呼语被后续消息覆盖）
     - C 会话列表中找不到对应公司
  只读，不改动任何投递记录；结束时回到推荐页。

用法（账户模式）:
  python scripts/run_account.py --config <账户配置> --run <run> verify-full
"""
import argparse
import json
import os
import re
import time

from scripts import webbridge_client as wb

CHAT_URL = "https://www.zhipin.com/web/geek/chat"
RECOMMEND_URL = "https://www.zhipin.com/web/geek/jobs?ka=header-jobs"
# 本技能招呼语的固定开头标记，用于判断会话里是否有我们发出的消息。
GREETING_MARKERS = ("learn-claude-code", "桃酥不是桃")

READ = r"""
(() => {
  const box = document.querySelector('.user-list-content');
  const lis = box ? [...box.querySelectorAll('li')] : [];
  const rows = lis.map(li => {
    const te = li.querySelector('.time');
    return {time: te ? (te.innerText || '').trim() : '',
            text: (li.innerText || '').replace(/\u00a0/g, ' ').split('\n')
                     .filter(Boolean).join(' | ').slice(0, 240)};
  }).filter(r => r.text);
  return JSON.stringify({rows, sh: box ? box.scrollHeight : 0,
                         st: box ? box.scrollTop : 0, ch: box ? box.clientHeight : 0});
})()
"""

STEP = r"""
(() => {
  const box = document.querySelector('.user-list-content');
  if (!box) return JSON.stringify({ok: false});
  box.scrollTop = box.scrollTop + box.clientHeight * 3;
  box.dispatchEvent(new Event('scroll', {bubbles: true}));
  return JSON.stringify({ok: true, st: box.scrollTop, sh: box.scrollHeight});
})()
"""


def identity(row_text):
    """会话身份 = 姓名 + 公司 + 角色；对消息预览变化鲁棒。"""
    parts = [p.strip() for p in row_text.split('|')]
    return parts[1] if len(parts) > 1 else row_text[:60]


def unique_window(text, others, width=12):
    """最早出现、且在 others 里都不存在的 width 字窗口 —— 该记录的唯一指纹。

    本技能招呼语共享同一段固定开头，用开头匹配会把所有记录指向同一个会话；
    必须取「只在本文本里出现」的片段，才能精确对应到具体会话。
    """
    for k in range(0, max(0, len(text) - width) + 1):
        window = text[k:k + width]
        if window and all(window not in other for other in others):
            return window
    return None


def enumerate_conversations(max_steps=150, stagnant_limit=12, verbose=True):
    wb.navigate(CHAT_URL, wait_range=(4, 6), expect_url_contains="chat", label="沟通列表")
    time.sleep(2.5)
    seen = {}
    stagnant = 0
    window = 0
    for i in range(max_steps):
        payload = json.loads(wb.evaluate(READ) or '{"rows": []}')
        window = payload.get("sh") or window
        new = 0
        for row in payload.get("rows") or []:
            key = row["time"] + "|" + identity(row["text"])
            if key not in seen:
                seen[key] = row
                new += 1
        stagnant = 0 if new else stagnant + 1
        if verbose and (new or i % 10 == 0):
            print(f"  step {i + 1}: window_h={window} new={new} total={len(seen)}", flush=True)
        if stagnant >= stagnant_limit:
            break
        wb.evaluate(STEP)
        time.sleep(1.4)
    return list(seen.values())


def audit(workdir, verbose=True):
    rows = enumerate_conversations(verbose=verbose)
    identities = {identity(r["text"]): r["text"] for r in rows}
    norm = lambda s: re.sub(r"[\s\u00a0]+", "", s or "")  # noqa: E731
    rows_n = [(identity(r["text"]), norm(r["text"])) for r in rows]
    blob_n = "\n".join(t for _, t in rows_n)

    path = os.path.join(workdir, "stream_progress.json")
    delivered = json.load(open(path, encoding="utf-8")).get("delivered", []) if os.path.exists(path) else []
    greetings = [norm(d.get("greeting")) for d in delivered]

    # 精确指纹：每条记录的招呼语互不相同，取只属于它的片段去定位会话，
    # 避免用公司名匹配（同公司会话多时会误判，也曾因此把多条记录指向同一会话）。
    precise, company_only, missing = [], [], []
    conv_seen = {}
    for i, d in enumerate(delivered):
        text = greetings[i] or ""
        others = [greetings[j] for j in range(len(greetings)) if j != i]
        window = unique_window(text, others) if text else None
        hit = next((idd for idd, txt in rows_n if window and window in txt), None)
        core = norm(d.get("company") or "").replace("...", "")
        has_company = bool(core) and core in blob_n
        if hit:
            precise.append(d)
            conv_seen.setdefault(hit, []).append(d)
        elif has_company:
            # 招呼语已不是最后一条消息（HR 已回复）或预览被截断，需人工确认。
            company_only.append(d)
        else:
            missing.append(d)

    duplicate_conversations = {k: [x.get("title") for x in v]
                               for k, v in conv_seen.items() if len(v) > 1}

    result = {
        "conversations_enumerated": len(identities),
        "conversations_with_our_greeting": sum(
            1 for t in identities.values() if any(m in t for m in GREETING_MARKERS)),
        "local_delivered": len(delivered),
        "A_greeting_is_latest": len(precise),
        "B_exists_but_replied": len(company_only),
        "C_not_found_in_chat_list": len(missing),
        "distinct_conversations_matched": len(conv_seen),
        "same_conversation_records": duplicate_conversations,
        "B_detail": [{"company": d.get("company"), "title": d.get("title"),
                      "delivered_at": d.get("delivered_at")} for d in company_only],
        "C_detail": [{"company": d.get("company"), "title": d.get("title"),
                      "delivered_at": d.get("delivered_at"), "message_id": d.get("message_id")}
                     for d in missing],
    }
    return result, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    try:
        result, rows = audit(args.workdir, verbose=not args.quiet)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        out = os.path.join(args.workdir, "_chat_audit.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"result": result, "rows": rows}, f, ensure_ascii=False, indent=2)
        print(f"[chat_audit] 明细已写入 {out}")
    finally:
        try:
            wb.navigate(RECOMMEND_URL, wait_range=(3, 5),
                        expect_url_contains="header-jobs", label="回推荐页")
        except Exception as error:
            print(f"[chat_audit] 回推荐页失败：{error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
