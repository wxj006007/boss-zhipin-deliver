"""同页扫描：一次导航后采集当前卡片，可逐条读取 JD，交智能体审核。

为什么需要它：BOSS 搜索结果是动态轮换的。`pipeline_batch.py jdc` 会为每个
来源关键词重新 navigate 一次，重新加载后卡片集已经变了，导致对老候选池的
命中率只有约 1/3（大量 panel_ok=False）。本脚本把流程改成「一次导航 → 同页读完
→ 同页原地投递」：导航后立刻 `extract_jobs()`（并按需滚动加载更多），把该页
**当前真实存在**的卡片落盘；随后用 `_live_driver.py open <jid>` /
`deliver <jid>` 在原页面上就地读写，不再重新加载，卡片不会中途消失。

用法:
  python page_scan.py <keyword> [scale] [job_type] [scrolls] [jd]

  环境变量: BOSS_WORKDIR / BOSS_CITY / BOSS_SCALE / BOSS_JOBTYPE / BOSS_SCAN_SCROLLS

  第 5 参传 `jd` 时：在同一页逐条读取未成功缓存的 JD，不按职业或届别关键词筛选。
  页面筛选采用命令参数 / 环境变量；适合与否由智能体结合用户画像判断。
  审核后用 pipeline_batch.py go decisions.json --current-page，避免重新导航。

输出:
  stdout 一行 JSON 摘要 + 每行 `jid|公司|规模|城市|标题|薪资|学历`
  <workdir>/scan_<keyword>.json  该页中大厂卡片（已剔除已投递）
  <workdir>/jd_<keyword>.json    同页点开的 JD 正文（仅 `jd` 模式）
"""
import sys, os, json, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from urllib.parse import quote
from scripts import webbridge_client as wb
from scripts.config_v3 import load_profile
from scripts.runtime import resolve_workdir, atomic_write_json
from scripts.hr_activity import display as display_activity

def valid_jd(entry):
    return (isinstance(entry, dict) and entry.get("panel_ok") is True
            and bool(str(entry.get("jd") or "").strip()) and not entry.get("error"))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    kw = sys.argv[1]
    scale = sys.argv[2] if len(sys.argv) > 2 else os.environ.get("BOSS_SCALE", "304,305,306")
    job_type = sys.argv[3] if len(sys.argv) > 3 else os.environ.get("BOSS_JOBTYPE", "1902")
    scrolls = int(sys.argv[4]) if len(sys.argv) > 4 else int(os.environ.get("BOSS_SCAN_SCROLLS", "3"))
    workdir = resolve_workdir()
    page = os.environ.get("BOSS_SCAN_PAGE", "").strip()
    city = os.environ.get("BOSS_CITY") or load_profile().get("city_code") or "100010000"
    url = (f"https://www.zhipin.com/web/geek/jobs?query={quote(kw)}"
           f"&city={city}&jobType={job_type}&scale={scale}")
    if page:
        url += f"&page={page}"

    wb.navigate(url, wait_range=(5, 7), expect_url_contains="query=", label=f"搜索-{kw}")
    time.sleep(1.5)
    cur = wb.evaluate("window.location.href") or ""
    anomaly, reason = wb.detect_page_anomaly(check_blank=False)
    if anomaly or "security" in cur:
        print(json.dumps({"ok": False, "kw": kw,
                          "reason": reason or "安全验证页；停止待人工检查"}, ensure_ascii=False))
        return 1

    seen = {}
    loading = None
    for i in range(max(0, scrolls) + 1):
        for j in wb.extract_jobs() or []:
            if j.get("jobId"):
                seen[j["jobId"]] = j
        if i < scrolls:
            from scripts.phase_stream import scroll_jobs_list
            loading = scroll_jobs_list(times=1)
            if loading['status'] in ('loading', 'stalled'):
                break
            if loading['confirmed_end']:
                # EOF may arrive together with the last page of cards.
                for j in wb.extract_jobs() or []:
                    if j.get('jobId'):
                        seen[j['jobId']] = j
                break

    pp = os.path.join(workdir, "stream_progress.json")
    done = set()
    if os.path.exists(pp):
        prog = json.load(open(pp, encoding="utf-8"))
        done = {x.get("jobId") for x in prog.get("delivered", [])}
        done |= {x.get("jobId") for x in prog.get("uncertain", [])}

    rows = []
    for j in seen.values():
        j["_kw"] = kw
        if j["jobId"] in done:
            continue
        rows.append(j)
    rows.sort(key=lambda x: (x.get("company") or ""))
    atomic_write_json(os.path.join(workdir, f"scan_{kw.replace(' ', '_')}.json"), rows)

    # 并入候选池，供 pipeline_batch.py go/ls 使用（_kw 记为本页关键词，
    # 保证批量投递时回到「刚刚确实看到过这张卡」的那个关键词页）。
    pool_file = os.path.join(workdir, "pool.json")
    pool = {}
    if os.path.exists(pool_file):
        for j in json.load(open(pool_file, encoding="utf-8")).get("jobs", []):
            pool[j["jobId"]] = j
    for j in rows:
        pool[j["jobId"]] = j
    atomic_write_json(pool_file, {"scale": scale, "job_type": job_type, "jobs": list(pool.values())})

    print(json.dumps({"ok": True, "kw": kw, "page_url": cur,
                      "cards_total": len(seen), "available": len(rows),
                      "loading": loading}, ensure_ascii=False))
    for j in rows:
        print(f"{j['jobId']}|{j.get('company')}|{j.get('scale')}|{j.get('city')}|"
              f"{j.get('title')}|{j.get('salary')}|{j.get('degree')}|HR:{display_activity(j.get('hr_activity'))}")

    if len(sys.argv) > 5 and sys.argv[5] == "jd":
        from scripts import _live_driver as drv
        import glob
        already = {}
        for f in glob.glob(os.path.join(workdir, "jd_*.json")):
            if os.path.basename(f) == "jd_cache.json":
                continue
            try:
                with open(f, encoding="utf-8") as cache_file:
                    cache = json.load(cache_file)
                already.update({jid: entry for jid, entry in cache.items() if valid_jd(entry)})
            except Exception:
                pass
        picked = [j for j in rows if j["jobId"] not in already]
        print(json.dumps({"opening_jd_for": [j["jobId"] for j in picked]}, ensure_ascii=False))
        out = {j["jobId"]: already[j["jobId"]] for j in rows if j["jobId"] in already}
        cache_path = os.path.join(workdir, f"jd_{kw.replace(' ', '_')}.json")
        for j in picked:
            jid = j["jobId"]
            try:
                op = drv.cmd_open(jid)
                time.sleep(0.8)
                out[jid] = {"kw": kw, "company": j.get("company"), "title": j.get("title"),
                            "salary": j.get("salary"), "city": j.get("city"),
                            "degree": j.get("degree"), "scale": j.get("scale"),
                            "panel_ok": op.get("panel_ok"),
                            "hr_activity": op.get('hr_activity'),
                            "jd": (op.get("detail") or "").replace("\n", " / ")}
            except wb.AbortScriptError:
                atomic_write_json(cache_path, out)
                raise
            except Exception as e:
                out[jid] = {"kw": kw, "company": j.get("company"), "title": j.get("title"),
                            "error": repr(e)[:200]}
            print(f"  OPEN {jid} panel_ok={out[jid].get('panel_ok')}", flush=True)
            atomic_write_json(cache_path, out)
        atomic_write_json(cache_path, out)
        print(json.dumps({"ok": True, "jd_written": len(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
