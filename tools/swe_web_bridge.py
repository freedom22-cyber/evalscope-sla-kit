#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SWE web 启停桥接守护（宿主机跑，自包含单文件）。

webapp 跑在 ais-bench 容器内：容器里没有 docker CLI、容器全局 env 也没有
DOCKER_HOST，更看不到宿主侧分批驱动进程——容器内的 /api/swe/run|stop 无法
直接启动/停止评测。本守护在宿主机上轮询共享目录 swe_pro/webctl/ 里的命令
文件，在宿主侧执行后把状态写回 webctl/state.json；webapp 按钮端点只写
命令文件、读 state.json（NFS 共享盘两侧可见，与 tools/docker_unix_relay.py
同类的跨边界守护）。

命令文件（处理后立即删除，防重放）：
  webctl/run.json   {"uuid": ..., "mode": "full"|"rerun", "start_offset",
                     "window", "concurrency", "keep_images", "limit",
                     "instance_file"}
  webctl/stop       空标志文件 → 完整停止序列

run 处理：
  mode=full   宿主侧拉起 swe_pro/run_all_batches.sh 分批驱动
              （START_OFFSET/WINDOW/CONCURRENCY/KEEP_IMAGES 环境变量；
              limit 传入时兼作 TOTAL 上限）
  mode=rerun  调 kit 的 swe_runner.py start-rerun --yes（自带双驱动护栏）
  两种模式都先做守卫（容器内 evalscope eval / 宿主驱动在跑 → 拒绝并写
  state.json），并补拉两件套看守：staged_watch（容器内）+ review_guard（宿主）

stop 序列（把主会话的手动停止流程脚本化，SIGKILL 保数据是关键）：
  1. 宿主 kill -9 run_all_batches / review_guard
  2. 容器 kill -9 evalscope eval / run_swe_bench_pro / staged_watch /
     jest_staged_watch（必须 -9：SIGTERM 会触发 evalscope 的
     discard_review_reruns 把已评分行删掉）
  3. docker rm -f 全部 sandbox-* 沙箱容器（绝不碰 dsv41-mmc /
     aiserver-agent / ais-bench）
  4. 数据抢救（仅停止时确有评测进程在跑才做，幂等空停跳过）：
     recover_reviews_from_workspaces.py 重建 + 从 batches.log 找最后一个
     无 finished 配对的批，diff reviews 得未评分实例，去重后追加 rerun_ids.txt
  5. 停止报告写入 state.json

pgrep 模式一律用括号字符类（如 evalscope eva[l]）防自匹配：模式文本会
出现在 bash -c 命令行里，正则不匹配自己的字面文本。

用法（宿主机；scripts/start_web.sh 会幂等拉起）：
  setsid nohup python3 tools/swe_web_bridge.py >> logs/swe_web_bridge.log 2>&1 &
"""
import glob
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time

# 路径可移植化: KIT_ROOT 从脚本自身位置推导（本机 live 布局下与原硬编码值
# 相同）; SWE_PRO 优先环境变量, 其次仓库内 bootstrap 布局, 最后本机 NFS live
# 布局——live 行为不变, 全新克隆自动用仓库内 swe_pro。
_HERE = os.path.dirname(os.path.abspath(__file__))
KIT_ROOT = os.path.dirname(_HERE)


def _default_swe_pro():
    # 与 kit_paths.resolve_swe_work_dir 同口径: 环境变量优先（SWE_WORK_DIR
    # 或 SWE_WEB_BRIDGE_WORK_DIR）, 其次 kit 相对 results/swe_pro,
    # 最后本机 NFS live 布局
    env = (os.environ.get("SWE_WORK_DIR")
           or os.environ.get("SWE_WEB_BRIDGE_WORK_DIR"))
    if env:
        return env
    for cand in (os.path.join(KIT_ROOT, "results", "swe_pro"),  # kit 相对布局
                 "/mnt/sfs_turbo/c00803992/swe_pro"):           # 本机 NFS live 布局
        if os.path.isdir(cand):
            return cand
    return "/mnt/sfs_turbo/c00803992/swe_pro"


SWE_PRO = _default_swe_pro()
CONTAINER = os.environ.get("SWE_WEB_BRIDGE_CONTAINER", "ais-bench")
VENV_PY = "/tmp/bench-venv/bin/python3"
RECOVER_PY = os.path.join(_HERE, "recover_reviews_from_workspaces.py")

WEBCTL = os.path.join(SWE_PRO, "webctl")
STATE_PATH = os.path.join(WEBCTL, "state.json")
RUN_CMD_PATH = os.path.join(WEBCTL, "run.json")
STOP_FLAG_PATH = os.path.join(WEBCTL, "stop")
LOGS_DIR = os.path.join(SWE_PRO, "logs")
BATCHES_LOG = os.path.join(LOGS_DIR, "batches.log")
RESULTS_DIR = os.path.join(SWE_PRO, "results", "accuracy",
                           "swe_bench_pro_dsv4")
RERUN_IDS = os.path.join(SWE_PRO, "rerun_ids.txt")

# 容器内 evalscope 的数据集 arrow 缓存（datasets.Dataset.from_file 直读，
# 不走网络；ModelScope 侧缓存副本路径不同，这里只认 evalscope 的）
DATASET_ARROW_GLOB = ("/root/.cache/evalscope/datasets/"
                      "ScaleAI_SWE-bench_Pro-*/data-*.arrow")
TOTAL_INSTANCES = 642

P_DRIVER = r"run_all_batches[.]sh"
P_GUARD = r"review_guard[.]sh"
P_EVAL = r"evalscope eva[l]"
P_SCRIPT = r"run_swe_bench_pro[.]sh"
P_WATCH = r"(^|[^_])staged_watch[.]sh"
P_JEST = r"jest_staged_watch[.]sh"
P_BRIDGE = r"swe_web_bridg[e][.]py"
# 容器内批量 kill 的组合模式（一个 docker exec 全杀）
P_CONTAINER_KILL = "|".join([P_EVAL, P_SCRIPT, P_WATCH, P_JEST])

# sandbox 沙箱容器清理的绝对禁区（startswith sandbox- 已排除，双保险）
PROTECTED_CONTAINERS = {"dsv41-mmc", "aiserver-agent", "ais-bench"}

_BATCH_START_RE = re.compile(r"===\s*batch\s+offset=(-?\d+)\s+count=(\d+)\s*===")
_BATCH_DONE_RE = re.compile(r"batch\s+offset=(-?\d+)\s+finished")

# 容器内读数据集 instance_id 的脚本（stdin 喂给 docker exec -i 的 venv python）
_DS_SCRIPT = """
import glob, json, sys
lo, hi = int(sys.argv[1]), int(sys.argv[2])
try:
    from datasets import Dataset
except ImportError as e:
    print(json.dumps({"error": "datasets lib missing: %s" % e}))
    raise SystemExit(0)
pats = sorted(glob.glob('/root/.cache/evalscope/datasets/ScaleAI_SWE-bench_Pro-*/data-*.arrow'))
if not pats:
    print(json.dumps({"error": "arrow cache not found"}))
    raise SystemExit(0)
ds = Dataset.from_file(pats[0])
ids = [ds[i]['instance_id'] for i in range(lo, min(hi, len(ds)))]
print(json.dumps({"rows": len(ds), "ids": ids}))
"""

_STATE = {"ok": True}


def now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def sh(cmd, timeout=30, input_text=None):
    """subprocess.run 包装：异常时返回 (None, '', 错误文本)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           input=input_text, timeout=timeout)
        return r.returncode, r.stdout or "", r.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, "", str(e)


def atomic_write_json(path, data):
    """tmp + os.replace 原子写（NFS 两侧读到的要么旧文件要么完整新文件）。"""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".webctl_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_state():
    _STATE["updated_at"] = now()
    # epoch 秒：webapp（容器）与宿主时区不同（CST vs UTC），跨侧年龄比较
    # 只能用 epoch，本地时间字符串仅作展示
    _STATE["updated_at_ts"] = time.time()
    atomic_write_json(STATE_PATH, _STATE)


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# --------------------------------------------------------------- 进程探测 ----
def host_pgrep(pattern):
    """宿主 pgrep -f → pid 列表；无匹配=[]；pgrep 不可用=None。"""
    rc, out, _ = sh(["pgrep", "-f", pattern], timeout=10)
    if rc == 0:
        return [int(x) for x in out.split() if x.isdigit()]
    if rc == 1:
        return []
    return None


def container_pgrep(pattern):
    """docker exec 进容器命名空间 pgrep；容器故障/docker 不可用=None。"""
    rc, out, _ = sh(["docker", "exec", CONTAINER, "pgrep", "-f", pattern],
                    timeout=20)
    if rc == 0:
        return [int(x) for x in out.split() if x.isdigit()]
    if rc == 1:
        return []
    return None


def container_liveness():
    """一次 docker exec 取容器内 eval / 批内脚本 / staged_watch 计数。"""
    inner = (
        "printf 'eval '; pgrep -fc '%s' || true; "
        "printf 'script '; pgrep -fc '%s' || true; "
        "printf 'watch '; pgrep -fc '%s' || true"
    ) % (P_EVAL, P_SCRIPT, P_WATCH)
    rc, out, _ = sh(["docker", "exec", CONTAINER, "bash", "-c", inner],
                    timeout=20)
    if rc is None or rc != 0:
        return None
    counts = {}
    for line in (out or "").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].strip().isdigit():
            counts[parts[0]] = int(parts[1])
    return counts or None


# ----------------------------------------------------------- run 命令处理 ----
def parse_run_params(data):
    """校验 run.json 参数（webapp 已校验过一轮，这里防直写文件的第二道）。

    返回 (params, None) 或 (None, error)。
    """
    if not isinstance(data, dict):
        return None, "run.json 不是 JSON 对象"
    mode = data.get("mode") or "full"
    if mode not in ("full", "rerun"):
        return None, "mode 必须是 full 或 rerun: %r" % (mode,)

    def _int(key, lo, hi, default):
        v = data.get(key)
        if v in (None, ""):
            return default, None
        try:
            v = int(v)
        except (TypeError, ValueError):
            return None, "%s 必须是整数: %r" % (key, v)
        if not lo <= v <= hi:
            return None, "%s 超出范围 [%d, %d]: %d" % (key, lo, hi, v)
        return v, None

    params = {"mode": mode}
    if mode == "full":
        off, e = _int("start_offset", 0, TOTAL_INSTANCES - 1, 0)
        if e:
            return None, e
        win, e = _int("window", 1, 64, 32)
        if e:
            return None, e
        conc, e = _int("concurrency", 1, 32, 16)
        if e:
            return None, e
        limit, e = _int("limit", 1, TOTAL_INSTANCES, None)
        if e:
            return None, e
        params.update(start_offset=off, window=win, concurrency=conc,
                      limit=limit, keep_images=1 if data.get("keep_images") else 0,
                      instance_file=data.get("instance_file") or None)
    else:
        conc, e = _int("concurrency", 1, 32, None)
        if e:
            return None, e
        params.update(concurrency=conc)
    return params, None


def guards_blocking():
    """双驱动护栏：容器内 evalscope eval 或宿主分批驱动在跑 → 拒绝文案。"""
    eval_pids = container_pgrep(P_EVAL)
    if eval_pids:
        return ("容器内已有 evalscope eval 进程 (pid %s)——双驱动会互相覆盖 "
                "review 数据，拒绝启动" % eval_pids)
    if eval_pids is None:
        # docker 探测失败不硬拒（驱动起来后自己 docker exec 也会失败、可见）
        pass
    drv = host_pgrep(P_DRIVER)
    if drv:
        return "宿主 run_all_batches.sh 驱动仍在跑 (pid %s)——它随时启动下一批，与本次启动撞车" % drv
    return None


def launch_full(params):
    """宿主侧拉起分批驱动（start_new_session = setsid，与桥接进程组隔离，
    桥接退出/重启不影响驱动；驱动 stdout 追加 batches.log 与手动流程一致）。"""
    env = dict(os.environ)
    env["START_OFFSET"] = str(params["start_offset"])
    env["WINDOW"] = str(params["window"])
    env["CONCURRENCY"] = str(params["concurrency"])
    env["KEEP_IMAGES"] = str(params["keep_images"])
    if params.get("limit"):
        env["TOTAL"] = str(params["limit"])
    os.makedirs(LOGS_DIR, exist_ok=True)
    total_s = " TOTAL=%d" % params["limit"] if params.get("limit") else ""
    logf = open(BATCHES_LOG, "ab")
    try:
        proc = subprocess.Popen(["bash", "run_all_batches.sh"], cwd=SWE_PRO,
                                env=env, stdout=logf, stderr=subprocess.STDOUT,
                                start_new_session=True)
    finally:
        logf.close()   # 子进程已继承 fd，关父进程句柄即可
    _STATE["last_run"] = {"mode": "full", "params": _public_params(params),
                          "driver_pid": proc.pid, "started": now()}
    _STATE["last_action"] = {
        "type": "run_full", "at": now(),
        "detail": ("驱动已启动 pid=%d（START_OFFSET=%d WINDOW=%d "
                   "CONCURRENCY=%d KEEP_IMAGES=%d%s，日志追加 %s）"
                   % (proc.pid, params["start_offset"], params["window"],
                      params["concurrency"], params["keep_images"],
                      total_s, BATCHES_LOG))}
    return proc.pid


def launch_rerun(params):
    """调 swe_runner.py start-rerun --yes（自带双驱动护栏，撞车时 exit 3）。"""
    cmd = ["python3", os.path.join(KIT_ROOT, "swe_runner.py"),
           "start-rerun", "--yes"]
    if params.get("concurrency"):
        cmd += ["--eval-batch-size", str(params["concurrency"])]
    rc, out, err = sh(cmd, timeout=300)
    tail = "\n".join(((out or "") + (err or "")).strip().splitlines()[-6:])
    if rc is None:
        _STATE["last_action"] = {"type": "run_error", "at": now(),
                                 "detail": "start-rerun 执行失败: " + tail}
    elif rc == 3:
        _STATE["last_action"] = {"type": "run_rejected", "at": now(),
                                 "detail": "start-rerun 护栏拒绝（有评测进程在跑）:\n" + tail}
    elif rc != 0:
        _STATE["last_action"] = {"type": "run_error", "at": now(),
                                 "detail": "start-rerun 退出码 %d:\n%s" % (rc, tail)}
    else:
        _STATE["last_run"] = {"mode": "rerun", "params": _public_params(params),
                              "started": now()}
        _STATE["last_action"] = {"type": "run_rerun", "at": now(),
                                 "detail": "补跑已启动:\n" + tail}
    return rc


def _public_params(params):
    """记入 state 的参数副本（去掉 uuid/ts 等调度字段）。"""
    return {k: v for k, v in params.items() if k not in ("uuid", "ts")}


def ensure_watchers():
    """补拉两件套看守（幂等：已在跑则不动）。返回本次拉起的名字列表。"""
    started = []
    # 容器内 staged_watch（组合 watcher v6，jest+go 补丁保护，批间隙存活）
    if not container_pgrep(P_WATCH):
        inner = ("cd %s && setsid nohup bash staged_watch.sh "
                 ">/dev/null 2>&1 &" % shlex.quote(SWE_PRO))
        sh(["docker", "exec", "-d", CONTAINER, "bash", "-c", inner], timeout=30)
        started.append("staged_watch(容器内)")
    # 宿主侧 review_guard（review 文件实时备份，防事务性丢失）
    if not host_pgrep(P_GUARD):
        subprocess.Popen(["bash", "review_guard.sh"], cwd=SWE_PRO,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        started.append("review_guard(宿主)")
    return started


def handle_run(params):
    ts = now()
    block = guards_blocking()
    if block:
        _STATE["last_action"] = {"type": "run_rejected", "at": ts,
                                 "detail": block, "params": _public_params(params)}
        return
    if params["mode"] == "full":
        launch_full(params)
    else:
        launch_rerun(params)
    started = ensure_watchers()
    act = _STATE.get("last_action") or {}
    if started:
        act["watchers_started"] = started
        _STATE["last_action"] = act


# ----------------------------------------------------------- stop 命令处理 ----
def _kill_host(pids):
    """宿主侧逐 pid kill -9；返回成功的 pid 列表。"""
    killed = []
    for pid in pids or []:
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except OSError:
            pass
    return killed


def _kill_container_procs():
    """容器内 kill -9 全部评测相关进程（pgrep 取 pid 再杀；括号模式防
    bash -c 自匹配）。返回 (pid 列表, docker exec rc)。"""
    inner = ("pids=\"$(pgrep -f '%s' || true)\"; "
             "if [ -n \"$pids\" ]; then kill -9 $pids 2>/dev/null || true; fi; "
             "echo \"$pids\"") % P_CONTAINER_KILL
    rc, out, _ = sh(["docker", "exec", CONTAINER, "bash", "-c", inner],
                    timeout=30)
    if rc is None:
        return [], None
    pids = [int(x) for x in (out or "").split() if x.isdigit()]
    return pids, rc


def _select_sandbox_containers(ps_lines):
    """docker ps --format '{{.ID}} {{.Names}}' 输出行 → 只留 sandbox-* 容器。

    绝不返回 dsv41-mmc / aiserver-agent / ais-bench（双保险：名字前缀 +
    保护名单都过）。
    """
    out = []
    for line in ps_lines:
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        cid, name = parts[0].strip(), parts[1].strip()
        if name.startswith("sandbox-") and name not in PROTECTED_CONTAINERS:
            out.append((cid, name))
    return out


def _remove_sandboxes():
    """docker rm -f 全部 sandbox-* 沙箱容器（评测拉起的 amd64 qemu 沙箱）。"""
    rc, out, _ = sh(["docker", "ps", "--format", "{{.ID}} {{.Names}}"],
                    timeout=30)
    if rc is None or rc != 0:
        return []
    removed = []
    for cid, name in _select_sandbox_containers((out or "").splitlines()):
        rm_rc, _, _ = sh(["docker", "rm", "-f", cid], timeout=60)
        if rm_rc == 0:
            removed.append(name)
    return removed


def find_interrupted_batch(path):
    """batches.log 最后一个无 finished 配对的 `=== batch offset=N count=M` 行。

    返回 (offset, count) 或 None（无日志/无批行/最后一个批已配对完成）。
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    last_start = None
    finished = set()
    for ln in lines:
        m = _BATCH_DONE_RE.search(ln)
        if m:
            finished.add(int(m.group(1)))
            continue
        m = _BATCH_START_RE.search(ln)
        if m:
            last_start = (int(m.group(1)), int(m.group(2)))
    if last_start and last_start[0] not in finished:
        return last_start
    return None


def read_dataset_ids(offset, count):
    """容器内 venv python 直读数据集 arrow 的 [offset, offset+count)
    instance_id（stdin 喂脚本，不走网络）。失败返回 None。"""
    rc, out, _ = sh(["docker", "exec", "-i", CONTAINER, VENV_PY, "-",
                     str(offset), str(offset + count)],
                    timeout=120, input_text=_DS_SCRIPT)
    if rc is None or rc != 0:
        return None
    stripped = (out or "").strip()
    if not stripped:
        return None
    try:
        data = json.loads(stripped.splitlines()[-1])
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("error"):
        return None
    ids = data.get("ids")
    if not isinstance(ids, list):
        return None
    return [str(x) for x in ids]


def collect_reviewed_ids(results_dir):
    """reviews/**/*.jsonl* 递归全扫已评分 instance_id（含 aa_recovered 与
    rerun 临时文件；instance_id 在 sample_score.sample_metadata.instance_id）。"""
    ids = set()
    root = os.path.join(results_dir, "reviews")
    for path in glob.glob(os.path.join(root, "**", "*.jsonl*"), recursive=True):
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    md = ((row.get("sample_score") or {})
                          .get("sample_metadata") or {})
                    iid = md.get("instance_id")
                    if iid:
                        ids.add(iid)
        except OSError:
            continue
    return ids


def append_rerun_ids(path, ids, comment):
    """去重追加补跑清单（输入内去重 + 既有清单里已有的不再追加）；
    返回实际追加条数。"""
    if not ids:
        return 0
    existing = set()
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    ln = raw.strip()
                    if ln and not ln.startswith("#"):
                        existing.add(ln)
        except OSError:
            pass
    new, seen = [], set()
    for i in ids:
        if i not in existing and i not in seen:
            seen.add(i)
            new.append(i)
    if not new:
        return 0
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n# %s\n" % comment)
            f.write("\n".join(new) + "\n")
    except OSError:
        return 0
    return len(new)


def _rescue_reviews(report):
    """停止序列第 4 步：抢救数据 + 未评分实例入补跑队列。"""
    # 4a) 从 prediction cache + workspace 重建已评分行（aa_recovered.jsonl）
    rc, out, err = sh(["python3", RECOVER_PY, RESULTS_DIR], timeout=600)
    report["recovered"] = {
        "rc": rc,
        "tail": ((out or "") + (err or "")).strip().splitlines()[-3:]}
    # 4b) 最后一个无 finished 配对的批
    inter = find_interrupted_batch(BATCHES_LOG)
    if not inter:
        report["rescue_note"] = "batches.log 无未完成批，跳过补跑队列追加"
        return
    off, cnt = inter
    # 4c) 数据集 [off, off+cnt) 的 instance_id
    ids = read_dataset_ids(off, cnt)
    if ids is None:
        report["rescue_note"] = ("批 offset=%d 中断，但数据集 instance_id 读取"
                                 "失败，未追加补跑队列（可手动跑 "
                                 "tools/recover_reviews_from_workspaces.py）" % off)
        return
    # 4d) diff reviews（含 4a 重建的行）得未评分集合
    reviewed = collect_reviewed_ids(RESULTS_DIR)
    unscored = [i for i in ids if i not in reviewed]
    appended = append_rerun_ids(
        RERUN_IDS, unscored,
        comment="%s 停止时批 offset=%d 中断，未评分实例入队" % (now(), off))
    report["rerun"] = {"batch_offset": off, "batch_count": cnt,
                       "dataset_ids": len(ids), "unscored": len(unscored),
                       "appended": appended, "file": RERUN_IDS}


def handle_stop():
    """完整停止序列；什么都没在跑 → 成功空操作报告（幂等）。"""
    ts = now()
    report = {"type": "stop", "at": ts, "killed": {}, "sandbox_removed": [],
              "recovered": None, "rerun": None}
    # 0) 停止前活性快照：决定是否做数据抢救（stale 未完成批行不触发抢救，
    #    防止幂等空停重复往 rerun_ids.txt 里灌 id）
    pre_driver = host_pgrep(P_DRIVER)
    pre_eval = container_pgrep(P_EVAL)
    pre_script = container_pgrep(P_SCRIPT)
    active = bool(pre_driver or pre_eval or pre_script)
    report["was_active"] = active
    # 1) 宿主：分批驱动 + review_guard（kill -9，记 pid）
    for pat, key in ((P_DRIVER, "run_all_batches"), (P_GUARD, "review_guard")):
        pids = host_pgrep(pat)
        report["killed"][key] = {"pids": pids or [],
                                 "killed": _kill_host(pids)}
    # 2) 容器：eval / 批内脚本 / 两个看守（kill -9；SIGTERM 会让 evalscope
    #    的 discard_review_reruns 删掉已评分行）
    pids, rc = _kill_container_procs()
    report["killed"]["container"] = {"pids": pids, "exec_rc": rc}
    # 3) sandbox 沙箱容器清理（只碰 sandbox-*）
    report["sandbox_removed"] = _remove_sandboxes()
    # 4) 数据抢救（仅确有评测进程在跑时）
    if active:
        _rescue_reviews(report)
    else:
        report["rescue_note"] = "无评测进程在跑，空操作（不触发数据抢救）"
    _STATE["last_action"] = report
    _STATE["last_stop"] = report


# --------------------------------------------------------------- 主循环 ----
def refresh_state():
    """持续刷新驱动/eval/看守存活、当前批；写入 state.json。"""
    driver = host_pgrep(P_DRIVER)
    guard = host_pgrep(P_GUARD)
    live = container_liveness()
    inter = find_interrupted_batch(BATCHES_LOG)
    _STATE["driver_running"] = bool(driver) if driver is not None else None
    _STATE["driver_pids"] = driver
    _STATE["eval_running"] = (bool(live.get("eval"))
                              if live is not None else None)
    _STATE["watchers"] = {
        "staged_watch": (bool(live.get("watch")) if live is not None else None),
        "review_guard": bool(guard) if guard is not None else None,
    }
    _STATE["current_batch"] = ({"offset": inter[0], "count": inter[1]}
                               if inter else None)
    write_state()


def _consume_run():
    data = None
    try:
        with open(RUN_CMD_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        _STATE["last_action"] = {"type": "run_error", "at": now(),
                                 "detail": "run.json 解析失败: %s" % e}
    finally:
        _unlink_quiet(RUN_CMD_PATH)   # 无论成败都删，防重放
    if data is None:
        return
    params, err = parse_run_params(data)
    if err:
        _STATE["last_action"] = {"type": "run_rejected", "at": now(),
                                 "detail": "参数校验失败: %s" % err}
    else:
        handle_run(params)


def _consume_stop():
    handle_stop()
    _unlink_quiet(STOP_FLAG_PATH)


def main():
    # 单实例护栏：已有别的桥接进程在跑就退出（pgrep 自身排除 + 过滤本 pid）
    others = [p for p in (host_pgrep(P_BRIDGE) or []) if p != os.getpid()]
    if others:
        print("已有 swe_web_bridge 在运行 (pid %s)，退出" % others,
              file=sys.stderr)
        return 1
    os.makedirs(WEBCTL, exist_ok=True)
    _STATE.update(
        ok=True, bridge_pid=os.getpid(), bridge_started=now(),
        driver_running=False, driver_pids=[], eval_running=False,
        watchers={}, current_batch=None, last_run=None, last_stop=None,
        last_action={"type": "bridge_start", "at": now(),
                     "detail": "桥接守护启动，轮询 %s（1s 命令 / 3s 状态）"
                               % WEBCTL})
    write_state()
    print("swe_web_bridge 启动: pid=%d webctl=%s" % (os.getpid(), WEBCTL),
          flush=True)
    stopping = {"flag": False}

    def _sig(_signum, _frame):
        stopping["flag"] = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    last_refresh = 0.0
    while not stopping["flag"]:
        try:
            if os.path.exists(STOP_FLAG_PATH):
                _consume_stop()
                write_state()
            if os.path.exists(RUN_CMD_PATH):
                _consume_run()
                write_state()
            if time.time() - last_refresh >= 3:
                refresh_state()
                last_refresh = time.time()
        except Exception as e:
            try:
                _STATE["last_action"] = {"type": "error", "at": now(),
                                         "detail": repr(e)}
                write_state()
            except Exception:
                pass
        time.sleep(1)
    _STATE["last_action"] = {"type": "bridge_exit", "at": now(),
                             "detail": "桥接守护退出"}
    write_state()
    return 0


if __name__ == "__main__":
    sys.exit(main())
