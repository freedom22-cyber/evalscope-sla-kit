#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalscope 摸高测试 Web 前端后端（FastAPI）。

三视图：
  1. 配置项   —— url / model / tokenizer / Host 头开关 / 测试场景增删启停
  2. 测试详情 —— 任务状态（已完成 / 进行中 / 未开始），点开看性能结果 + 日志路径
  3. 数据集详情 —— 按场景配置数据集条数、复用同一 (input_len, hit_rate) 的数据集、
                   一键生成全部数据集

设计:
  config.yaml 作为「带注释的默认配置」（CLI 与 Web 共用），Web UI 的改动落盘到
  同目录的 web_state.json（overlay），从而不破坏 config.yaml 的注释。

启动:
  python3 webapp.py                 # 默认 0.0.0.0:8300
  python3 webapp.py --port 8300
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import glob
from copy import deepcopy

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT_ROOT)

import runner  # noqa: E402  复用场景解析 / 数据集 / 命令行构建

try:
    from fastapi import FastAPI, Body
    from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
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
_TASKS = {}          # id -> task dict
_RUN_QUEUE = []      # pending scenario ids
_RUN_THREAD = None
_STOP_FLAG = threading.Event()


# ---------------------------------------------------------------- state ----
def _default_state():
    return {
        "service_override": {},
        "request_override": {},
        "benchmark_override": {},
        "scenarios": None,            # None = 用 config.yaml
        "scenarios_disabled": [],
        "number_overrides": {},       # "<input_len>_<hit_rate>" -> 条数
        "tasks": {},
    }


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
    """config.yaml 深合并 web_state 覆盖，得到 Web/CLI 一致的有效配置。"""
    cfg = runner.load_config(CONFIG_PATH)
    st = load_state()
    for sec in ("service", "request", "benchmark"):
        ov = st.get(sec + "_override") or {}
        if ov:
            cfg.setdefault(sec, {}).update(ov)
    # 场景列表：Web 若自定义则覆盖 config.yaml
    if st.get("scenarios"):
        cfg["scenarios"] = list(st["scenarios"])
    # 数据集条数覆盖
    cfg["dataset"]["number_overrides"] = dict(st.get("number_overrides") or {})
    return cfg, st


def parse_scenarios(cfg, st):
    """返回场景列表，每个含 id/字段/enabled。"""
    out = []
    disabled = set(st.get("scenarios_disabled") or [])
    for line in cfg.get("scenarios") or []:
        line = str(line).strip()
        if not line or line.startswith("#"):
            continue
        try:
            sc = runner.parse_scenario(line)
        except Exception as e:
            out.append({"id": None, "raw": line, "error": str(e), "enabled": False})
            continue
        sid = runner.scenario_id(sc)
        sc["id"] = sid
        sc["enabled"] = sid not in disabled
        out.append(sc)
    return out


# --------------------------------------------------------------- tasks ----
def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _task_running():
    return any(t.get("status") == "running" for t in _TASKS.values())


def _persist_tasks():
    st = load_state()
    st["tasks"] = {
        k: {kk: vv for kk, vv in v.items()}
        for k, v in _TASKS.items()
    }
    save_state(st)


def _set_task(sid, **kw):
    with STATE_LOCK:
        t = _TASKS.setdefault(sid, {"id": sid, "status": "pending"})
        t.update(kw)
    _persist_tasks()


def _result_of(sid):
    """找场景最新 sla_summary.json 并解析成表。"""
    sc_dir = os.path.join(RESULTS_DIR, sid)
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
                rows.append({
                    "parallel": par,
                    "ttft": m.get("Avg TTFT (ms)"),
                    "tpot": m.get("Avg TPOT (ms)"),
                    "latency": m.get("Avg Latency (s)"),
                    "ok": m.get("Success Requests"),
                    "total": m.get("Total Requests"),
                    "out_tps": m.get("Output Throughput (tok/s)"),
                    "total_tps": m.get("Total Throughput (tok/s)"),
                })
            rows.sort(key=lambda r: r["parallel"])
        except Exception as e:
            rows = [{"error": str(e)}]
    return {"summary_path": summary, "rows": rows}


# ------------------------------------------------------------- runner ----
def _run_one(sid, cfg, st):
    """执行单个场景：确保数据集 -> 构建命令 -> 跑 evalscope -> 记录结果。"""
    scenarios = parse_scenarios(cfg, st)
    sc = next((s for s in scenarios if s.get("id") == sid), None)
    if sc is None:
        _set_task(sid, status="error", error="场景不存在")
        return

    log_path = os.path.join(LOGS_DIR, f"web_{sid}_{time.strftime('%Y%m%d_%H%M%S')}.log")
    _set_task(sid, status="running", started=_now(), finished=None,
              log_path=log_path, result_dir=os.path.join(RESULTS_DIR, sid), error=None)

    logf = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(msg):
        logf.write(msg + "\n")
        logf.flush()

    try:
        dataset_path = None
        if sc["dataset"] == "gsm8k":
            log(f"[{sid}] 确保数据集存在 ...")
            dataset_path = runner.ensure_gsm8k_dataset(cfg, sc, force=False, log_handle=logf)
            log(f"[{sid}] 数据集: {dataset_path}")

        cmd = runner.build_evalscope_cmd(cfg, sc, dataset_path)
        log(f"[{sid}] 命令: " + " ".join(cmd))
        log(f"[{sid}] 开始 {_now()}")
        env = dict(os.environ)
        r = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT, env=env)
        if _STOP_FLAG.is_set():
            _set_task(sid, status="stopped", finished=_now())
            log(f"[{sid}] 已手动停止")
        elif r.returncode == 0:
            res = _result_of(sid)
            _set_task(sid, status="done", finished=_now(),
                      summary_path=res["summary_path"])
            log(f"[{sid}] 完成 {_now()}")
        else:
            _set_task(sid, status="error", finished=_now(),
                      error=f"exit code {r.returncode}")
            log(f"[{sid}] 失败 exit={r.returncode}")
    except Exception as e:
        _set_task(sid, status="error", finished=_now(), error=str(e))
        log(f"[{sid}] 异常: {e}")
    finally:
        logf.close()


def _run_worker():
    global _RUN_THREAD
    while True:
        with STATE_LOCK:
            if not _RUN_QUEUE:
                _RUN_THREAD = None
                return
            sid = _RUN_QUEUE.pop(0)
        if _STOP_FLAG.is_set():
            continue
        cfg, st = effective_config()
        try:
            _run_one(sid, cfg, st)
        except Exception as e:
            _set_task(sid, status="error", error=str(e), finished=_now())
        time.sleep(1)


def enqueue_runs(ids):
    global _RUN_THREAD
    with STATE_LOCK:
        for sid in ids:
            if sid not in _RUN_QUEUE:
                _RUN_QUEUE.append(sid)
            _TASKS.setdefault(sid, {"id": sid})
            if _TASKS[sid].get("status") not in ("running", "done"):
                _TASKS[sid]["status"] = "pending"
        _STOP_FLAG.clear()
        if _RUN_THREAD is None or not _RUN_THREAD.is_alive():
            _RUN_THREAD = threading.Thread(target=_run_worker, daemon=True)
            _RUN_THREAD.start()
    _persist_tasks()


# ---------------------------------------------------------- app ------------
app = FastAPI(title="evalscope 摸高测试控制台")


@app.get("/", response_class=HTMLResponse)
def index():
    idx = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(idx):
        with open(idx, encoding="utf-8") as f:
            return f.read()
    return "<h1>web/index.html 缺失</h1>"


@app.get("/api/config")
def api_get_config():
    cfg, st = effective_config()
    return {
        "service": cfg.get("service", {}),
        "request": cfg.get("request", {}),
        "benchmark": cfg.get("benchmark", {}),
        "dataset": cfg.get("dataset", {}),
        "host_header_effective": runner.build_headers(cfg),
    }


@app.post("/api/config")
def api_set_config(payload: dict = Body(...)):
    st = load_state()
    for sec in ("service", "request", "benchmark"):
        if sec in payload and isinstance(payload[sec], dict):
            st[sec + "_override"] = payload[sec]
    if "dataset" in payload and isinstance(payload["dataset"], dict):
        # 仅接受 number_overrides/gsm8k_number 这类可写键
        ds = payload["dataset"]
        if "number_overrides" in ds:
            st["number_overrides"] = ds["number_overrides"]
    save_state(st)
    return {"ok": True, "state": st}


@app.get("/api/scenarios")
def api_scenarios():
    cfg, st = effective_config()
    scs = parse_scenarios(cfg, st)
    for s in scs:
        sid = s.get("id")
        if sid and sid in _TASKS:
            s["status"] = _TASKS[sid].get("status")
        elif sid:
            s["status"] = "pending" if False else "not_started"
    return {"scenarios": scs}


@app.post("/api/scenarios")
def api_set_scenarios(payload: dict = Body(...)):
    """增删改场景列表 + 启停。"""
    st = load_state()
    if "scenarios" in payload and payload["scenarios"] is not None:
        st["scenarios"] = [str(x).strip() for x in payload["scenarios"] if str(x).strip()]
    if "disabled" in payload:
        st["scenarios_disabled"] = list(payload["disabled"])
    save_state(st)
    return {"ok": True}


@app.get("/api/datasets")
def api_datasets():
    """只列 gsm8k 场景（random 无需本地数据集）。含复用信息。"""
    cfg, st = effective_config()
    scs = [s for s in parse_scenarios(cfg, st) if s.get("dataset") == "gsm8k"]
    seen = {}
    out = []
    for s in scs:
        jsonl, prompts = runner.gsm8k_dataset_paths(cfg, s)
        key = os.path.basename(prompts)
        exists = os.path.exists(prompts)
        n = runner.dataset_number(cfg, s)
        entry = {
            "scenario_id": s["id"],
            "input_len": s["input_len"],
            "hit_rate": s["hit_rate"],
            "num": n,
            "dataset_file": os.path.basename(jsonl),
            "prompts_path": prompts,
            "exists": exists,
            "size": os.path.getsize(prompts) if exists else 0,
            "shared_key": f"{s['input_len']}_{s['hit_rate']}",
            "shared": key in seen,
        }
        seen[key] = True
        out.append(entry)
    return {"datasets": out}


@app.post("/api/datasets/number")
def api_set_dataset_number(payload: dict = Body(...)):
    """设置某数据集条数（按 <input_len>_<hit_rate> 键）。"""
    st = load_state()
    key = str(payload.get("key", "")).strip()
    if not key:
        return JSONResponse({"ok": False, "error": "缺少 key"}, status_code=400)
    if payload.get("num") in (None, ""):
        (st.get("number_overrides") or {}).pop(key, None)
    else:
        st.setdefault("number_overrides", {})[key] = int(payload["num"])
    save_state(st)
    return {"ok": True}


@app.post("/api/datasets/generate")
def api_generate_datasets(payload: dict = Body(default={})):
    """一键生成全部 / 指定数据集（后台线程）。"""
    cfg, st = effective_config()
    force = bool(payload.get("force"))
    only = payload.get("keys")  # None = 全部
    scs = [s for s in parse_scenarios(cfg, st) if s.get("dataset") == "gsm8k"]
    # 去重：同一 (input_len, hit_rate) 只生成一次
    uniq = {}
    for s in scs:
        k = f"{s['input_len']}_{s['hit_rate']}"
        if only is not None and k not in only:
            continue
        uniq[k] = s

    def gen_all():
        for k, s in uniq.items():
            log_path = os.path.join(LOGS_DIR, f"gen_{k}_{time.strftime('%Y%m%d_%H%M%S')}.log")
            with open(log_path, "a", encoding="utf-8", buffering=1) as lf:
                try:
                    lf.write(f"[dataset {k}] 生成中 num={runner.dataset_number(cfg, s)} force={force}\n")
                    runner.ensure_gsm8k_dataset(cfg, s, force=force, log_handle=lf)
                    lf.write(f"[dataset {k}] 完成\n")
                except Exception as e:
                    lf.write(f"[dataset {k}] 失败: {e}\n")

    threading.Thread(target=gen_all, daemon=True).start()
    return {"ok": True, "count": len(uniq)}


@app.get("/api/tasks")
def api_tasks():
    cfg, st = effective_config()
    scs = parse_scenarios(cfg, st)
    out = []
    for s in scs:
        sid = s.get("id")
        if not sid:
            continue
        t = _TASKS.get(sid, {})
        res = _result_of(sid) if os.path.isdir(os.path.join(RESULTS_DIR, sid)) else {"summary_path": None, "rows": []}
        out.append({
            "id": sid,
            "raw": s.get("raw"),
            "dataset": s.get("dataset"),
            "input_len": s.get("input_len"),
            "output_len": s.get("output_len"),
            "hit_rate": s.get("hit_rate"),
            "ttft_sla_ms": s.get("ttft_sla_ms"),
            "tpot_sla_ms": s.get("tpot_sla_ms"),
            "status": t.get("status", "not_started"),
            "started": t.get("started"),
            "finished": t.get("finished"),
            "error": t.get("error"),
            "log_path": t.get("log_path"),
            "result_dir": t.get("result_dir") or os.path.join(RESULTS_DIR, sid),
            "summary_path": t.get("summary_path") or res["summary_path"],
            "result_rows": res["rows"],
        })
    return {"tasks": out}


@app.get("/api/tasks/{sid}")
def api_task_detail(sid: str):
    res = _result_of(sid)
    t = _TASKS.get(sid, {})
    sc_dir = os.path.join(RESULTS_DIR, sid)
    all_logs = sorted(glob.glob(os.path.join(sc_dir, "*", "*", "sla_tuning", "*", "benchmark.log")))
    return {
        "id": sid,
        "task": t,
        "summary_path": res["summary_path"],
        "rows": res["rows"],
        "result_dir": sc_dir,
        "per_concurrency_logs": all_logs,
    }


@app.get("/api/log")
def api_log(path: str):
    """读取日志文件尾部若干行（仅允许 kit 目录内的文件）。"""
    ap = os.path.abspath(path)
    if not ap.startswith(os.path.abspath(KIT_ROOT)):
        return JSONResponse({"ok": False, "error": "路径越界"}, status_code=400)
    if not os.path.exists(ap):
        return {"ok": False, "error": "文件不存在", "lines": []}
    with open(ap, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    return {"ok": True, "lines": lines[-400:]}


@app.post("/api/run")
def api_run(payload: dict = Body(default={})):
    cfg, st = effective_config()
    scs = parse_scenarios(cfg, st)
    enabled_ids = [s["id"] for s in scs if s.get("enabled") and s.get("id")]
    ids = payload.get("ids") or enabled_ids
    ids = [i for i in ids if i]
    if not ids:
        return JSONResponse({"ok": False, "error": "没有可运行的场景"}, status_code=400)
    for sid in ids:
        _set_task(sid, status="pending")
    enqueue_runs(ids)
    return {"ok": True, "queued": ids}


@app.post("/api/stop")
def api_stop():
    _STOP_FLAG.set()
    with STATE_LOCK:
        _RUN_QUEUE.clear()
    for sid, t in _TASKS.items():
        if t.get("status") in ("pending", "running"):
            _set_task(sid, status="stopped", finished=_now())
    return {"ok": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8300)
    args = ap.parse_args()
    # 载入历史任务状态
    st = load_state()
    for sid, t in (st.get("tasks") or {}).items():
        if t.get("status") == "running":
            t["status"] = "interrupted"
        _TASKS[sid] = t
    print(f"evalscope 摸高测试控制台: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
