#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalscope 摸高测试 Web 控制台后端（FastAPI）。

三视图：
  1. 配置项   —— url / model / tokenizer / Host 头开关 / 摸高参数
  2. 测试用例 —— 用例列表（增删启停）、每个用例设置数据集条数、
                 「检测数据集是否生成」开关（自动补齐未生成的数据集）、
                 自定义添加要测试的用例
  3. 测试工程 —— 工程列表（新建/删除），工程内可启动/终止测试任务；
                 打开工程查看其用例，每个用例查看性能结果与日志路径

数据模型（落盘 web_state.json，不覆盖 config.yaml 注释）:
  cases:    [{id, raw, dataset, input_len, output_len, hit_rate, prefix_num,
              ttft_sla_ms, tpot_sla_ms, dataset_num, enabled}, ...]
  projects: [{id, name, case_ids: [...], created}, ...]
  tasks:    {"<project_id>/<case_id>": {status, started, finished, log_path, ...}}
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT_ROOT)

import runner  # noqa: E402

try:
    from fastapi import FastAPI, Body
    from fastapi.responses import JSONResponse, HTMLResponse
    import uvicorn
except ImportError:
    print("缺少 fastapi/uvicorn，请先 pip install fastapi uvicorn", file=sys.stderr)
    sys.exit(1)

CONFIG_PATH = os.path.join(KIT_ROOT, "config.yaml")
STATE_PATH = os.path.join(KIT_ROOT, "web_state.json")
LOGS_DIR = os.path.join(KIT_ROOT, "logs")
RESULTS_DIR = os.path.join(KIT_ROOT, "results")
WEB_DIR = os.path.join(KIT_ROOT, "web")
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(RESULTS_DIR, exist_ok=True)

STATE_LOCK = threading.Lock()
_TASKS = {}          # "<project>/<case>" -> task
_RUN_QUEUE = []      # [(project_id, case_id)]
_RUN_THREAD = None
_STOP_FLAG = threading.Event()


# ---------------------------------------------------------------- state ----
def _default_state():
    return {"service_override": {}, "request_override": {}, "benchmark_override": {},
            "cases": None, "projects": [], "tasks": {}}


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            st = _default_state()
    else:
        st = _default_state()
    base = _default_state()
    base.update(st or {})
    return base


def save_state(st):
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)


def effective_config():
    cfg = runner.load_config(CONFIG_PATH)
    st = load_state()
    for sec in ("service", "request", "benchmark"):
        ov = st.get(sec + "_override") or {}
        if ov:
            cfg.setdefault(sec, {}).update(ov)
    # 用例级条数覆盖（文件名含条数 -> 相同长度/命中/条数天然复用）
    case_num = {c["id"]: c.get("dataset_num")
                for c in (st.get("cases") or []) if c.get("dataset_num")}
    cfg["dataset"]["case_number_overrides"] = case_num
    return cfg, st


def case_from_raw(raw, cid=None):
    """把一行场景文本解析成用例 dict。"""
    sc = runner.parse_scenario(raw)
    sc["id"] = cid or runner.scenario_id(sc)
    sc["dataset_num"] = None
    sc["enabled"] = True
    return sc


def get_cases(st=None):
    """取用例列表：state 有则用 state，否则从 config.yaml 派生。"""
    st = st or load_state()
    if st.get("cases"):
        return st["cases"]
    cfg = runner.load_config(CONFIG_PATH)
    out = []
    for line in cfg.get("scenarios") or []:
        line = str(line).strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(case_from_raw(line))
        except Exception:
            continue
    return out


def save_cases(st, cases):
    st["cases"] = cases
    save_state(st)


# --------------------------------------------------------------- tasks ----
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _persist_tasks():
    st = load_state()
    st["tasks"] = {k: dict(v) for k, v in _TASKS.items()}
    save_state(st)


def _set_task(key, **kw):
    with STATE_LOCK:
        t = _TASKS.setdefault(key, {"key": key})
        t.update(kw)
    _persist_tasks()


def _task_key(project_id, case_id):
    return f"{project_id}/{case_id}"


def _result_of(case_id):
    """找用例最新 sla_summary.json 并解析成表。"""
    sc_dir = os.path.join(RESULTS_DIR, case_id)
    summary = None
    paths = sorted(glob.glob(os.path.join(sc_dir, "*", "*", "sla_summary.json")))
    if paths:
        summary = paths[-1]
    rows = []
    if summary:
        try:
            with open(summary, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in data.items():
                if not k.startswith("parallel"):
                    continue
                try:
                    par = int(k.split("_")[1])
                except (IndexError, ValueError):
                    continue
                m = (v or {}).get("metrics", {}) or {}
                rows.append({"parallel": par, "ttft": m.get("Avg TTFT (ms)"),
                             "tpot": m.get("Avg TPOT (ms)"), "latency": m.get("Avg Latency (s)"),
                             "ok": m.get("Success Requests"), "total": m.get("Total Requests"),
                             "out_tps": m.get("Output Throughput (tok/s)"),
                             "total_tps": m.get("Total Throughput (tok/s)")})
            rows.sort(key=lambda r: r["parallel"])
        except Exception as e:
            rows = [{"error": str(e)}]
    return {"summary_path": summary, "rows": rows, "result_dir": sc_dir}


# --------------------------------------------------------- data pipeline ---
def dataset_status(cfg, st):
    """返回每个 gsm8k 用例的数据集状态（含复用信息）。"""
    cases = get_cases(st)
    seen = {}
    out = []
    for c in cases:
        if c.get("dataset") != "gsm8k":
            continue
        jsonl, prompts = runner.gsm8k_dataset_paths(cfg, c)
        key = os.path.basename(prompts)
        exists = os.path.exists(prompts)
        n = runner.dataset_number(cfg, c)
        out.append({"case_id": c["id"], "input_len": c["input_len"], "hit_rate": c["hit_rate"],
                    "num": n, "dataset_file": os.path.basename(jsonl), "prompts_path": prompts,
                    "exists": exists, "size": os.path.getsize(prompts) if exists else 0,
                    "shared_key": key, "shared": key in seen})
        seen[key] = True
    return out


def _run_case(project_id, case_id, cfg, st):
    """执行一个用例并记录任务状态。"""
    key = _task_key(project_id, case_id)
    cases = get_cases(st)
    c = next((x for x in cases if x.get("id") == case_id), None)
    if c is None:
        _set_task(key, status="error", error="用例不存在", finished=_now())
        return

    log_path = os.path.join(LOGS_DIR, f"proj_{project_id}_{case_id}_{time.strftime('%Y%m%d_%H%M%S')}.log")
    _set_task(key, status="running", started=_now(), finished=None, error=None,
              log_path=log_path, result_dir=os.path.join(RESULTS_DIR, case_id),
              case_id=case_id, project_id=project_id)

    with open(log_path, "a", encoding="utf-8", buffering=1) as logf:
        def log(msg):
            logf.write(msg + "\n")
            logf.flush()

        try:
            dataset_path = None
            if c["dataset"] == "gsm8k":
                log(f"[{case_id}] 确保数据集存在 ...")
                dataset_path = runner.ensure_gsm8k_dataset(cfg, c, force=False, log_handle=logf)
                log(f"[{case_id}] 数据集: {dataset_path}")
            cmd = runner.build_evalscope_cmd(cfg, c, dataset_path)
            log(f"[{case_id}] 命令: " + " ".join(cmd))
            log(f"[{case_id}] 开始 {_now()}")
            r = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
            if _STOP_FLAG.is_set():
                _set_task(key, status="stopped", finished=_now())
                log(f"[{case_id}] 已手动停止")
            elif r.returncode == 0:
                res = _result_of(case_id)
                _set_task(key, status="done", finished=_now(), summary_path=res["summary_path"])
                log(f"[{case_id}] 完成 {_now()}")
            else:
                _set_task(key, status="error", finished=_now(), error=f"exit code {r.returncode}")
                log(f"[{case_id}] 失败 exit={r.returncode}")
        except Exception as e:
            _set_task(key, status="error", finished=_now(), error=str(e))
            log(f"[{case_id}] 异常: {e}")


def _run_worker():
    global _RUN_THREAD
    while True:
        with STATE_LOCK:
            if not _RUN_QUEUE:
                _RUN_THREAD = None
                return
            project_id, case_id = _RUN_QUEUE.pop(0)
        if _STOP_FLAG.is_set():
            continue
        cfg, st = effective_config()
        try:
            _run_case(project_id, case_id, cfg, st)
        except Exception as e:
            _set_task(_task_key(project_id, case_id), status="error", error=str(e), finished=_now())
        time.sleep(1)


def enqueue_runs(items):
    """items: [(project_id, case_id), ...]"""
    global _RUN_THREAD
    with STATE_LOCK:
        for project_id, case_id in items:
            if (project_id, case_id) not in _RUN_QUEUE:
                _RUN_QUEUE.append((project_id, case_id))
            key = _task_key(project_id, case_id)
            _TASKS.setdefault(key, {"key": key})
            if _TASKS[key].get("status") != "running":
                _TASKS[key]["status"] = "pending"
        _STOP_FLAG.clear()
        if _RUN_THREAD is None or not _RUN_THREAD.is_alive():
            _RUN_THREAD = threading.Thread(target=_run_worker, daemon=True)
            _RUN_THREAD.start()
    _persist_tasks()


def _latest_case_log(pid, cid):
    pats = sorted(glob.glob(os.path.join(LOGS_DIR, f"proj_{pid}_{cid}_*.log")))
    return pats[-1] if pats else None


# ---------------------------------------------------------- app ------------
app = FastAPI(title="evalscope 摸高测试控制台")


@app.get("/", response_class=HTMLResponse)
def index():
    idx = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(idx):
        with open(idx, encoding="utf-8") as f:
            return f.read()
    return "<h1>web/index.html 缺失</h1>"


# ----- 配置项 -----
@app.get("/api/config")
def api_get_config():
    cfg, _ = effective_config()
    return {"service": cfg.get("service", {}), "request": cfg.get("request", {}),
            "benchmark": cfg.get("benchmark", {}), "dataset": cfg.get("dataset", {}),
            "host_header_effective": runner.build_headers(cfg)}


@app.post("/api/config")
def api_set_config(payload: dict = Body(...)):
    st = load_state()
    for sec in ("service", "request", "benchmark"):
        if sec in payload and isinstance(payload[sec], dict):
            st[sec + "_override"] = payload[sec]
    save_state(st)
    return {"ok": True}


# ----- 测试用例 -----
@app.get("/api/cases")
def api_cases():
    cfg, st = effective_config()
    cases = get_cases(st)
    ds = {d["case_id"]: d for d in dataset_status(cfg, st)}
    for c in cases:
        d = ds.get(c["id"])
        if d:
            c["dataset_exists"] = d["exists"]
            c["dataset_file"] = d["dataset_file"]
            c["dataset_num"] = d["num"]
            c["dataset_shared"] = d["shared"]
        else:
            c["dataset_exists"] = None
    return {"cases": cases}


@app.post("/api/cases")
def api_set_cases(payload: dict = Body(...)):
    """整体替换用例列表（增删改都由前端提交完整列表）。

    允许条目只含 {id, enabled, dataset_num}：缺 raw 时按 id 从现有用例补齐。
    """
    st = load_state()
    raw_cases = payload.get("cases")
    if raw_cases is None:
        return JSONResponse({"ok": False, "error": "缺少 cases"}, status_code=400)
    existing = {c["id"]: c for c in get_cases(st)}
    parsed = []
    for rc in raw_cases:
        raw = rc.get("raw") or rc.get("line")
        if not raw:
            old = existing.get(rc.get("id"))
            if old is None:
                return JSONResponse({"ok": False, "error": f"未知用例且无 raw: {rc.get('id')}"}, status_code=400)
            c = dict(old)
        else:
            try:
                c = case_from_raw(raw, cid=rc.get("id"))
            except Exception as e:
                return JSONResponse({"ok": False, "error": f"用例格式错误: {raw} ({e})"}, status_code=400)
        if rc.get("dataset_num") not in (None, ""):
            try:
                c["dataset_num"] = int(rc["dataset_num"])
            except (TypeError, ValueError):
                c["dataset_num"] = None
        else:
            c["dataset_num"] = None
        c["enabled"] = bool(rc.get("enabled", True))
        parsed.append(c)
    save_cases(st, parsed)
    return {"ok": True, "count": len(parsed)}


@app.post("/api/cases/add")
def api_case_add(payload: dict = Body(...)):
    raw = payload.get("raw", "")
    try:
        c = case_from_raw(raw)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"格式错误: {e}"}, status_code=400)
    if payload.get("dataset_num") not in (None, ""):
        c["dataset_num"] = int(payload["dataset_num"])
    st = load_state()
    cases = get_cases(st)
    if any(x["id"] == c["id"] for x in cases):
        return JSONResponse({"ok": False, "error": f"用例已存在: {c['id']}"}, status_code=400)
    cases.append(c)
    save_cases(st, cases)
    return {"ok": True, "case": c}


@app.post("/api/cases/delete")
def api_case_delete(payload: dict = Body(...)):
    cid = payload.get("id")
    st = load_state()
    cases = [c for c in get_cases(st) if c["id"] != cid]
    save_cases(st, cases)
    return {"ok": True}


# ----- 数据集 -----
@app.get("/api/datasets")
def api_datasets():
    cfg, st = effective_config()
    return {"datasets": dataset_status(cfg, st)}


@app.post("/api/datasets/generate")
def api_datasets_generate(payload: dict = Body(default={})):
    """生成数据集：payload.case_ids=指定用例；否则生成所有未生成的 gsm8k 用例。"""
    cfg, st = effective_config()
    force = bool(payload.get("force"))
    case_ids = payload.get("case_ids")
    cases = get_cases(st)
    todo = {}
    for c in cases:
        if c.get("dataset") != "gsm8k":
            continue
        if case_ids is not None and c["id"] not in case_ids:
            continue
        jsonl, prompts = runner.gsm8k_dataset_paths(cfg, c)
        if os.path.exists(prompts) and not force:
            continue  # 已生成且非强制 -> 跳过
        todo[os.path.basename(prompts)] = c

    def gen_all():
        for fn, c in todo.items():
            log_path = os.path.join(LOGS_DIR, f"gen_{c['id']}_{time.strftime('%Y%m%d_%H%M%S')}.log")
            with open(log_path, "a", encoding="utf-8", buffering=1) as lf:
                try:
                    lf.write(f"[dataset {c['id']}] 生成中 num={runner.dataset_number(cfg, c)} force={force}\n")
                    runner.ensure_gsm8k_dataset(cfg, c, force=force, log_handle=lf)
                    lf.write(f"[dataset {c['id']}] 完成\n")
                except Exception as e:
                    lf.write(f"[dataset {c['id']}] 失败: {e}\n")

    threading.Thread(target=gen_all, daemon=True).start()
    return {"ok": True, "count": len(todo)}


# ----- 测试工程 -----
@app.get("/api/projects")
def api_projects():
    st = load_state()
    projects = st.get("projects") or []
    out = []
    for p in projects:
        keys = [_task_key(p["id"], cid) for cid in p.get("case_ids", [])]
        statuses = [_TASKS.get(k, {}).get("status", "not_started") for k in keys]
        out.append({**p, "case_count": len(p.get("case_ids", [])),
                    "statuses": statuses,
                    "done": statuses.count("done"),
                    "running": statuses.count("running") + statuses.count("pending")})
    return {"projects": out}


@app.post("/api/projects")
def api_project_add(payload: dict = Body(...)):
    st = load_state()
    name = (payload.get("name") or "").strip() or f"工程{len(st.get('projects') or []) + 1}"
    case_ids = payload.get("case_ids")
    if case_ids is None:
        case_ids = [c["id"] for c in get_cases(st) if c.get("enabled")]
    pid = re.sub(r"[^0-9A-Za-z_\-]", "_", name)[:40] + "_" + time.strftime("%H%M%S")
    st.setdefault("projects", []).append({"id": pid, "name": name, "case_ids": list(case_ids),
                                          "created": _now()})
    save_state(st)
    return {"ok": True, "project": {"id": pid, "name": name}}


@app.post("/api/projects/delete")
def api_project_delete(payload: dict = Body(...)):
    st = load_state()
    pid = payload.get("id")
    st["projects"] = [p for p in (st.get("projects") or []) if p["id"] != pid]
    save_state(st)
    return {"ok": True}


@app.get("/api/projects/{pid}")
def api_project_detail(pid: str):
    st = load_state()
    p = next((x for x in (st.get("projects") or []) if x["id"] == pid), None)
    if not p:
        return JSONResponse({"ok": False, "error": "工程不存在"}, status_code=404)
    cases = {c["id"]: c for c in get_cases(st)}
    items = []
    for cid in p.get("case_ids", []):
        key = _task_key(pid, cid)
        t = _TASKS.get(key, {})
        c = cases.get(cid, {})
        items.append({
            "case_id": cid, "raw": c.get("raw"), "dataset": c.get("dataset"),
            "input_len": c.get("input_len"), "output_len": c.get("output_len"),
            "hit_rate": c.get("hit_rate"),
            "ttft_sla_ms": c.get("ttft_sla_ms"), "tpot_sla_ms": c.get("tpot_sla_ms"),
            "status": t.get("status", "not_started"),
            "started": t.get("started"), "finished": t.get("finished"), "error": t.get("error"),
            "log_path": t.get("log_path") or _latest_case_log(pid, cid),
            "result_dir": os.path.join(RESULTS_DIR, cid),
        })
    return {"project": p, "cases": items}


@app.get("/api/projects/{pid}/case/{cid}")
def api_project_case_detail(pid: str, cid: str):
    res = _result_of(cid)
    t = _TASKS.get(_task_key(pid, cid), {})
    per_logs = sorted(glob.glob(os.path.join(res["result_dir"], "*", "*", "sla_tuning", "*", "benchmark.log")))
    return {"case_id": cid, "task": t, "summary_path": res["summary_path"],
            "rows": res["rows"], "result_dir": res["result_dir"],
            "log_path": t.get("log_path") or _latest_case_log(pid, cid),
            "per_concurrency_logs": per_logs}


@app.post("/api/projects/{pid}/run")
def api_project_run(pid: str, payload: dict = Body(default={})):
    st = load_state()
    p = next((x for x in (st.get("projects") or []) if x["id"] == pid), None)
    if not p:
        return JSONResponse({"ok": False, "error": "工程不存在"}, status_code=404)
    cids = payload.get("case_ids") or p.get("case_ids", [])
    items = [(pid, cid) for cid in cids]
    for _, cid in items:
        _set_task(_task_key(pid, cid), status="pending")
    enqueue_runs(items)
    return {"ok": True, "queued": [c for _, c in items]}


@app.post("/api/projects/{pid}/stop")
def api_project_stop(pid: str):
    _STOP_FLAG.set()
    with STATE_LOCK:
        _RUN_QUEUE[:] = [it for it in _RUN_QUEUE if it[0] != pid]
    st = load_state()
    p = next((x for x in (st.get("projects") or []) if x["id"] == pid), None)
    if p:
        for cid in p.get("case_ids", []):
            t = _TASKS.get(_task_key(pid, cid), {})
            if t.get("status") in ("pending", "running"):
                _set_task(_task_key(pid, cid), status="stopped", finished=_now())
    return {"ok": True}


@app.get("/api/log")
def api_log(path: str):
    ap = os.path.abspath(path)
    if not ap.startswith(os.path.abspath(KIT_ROOT)):
        return JSONResponse({"ok": False, "error": "路径越界"}, status_code=400)
    if not os.path.exists(ap):
        return {"ok": False, "error": "文件不存在", "lines": []}
    with open(ap, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return {"ok": True, "lines": lines[-400:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8300)
    args = ap.parse_args()
    st = load_state()
    for k, t in (st.get("tasks") or {}).items():
        if t.get("status") == "running":
            t["status"] = "interrupted"
        _TASKS[k] = t
    print(f"evalscope 摸高测试控制台: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
