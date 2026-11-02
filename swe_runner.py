#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SWE-bench_Pro 精度评测 runner（第七条测试线）。

对 OpenAI 兼容推理服务跑 SWE-bench_Pro（ModelScope ScaleAI/SWE-bench_Pro,
实际 642 实例, agentic 精度评测: 模型生成补丁 → 每实例独立 amd64 沙箱容器
跑金标测试 → resolved 率）。与其它线不同, **本 runner 不驱动全量评测**——
全量由 /mnt/sfs_turbo/c00803992/swe_pro 下的分批驱动脚本负责
（run_all_batches.sh: 642 实例按窗口 32 分批, 每批拉镜像→评→prune）。
本 runner 提供只读监控与补跑入口:

    status       只读聚合当前评测状态: batches.log 批次/最新 batch log 的
                 tqdm 进度行/eval_rc.log 崩溃标记/进程活性/磁盘/补跑队列
    aggregate    调 tools/aggregate_swe_bench_pro.py 聚合已评分结果
                 （repo × resolved/total/acc + 总 acc）
    rerun-list   打印补跑清单 rerun_ids.txt 的非注释行数 + 注释分组摘要 + 样例
    env-check    检查 relay / binfmt / 容器内 venv 补丁等环境前提
                 （对照 tools/prepare_swe_bench_env.sh 的各步, 只检查不修复）
    start-rerun  打印补跑建议命令（分两半顺序跑, 防一次崩溃带走全部补跑）;
                 容器内已有 evalscope eval 进程或宿主驱动仍在跑时拒绝执行。
                 默认 dry-run, --yes 才真正启动

关键约束: /mnt/sfs_turbo/c00803992/swe_pro 是 live 评测工作区（有 watcher /
驱动进程正按这些路径工作）——本 runner 对它**只读**, 绝不移动/修改/删除其
中任何已有文件（start-rerun 只新建 rerun_part*.txt 分片文件）。

命名空间感知: 本 runner 可能在宿主机（bash run.sh --swe）或 ais-bench 容器内
（webapp 转调）运行。容器内看不到宿主进程 / /proc/sys/fs/binfmt_misc /
/mnt/docker, 这些项在容器内报 unknown 而不是误报"不存在"; 反之容器内可直接
pgrep 到 evalscope eval / staged_watch（它们跑在容器里）, 也可直接探测
docker unix relay socket（在 NFS 共享盘上）。

用法:
    python3 swe_runner.py status                # 人读状态总览
    python3 swe_runner.py status --json         # 结构化 JSON（webapp 用）
    python3 swe_runner.py aggregate             # 聚合已评分结果
    python3 swe_runner.py rerun-list            # 补跑清单计数 + 样例
    python3 swe_runner.py env-check             # 环境前提检查
    python3 swe_runner.py start-rerun           # 打印补跑命令（dry-run）
    python3 swe_runner.py start-rerun --yes     # 真正启动（有评测进程则拒绝）
    bash run.sh --swe status                    # 等价 run.sh 分发
"""
import argparse
import glob
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import time

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT_ROOT)

try:
    import yaml
except ImportError:
    print("缺少 pyyaml，请先运行: pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_CONFIG = os.path.join(KIT_ROOT, "config.yaml")

# 聚合器用仓库内副本（原件在 /mnt/sfs_turbo/c00803992/tools, live 不受影响）
AGGREGATE_PY = os.path.join(KIT_ROOT, "tools", "aggregate_swe_bench_pro.py")

# 是否在 ais-bench 容器命名空间内（webapp 转调时为 True）
IN_CONTAINER = os.path.exists("/.dockerenv")

# swe 段缺省值（config.yaml 可逐键覆盖; url/model 不回退 service 段——
# 本线 live 评测固定打 dsv4 服务, 与 kit 的 service 段指向可以不同）
DEFAULTS = {
    "work_dir": "/mnt/sfs_turbo/c00803992/swe_pro",
    "results_dir": "",          # 留空 = <work_dir>/results/accuracy/swe_bench_pro_dsv4
    "rerun_ids": "",            # 留空 = <work_dir>/rerun_ids.txt
    "repo_path": "/mnt/sfs_turbo/c00803992/swe_bench_pro_os/repo",
    "api_url": "http://192.168.0.9:18020/v1/chat/completions",
    "model": "dsv4",
    "venv": "/tmp/bench-venv",  # 容器内 evalscope 客户端 venv（勿装东西）
    "container": "ais-bench",
    "relay_socket": "/mnt/sfs_turbo/c00803992/dockersock/docker.sock",
    "disk_mount": "/mnt/docker",
    "batch_window": 32,         # 每批实例数（run_all_batches.sh WINDOW）
    "total_instances": 642,     # 数据集实际条数（文档写 731, 实测 642）
    "eval_batch_size": 16,      # 批内并发（补跑默认值）
}

# tqdm 进度行两种形态（同一条 bar 的 \r 刷新原串与 evalscope INFO 日志行）:
#   Evaluating[swe_bench_pro]:  94%|█████████▍| 30/32 [3:55:05<20:46, 623.07s/it]
#   2026-10-31 02:58:27 - evalscope - INFO: Evaluating[swe_bench_pro]  94%| 30/32
#     [Elapsed: 3:56:05 < Remaining: 20:46, 623.07s/it]
_PROG_RE = re.compile(
    r"Evaluating\[swe_bench_pro\][:\s]*(\d+)%\|[^|\d]*\|?\s*(\d+)/(\d+)\s*\["
    r"[^\]]*?(\d[\d:]+)\s*<\s*([^\],]+?),\s*([\d.?]+)\s*(s/it|it/s)\]")
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_BATCH_HDR_RE = re.compile(r"window=(\d+)\s+concurrency=(\d+)\s+total=(\d+)"
                           r"\s+start_offset=(-?\d+)")
_BATCH_START_RE = re.compile(r"===\s*batch\s+offset=(-?\d+)\s+count=(\d+)\s*===")
_BATCH_DONE_RE = re.compile(r"batch\s+offset=(-?\d+)\s+finished")
_EVAL_RC_RE = re.compile(r"offset=(-?\d+)\s+rc=(-?\d+)")


def load_config(path: str) -> dict:
    """读 config.yaml; swe 段允许缺失（全用默认值）"""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_swe_config(cfg: dict, work_dir: str = None) -> dict:
    """合并 config swe 段与默认值; 派生路径跟随 work_dir 推导（显式键优先）。"""
    sec = cfg.get("swe") or {}
    wd = str(work_dir or sec.get("work_dir") or DEFAULTS["work_dir"]).rstrip("/")
    if not wd:
        wd = DEFAULTS["work_dir"]
    sw = dict(DEFAULTS)
    sw.update(sec)
    sw["work_dir"] = wd
    if not str(sw.get("results_dir") or "").strip():
        sw["results_dir"] = os.path.join(wd, "results", "accuracy",
                                         "swe_bench_pro_dsv4")
    if not str(sw.get("rerun_ids") or "").strip():
        sw["rerun_ids"] = os.path.join(wd, "rerun_ids.txt")
    sw["logs_dir"] = os.path.join(wd, "logs")
    sw["batches_log"] = os.path.join(sw["logs_dir"], "batches.log")
    sw["eval_rc_log"] = os.path.join(sw["logs_dir"], "eval_rc.log")
    sw["live_driver"] = os.path.join(wd, "run_all_batches.sh")
    return sw


# --------------------------------------------------------------- 日志解析 ----
def parse_batches_log(path: str, tail_n: int = 12) -> dict:
    """解析 logs/batches.log（分批驱动日志）。

    行样例:
      [2026-10-30 16:08:35] window=32 concurrency=16 total=642 start_offset=38
      [2026-10-30 16:08:35] === batch offset=38 count=32 ===
      [2026-10-30 16:22:37] batch offset=38 finished (log 89942B)
      [2026-10-31 07:02:17] === batch offset=198 count=32 ===
    返回 window/total/start_offset/finished_offsets/current{offset,count,
    started,done}/all_done/log_tail; 文件缺失返回 exists=False 其余空。
    """
    out = {"exists": os.path.isfile(path), "window": None, "concurrency": None,
           "total": None, "start_offset": None, "finished_offsets": [],
           "finished_count": 0, "current": None, "all_done": False,
           "log_tail": []}
    if not out["exists"]:
        return out
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    out["log_tail"] = lines[-tail_n:]
    for ln in lines:
        m = _BATCH_HDR_RE.search(ln)
        if m:
            out["window"] = int(m.group(1))
            out["concurrency"] = int(m.group(2))
            out["total"] = int(m.group(3))
            out["start_offset"] = int(m.group(4))
            continue
        m = _BATCH_START_RE.search(ln)
        if m:
            ts = _TS_RE.search(ln)
            out["current"] = {"offset": int(m.group(1)),
                              "count": int(m.group(2)),
                              "started": ts.group(1) if ts else None,
                              "done": False}
            continue
        m = _BATCH_DONE_RE.search(ln)
        if m:
            off = int(m.group(1))
            if off not in out["finished_offsets"]:
                out["finished_offsets"].append(off)
    out["finished_count"] = len(out["finished_offsets"])
    if out["current"] is not None:
        out["current"]["done"] = out["current"]["offset"] in out["finished_offsets"]
    out["all_done"] = any("all batches done" in ln for ln in lines)
    return out


def compute_resume_offset(sw: dict) -> dict:
    """断点续跑默认 start_offset（web 启动表单默认值）。

    batches.log 最后一个 `=== batch offset=N` 无 finished 配对 → 取 N
    （重跑该中断批）；否则取最后一个批的下一偏移（offset+count，封顶
    total_instances）；无日志/无批行 → 0。
    返回 {offset, note, interrupted, all_done}。
    """
    out = {"offset": 0, "note": "无批次日志，从头开始", "interrupted": False,
           "all_done": False}
    path = sw["batches_log"]
    if not os.path.isfile(path):
        return out
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    starts, finished = [], set()
    for ln in lines:
        m = _BATCH_DONE_RE.search(ln)
        if m:
            finished.add(int(m.group(1)))
            continue
        m = _BATCH_START_RE.search(ln)
        if m:
            starts.append((int(m.group(1)), int(m.group(2))))
    if not starts:
        return out
    last_off, last_cnt = starts[-1]
    if last_off not in finished:
        out.update(offset=last_off, interrupted=True,
                   note="批 offset=%d 未完成，默认续跑该批" % last_off)
        return out
    nxt = last_off + last_cnt
    out["all_done"] = any("all batches done" in ln for ln in lines)
    total = int(sw.get("total_instances") or 0)
    if total > 0:
        nxt = min(nxt, total)
    out["offset"] = nxt
    if out["all_done"]:
        out["note"] = "全部批次已完成，默认 offset=%d（改小可重跑）" % nxt
    else:
        out["note"] = "上一批 offset=%d 已完成，默认从 %d 开始" % (last_off, nxt)
    return out


def parse_progress_line(line: str) -> dict:
    """解析单条 tqdm 进度行（Evaluating[swe_bench_pro]）; 不匹配返回 None。

    尚无完成实例时 tqdm 的 rate 是 "?"（无 ETA 阶段）——返回字符串 "?" 而非
    数值; CLI/web 端按原样展示。"""
    m = _PROG_RE.search(line)
    if not m:
        return None
    eta = m.group(5).replace("Remaining:", "").strip()
    ts = _TS_RE.search(line)
    rate = m.group(6)
    # rate_unit: tqdm 按 rate 选单位（无 ETA 阶段 ?it/s、慢速 s/it、快速 it/s）,
    # 展示层按原样拼, 别硬编码 s/it——否则无 ETA 阶段会显示 "?s/it"
    return {"pct": int(m.group(1)), "n": int(m.group(2)),
            "total": int(m.group(3)), "elapsed": m.group(4), "eta": eta,
            "rate": rate if rate == "?" else float(rate),
            "rate_unit": m.group(7),
            "ts": ts.group(1) if ts else None}


def find_progress(path: str, block: int = 1024 * 1024, overlap: int = 262144,
                  cap: int = 8 * 1024 * 1024) -> dict:
    """找日志里最后一条 Evaluating[swe_bench_pro] 进度行并解析。

    tqdm 用 \\r 就地刷新, evalscope 又把它转写进 INFO 日志行——两种都切开后
    取最后一条。从文件末尾逐块向前扫（eval 实例输出会淹没较早的进度行——
    rerun 日志里两条 tqdm 更新之间可隔数 MB 的 ms_enclave 输出, 固定小尾窗
    会漏）; 单块找到即停, 最多回看 cap 字节。跨块边界行由 overlap 兜住。
    """
    if not path or not os.path.isfile(path):
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            end, scanned = size, 0
            while end > 0 and scanned < cap:
                start = max(0, end - block - overlap)
                f.seek(start)
                text = f.read(end - start).decode("utf-8", errors="replace")
                for chunk in reversed(re.split(r"[\r\n]", text)):
                    if "Evaluating[swe_bench_pro]" not in chunk:
                        continue
                    prog = parse_progress_line(chunk)
                    if prog:
                        prog["log"] = path
                        return prog
                end -= block
                scanned += block
    except OSError:
        return None
    return None


def latest_batch_log(sw: dict, batches: dict) -> str:
    """当前批的 batch_<offset>.log; batches.log 解析不出时按 mtime 取最新。"""
    cur = (batches.get("current") or {}).get("offset")
    if cur is not None:
        p = os.path.join(sw["logs_dir"], f"batch_{cur}.log")
        if os.path.isfile(p):
            return p
    pats = [p for p in glob.glob(os.path.join(sw["logs_dir"], "batch_*.log"))
            if os.path.isfile(p)]
    if not pats:
        return None
    return max(pats, key=lambda p: (os.path.getmtime(p), p))


def latest_rerun_log(sw: dict) -> str:
    """logs/rerun_part*.log 中 mtime 最新的一个; 无则 None。

    rerun 模式没有 batch log——进度行/运行日志全在 rerun_part*.log。"""
    pats = [p for p in glob.glob(os.path.join(sw["logs_dir"], "rerun_part*.log"))
            if os.path.isfile(p)]
    return max(pats, key=lambda p: (os.path.getmtime(p), p)) if pats else None


def running_eval_cmds(sw: dict) -> list:
    """当前实跑的评测命令原文（/proc 进程口径, 供 CLI/web 展示与复现）。

    rerun: 容器内 docker exec 拉起的 bash -c 包装进程, cmdline 即
    start-rerun 拼的完整分片串联命令——原文回显, 与实跑零出入。
    full: 宿主 run_all_batches.sh 驱动 + 读其 environ 还原驱动参数
    （容器内看不到宿主进程, 由 bridge_state 口径兜底）。
    找不到（分片间隙/已结束/权限）返回空列表。
    """
    cmds = []
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return cmds
    for pid in pids:
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                argv = [a for a in f.read().decode("utf-8", "replace").split("\0") if a]
        except OSError:
            continue
        if not argv:
            continue
        base = os.path.basename(argv[0])
        if base == "bash" and "-c" in argv[:2] and len(argv) > 2 \
                and "EVALSCOPE_INSTANCE_FILE=" in argv[2]:
            # 与 build_rerun_plan 的 docker_cmd 同形: docker exec -d <容器> bash -c <shell>
            cmds.append("docker exec -d %s bash -c %s"
                        % (sw["container"], shlex.quote(argv[2])))
        elif any("run_all_batches" in a for a in argv[:2]):
            env = {}
            try:
                with open("/proc/%s/environ" % pid, "rb") as f:
                    for kv in f.read().decode("utf-8", "replace").split("\0"):
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            if k in ("START_OFFSET", "WINDOW", "CONCURRENCY",
                                     "KEEP_IMAGES", "TOTAL"):
                                env[k] = v
            except OSError:
                pass
            pairs = " ".join("%s=%s" % (k, env[k]) for k in
                             ("START_OFFSET", "WINDOW", "CONCURRENCY",
                              "KEEP_IMAGES", "TOTAL") if k in env)
            cmds.append("cd %s && %s bash run_all_batches.sh"
                        % (shlex.quote(sw["work_dir"]), pairs).replace("  ", " "))
    return cmds


def bridge_launch_cmd(sw: dict) -> str:
    """webctl/state.json 的 last_run → 启动口径命令（full=驱动 env 前缀,
    rerun=start-rerun CLI 原样）。进程口径找不到时的兜底展示。"""
    try:
        with open(os.path.join(sw["work_dir"], "webctl", "state.json"),
                  encoding="utf-8") as f:
            st = json.load(f)
        lr = st.get("last_run") or {}
    except (OSError, ValueError):
        return None
    mode, params = lr.get("mode"), lr.get("params") or {}
    if not mode:
        return None
    if mode == "full":
        pairs = []
        for k, pk in (("START_OFFSET", "start_offset"), ("WINDOW", "window"),
                      ("CONCURRENCY", "concurrency"), ("KEEP_IMAGES", "keep_images")):
            if params.get(pk) is not None:
                pairs.append("%s=%s" % (k, params[pk]))
        if params.get("limit"):
            pairs.append("TOTAL=%s" % params["limit"])
        return ("cd %s && %s bash run_all_batches.sh >> logs/batches.log 2>&1"
                % (shlex.quote(sw["work_dir"]), " ".join(pairs)))
    if mode == "rerun":
        c = params.get("concurrency") or params.get("eval_batch_size")
        return ("python3 swe_runner.py start-rerun %s--yes"
                % ("--eval-batch-size %s " % c if c else ""))
    return None


def parse_eval_rc(path: str, tail_n: int = 8) -> dict:
    """解析 logs/eval_rc.log（run_swe_bench_pro.sh 尾部追加的退出码记录）。

    行样例: [eval] offset=198 rc=1 ended 10:58:00
    rc!=0 = 该批 evalscope 崩溃标记（straggler APITimeoutError 等）。
    文件不存在 = 尚无批用过带记录的脚本版本/当前批未结束, 属正常。
    """
    out = {"exists": os.path.isfile(path), "records": [], "last": None,
           "crash_offsets": [], "tail": []}
    if not out["exists"]:
        return out
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = [ln.rstrip() for ln in f.read().splitlines() if ln.strip()]
    except OSError:
        return out
    out["tail"] = lines[-tail_n:]
    for ln in lines:
        m = _EVAL_RC_RE.search(ln)
        if m:
            rec = {"offset": int(m.group(1)), "rc": int(m.group(2)), "line": ln}
            out["records"].append(rec)
            if rec["rc"] != 0:
                out["crash_offsets"].append(rec["offset"])
    out["last"] = out["records"][-1] if out["records"] else None
    return out


def parse_rerun_ids(path: str, sample_n: int = 30,
                    include_ids: bool = False) -> dict:
    """解析 rerun_ids.txt: 非注释行 = 待补跑 instance_id, '#' 行 = 分组注释。

    返回 count/注释行数/分组摘要（注释 + 其后非注释条数）/前 sample_n 条样例;
    include_ids=True 时附全量 ids（start-rerun 分片用, JSON 输出勿带）。
    """
    out = {"exists": os.path.isfile(path), "file": path, "count": 0,
           "comment_count": 0, "groups": [], "sample": []}
    if not out["exists"]:
        return out
    ids = []
    groups = []   # [注释文本, 其后非注释条数]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    groups.append([line.lstrip("# ").strip(), 0])
                    out["comment_count"] += 1
                    continue
                ids.append(line)
                if groups:
                    groups[-1][1] += 1
    except OSError:
        return out
    out["count"] = len(ids)
    out["groups"] = [{"comment": c, "count": n} for c, n in groups]
    out["sample"] = ids[:sample_n]
    if include_ids:
        out["ids"] = ids
    return out


# --------------------------------------------------------------- 进程探测 ----
def _pgrep(pattern: str, container: str = None, timeout: int = 15):
    """pgrep -f <pattern> → pid 列表; 无匹配=[]; 命令不可用/超时/容器故障=None。

    container 非空 = 经 docker exec 进容器命名空间查（宿主机模式用;
    镜像 run_all_batches.sh 等待循环的同款口径）。
    """
    cmd = ["docker", "exec", str(container), "pgrep", "-f", pattern] if container \
        else ["pgrep", "-f", pattern]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode == 0:
        try:
            return [int(x) for x in (r.stdout or "").split()]
        except ValueError:
            return []
    if r.returncode == 1:
        return []          # pgrep: 无匹配
    return None            # docker exec 失败（容器没起/docker 不可用）


def container_pgrep(sw: dict, pattern: str):
    """在容器命名空间里 pgrep; 容器模式直接查, 宿主模式 docker exec
    （docker 不可用时退回宿主 pgrep——docker exec 起的进程宿主也可见）。"""
    if IN_CONTAINER:
        return _pgrep(pattern), "container"
    pids = _pgrep(pattern, container=sw.get("container"))
    if pids is None:
        return _pgrep(pattern), "host-fallback"
    return pids, "container"


def relay_reachable(sock_path: str, timeout: float = 6.0) -> bool:
    """经 unix relay socket 只读 GET /version（宿主/容器两侧都能探测——
    socket 在 NFS 共享盘上, 容器内 evalscope 就是用它当 DOCKER_HOST）。"""
    if not os.path.exists(sock_path):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(b"GET /version HTTP/1.1\r\nHost: localhost\r\n"
                  b"Connection: close\r\n\r\n")
        head = b""
        while b"\r\n\r\n" not in head and len(head) < 4096:
            chunk = s.recv(1024)
            if not chunk:
                break
            head += chunk
        s.close()
    except OSError:
        return False
    first = head.split(b"\r\n", 1)[0]
    return b" 200 " in first or b" 204 " in first


def binfmt_status() -> dict:
    """binfmt_misc qemu-x86_64 条目状态（宿主可见; 容器内 /proc/sys/fs/
    binfmt_misc 为空挂载, 报 visible=False 而不是误报未注册）。"""
    d = "/proc/sys/fs/binfmt_misc"
    entry = os.path.join(d, "qemu-x86_64")
    out = {"visible": os.path.isfile(entry), "registered": False,
           "enabled": False, "flags": None, "interpreter": None}
    if not out["visible"]:
        return out
    out["registered"] = True
    try:
        with open(entry, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if ln == "enabled":
                    out["enabled"] = True
                elif ln.startswith("interpreter "):
                    out["interpreter"] = ln.split(None, 1)[1].strip()
                elif ln.startswith("flags:"):
                    out["flags"] = ln.split(":", 1)[1].strip()
    except OSError:
        pass
    return out


def disk_status(mount: str) -> dict:
    """df -h <mount>（docker 根盘, 每批 ~66GB 镜像涨落）; 容器内不可见。"""
    out = {"visible": os.path.isdir(mount), "mount": mount,
           "avail": None, "used_pct": None}
    if not out["visible"]:
        return out
    try:
        r = subprocess.run(["df", "-h", mount], capture_output=True, text=True,
                           timeout=10)
        fields = (r.stdout or "").strip().splitlines()[-1].split()
        if len(fields) >= 5:
            out["avail"] = fields[3]
            out["used_pct"] = fields[4]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    return out


def _mtime_str(path: str):
    if not os.path.exists(path):
        return None
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(os.path.getmtime(path)))
    except OSError:
        return None


HOST_ONLY_NOTE = "宿主侧进程/挂载，容器内不可见（宿主机上查为准）"


def processes_status(sw: dict) -> dict:
    """六类进程活性。命名空间规则:
    - 容器内跑的（evalscope eval / run_swe_bench_pro.sh / staged_watch.sh）:
      容器模式直接 pgrep, 宿主模式 docker exec 进容器 pgrep
    - 宿主上跑的（run_all_batches.sh / review_guard.sh / docker_unix_relay）:
      只能宿主 pgrep; 容器内报 running=None + note
    - relay 另有 socket 探测（两侧都行, 权威判活）
    """
    procs = {}

    def host_only(pattern):
        if IN_CONTAINER:
            return {"running": None, "pids": None, "note": HOST_ONLY_NOTE}
        pids = _pgrep(pattern)
        return {"running": bool(pids) if pids is not None else None,
                "pids": pids,
                "note": None if pids is not None else "pgrep 不可用"}

    def in_container_proc(pattern):
        pids, ns = container_pgrep(sw, pattern)
        return {"running": bool(pids) if pids is not None else None,
                "pids": pids, "namespace": ns}

    procs["run_all_batches"] = host_only(r"run_all_batches[.]sh")
    procs["run_swe_bench_pro"] = in_container_proc(r"run_swe_bench_pro[.]sh")
    procs["evalscope_eval"] = in_container_proc(r"evalscope eval")
    # 组合 watcher v6（[前置字符非 _] 排除旧版 jest_staged_watch.sh）
    procs["staged_watch"] = in_container_proc(r"(^|[^_])staged_watch[.]sh")
    procs["jest_staged_watch"] = in_container_proc(r"jest_staged_watch[.]sh")
    procs["review_guard"] = host_only(r"review_guard[.]sh")
    relay = host_only(r"docker_unix_relay[.]py")
    relay["socket_reachable"] = relay_reachable(sw.get("relay_socket"))
    # socket 可达是最权威判活（宿主/容器两侧都能探测）
    if relay["socket_reachable"]:
        relay["running"] = True
    procs["docker_relay"] = relay
    return procs


# --------------------------------------------------------------- status ----
def collect_status(sw: dict) -> dict:
    """聚合全部只读状态; 任何一块失败不影响其余（各块独立 try/except）。"""
    batches = parse_batches_log(sw["batches_log"])
    blog = latest_batch_log(sw, batches)
    progress = find_progress(blog)
    # 全量口径: 当前批 offset + 批内进度 n
    if progress and batches.get("current") is not None:
        progress["overall_n"] = batches["current"]["offset"] + progress["n"]
        progress["overall_total"] = batches.get("total")
    # rerun 模式: 进度行/日志在 logs/rerun_part*.log（无 batch log）。
    # 最新 rerun 日志 15 分钟内有写入、且比当前 batch log 新 = rerun 推进中,
    # 进度改从它解析——否则状态页的"进度"行会一直挂着 stale 的 batch 进度。
    rlog = latest_rerun_log(sw)
    rerun_active = bool(
        rlog and time.time() - os.path.getmtime(rlog) < 15 * 60
        and (blog is None or os.path.getmtime(rlog) > os.path.getmtime(blog)))
    if rerun_active:
        progress = find_progress(rlog)
    eval_rc = parse_eval_rc(sw["eval_rc_log"])
    rerun = parse_rerun_ids(sw["rerun_ids"])
    rerun.pop("groups", None)   # 轻量: status 只带计数, 明细走 rerun-list
    # 评测命令: 实跑进程口径优先（原文零出入）, 桥接 state 兜底
    eval_cmds = running_eval_cmds(sw)
    eval_cmd_fallback = None if eval_cmds else bridge_launch_cmd(sw)
    return {
        "ok": True,
        "namespace": "container" if IN_CONTAINER else "host",
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "work_dir": sw["work_dir"],
        "results_dir": sw["results_dir"],
        "model": sw.get("model"),
        "api_url": sw.get("api_url"),
        "batches": batches,
        "current_batch_log": blog,
        "progress": progress,
        "eval_rc": eval_rc,
        "processes": processes_status(sw),
        "binfmt": binfmt_status(),
        "disk": disk_status(sw.get("disk_mount")),
        "rerun": rerun,
        "rerun_log": rlog,
        "rerun_active": rerun_active,
        "eval_cmds": eval_cmds,
        "eval_cmd_fallback": eval_cmd_fallback,
        "log_mtime": {
            "batches_log": _mtime_str(sw["batches_log"]),
            "current_batch_log": _mtime_str(blog) if blog else None,
            "staged_watch_log": _mtime_str(os.path.join(sw["work_dir"],
                                                        "staged_watch.log")),
            "review_guard_log": _mtime_str(os.path.join(sw["work_dir"],
                                                        "review_guard.log")),
        },
    }


def _proc_mark(v: dict) -> str:
    if v.get("running") is None:
        return "?" if not IN_CONTAINER else "不可见"
    return "运行中" if v["running"] else "未运行"


def cmd_status(sw: dict, as_json: bool) -> int:
    st = collect_status(sw)
    if as_json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    b = st["batches"]
    print("=" * 72)
    print("SWE-bench_Pro 评测状态（只读聚合）  " + st["collected_at"]
          + f"   [namespace={st['namespace']}]")
    print("=" * 72)
    print(f"work-dir: {st['work_dir']}")
    if not b["exists"]:
        print(f"批次日志缺失: {sw['batches_log']}（尚未开始评测?）")
    else:
        done = b["finished_count"]
        cur = b.get("current")
        cur_s = "-"
        if cur is not None:
            state = "已完成" if cur["done"] else "进行中"
            cur_s = (f"offset={cur['offset']} count={cur['count']} {state}"
                     + (f"（开始 {cur['started']}）" if cur["started"] else ""))
            if b["window"]:
                cur_s += f"  第 {done + 1} 批左右"
        print(f"批次: 已完成 {done} 批  window={b['window']} "
              f"total={b['total']} start_offset={b['start_offset']}")
        print(f"当前批: {cur_s}" + ("  [all done]" if b["all_done"] else ""))
    p = st["progress"]
    if p is None:
        print("进度: 无 tqdm 进度行（当前批日志缺失或尚未开始评）"
              + (f"  日志: {st['current_batch_log']}" if st['current_batch_log'] else ""))
    else:
        overall = ""
        if p.get("overall_total"):
            overall = f"  全量 {p['overall_n']}/{p['overall_total']}"
        ts = f"  @{p['ts']}" if p.get("ts") else ""
        print(f"进度: {p['n']}/{p['total']} ({p['pct']}%)  "
              f"已跑 {p['elapsed']}  ETA {p['eta']} ({p['rate']}{p.get('rate_unit') or 's/it'})"
              f"{overall}{ts}")
        print(f"      来源: {p['log']}")
    rc = st["eval_rc"]
    if not rc["exists"]:
        print(f"崩溃标记: {sw['eval_rc_log']} 尚无记录（当前批未结束属正常; "
              f"该记录 2026-10-31 03:50 后的驱动脚本才有）")
    else:
        last = rc["last"]
        if last is None:
            print(f"崩溃标记: {sw['eval_rc_log']} 存在但无记录行")
        elif last["rc"] != 0:
            print(f"崩溃标记: 最近一批 offset={last['offset']} rc={last['rc']}"
                  f"（evalscope 异常退出, 检查 review 数据是否被事务性丢弃）")
        else:
            print(f"崩溃标记: 最近一批 offset={last['offset']} rc=0（正常结束）")
        if rc["crash_offsets"]:
            print(f"      历史崩溃批: {rc['crash_offsets']}")
    print("进程:")
    procs = st["processes"]
    for key in ("run_all_batches", "evalscope_eval", "run_swe_bench_pro",
                "staged_watch", "jest_staged_watch", "review_guard",
                "docker_relay"):
        v = procs[key]
        pids = v.get("pids")
        pid_s = f" (pid {','.join(str(x) for x in pids)})" if pids else ""
        note = f"  {v['note']}" if v.get("note") else ""
        extra = ""
        if key == "docker_relay" and v.get("socket_reachable"):
            extra = "  socket 可达"
        print(f"  {key:20s} {_proc_mark(v)}{pid_s}{note}{extra}")
    bf = st["binfmt"]
    if bf["visible"]:
        print(f"binfmt qemu-x86_64: {'已注册' if bf['registered'] else '未注册'}"
              f"  enabled={bf['enabled']} flags={bf['flags']} "
              f"interpreter={bf['interpreter']}")
    else:
        print("binfmt qemu-x86_64: 容器内不可见（宿主侧: "
              "cat /proc/sys/fs/binfmt_misc/qemu-x86_64）")
    dk = st["disk"]
    if dk["visible"]:
        print(f"磁盘 {dk['mount']}: {dk['avail']} 可用 ({dk['used_pct']} 已用)")
    else:
        print(f"磁盘 {dk['mount']}: 容器内不可见")
    rr = st["rerun"]
    if rr["exists"]:
        print(f"补跑队列: {rr['count']} 条  ({rr['file']})"
              f"  明细: python3 swe_runner.py rerun-list")
    else:
        print(f"补跑队列: 清单缺失 ({rr['file']})")
    if st.get("rerun_log"):
        print(f"rerun 日志: {st['rerun_log']}"
              + ("（近 15 分钟有写入, 推进中; tail -f 看实时）" if st.get("rerun_active")
                 else ""))
    cmds = st.get("eval_cmds") or ([st["eval_cmd_fallback"]] if st.get("eval_cmd_fallback") else [])
    if cmds:
        print("评测命令:")
        for c in cmds:
            print(f"  {c}")
    if st["namespace"] == "container":
        print(f"注: 本进程在容器内, {HOST_ONLY_NOTE}")
    return 0


# --------------------------------------------------------------- aggregate ----
def cmd_aggregate(sw: dict, model: str) -> int:
    """调仓库内聚合器副本, 透传输出（argv 传参覆盖副本内默认, 原件不受影响）。"""
    if not os.path.isfile(AGGREGATE_PY):
        print(f"缺少聚合器副本: {AGGREGATE_PY}", file=sys.stderr)
        return 2
    if not os.path.isdir(os.path.join(sw["results_dir"], "reviews")):
        print(f"work-dir 下没有 reviews 目录: {sw['results_dir']}（还没有已评分结果）")
        return 1
    cmd = [sys.executable, AGGREGATE_PY, sw["results_dir"]] + \
          ([model] if model else [])
    try:
        return subprocess.run(cmd).returncode
    except OSError as e:
        print(f"聚合器执行失败: {e}", file=sys.stderr)
        return 1


# --------------------------------------------------------------- rerun-list ----
def cmd_rerun_list(sw: dict, head_n: int, as_json: bool) -> int:
    rr = parse_rerun_ids(sw["rerun_ids"], sample_n=head_n)
    if as_json:
        print(json.dumps(rr, ensure_ascii=False, indent=2))
        return 0 if rr["exists"] else 1
    if not rr["exists"]:
        print(f"补跑清单不存在: {rr['file']}")
        return 1
    print(f"补跑清单: {rr['file']}")
    print(f"待补跑实例: {rr['count']} 条（注释分组 {rr['comment_count']} 行）")
    if rr["groups"]:
        print("\n分组摘要（注释 → 其后条数）:")
        for g in rr["groups"]:
            print(f"  {g['count']:4d}  {g['comment']}")
    print(f"\n前 {len(rr['sample'])} 条:")
    for iid in rr["sample"]:
        print(f"  {iid}")
    if rr["count"] > len(rr["sample"]):
        print(f"  ... 其余 {rr['count'] - len(rr['sample'])} 条略")
    return 0


# --------------------------------------------------------------- env-check ----
def _container_running(sw: dict):
    """容器运行状态: 容器模式恒 True; 宿主模式 docker inspect。"""
    if IN_CONTAINER:
        return True, "本进程就在 %s 容器内" % sw.get("container")
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}",
                            str(sw.get("container"))],
                           capture_output=True, text=True, timeout=15)
        running = (r.stdout or "").strip() == "true"
        return running, "docker inspect State.Running=" + (r.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"docker 不可用: {e}"


def _venv_file_patched(sw: dict, rel: str, marker: str):
    """检查容器 venv 里 evalscope 文件是否已打补丁（marker 注释行）。
    容器模式直接读; 宿主模式 docker exec grep。返回 (ok, detail)。"""
    if IN_CONTAINER:
        pats = glob.glob(os.path.join(sw["venv"], "lib", "python*",
                                      "site-packages", "evalscope", rel))
        if not pats:
            return False, f"容器内找不到 {sw['venv']}/lib/python*/site-packages/evalscope/{rel}"
        try:
            return marker in open(pats[0], encoding="utf-8",
                                  errors="replace").read(), pats[0]
        except OSError as e:
            return False, str(e)
    # glob 路径（python*）不能整体加引号, 否则容器 bash 不展开——venv 路径
    # 含空格时才需人工处理（config 默认 /tmp/bench-venv 无空格）
    glob_path = os.path.join(sw["venv"], "lib", "python*", "site-packages",
                             "evalscope", rel)
    shell = f"grep -l -F {shlex.quote(marker)} {glob_path}"
    try:
        r = subprocess.run(["docker", "exec", str(sw.get("container")),
                            "bash", "-c", shell],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0, (r.stdout or "").strip() or shell
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f"docker exec 失败: {e}"


def cmd_env_check(sw: dict) -> int:
    """对照 tools/prepare_swe_bench_env.sh 各步做存在性检查, 只检查不修复。"""
    items = []

    def add(item, ok, detail, warn_only=False):
        status = "ok" if ok else ("warn" if ok is None or warn_only else "fail")
        items.append({"item": item, "status": status, "detail": detail})

    add("评测容器 %s" % sw["container"], *_container_running(sw))
    sock = sw["relay_socket"]
    reach = relay_reachable(sock)
    if IN_CONTAINER:
        add("docker unix relay", reach,
            f"{sock} {'可达(GET /version 200)' if reach else '不可达'}（进程在宿主, 容器内以 socket 探测）")
    else:
        pids = _pgrep(r"docker_unix_relay[.]py")
        running = bool(pids) if pids is not None else None
        add("docker unix relay", running and reach,
            f"进程 pid={pids}; socket {sock} "
            f"{'可达(GET /version 200)' if reach else '不可达'}")
    bf = binfmt_status()
    if bf["visible"]:
        add("qemu-x86_64 binfmt", bf["registered"] and bf["enabled"],
            f"flags={bf['flags']} interpreter={bf['interpreter']}"
            + ("" if bf["registered"] else "（条目未注册, 跑 tools/ensure_qemu_binfmt.sh）"))
    else:
        add("qemu-x86_64 binfmt", None,
            "容器内 /proc/sys/fs/binfmt_misc 不可见; 宿主侧: "
            "cat /proc/sys/fs/binfmt_misc/qemu-x86_64", warn_only=True)
    ok, detail = _venv_file_patched(
        sw, os.path.join("benchmarks", "swe_bench_pro", "utils.py"),
        "# swe-bench-pro-testpatch")
    add("venv 补丁: 金标 test_patch 进容器", ok,
        detail + ("" if ok else "（跑 tools/prepare_swe_bench_env.sh 第 3 步重打）"))
    ok, detail = _venv_file_patched(
        sw, os.path.join("api", "dataset", "loader.py"),
        "# evalscope-instance-filter")
    add("venv 补丁: INSTANCE_FILE 过滤器", ok,
        detail + ("" if ok else "（跑 tools/prepare_swe_bench_env.sh 第 3 步重打）"))
    run_scripts = os.path.join(sw["repo_path"], "run_scripts")
    add("SWE-bench_Pro-os 仓库", os.path.isdir(run_scripts),
        f"{run_scripts}" + ("" if os.path.isdir(run_scripts)
                            else "（需克隆并固定 commit ca10a60, 见 README）"))
    add("live 工作区", os.path.isdir(sw["work_dir"]), sw["work_dir"])

    print("SWE 环境检查（对照 tools/prepare_swe_bench_env.sh, 只检查不修复）:")
    fails = 0
    for it in items:
        mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[it["status"]]
        print(f"  [{mark}] {it['item']}: {it['detail']}")
        if it["status"] == "fail":
            fails += 1
    if IN_CONTAINER:
        print(f"  注: 容器内运行, 宿主侧项（relay 进程/binfmt/磁盘）判据受限")
    if fails:
        print(f"  {fails} 项失败。完整环境准备（宿主机执行, 幂等）: "
              f"bash tools/prepare_swe_bench_env.sh")
        return 1
    print("  环境就绪。")
    return 0


# --------------------------------------------------------------- start-rerun ----
def check_double_driver(sw: dict) -> list:
    """补跑执行前置检查: 任何在跑的评测驱动都必须拒绝（双驱动会互相
    覆盖 reviews / 抢镜像 / 打爆并发）。返回 [(是否硬拒绝, 文案)]。"""
    problems = []
    pids, ns = container_pgrep(sw, r"evalscope eval")
    if pids:
        problems.append((True, f"容器内已有 evalscope eval 进程 (pid {pids})——"
                        "双驱动会互相覆盖 review 数据, 拒绝启动"))
    elif pids is None:
        problems.append((False, "无法探测容器内 evalscope eval 进程"
                        "（docker 不可用）, 请人工确认后再 --yes"))
    if IN_CONTAINER:
        problems.append((False, "容器内看不到宿主 run_all_batches.sh 驱动, "
                        "请确认全量已跑完（宿主: pgrep -f run_all_batches）"))
    else:
        drv = _pgrep(r"run_all_batches[.]sh")
        if drv:
            problems.append((True, f"宿主 run_all_batches.sh 驱动仍在跑 "
                            f"(pid {drv})——它随时启动下一批, 与补跑撞车"))
    return problems


def build_rerun_plan(sw: dict, batch_size: int, parts: int) -> dict:
    """补跑计划: rerun_ids.txt 按非注释行均分 parts 份, 顺序（非并行）执行。

    - EVALSCOPE_DATASET_OFFSET=0 + --limit 条数+8（limit 必须大于清单数,
      loader 的 INSTANCE_FILE 过滤才作用得上）
    - 分多份顺序跑: 一份被 straggler 崩掉只丢这一份的 review
      （recover 脚本可救）, 不至于全量补跑一次蒸发
    - 用 swe_pro 原目录的 run_swe_bench_pro.sh（live 版, 带 eval_rc 记录与
      批后快照）; 仓库内 scripts/swe 副本仅存档
    """
    rr = parse_rerun_ids(sw["rerun_ids"], include_ids=True)
    ids = rr.get("ids") or []
    if not ids:
        return None
    parts = max(1, min(parts, len(ids)))
    chunks, step = [], (len(ids) + parts - 1) // parts
    for i in range(0, len(ids), step):
        chunks.append(ids[i:i + step])
    files = [os.path.join(sw["work_dir"], f"rerun_part{i + 1}.txt")
             for i in range(len(chunks))]
    inner = []
    for i, chunk in enumerate(chunks):
        log = os.path.join(sw["logs_dir"], f"rerun_part{i + 1}.log")
        inner.append(
            f"EVALSCOPE_INSTANCE_FILE={shlex.quote(files[i])} "
            f"EVALSCOPE_DATASET_OFFSET=0 bash run_swe_bench_pro.sh "
            f"--limit {len(chunk) + 8} --eval-batch-size {batch_size} "
            f"> {shlex.quote(log)} 2>&1")
    # 顺序串联（; 不是 &）: 每份跑完再起下一份, 全程只有一个 eval 进程
    shell = "cd %s && %s" % (shlex.quote(sw["work_dir"]), "; ".join(inner))
    return {"ids": ids, "chunks": chunks, "files": files,
            "count": len(ids), "parts": len(chunks),
            "docker_cmd": ["docker", "exec", "-d", str(sw["container"]),
                           "bash", "-c", shell],
            "shell": shell}


def cmd_start_rerun(sw: dict, yes: bool, batch_size: int, parts: int) -> int:
    if IN_CONTAINER:
        print("start-rerun 需在宿主机执行（容器内无 docker CLI, 且看不到宿主驱动）",
              file=sys.stderr)
        return 2
    plan = build_rerun_plan(sw, batch_size, parts)
    if plan is None:
        print(f"补跑清单为空或缺失: {sw['rerun_ids']}", file=sys.stderr)
        return 1
    problems = check_double_driver(sw)
    print(f"补跑计划: {plan['count']} 条 → 分 {plan['parts']} 份顺序跑 "
          f"(batch_size={batch_size})")
    for i, (chunk, f) in enumerate(zip(plan["chunks"], plan["files"])):
        print(f"  part{i + 1}: {len(chunk)} 条 → {f} "
              f"(--limit {len(chunk) + 8})")
    print("\n启动命令（docker exec -d, 单条 bash -c 内顺序串联, 防双驱动）:")
    print("  " + " ".join(shlex.quote(c) for c in plan["docker_cmd"]))
    if problems:
        print("\n前置检查:")
        for hard, msg in problems:
            print(f"  {'拒绝' if hard else '提示'}: {msg}")
        if any(h for h, _ in problems):
            print("\n存在硬拒绝项, 不执行（当前评测进行中, 勿并行补跑）。")
            return 3
    print("\n提示:")
    print("  - 启动前确认 staged_watch 看守活着（补跑也会起 run_script, 需要"
          "jest/go 补丁保护; 死了就重拉: docker exec -d ais-bench bash -c "
          "'cd %s && setsid nohup bash staged_watch.sh >/dev/null 2>&1 &'）"
          % sw["work_dir"])
    print("  - 补跑完成后: python3 swe_runner.py aggregate 查看最新聚合")
    if not yes:
        print("\n[dry-run] 只打印命令未执行; 确认无冲突后加 --yes 真正启动。")
        return 0
    # --yes: 写分片文件（只新建, 不动已有文件）→ 再查一次双驱动 → 启动
    for hard, msg in problems:
        if hard:
            print(f"拒绝执行: {msg}", file=sys.stderr)
            return 3
    for i, (chunk, f) in enumerate(zip(plan["chunks"], plan["files"])):
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("# split from rerun_ids.txt by swe_runner start-rerun "
                     f"@{time.strftime('%F %T')} part{i + 1}/{plan['parts']}\n")
            for iid in chunk:
                fh.write(iid + "\n")
        print(f"已写分片: {f} ({len(chunk)} 条)")
    try:
        r = subprocess.run(plan["docker_cmd"], capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"docker exec 启动失败: {e}", file=sys.stderr)
        return 1
    if r.returncode != 0:
        print(f"docker exec 返回 {r.returncode}: {r.stderr or r.stdout}",
              file=sys.stderr)
        return 1
    print("补跑已启动（detached）。日志: "
          + ", ".join(os.path.join(sw["logs_dir"], f"rerun_part{i + 1}.log")
                      for i in range(plan["parts"])))
    print("跟踪: python3 swe_runner.py status")
    return 0


# --------------------------------------------------------------- main ----
def main():
    ap = argparse.ArgumentParser(
        description="SWE-bench_Pro 评测 runner（只读监控 + 补跑入口; "
                    "全量评测由 swe_pro 分批驱动负责）")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--work-dir", default=None,
                    help="live 工作区（默认取 config swe.work_dir; "
                         "results/logs/rerun 清单都从这里推）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="只读聚合当前评测状态")
    sp.add_argument("--json", action="store_true", help="输出结构化 JSON")

    sp = sub.add_parser("aggregate", help="聚合已评分结果（repo × resolved/acc）")
    sp.add_argument("--model", default=None, help="只聚合该模型目录（默认全部）")

    sp = sub.add_parser("rerun-list", help="补跑清单计数 + 分组摘要 + 样例")
    sp.add_argument("-n", "--head", type=int, default=30, help="样例条数")
    sp.add_argument("--json", action="store_true", help="输出结构化 JSON")

    sub.add_parser("env-check", help="环境前提检查（只检查不修复）")

    sp = sub.add_parser("start-rerun", help="补跑启动（默认 dry-run, --yes 执行）")
    sp.add_argument("--yes", action="store_true",
                    help="真正启动（默认只打印命令; 有评测进程则拒绝）")
    sp.add_argument("--eval-batch-size", type=int, default=None,
                    help="批内并发（默认 config swe.eval_batch_size）")
    sp.add_argument("--parts", type=int, default=2,
                    help="清单均分几份顺序跑（默认 2, 防一次崩溃全丢）")

    args = ap.parse_args()
    cfg = load_config(args.config)
    sw = resolve_swe_config(cfg, args.work_dir)

    if args.cmd == "status":
        return cmd_status(sw, args.json)
    if args.cmd == "aggregate":
        return cmd_aggregate(sw, args.model)
    if args.cmd == "rerun-list":
        return cmd_rerun_list(sw, max(0, args.head), args.json)
    if args.cmd == "env-check":
        return cmd_env_check(sw)
    if args.cmd == "start-rerun":
        bs = args.eval_batch_size if args.eval_batch_size is not None \
            else int(sw.get("eval_batch_size") or 16)
        return cmd_start_rerun(sw, args.yes, bs, args.parts)
    return 2


if __name__ == "__main__":
    sys.exit(main())
