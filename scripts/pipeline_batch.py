"""投递流水线（v6.11 新增）：按「来源关键词页」分组，同页内完成 JD 复核 + 批量投递。

为什么按页分组：BOSS 投递必须在「卡片所在的那个页面」原地点击
（deliver_click 用 `a[href*="job_detail/<jid>"]` 定位卡片；_ensure_recommend 只在
非 /web/geek/jobs 路径时才导航）。而每次关键词搜索只加载 15 张卡，且卡片集会随
重新加载而轮换。所以：候选池 -> 按 _kw 分组 -> 逐页 navigate -> 页内 reject/deliver。

用法:
  python pipeline_batch.py jdc <jid...>     只拉 JD 正文（复核届别/学历/是否外包）
  python pipeline_batch.py go decisions.json 执行投递
  python pipeline_batch.py ls [kw]           查看候选池（可按来源关键词过滤）

decisions.json:
  {"deliver": ["jid1", "jid2"], "reject": ["jid3"]}

workdir 取环境变量 BOSS_WORKDIR，缺省统一为本机技能工作目录。
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from urllib.parse import quote
from scripts import webbridge_client as wb
from scripts import _live_driver as drv
from scripts.runtime import resolve_workdir, atomic_write_json

WORKDIR = resolve_workdir()
POOL = os.path.join(WORKDIR, "pool.json")
SCALE = os.environ.get("BOSS_SCALE", "304,305,306")
JOB_TYPE = os.environ.get("BOSS_JOBTYPE", "1902")


def load_pool():
    if not os.path.exists(POOL):
        return {}
    return {j["jobId"]: j for j in json.load(open(POOL, encoding="utf-8")).get("jobs", [])}


def kw_url(kw):
    city = os.environ.get("BOSS_CITY") or drv.load_profile_cached().get("city_code") or "100010000"
    return (f"https://www.zhipin.com/web/geek/jobs?query={quote(kw)}"
            f"&city={city}&jobType={JOB_TYPE}&scale={SCALE}")


def goto(kw):
    from scripts import phase_stream as ps
    wb.navigate(kw_url(kw), wait_range=(5, 7), expect_url_contains="query=", label=f"搜索-{kw}")
    ps.CURRENT_WORK_URL["url"] = kw_url(kw)  # v6.11.2: _ensure_recommend 意外跳页时回本组关键词页
    time.sleep(1.5)
    anomaly, reason = wb.detect_page_anomaly(check_blank=False)
    if anomaly or "security" in (wb.evaluate("window.location.href") or ""):
        raise wb.AbortScriptError(reason or "安全验证页；停止待人工检查")
    return wb.extract_jobs()


def group_by_kw(pool, jids):
    g = {}
    for j in jids:
        kw = (pool.get(j) or {}).get("_kw", "")
        if not kw:
            print(f"WARN 不在 pool 中，跳过分组: {j}", flush=True)
            continue
        g.setdefault(kw, []).append(j)
    return g


def cmd_jdc(jids):
    pool = load_pool()
    g = group_by_kw(pool, jids)
    out = {}
    for kw, ids in g.items():
        print(f"\n=== [{kw}] {len(ids)} cards ===", flush=True)
        goto(kw)
        for jid in ids:
            meta = pool.get(jid) or {}
            try:
                op = drv.cmd_open(jid)
                time.sleep(1.0)
                txt = (op.get("detail") or "").replace("\n", " ⏎ ")
                out[jid] = {"kw": kw, "company": meta.get("company"),
                            "title": meta.get("title"), "panel_ok": op.get("panel_ok"),
                            "jd": txt}
                print(f"--- {jid} | {meta.get('company')} | {meta.get('title')}", flush=True)
                print(f"    panel_ok={op.get('panel_ok')}", flush=True)
                print(f"    JD: {txt}", flush=True)
            except wb.AbortScriptError:
                atomic_write_json(os.path.join(WORKDIR, "jd_cache.json"), out)
                raise
            except Exception as e:
                out[jid] = {"kw": kw, "error": repr(e)[:160]}
                print(f"--- {jid} ERR {repr(e)[:120]}", flush=True)
    atomic_write_json(os.path.join(WORKDIR, "jd_cache.json"), out)


def cmd_go(path):
    pool = load_pool()
    with open(path, encoding="utf-8") as f:
        dec = json.load(f)
    delivers, rejects = list(dict.fromkeys(dec.get("deliver", []))), set(dec.get("reject", []))
    if set(delivers) & rejects:
        raise ValueError("同一岗位同时出现在deliver/reject中，停止以免执行错误决策")
    if any(jid not in pool for jid in delivers):
        raise ValueError("待投岗位不在候选池中，先重新采集并审核")
    res = {"delivered": [], "failed": [], "rejected": [], "skipped": [], "stopped": False}
    try:
        for kw, ids in group_by_kw(pool, delivers + list(rejects)).items():
            goto(kw)
            for jid in ids:
                meta = pool[jid]
                if jid in rejects:
                    drv.cmd_reject(jid)
                    res["rejected"].append(jid)
                    continue
                try:
                    r = drv.cmd_deliver(jid, meta.get("company", ""), meta.get("salary", ""),
                                        meta.get("industry", ""), meta.get("direction", ""))
                except Exception as e:
                    res["failed"].append({"jobId": jid, "reason": str(e)})
                    res["stopped"], res["stop_reason"] = True, str(e)
                    return res
                if r.get("already_done"):
                    res["skipped"].append(jid)
                elif r.get("delivered_ok"):
                    res["delivered"].append(dict(meta, verify=r.get("verify") is True))
                else:
                    res["failed"].append({"jobId": jid, "reason": r})
                if r.get("stopped") or r.get("daily_limit") or r.get("anomaly") or r.get("wrong_path"):
                    res["stopped"], res["stop_reason"] = True, r
                    return res
                time.sleep(.7)
        return res
    except wb.AbortScriptError as e:
        res["stopped"], res["stop_reason"] = True, str(e)
        return res
    finally:
        atomic_write_json(os.path.join(WORKDIR, "run_result.json"), res)
        print(json.dumps(res, ensure_ascii=False, indent=2), flush=True)


def cmd_ls(kw_filter=""):
    pool = load_pool()
    rows = [j for j in pool.values() if not kw_filter or j.get("_kw") == kw_filter]
    print(f"pool={len(pool)} shown={len(rows)}")
    for j in sorted(rows, key=lambda x: (x.get("_kw") or "", x.get("company") or "")):
        print(f"  {j.get('jobId')} | [{j.get('_kw')}] | {j.get('company')} | {j.get('scale')} | "
              f"{j.get('city')} | {j.get('title')} | {j.get('salary')} | {j.get('degree')}")


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else "ls"
    if a == "jdc":
        cmd_jdc(sys.argv[2:])
    elif a == "go":
        cmd_go(sys.argv[2])
    else:
        cmd_ls(sys.argv[2] if len(sys.argv) > 2 else "")
