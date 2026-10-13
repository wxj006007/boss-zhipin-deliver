"""批量采集候选池（v6.11 新增）。

关键突破：BOSS 搜索 URL 的 `scale`（公司规模）参数要传**筛选码**而非区间文案：
  301=0-20人 302=20-99人 303=100-499人 304=500-999人 305=1000-9999人 306=10000人以上
多选写法 `&scale=304,305,306`。配合 `jobType=1902`（实习），单页 15 张卡几乎全是中大厂，
命中率从 ~20% 提升到接近 100%（详见 references/search_filters.md）。

用法:
  python harvest_pool.py keywords.json [scale_codes] [job_type] [workdir]

  keywords.json 是字符串数组，如 ["后端开发 实习", "AI Agent 实习"]
  workdir 缺省取环境变量 BOSS_WORKDIR，再缺省为统一的本机技能工作目录

输出:
  <workdir>/pool.json   去重后的候选池（含 _kw 字段记录来源关键词，供 pipeline_batch 分组）
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from urllib.parse import quote
from scripts import webbridge_client as wb
from scripts.runtime import resolve_workdir, atomic_write_json
from scripts.config_v3 import load_profile


def jobs_now():
    raw = wb.extract_jobs()
    if isinstance(raw, str):
        return wb._parse_jobs(raw)
    if isinstance(raw, dict):
        return raw.get("list") or []
    return raw or []


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    kwfile = sys.argv[1]
    scale = sys.argv[2] if len(sys.argv) > 2 else "304,305,306"
    job_type = sys.argv[3] if len(sys.argv) > 3 else "1902"
    workdir = resolve_workdir(sys.argv[4] if len(sys.argv) > 4 else "")
    os.makedirs(workdir, exist_ok=True)
    pool_file = os.path.join(workdir, "pool.json")

    kws = json.load(open(kwfile, encoding="utf-8"))

    delivered = set()
    pp = os.path.join(workdir, "stream_progress.json")
    if os.path.exists(pp):
        delivered = {x.get("jobId") for x in json.load(
            open(pp, encoding="utf-8")).get("delivered", [])}

    pool = {}
    if os.path.exists(pool_file):
        for j in json.load(open(pool_file, encoding="utf-8")).get("jobs", []):
            pool[j["jobId"]] = j

    for kw in kws:
        city = os.environ.get("BOSS_CITY") or load_profile().get("city_code") or "100010000"
        url = (f"https://www.zhipin.com/web/geek/jobs?query={quote(kw)}"
               f"&city={city}&jobType={job_type}&scale={scale}")
        got = 0
        for _attempt in (1, 2):
            try:
                wb.navigate(url, wait_range=(5, 7))
            except Exception:
                atomic_write_json(pool_file, {"scale": scale, "job_type": job_type, "jobs": list(pool.values())})
                raise
            time.sleep(1.5)
            cur = wb.evaluate("window.location.href") or ""
            if "security" in cur:
                atomic_write_json(pool_file, {"scale": scale, "job_type": job_type, "jobs": list(pool.values())})
                raise wb.AbortScriptError("遇安全验证页，停止采集待人工检查")
            anomaly, reason = wb.detect_page_anomaly(check_blank=False)
            if anomaly:
                atomic_write_json(pool_file, {"scale": scale, "job_type": job_type, "jobs": list(pool.values())})
                raise wb.AbortScriptError(reason)
            js = jobs_now()
            got = len(js)
            for j in js:
                jid = j.get("jobId")
                if not jid or jid in delivered or jid in pool:
                    continue
                j["_kw"] = kw
                pool[jid] = j
            if got:
                break
        print(f"[{kw}] got={got} pool={len(pool)}", flush=True)
        time.sleep(3.0)

    atomic_write_json(pool_file, {"scale": scale, "job_type": job_type, "jobs": list(pool.values())})

    keep = [j for j in pool.values() if j.get("scale") in
            ("500-999人", "1000-9999人", "10000人以上")]
    print(f"\n=== POOL total={len(pool)} 中大厂={len(keep)} ===", flush=True)
    for j in sorted(keep, key=lambda x: (x.get("company") or "")):
        print(f"  {j.get('jobId')} | {j.get('company')} | {j.get('scale')} | "
              f"{j.get('stage')} | {j.get('city')} | {j.get('title')} | "
              f"{j.get('salary')} | {j.get('degree')}", flush=True)


if __name__ == "__main__":
    main()
