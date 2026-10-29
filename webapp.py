#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalscope 摸高测试 Web 控制台后端（FastAPI）。

三视图：
  1. 配置项   —— url / model / tokenizer / Host 头开关 / 摸高参数
  2. 测试用例 —— 用例列表（增删启停）、数据集条数在添加用例时固化
                 （已有用例不可改条数，要改就新增用例）、
                 「检测数据集是否生成」仅刷新状态列（生成由用户逐用例点击）、
                 自定义添加要测试的用例
  3. 测试工程 —— 新建时可勾选要执行的用例；工程内可启动/终止测试任务；
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
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT_ROOT)

import runner  # noqa: E402

try:
    from fastapi import FastAPI, Body
    from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse
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
_STOP_FLAG = threading.Event()     # 整个工程停止（仅 enqueue 新一批时清）
_GEN_TASKS = {}      # case_id -> {status: idle|generating|done|error, error: str}
_GEN_LOCK = threading.Lock()     # 数据集去重：置 generating 必须与检查原子
_CASE_PROC = None    # 当前正在跑的性能用例子进程 (Popen, 独立进程组)
_CASE_STOP = threading.Event()   # 请求停止当前用例
_ACTIVE = {"project_id": None, "case_id": None}   # 当前正在跑的用例（精确停止用）


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
    """原子落盘：每个写者用唯一临时文件，避免并发写互相覆盖/抢同名 tmp。"""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_PATH) or ".",
                               prefix=".web_state_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_PATH)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def update_state(mutator):
    """锁内「读-改-写」web_state.json，所有状态变更走这里防丢失更新。

    mutator(st) 就地修改 st；返回值作为本函数返回值。
    """
    with STATE_LOCK:
        st = load_state()
        r = mutator(st)
        save_state(st)
        return r


def effective_config():
    cfg = runner.load_config(CONFIG_PATH)
    st = load_state()
    for sec in ("service", "request", "benchmark", "accuracy", "functional"):
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
    """取用例列表：state 有则用 state，否则从 config.yaml 派生。

    注意用 `is not None` 判断——空列表是合法状态（用户删光了用例），
    当假值会让 config 场景全部复活且 add 报「已存在」。
    """
    st = st or load_state()
    if st.get("cases") is not None:
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
    with STATE_LOCK:
        snapshot = {k: dict(v) for k, v in list(_TASKS.items())}
        st = load_state()
        st["tasks"] = snapshot
        save_state(st)


def _set_task(key, **kw):
    with STATE_LOCK:
        t = _TASKS.setdefault(key, {"key": key})
        t.update(kw)
        snapshot = {k: dict(v) for k, v in list(_TASKS.items())}
        st = load_state()
        st["tasks"] = snapshot
        save_state(st)


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
    """返回每个用例（gsm8k + random）的数据集状态（含复用信息）。

    random 现在也用 gen_dataset 脚本本地生成定长数据集，与 gsm8k 同链路。
    """
    cases = get_cases(st)
    seen = {}
    out = []
    for c in cases:
        if c.get("dataset") not in ("gsm8k", "random"):
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
    _ACTIVE["project_id"], _ACTIVE["case_id"] = project_id, case_id
    _set_task(key, status="running", started=_now(), finished=None, error=None,
              log_path=log_path, result_dir=os.path.join(RESULTS_DIR, case_id),
              case_id=case_id, project_id=project_id, phase="准备中")

    with open(log_path, "a", encoding="utf-8", buffering=1) as logf:
        def log(msg):
            logf.write(msg + "\n")
            logf.flush()

        def _stopped():
            # 精确判断：只有在停的是本工程/本用例时才认（_STOP_FLAG 为整体停止仍生效）
            active_mine = (_ACTIVE.get("project_id") == project_id
                           and _ACTIVE.get("case_id") == case_id)
            return _STOP_FLAG.is_set() or (_CASE_STOP.is_set() and active_mine)

        def progress(stage, detail=""):
            """预热阶段进度 -> task 状态（页面可观测）。尽力而为，不中断主流程。"""
            try:
                _set_task(key, phase=stage, phase_detail=detail)
            except Exception:
                pass

        try:
            # 预检 tokenizer 路径（避免 modelscope 404）
            try:
                runner.check_tokenizer_path(cfg)
            except FileNotFoundError as e:
                _set_task(key, status="error", finished=_now(), error=str(e))
                log(f"[{case_id}] {e}")
                return

            # hixl 建链（工程本轮首个用例前执行一次）
            if not _HIXL_DONE.is_set():
                _set_task(key, phase="hixl 建链", phase_detail="1k-1k / 并发 512 / 请求 1024 / warmup 512")
                log(f"[{case_id}] hixl 建链预热 ...")
                hixl_ds = _ensure_hixl_dataset(cfg, logf)
                if hixl_ds:
                    ok = runner.run_hixl_warmup(cfg, hixl_ds, log_handle=logf,
                                                stop_check=_stopped)
                    log(f"[{case_id}] hixl 建链 {'完成' if ok else '失败(继续)'}")
                else:
                    log(f"[{case_id}] hixl 建链数据集生成失败, 跳过")
                _HIXL_DONE.set()
            if _stopped():
                _set_task(key, status="stopped", finished=_now())
                log(f"[{case_id}] hixl 建链后停止")
                return

            _set_task(key, phase="数据集准备")
            dataset_path = None
            if c["dataset"] in ("gsm8k", "random"):
                log(f"[{case_id}] 确保数据集存在 ...")
                dataset_path = runner.ensure_gsm8k_dataset(cfg, c, force=False, log_handle=logf)
                log(f"[{case_id}] 数据集: {dataset_path}")
            if _stopped():
                _set_task(key, status="stopped", finished=_now())
                log(f"[{case_id}] 数据集准备后停止")
                return

            # prefix 命中率 > 0: 正式摸高前先做前缀预热（阶梯并发, 页面可观测）
            if (c.get("hit_rate") or 0) > 0 and c["dataset"] in ("gsm8k", "random"):
                prefix_prompts = runner.ensure_prefix_prompts(cfg, c, log_handle=logf)
                if prefix_prompts:
                    log(f"[{case_id}] 前缀预热: {prefix_prompts}")
                    runner.run_prefix_warmup(cfg, c, prefix_prompts,
                                             log_handle=logf, progress_cb=progress,
                                             stop_check=_stopped)
                else:
                    log(f"[{case_id}] 前缀数据集缺失, 跳过前缀预热")
            if _stopped():
                _set_task(key, status="stopped", finished=_now())
                log(f"[{case_id}] 前缀预热后停止")
                return

            _set_task(key, phase="摸高测试")
            cmd = runner.build_evalscope_cmd(cfg, c, dataset_path)
            log(f"[{case_id}] 命令: " + " ".join(cmd))
            log(f"[{case_id}] 开始 {_now()}")
            global _CASE_PROC
            # 注意: 不要在此 clear _CASE_STOP —— 预热阶段若已被请求停止，这里会被清掉导致误跑
            # start_new_session: 独立进程组, stop 时 killpg 一次杀干净 (evalscope 还会派生 worker)
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
            _CASE_PROC = (project_id, proc)
            r = proc.wait()
            _CASE_PROC = None
            if _stopped():
                _set_task(key, status="stopped", finished=_now())
                log(f"[{case_id}] 已手动停止")
            elif r == 0:
                res = _result_of(case_id)
                _set_task(key, status="done", finished=_now(), summary_path=res["summary_path"])
                log(f"[{case_id}] 完成 {_now()}")
            else:
                _set_task(key, status="error", finished=_now(), error=f"exit code {r}")
                log(f"[{case_id}] 失败 exit={r}")
        except Exception as e:
            _set_task(key, status="error", finished=_now(), error=str(e))
            log(f"[{case_id}] 异常: {e}")
        finally:
            # 用例结束（无论何种路径）：清掉当前活跃标记与停止标记，避免影响后续工程/用例
            if _ACTIVE.get("project_id") == project_id and _ACTIVE.get("case_id") == case_id:
                _ACTIVE["project_id"], _ACTIVE["case_id"] = None, None
                _CASE_STOP.clear()


_HIXL_DONE = threading.Event()   # 工程本轮 hixl 建链只做一次


def _ensure_hixl_dataset(cfg: dict, log_handle=None) -> str:
    """hixl 建链用 1k-1k 数据集（gen_dataset 脚本, prefix_flag=0, 1024 条）。"""
    sc = {"dataset": "random", "input_len": 1024, "output_len": 1024,
          "hit_rate": 0, "prefix_num": 1, "id": "_hixl_1k"}
    try:
        return runner.ensure_gsm8k_dataset(cfg, sc, force=False, log_handle=log_handle)
    except Exception:
        return None


def _run_worker():
    global _RUN_THREAD
    while True:
        with STATE_LOCK:
            if not _RUN_QUEUE:
                _RUN_THREAD = None
                return
            project_id, case_id = _RUN_QUEUE.pop(0)
        if _STOP_FLAG.is_set():
            # 停止请求：被弹出的项要打标 stopped，否则永久卡 pending
            _set_task(_task_key(project_id, case_id), status="stopped", finished=_now())
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
        _CASE_STOP.clear()
        _HIXL_DONE.clear()   # 新一轮工程启动: 重新做 hixl 建链
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
        gt = _GEN_TASKS.get(c["id"])
        c["dataset_gen_status"] = gt.get("status", "idle") if gt else "idle"
        c["dataset_gen_error"] = gt.get("error") if gt else None
    return {"cases": cases}


@app.post("/api/cases")
def api_set_cases(payload: dict = Body(...)):
    """整体替换用例列表（启停由前端提交完整列表）。

    数据集条数在添加用例时固化：带 raw 重新解析时保留该 id 已有的
    dataset_num（要改条数 = 新增一个用例）。
    """
    def _mutate(st):
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
                # 保留固化条数：raw 重解析会丢 dataset_num，从既有用例补回
                old = existing.get(c["id"])
                if old is not None and old.get("dataset_num") is not None:
                    c["dataset_num"] = old["dataset_num"]
            c["enabled"] = bool(rc.get("enabled", True))
            parsed.append(c)
        st["cases"] = parsed
        return {"ok": True, "count": len(parsed)}
    return update_state(_mutate)


@app.post("/api/cases/add")
def api_case_add(payload: dict = Body(...)):
    raw = payload.get("raw", "")
    try:
        c = case_from_raw(raw)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"格式错误: {e}"}, status_code=400)
    dataset_num = payload.get("dataset_num")
    if dataset_num not in (None, ""):
        try:
            c["dataset_num"] = int(dataset_num)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": f"dataset_num 必须是整数: {dataset_num!r}"}, status_code=400)
    # dataset_num 进 ID：仅条数不同的用例是不同用例（结果目录/数据集都按条数隔离）
    if c.get("dataset_num"):
        c["id"] = f"{c['id']}_num{c['dataset_num']}"

    def _mutate(st):
        cases = get_cases(st)
        if any(x["id"] == c["id"] for x in cases):
            return JSONResponse({"ok": False, "error": f"用例已存在: {c['id']}"}, status_code=400)
        cases.append(c)
        st["cases"] = cases
        return {"ok": True, "case": c}
    return update_state(_mutate)


_ID_RE = re.compile(r"^[0-9A-Za-z_.\-]{1,120}$")


def _safe_id(x):
    """校验 id 防路径穿越/glob 注入（拒绝 / .. * 等）。"""
    if not isinstance(x, str) or not _ID_RE.match(x):
        return None
    return x


@app.post("/api/cases/delete")
def api_case_delete(payload: dict = Body(...)):
    cid = _safe_id(payload.get("id"))
    if cid is None:
        return JSONResponse({"ok": False, "error": "非法用例 ID"}, status_code=400)

    def _mutate(st):
        cases = [c for c in get_cases(st) if c["id"] != cid]
        st["cases"] = cases
        return {"ok": True}
    return update_state(_mutate)


# ----- 数据集 -----
@app.get("/api/datasets")
def api_datasets():
    cfg, st = effective_config()
    return {"datasets": dataset_status(cfg, st)}


@app.post("/api/datasets/generate")
def api_datasets_generate(payload: dict = Body(default={})):
    """生成数据集：payload.case_ids=指定用例；否则生成所有未生成的 gsm8k/random 用例。

    random 与 gsm8k 同链路（gen_dataset 脚本本地生成定长数据）。
    生成状态追踪在 _GEN_TASKS[case_id] 中，前端据此禁用按钮。
    """
    cfg, st = effective_config()
    force = bool(payload.get("force"))
    case_ids = payload.get("case_ids")
    cases = get_cases(st)
    todo = {}
    with _GEN_LOCK:
        for c in cases:
            if c.get("dataset") not in ("gsm8k", "random"):
                continue
            if case_ids is not None and c["id"] not in case_ids:
                continue
            # 去重竞态修复：在端点内（起线程之前）就置 generating，双击第二次直接跳过
            gt = _GEN_TASKS.get(c["id"])
            if gt and gt.get("status") == "generating":
                continue
            jsonl, prompts = runner.gsm8k_dataset_paths(cfg, c)
            if os.path.exists(prompts) and not force:
                continue  # 已生成且非强制 -> 跳过
            todo[os.path.basename(prompts)] = c
            _GEN_TASKS[c["id"]] = {"status": "generating", "error": None}

    def gen_all():
        for fn, c in todo.items():
            cid = c["id"]
            log_path = os.path.join(LOGS_DIR, f"gen_{cid}_{time.strftime('%Y%m%d_%H%M%S')}.log")
            with open(log_path, "a", encoding="utf-8", buffering=1) as lf:
                try:
                    lf.write(f"[dataset {cid}] 生成中 num={runner.dataset_number(cfg, c)} force={force}\n")
                    runner.ensure_gsm8k_dataset(cfg, c, force=force, log_handle=lf)
                    lf.write(f"[dataset {cid}] 完成\n")
                    _GEN_TASKS[cid] = {"status": "done", "error": None}
                except Exception as e:
                    lf.write(f"[dataset {cid}] 失败: {e}\n")
                    _GEN_TASKS[cid] = {"status": "error", "error": str(e)}

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
                    "running": statuses.count("running") + statuses.count("pending"),
                    "is_running": any(s in ("running", "pending") for s in statuses),
                    "all_done": bool(statuses) and all(s in ("done", "error", "stopped") for s in statuses),
                    "has_started": any(s != "not_started" for s in statuses)})
    return {"projects": out}


@app.post("/api/projects")
def api_project_add(payload: dict = Body(...)):
    def _mutate(st):
        name = payload.get("name")
        if not isinstance(name, str):
            name = ""
        name = name.strip() or f"工程{len(st.get('projects') or []) + 1}"
        case_ids = payload.get("case_ids")
        if case_ids is None:
            case_ids = [c["id"] for c in get_cases(st) if c.get("enabled")]
        # id 加毫秒+随机段：同秒同名工程不再冲突
        pid = (re.sub(r"[^0-9A-Za-z_\-]", "_", name)[:40] + "_"
               + time.strftime("%H%M%S") + f"{int(time.time() * 1000) % 1000:03d}"
               + uuid.uuid4().hex[:4])
        st.setdefault("projects", []).append({"id": pid, "name": name, "case_ids": list(case_ids),
                                              "created": _now()})
        return {"ok": True, "project": {"id": pid, "name": name}}
    return update_state(_mutate)


@app.post("/api/projects/delete")
def api_project_delete(payload: dict = Body(...)):
    pid = payload.get("id")
    def _mutate(st):
        st["projects"] = [p for p in (st.get("projects") or []) if p["id"] != pid]
        return {"ok": True}
    return update_state(_mutate)


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
            "phase": t.get("phase"), "phase_detail": t.get("phase_detail"),
            "started": t.get("started"), "finished": t.get("finished"), "error": t.get("error"),
            "log_path": t.get("log_path") or _latest_case_log(pid, cid),
            "result_dir": os.path.join(RESULTS_DIR, cid),
        })
    return {"project": p, "cases": items}


@app.get("/api/projects/{pid}/case/{cid}")
def api_project_case_detail(pid: str, cid: str):
    # id 校验防路径穿越（cid 会拼进 glob 与文件路径）
    if _safe_id(cid) is None:
        return JSONResponse({"ok": False, "error": "非法用例 ID"}, status_code=400)
    res = _result_of(cid)
    t = _TASKS.get(_task_key(pid, cid), {})
    per_logs = sorted(glob.glob(os.path.join(res["result_dir"], "*", "*", "sla_tuning", "*", "benchmark.log")))
    # 附带用例 SLA 阈值供前端判定达标
    st = load_state()
    c = next((x for x in get_cases(st) if x.get("id") == cid), {})
    return {"case_id": cid, "task": t, "summary_path": res["summary_path"],
            "rows": res["rows"], "result_dir": res["result_dir"],
            "log_path": t.get("log_path") or _latest_case_log(pid, cid),
            "per_concurrency_logs": per_logs,
            "ttft_sla_ms": c.get("ttft_sla_ms"), "tpot_sla_ms": c.get("tpot_sla_ms")}


@app.post("/api/projects/{pid}/run")
def api_project_run(pid: str, payload: dict = Body(default={})):
    st = load_state()
    p = next((x for x in (st.get("projects") or []) if x["id"] == pid), None)
    if not p:
        return JSONResponse({"ok": False, "error": "工程不存在"}, status_code=404)
    cids = payload.get("case_ids") or p.get("case_ids", [])
    items = [(pid, cid) for cid in cids]
    # 不在此覆盖状态（会把正在 running 的用例重置成 pending）；enqueue_runs 内部已做
    # “非 running 才置 pending，已在队列不重复入队”的保护。
    enqueue_runs(items)
    return {"ok": True, "queued": [c for _, c in items]}


@app.post("/api/projects/{pid}/stop")
def api_project_stop(pid: str):
    global _CASE_PROC
    # 只停本工程：从队列清掉本 pid 的项，杀本 pid 正在跑的用例。
    # 不碰全局 _STOP_FLAG / 不误杀其他工程。
    with STATE_LOCK:
        _RUN_QUEUE[:] = [it for it in _RUN_QUEUE if it[0] != pid]
    cp = _CASE_PROC
    # 用例正在跑（摸高 或 预热阶段）—— 只要活跃用例属于本工程就打停止标记：
    # 预热阶段 _CASE_PROC 尚未赋值(None)，靠 _ACTIVE 判断；_stopped() 读 _CASE_STOP+active
    if _ACTIVE.get("project_id") == pid:
        _CASE_STOP.set()
        # 杀正在跑的用例子进程树（摸高阶段才有）
        if cp and cp[0] == pid:
            _kill_tree(cp[1])
            _CASE_PROC = None
    st = load_state()
    p = next((x for x in (st.get("projects") or []) if x["id"] == pid), None)
    if p:
        for cid in p.get("case_ids", []):
            t = _TASKS.get(_task_key(pid, cid), {})
            if t.get("status") in ("pending", "running"):
                _set_task(_task_key(pid, cid), status="stopped", finished=_now())
    return {"ok": True}


@app.get("/api/log")
def api_log(path: str, offset: int = 0, tail: int = 0):
    """读取日志文件。offset=从第几行开始(0-based)；tail=只返回最后 N 行(与 offset 互斥)。

    offset=0 时返回文件**前** 800 行（不是最后 800 行）——前端按「已读行号」
    递增轮询，若首屏给尾部会导致 offset 语义错位、重复追加。
    """
    ap = os.path.abspath(path)
    B = os.path.abspath(KIT_ROOT)
    if not (ap == B or ap.startswith(B + os.sep)):
        return JSONResponse({"ok": False, "error": "路径越界"}, status_code=400)
    if not os.path.isfile(ap):
        return {"ok": False, "error": "文件不存在", "lines": [], "total": 0}
    with open(ap, "r", encoding="utf-8", errors="replace") as f:
        all_lines = f.readlines()
    total = len(all_lines)
    if tail > 0:
        return {"ok": True, "lines": all_lines[-tail:], "total": total}
    if offset > 0:
        return {"ok": True, "lines": all_lines[offset:offset + 800], "total": total, "offset": offset}
    return {"ok": True, "lines": all_lines[:800], "total": total, "offset": 0}


# ----------------------------------------------------- 功能/精度/AgentX 测试 -------
_FUNC_TASK = {}   # {status, started, finished, log_path, error, report_path, pid}
_ACC_TASK = {}
_AGENTX_TASK = {}
_FUNC_PROC = None   # subprocess.Popen
_ACC_PROC = None
_AGENTX_PROC = None


def _kill_tree(proc):
    """杀整棵进程树。子进程用 start_new_session 起在独立进程组，killpg 一次清干净。

    主进程已退出时也尝试 killpg——同组的 worker 可能还活着（孤儿压测）。
    """
    if proc is None:
        return False
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    killed = False
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            killed = True
        except (ProcessLookupError, PermissionError):
            pass
    try:
        if proc.poll() is None:
            proc.kill()
            killed = True
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    return killed


def _run_generic(key, task_dict, cmd, log_prefix, report_parser=None):
    """通用后台执行：功能/精度/AgentX 测试共用。start_new_session 起独立进程组供 stop 整组杀。

    调用方须已在线程外把 task_dict 置为 running（CAS 防双提交）。
    """
    global _FUNC_PROC, _ACC_PROC, _AGENTX_PROC
    log_path = os.path.join(LOGS_DIR, f"{log_prefix}_{time.strftime('%Y%m%d_%H%M%S')}.log")
    task_dict.update(started=_now(), finished=None, error=None, log_path=log_path)
    with open(log_path, "a", encoding="utf-8", buffering=1) as logf:
        try:
            logf.write(f"[{log_prefix}] 命令: {' '.join(cmd)}\n")
            proc = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                    cwd=KIT_ROOT, start_new_session=True)
            task_dict["pid"] = proc.pid
            if log_prefix == "func":
                _FUNC_PROC = proc
            elif log_prefix == "acc":
                _ACC_PROC = proc
            else:
                _AGENTX_PROC = proc
            # Popen 之后发现已被请求停止：立即杀掉，避免幽灵进程
            if task_dict.get("status") == "stopped":
                _kill_tree(proc)
                if log_prefix == "func": _FUNC_PROC = None
                elif log_prefix == "acc": _ACC_PROC = None
                else: _AGENTX_PROC = None
                return
            r = proc.wait()
            if task_dict.get("status") == "stopped":
                pass   # 已被 stop 标记，不覆盖
            elif r == 0:
                task_dict.update(status="done", finished=_now())
            else:
                task_dict.update(status="error", finished=_now(), error=f"exit code {r}")
            # 精度测试: 运行结束把结果记入历史（web_state accuracy_history）
            if log_prefix == "acc" and task_dict.get("status") in ("done", "error"):
                _record_accuracy_history(task_dict)
        except Exception as e:
            if task_dict.get("status") != "stopped":
                task_dict.update(status="error", finished=_now(), error=str(e))

def _build_service_cmd(script, extra_args=None):
    """构建功能/精度测试命令，从 effective_config 取 url/model/api_key。"""
    cfg, _ = effective_config()
    svc = cfg.get("service", {})
    req = cfg.get("request", {})
    cmd = ["python3", script, "--config", CONFIG_PATH]
    if svc.get("url"):
        cmd += ["--base-url", svc["url"].replace("/v1/chat/completions", "")]
    if svc.get("model"):
        cmd += ["--model", svc["model"]]
    api_key = req.get("api_key", "")
    if api_key:
        cmd += ["--api-key", api_key]
    # Host header
    if req.get("add_host_header"):
        host = req.get("host_header") or ""
        if not host:
            from urllib.parse import urlsplit
            host = urlsplit(svc.get("url") or "").hostname or ""
        if host:
            cmd += ["--header", f"Host={host}"]
    if extra_args:
        cmd += extra_args
    return cmd


@app.get("/api/functional/status")
def api_func_status():
    return dict(_FUNC_TASK) if _FUNC_TASK else {"status": "not_started"}

@app.post("/api/functional/run")
def api_func_run(payload: dict = Body(default={})):
    # CAS 防双提交：锁内检查并置 running，成功才起线程
    with STATE_LOCK:
        if _FUNC_TASK.get("status") == "running":
            return JSONResponse({"ok": False, "error": "功能测试正在运行中"}, status_code=409)
        _FUNC_TASK.clear()
        _FUNC_TASK.update(status="running")
    cfg, _ = effective_config()
    extra = []
    fcfg = cfg.get("functional", {})
    if fcfg.get("skip_long_context_api"):
        extra.append("--skip-long-context-api")
    cmd = _build_service_cmd(os.path.join("scripts", "run_api_e2e.py"), extra)
    threading.Thread(target=_run_generic, args=("func", _FUNC_TASK, cmd, "func"), daemon=True).start()
    return {"ok": True}

@app.post("/api/functional/stop")
def api_func_stop():
    global _FUNC_PROC
    if _FUNC_TASK.get("status") != "running":
        return JSONResponse({"ok": False, "error": "功能测试未在运行"}, status_code=409)
    killed = _kill_tree(_FUNC_PROC)
    _FUNC_TASK.update(status="stopped", finished=_now())
    _FUNC_PROC = None
    return {"ok": True, "killed": killed}

@app.get("/api/functional/report")
def api_func_report():
    """读最新功能测试 markdown 报告。"""
    base = os.path.join(RESULTS_DIR, "functional")
    reports = sorted(glob.glob(os.path.join(base, "**", "report.md"), recursive=True))
    if not reports:
        return {"ok": False, "report": None}
    path = reports[-1]
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return {"ok": True, "report": text, "path": path}

@app.get("/api/functional/list")
def api_func_list():
    """列出功能测试用例（--list）。"""
    cmd = ["python3", os.path.join(KIT_ROOT, "scripts", "run_api_e2e.py"), "--config", CONFIG_PATH, "--list"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=KIT_ROOT)
        return {"ok": True, "lines": (r.stdout or "").splitlines()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/functional/cases")
def api_func_cases():
    """返回每个功能测试用例的 curl 命令，供前端复制手动执行。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_api_e2e",
        os.path.join(KIT_ROOT, "scripts", "run_api_e2e.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    cfg, _ = effective_config()
    svc = cfg.get("service", {})
    req = cfg.get("request", {})
    # 构建 Config 对象
    raw_base = svc.get("url", "")
    base_url = mod.normalize_base_url(mod.base_url_of(raw_base))
    api_key = req.get("api_key", "") or ""
    headers = mod.headers_from_kit_config(cfg)
    if req.get("add_host_header"):
        host = req.get("host_header") or ""
        if not host:
            from urllib.parse import urlsplit
            host = urlsplit(raw_base).hostname or ""
        if host:
            headers["Host"] = host
    for item in req.get("extra_headers") or []:
        if "=" in str(item):
            k, _, v = str(item).partition("=")
        else:
            k, _, v = str(item).partition(":")
        if k.strip():
            headers[k.strip()] = v.strip()

    config = mod.Config(
        base_url=base_url, api_key=api_key, model=svc.get("model", ""),
        interface_types=["openai"], openai_api_version="v1",
        messages_api_version="v1", gemini_api_version="v1beta",
        embedding_api_version="v1", gemini_interactions_api_revision="2025-04-21",
        headers=headers, timeout_seconds=300,
        report_path=None, print_logs=False, log_max_chars=12000, verify_ssl=True,
        skip_long_context_api=True, long_context_api_tokens=8192,
    )
    cases = mod.build_cases(config)
    out = []
    for c in cases:
        url = mod.endpoint_url(base_url, c.endpoint)
        hdrs = mod.request_headers(config, c.extra_headers)
        parts = ["curl -sS"]
        if c.method != "GET":
            parts += ["-X", c.method]
        for k, v in hdrs.items():
            parts += ["-H", f'"{k}: {v}"']
        if c.payload is not None:
            body = mod.compact_json(c.payload)
            parts += ["-d", "'" + body.replace("'", "'\\''") + "'"]
        parts.append(f'"{url}"')
        curl = " ".join(parts)
        out.append({
            "category": c.category, "name": c.name,
            "tags": c.tags, "endpoint": c.endpoint,
            "method": c.method, "stream": c.stream,
            "curl": curl,
        })
    return {"ok": True, "cases": out}


@app.get("/api/accuracy/status")
def api_acc_status():
    return dict(_ACC_TASK) if _ACC_TASK else {"status": "not_started"}

@app.get("/api/accuracy/config")
def api_acc_config():
    """读精度数据集组配置 + 可用数据集列表。"""
    cfg, st = effective_config()
    acc = cfg.get("accuracy") or {}
    groups = []
    all_datasets = set()
    for line in acc.get("datasets") or []:
        try:
            g = _parse_acc_group(line)
            g["enabled"] = True
            groups.append(g)
            all_datasets.update(g["datasets"])
        except Exception:
            continue
    # 可用数据集：override 后的 + config.yaml 原始的（防止 override 覆盖后丢失全集）
    raw_cfg = runner.load_config(CONFIG_PATH)
    raw_acc = raw_cfg.get("accuracy") or {}
    for line in raw_acc.get("datasets") or []:
        try:
            g = _parse_acc_group(line)
            all_datasets.update(g["datasets"])
        except Exception:
            continue
    return {"ok": True, "groups": groups,
            "available_datasets": sorted(all_datasets),
            "proxy": acc.get("proxy", ""),
            "evalscope_bin": acc.get("evalscope_bin", "evalscope"),
            "from_override": bool(st.get("accuracy_override"))}

def _parse_acc_group(line: str) -> dict:
    """复用 accuracy_runner.parse_dataset_line 解析组行。"""
    import accuracy_runner
    g = accuracy_runner.parse_dataset_line(line)
    return g

@app.post("/api/accuracy/config")
def api_acc_set_config(payload: dict = Body(...)):
    """保存精度数据集组到 web_state.json accuracy_override（不写 config.yaml）。"""
    st = load_state()
    groups = payload.get("groups")
    if groups is None:
        return JSONResponse({"ok": False, "error": "缺少 groups"}, status_code=400)
    lines = []
    for g in groups:
        line = g.get("raw")
        if not line:
            # 从字段拼组行: tag, datasets, rounds, effort, max_tokens, batch, k
            line = ", ".join([
                str(g.get("tag") or "").strip(),
                " ".join(g.get("datasets") or []),
                str(g.get("rounds") or 1),
                str(g.get("effort") or "max"),
                str(g.get("max_tokens") or 131072),
                str(g.get("batch_size") or 32),
                str(g.get("k") or 1),
            ])
        try:
            _parse_acc_group(line)  # 校验
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"组行格式错误: {line} ({e})"}, status_code=400)
        lines.append(line)
    st["accuracy_override"] = {"datasets": lines}
    if "proxy" in payload:
        st["accuracy_override"]["proxy"] = payload["proxy"]
    save_state(st)
    return {"ok": True, "count": len(lines)}

@app.post("/api/accuracy/run")
def api_acc_run(payload: dict = Body(default={})):
    # CAS 防双提交：锁内检查并置 running，成功才继续构建命令
    with STATE_LOCK:
        if _ACC_TASK.get("status") == "running":
            return JSONResponse({"ok": False, "error": "精度测试正在运行中"}, status_code=409)
        _ACC_TASK.clear()
        _ACC_TASK.update(status="running")
    # 用户本次运行选的数据集只在内存生效（临时 YAML），不写 web_state——
    # 否则会把整组配置覆盖成只剩本次选中的数据集，刷新后丢失其余数据集。
    cfg, _ = effective_config()
    datasets = payload.get("datasets")
    if datasets and isinstance(datasets, list) and len(datasets) > 0:
        k = payload.get("k") or 4
        rounds = payload.get("rounds") or 1
        effort = payload.get("effort") or "max"
        max_tokens = payload.get("max_tokens") or 131072
        batch = payload.get("batch_size") or 32
        tag = "web_" + "_".join(datasets)
        line = f"{tag}, {' '.join(datasets)}, {rounds}, {effort}, {max_tokens}, {batch}, {k}"
        # 仅覆盖本次运行用的 cfg 副本，不落 web_state
        cfg.setdefault("accuracy", {})["datasets"] = [line]
    # 临时配置文件：放 TMPDIR（不落工程根），运行结束 finally 删 + Timer 兜底
    import tempfile, yaml as _yaml
    tmp_dir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    tmp_cfg = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False,
                                          dir=tmp_dir, encoding="utf-8")
    _yaml.dump(cfg, tmp_cfg, allow_unicode=True, default_flow_style=False)
    tmp_cfg.close()
    cmd = ["python3", os.path.join(KIT_ROOT, "accuracy_runner.py"),
           "--config", tmp_cfg.name]
    k = payload.get("k")
    if k and not datasets:
        cmd += ["--k", str(int(k))]
    only = payload.get("only")
    if only:
        cmd += ["--only", only]
    limit = payload.get("limit")
    if limit:
        cmd += ["--limit", str(int(limit))]
    def _cleanup():
        try: os.unlink(tmp_cfg.name)
        except OSError: pass
    def _run_and_cleanup():
        try:
            _run_generic("acc", _ACC_TASK, cmd, "acc")
        finally:
            _cleanup()
    threading.Thread(target=_run_and_cleanup, daemon=True).start()
    threading.Timer(3600, _cleanup).start()   # 兜底：防 finally 意外未执行
    return {"ok": True}

@app.post("/api/accuracy/stop")
def api_acc_stop():
    global _ACC_PROC
    if _ACC_TASK.get("status") != "running":
        return JSONResponse({"ok": False, "error": "精度测试未在运行"}, status_code=409)
    killed = _kill_tree(_ACC_PROC)
    _ACC_TASK.update(status="stopped", finished=_now())
    _ACC_PROC = None
    return {"ok": True, "killed": killed}

@app.get("/api/accuracy/report")
def api_acc_report():
    """读 results/accuracy/scores.md。"""
    path = os.path.join(RESULTS_DIR, "accuracy", "scores.md")
    if not os.path.exists(path):
        return {"ok": False, "report": None}
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return {"ok": True, "report": text, "path": path}

@app.get("/api/accuracy/list")
def api_acc_list():
    cmd = ["python3", os.path.join(KIT_ROOT, "accuracy_runner.py"), "--config", CONFIG_PATH, "--list"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, cwd=KIT_ROOT)
        return {"ok": True, "lines": (r.stdout or "").splitlines()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ----------------------------------------------------- AgentX 智能体回放 -------
@app.get("/api/agentx/status")
def api_agentx_status():
    return dict(_AGENTX_TASK) if _AGENTX_TASK else {"status": "not_started"}


def _record_accuracy_history(task_dict: dict):
    """精度运行结束：解析最新 work-dir 的分数，追加到 web_state accuracy_history。"""
    try:
        acc_dir = os.path.join(RESULTS_DIR, "accuracy")
        # 找本次运行更新的 work-dir（started 之后修改的）
        work_dirs = []
        for d in glob.glob(os.path.join(acc_dir, "*_r*")):
            if os.path.getmtime(d) >= time.time() - 60 * 60 * 24:  # 24h 内
                work_dirs.append(d)
        if not work_dirs:
            return
        work_dir = max(work_dirs, key=os.path.getmtime)
        tag = os.path.basename(work_dir)
        # 解析 reports/<model>/<ds>.json 的分数
        scores = {}
        for rep in glob.glob(os.path.join(work_dir, "reports", "*", "*.json")):
            ds = os.path.basename(rep).rsplit(".", 1)[0]
            try:
                with open(rep, encoding="utf-8") as f:
                    data = json.load(f)
                for m in data.get("metrics", []):
                    ident = m.get("identity", {})
                    agg = ident.get("aggregation")
                    if agg == "mean":
                        scores.setdefault(ds, {})["mean"] = m.get("score")
                        scores.setdefault(ds, {})["num"] = m.get("num")
                    elif agg == "pass_at_k":
                        k = (ident.get("dimensions") or {}).get("k")
                        if k is not None:
                            scores.setdefault(ds, {})[f"pass@{k}"] = m.get("score")
            except Exception:
                continue
        if not scores:
            return
        st = load_state()
        st.setdefault("accuracy_history", []).append({
            "time": _now(),
            "tag": tag,
            "work_dir": work_dir,
            "log_path": task_dict.get("log_path"),
            "status": task_dict.get("status"),
            "scores": scores,
        })
        # 封顶 100 条
        if len(st["accuracy_history"]) > 100:
            st["accuracy_history"] = st["accuracy_history"][-100:]
        save_state(st)
    except Exception:
        pass  # 历史记录失败不影响主流程


@app.get("/api/accuracy/history")
def api_acc_history():
    """精度测试历史记录列表。"""
    st = load_state()
    return {"ok": True, "history": list(reversed(st.get("accuracy_history") or []))}

@app.get("/api/agentx/config")
def api_agentx_config():
    """读 AgentX 变体配置（config.yaml agentx.datasets）。"""
    cfg, st = effective_config()
    ax = cfg.get("agentx") or {}
    variants = []
    for line in ax.get("datasets") or []:
        line = str(line).strip()
        if not line or line.startswith("#"):
            continue
        try:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            variants.append({
                "tag": parts[0], "dataset": parts[1],
                "concurrency": int(parts[2]),
                "tokenizer": parts[3] if len(parts) > 3 else "",
                "raw": line,
            })
        except (ValueError, IndexError):
            continue
    return {"ok": True, "variants": variants,
            "from_override": bool(st.get("agentx_override"))}

@app.post("/api/agentx/run")
def api_agentx_run(payload: dict = Body(default={})):
    # CAS 防双提交
    with STATE_LOCK:
        if _AGENTX_TASK.get("status") == "running":
            return JSONResponse({"ok": False, "error": "AgentX 测试正在运行中"}, status_code=409)
        _AGENTX_TASK.clear()
        _AGENTX_TASK.update(status="running")
    # effective_config 写临时 YAML（与精度测试同模式，保证读到页面配置）
    cfg, _ = effective_config()
    import tempfile, yaml as _yaml
    tmp_dir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    tmp_cfg = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False,
                                          dir=tmp_dir, encoding="utf-8")
    _yaml.dump(cfg, tmp_cfg, allow_unicode=True, default_flow_style=False)
    tmp_cfg.close()
    cmd = ["python3", os.path.join(KIT_ROOT, "agentx_runner.py"), "--config", tmp_cfg.name]
    only = payload.get("only")
    if only:
        cmd += ["--only", only]
    conc = payload.get("concurrency")
    if conc:
        cmd += ["--concurrency", str(int(conc))]
    def _cleanup():
        try: os.unlink(tmp_cfg.name)
        except OSError: pass
    def _run_and_cleanup():
        try:
            _run_generic("agentx", _AGENTX_TASK, cmd, "agentx")
        finally:
            _cleanup()
    threading.Thread(target=_run_and_cleanup, daemon=True).start()
    threading.Timer(3600, _cleanup).start()
    return {"ok": True}

@app.post("/api/agentx/stop")
def api_agentx_stop():
    global _AGENTX_PROC
    if _AGENTX_TASK.get("status") != "running":
        return JSONResponse({"ok": False, "error": "AgentX 测试未在运行"}, status_code=409)
    killed = _kill_tree(_AGENTX_PROC)
    _AGENTX_TASK.update(status="stopped", finished=_now())
    _AGENTX_PROC = None
    return {"ok": True, "killed": killed}

@app.get("/api/agentx/report")
def api_agentx_report():
    """读最新 AgentX 运行的实时指标（从日志尾部解析 records_manager 指标行）。"""
    log_path = _AGENTX_TASK.get("log_path")
    if not log_path or not os.path.exists(log_path):
        # 找最新的 agentx 日志
        pats = sorted(glob.glob(os.path.join(LOGS_DIR, "agentx_*.log")))
        log_path = pats[-1] if pats else None
    if not log_path or not os.path.exists(log_path):
        return {"ok": False, "report": None}
    # 从日志尾部提取最近一次 realtime 指标块
    metrics = {}
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    block = []
    for ln in reversed(lines[-500:]):
        if "realtime" in ln and block:
            break
        if any(k in ln for k in ("rps=", "ttft ", "itl ", "e2e ", "isl ", "osl ", "tot ", "trace ", "intvty")):
            block.append(ln.rstrip())
    block.reverse()
    return {"ok": True, "report": "\n".join(block[-12:]), "path": log_path}


# ----------------------------------------------------- 日志打包下载 -------
@app.get("/api/projects/{pid}/pack")
def api_project_pack(pid: str):
    """打包工程所有日志+结果为 zip 下载。

    注意: 结果目录可能很大（GB 级），绝不能整包驻留内存——
    先压缩到 TMPDIR 下的临时文件, 再用 FileResponse 流式回传, 响应结束删除。
    """
    import zipfile, tempfile
    from fastapi.responses import FileResponse
    st = load_state()
    p = next((x for x in (st.get("projects") or []) if x["id"] == pid), None)
    if not p:
        return JSONResponse({"ok": False, "error": "工程不存在"}, status_code=404)

    tmp_dir = os.environ.get("TMPDIR") or tempfile.gettempdir()
    fd, zip_path = tempfile.mkstemp(suffix=".zip", dir=tmp_dir)
    os.close(fd)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 1) 工程用例日志
            for cid in p.get("case_ids", []):
                for log in sorted(glob.glob(os.path.join(LOGS_DIR, f"proj_{pid}_{cid}_*.log"))):
                    zf.write(log, os.path.relpath(log, KIT_ROOT))
            # 2) 结果目录
            for cid in p.get("case_ids", []):
                res_dir = os.path.join(RESULTS_DIR, cid)
                if os.path.isdir(res_dir):
                    for root, _, files in os.walk(res_dir):
                        for f in files:
                            full = os.path.join(root, f)
                            zf.write(full, os.path.relpath(full, KIT_ROOT))
            # 3) web_state 中的 task 信息
            tasks = {k: v for k, v in (st.get("tasks") or {}).items() if k.startswith(pid + "/")}
            if tasks:
                zf.writestr("web_tasks.json", json.dumps(tasks, ensure_ascii=False, indent=2))
    except Exception as e:
        try: os.unlink(zip_path)
        except OSError: pass
        return JSONResponse({"ok": False, "error": f"打包失败: {e}"}, status_code=500)

    # RFC 5987: filename* 支持 UTF-8（中文工程名），filename 给 ASCII 兜底
    import urllib.parse as _up
    raw_name = f"{p.get('name','project')}_logs.zip"
    ascii_name = raw_name.encode("ascii", "ignore").decode() or "project_logs.zip"
    quoted = _up.quote(raw_name)
    return FileResponse(zip_path, media_type="application/zip", filename=ascii_name,
                        headers={"Content-Disposition":
                                 f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"},
                        background=_FileCleanup(zip_path))


class _FileCleanup:
    """FileResponse 发送完成后删除临时 zip（Starlette BackgroundTask 协议）。"""
    def __init__(self, path):
        self.path = path
    async def __call__(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8300)
    args = ap.parse_args()

    def _mutate(st):
        for k, t in (st.get("tasks") or {}).items():
            if t.get("status") == "running":
                t["status"] = "interrupted"
        return None
    update_state(_mutate)
    with STATE_LOCK:
        for k, t in load_state().get("tasks", {}).items():
            _TASKS[k] = t
    print(f"evalscope 摸高测试控制台: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
