#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalscope 摸高测试 runner：解析 config.yaml，逐场景构建并执行 evalscope 命令。

用法:
    python3 runner.py --dry-run          # 只打印将要执行的命令，不执行
    python3 runner.py --only random_1k_1k  # 只跑指定场景（场景 ID 见 --list）
    python3 runner.py --list             # 列出所有启用场景
    python3 runner.py                    # 顺序执行所有启用场景

场景执行流程:
    1. gsm8k 场景: 先确保数据集存在（tools/gen_dataset_only.py 生成 +
       tools/jsonl_to_prompts.py 转纯文本），已存在则复用
    2. 构建 evalscope perf --sla-auto-tune 命令并顺序执行
    3. 每个场景独立 outputs-dir: results/<场景ID>/<时间戳>/
    4. time_budget_min>0 时预算到限终止该场景，并按已完成探测档算 partial 结论
       落盘 results/<场景ID>/partial_summary.json（最大达标并发是真实上限的下界）;
       health_check_interval_s>0 时压测期间定期探活 /v1/models，连续失败只告警
"""
import argparse
import glob
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT_ROOT)

try:
    import yaml
except ImportError:
    print("缺少 pyyaml，请先运行: pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_CONFIG = os.path.join(KIT_ROOT, "config.yaml")
DATASETS_DIR = os.path.join(KIT_ROOT, "datasets")
RESULTS_DIR = os.path.join(KIT_ROOT, "results")
LOGS_DIR = os.path.join(KIT_ROOT, "logs")
TOOL_DIR = os.path.join(KIT_ROOT, "tools")

# evalscope 对 custom 数据集的 prompt 过滤阈值 = input_len + 模板开销(~2000)
PROMPT_FILTER_MARGIN = 2000

# benchmark_summary.json 顶层指标键名（与 evalscope perf_constants 一致），partial 判定用
BS_TTFT = "Avg TTFT (ms)"
BS_TPOT = "Avg TPOT (ms)"
BS_OK = "Success Requests"
BS_FAIL = "Failed Requests"
BS_TOTAL = "Total Requests"
# 探测目录名形如 sla_parallel_4_run_1
PROBE_DIR_RE = re.compile(r"^sla_parallel_(\d+)_run_(\d+)$")

# 健康探活走直连（等价 curl --noproxy '*'，避免 http_proxy 环境变量把内网探活
# 路由到代理而误报服务无响应）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def parse_len(s: str) -> int:
    """'1k'/'8k'/'1000k' -> 1024/8192/1024000；'1024' -> 1024"""
    s = str(s).strip().lower()
    m = re.match(r"^(\d+(?:\.\d+)?)(k|kb)?$", s)
    if not m:
        raise ValueError(f"无法解析长度: {s!r}")
    val = float(m.group(1))
    if m.group(2):
        val *= 1024
    return int(val)


def parse_hit_rate(s: str) -> float:
    """'0'/'0.3'/'90%' -> 0.0/0.3/0.9"""
    s = str(s).strip()
    if s.endswith("%"):
        return float(s[:-1]) / 100.0
    return float(s)


def parse_scenario(line: str) -> dict:
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 7 or len(parts) > 8:
        raise ValueError(f"场景行格式错误（需 7-8 个逗号分隔字段）: {line!r}")
    dset, in_len, out_len, hit, pnum, ttft, tpot = parts[:7]
    dataset_num = parts[7] if len(parts) == 8 else None
    if dset not in ("random", "gsm8k"):
        raise ValueError(f"数据集类型必须是 random/gsm8k: {line!r}")
    hit_rate = parse_hit_rate(hit)
    pnum_val = int(pnum)
    if dset == "random":
        # random 走 evalscope 原生前缀（--prefix-length/--prefix-count），总输入 = 前缀 + 后缀
        if hit_rate >= 1:
            raise ValueError(f"random 数据集缓存命中率需 < 1（后缀长度不能为 0）: {line!r}")
        if hit_rate == 0 and pnum_val > 1:
            raise ValueError(f"random 数据集 prefix_num>1 需搭配缓存命中率>0: {line!r}")
    else:
        # gsm8k 前缀由数据集文件承载（hit=0 不生成前缀，prefix_num 无效）
        if hit_rate == 0 and pnum_val > 1:
            raise ValueError(f"gsm8k 数据集 hit_rate=0 时 prefix_num 必须为 1: {line!r}")
    sc = {
        "dataset": dset,
        "input_len": parse_len(in_len),
        "output_len": parse_len(out_len),
        "hit_rate": hit_rate,
        "prefix_num": pnum_val,
        "ttft_sla_ms": int(ttft),
        "tpot_sla_ms": int(tpot),
        "raw": line,
    }
    if dataset_num is not None and dataset_num != "":
        sc["dataset_num"] = int(dataset_num)
    return sc


def scenario_id(sc: dict) -> str:
    """场景 ID: random_1k_1k / gsm8k_64k_1k_hit30pct / gsm8k_64k_1k_hit90pct_pfx3"""
    def kfmt(n: int) -> str:
        return f"{n // 1024}k" if n % 1024 == 0 else str(n)
    hit = "0" if sc["hit_rate"] == 0 else f"{sc['hit_rate']:.0%}".replace("%", "pct")
    sid = f"{sc['dataset']}_{kfmt(sc['input_len'])}_{kfmt(sc['output_len'])}_hit{hit}"
    if sc.get("prefix_num", 1) > 1:
        sid += f"_pfx{sc['prefix_num']}"
    return sid


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in ("service", "request", "benchmark", "dataset", "scenarios"):
        if key not in cfg:
            raise ValueError(f"config 缺少必需段: {key}")
    return cfg


def check_tokenizer_path(cfg: dict) -> None:
    """预检 tokenizer_path：不存在时提前报错，避免 modelscope 退化成远程下载 404。"""
    tok = cfg.get("service", {}).get("tokenizer_path", "")
    if not tok:
        return
    if os.path.isdir(tok):
        return
    # 常见误用：推理集群路径在压测机上不存在
    raise FileNotFoundError(
        f"tokenizer_path 目录不存在: {tok}\n"
        f"  evalscope/modelscope 会把该路径当模型 ID 去 modelscope.cn 下载 → 404。\n"
        f"  请在配置页填本机可用的 tokenizer 目录（含 tokenizer_config.json）。"
    )


def build_headers(cfg: dict) -> list:
    """构建 --headers 参数列表（Host 开关在这里生效）"""
    req = cfg["request"]
    headers = []
    if req.get("add_host_header"):
        host_val = req.get("host_header") or ""
        if not host_val:
            # 自动取 url 中的主机名（不带端口）
            host_val = urlsplit(cfg["service"]["url"]).hostname or ""
        headers.append(f"Host={host_val}")
    for h in req.get("extra_headers") or []:
        headers.append(str(h))
    if req.get("api_key"):
        headers.append(f"Authorization=Bearer {req['api_key']}")
    return headers


def dataset_number(cfg: dict, sc: dict) -> int:
    """数据集条数：场景级 dataset_num -> 用例级覆盖 -> (input_len, hit_rate) 覆盖 -> 全局默认。

    场景行第 8 字段 dataset_num 优先；Web UI 按用例存条数
    （case_number_overrides[case_id]）；文件名含条数，
    相同 (input_len, hit_rate, 条数) 的用例天然复用同一数据集文件。
    """
    if sc.get("dataset_num") is not None:
        return int(sc["dataset_num"])
    ds = cfg["dataset"]
    case_ov = ds.get("case_number_overrides") or {}
    if sc.get("id") and sc["id"] in case_ov:
        return int(case_ov[sc["id"]])
    overrides = ds.get("number_overrides") or {}
    key = f"{sc['input_len']}_{sc['hit_rate']}"
    return int(overrides.get(key, ds["gsm8k_number"]))


def gsm8k_dataset_paths(cfg: dict, sc: dict) -> tuple:
    """返回 (jsonl_path, prompts_path)；文件名规则与 gen_dataset_only.py 一致"""
    tok_name = os.path.basename(os.path.normpath(cfg["service"]["tokenizer_path"]))
    n = dataset_number(cfg, sc)
    in_len = sc["input_len"]
    hit = sc["hit_rate"]
    if hit == 0:
        jsonl = os.path.join(DATASETS_DIR, f"GSM8K-in{in_len}-num{n}-{tok_name}.jsonl")
    else:
        rr = hit if hit < 1 else 1
        # 多前缀数据集文件名带 -pfxN，避免与单前缀互相错误复用
        pfx = f"-pfx{sc['prefix_num']}" if sc.get("prefix_num", 1) > 1 else ""
        jsonl = os.path.join(
            DATASETS_DIR, f"GSM8K-in{in_len}-num{n}-{tok_name}-repeatRate{rr}{pfx}.jsonl")
    prompts = jsonl.rsplit(".", 1)[0] + ".prompts.txt"
    return jsonl, prompts


def ensure_gsm8k_dataset(cfg: dict, sc: dict, dry_run: bool = False,
                         force: bool = False, log_handle=None) -> str:
    """确保 gsm8k 数据集存在，返回 prompts.txt 路径。不存在则生成。

    force=True 或 cfg dataset.force_regenerate 时强制重新生成。
    log_handle 传入文件对象时，子进程输出重定向到该文件（Web UI 用）。
    """
    jsonl_path, prompts_path = gsm8k_dataset_paths(cfg, sc)
    if os.path.exists(prompts_path) and not (force or cfg["dataset"].get("force_regenerate")):
        return prompts_path
    if dry_run:
        print(f"    [dry-run] 将生成数据集: {prompts_path}")
        return prompts_path

    ds_cfg = cfg["dataset"]
    cmd = [
        sys.executable, os.path.join(TOOL_DIR, "gen_dataset_only.py"),
        "--tokenizer_path", cfg["service"]["tokenizer_path"],
        "--input_len", str(sc["input_len"]),
        "--number", str(dataset_number(cfg, sc)),
        "--save_path", DATASETS_DIR,
        "--seed", str(ds_cfg["gsm8k_seed"]),
        "--prefix_num", str(sc["prefix_num"] or ds_cfg.get("gsm8k_prefix_num", 1)),
        "--dp", str(ds_cfg["gsm8k_dp"]),
    ]
    if sc["hit_rate"] > 0:
        cmd += ["--prefix_flag", "1", "--repeat_rate", str(sc["hit_rate"])]
    if ds_cfg.get("force_regenerate") or force:
        cmd.append("--force_regenerate")
    print(f"    生成数据集: {os.path.basename(jsonl_path)}")
    # gen_dataset_only.py 必须在 tool/ 目录下跑（GSM8K.jsonl 是相对路径）
    r = subprocess.run(cmd, cwd=TOOL_DIR, stdout=log_handle, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        raise RuntimeError(f"数据集生成失败: {' '.join(cmd)}")
    if not os.path.exists(jsonl_path):
        raise RuntimeError(f"数据集生成后未找到: {jsonl_path}")

    # jsonl -> 纯文本 prompts（evalscope custom 插件要求）
    r = subprocess.run([sys.executable, os.path.join(TOOL_DIR, "jsonl_to_prompts.py"), jsonl_path],
                       stdout=log_handle, stderr=subprocess.STDOUT)
    if r.returncode != 0 or not os.path.exists(prompts_path):
        raise RuntimeError(f"prompts 转换失败: {prompts_path}")
    return prompts_path


def build_evalscope_cmd(cfg: dict, sc: dict, dataset_path: str = None) -> list:
    """构建单场景 evalscope perf 命令"""
    svc, bench = cfg["service"], cfg["benchmark"]
    sid = scenario_id(sc)
    out_dir = os.path.join(RESULTS_DIR, sid)

    cmd = [
        "evalscope", "perf",
        "--model", svc["model"],
        "--tokenizer-path", svc["tokenizer_path"],
        "--url", svc["url"],
        "--api", "openai",
        "--apply-chat-template",
        "--max-tokens", str(sc["output_len"]),
        "--sla-auto-tune", "--sla-variable", "parallel",
        "--sla-params", json.dumps(
            [{"avg_ttft": f"<={sc['ttft_sla_ms']}", "avg_tpot": f"<={sc['tpot_sla_ms']}"}]),
        "--sla-lower-bound", str(bench["sla_lower_bound"]),
        "--sla-upper-bound", str(bench["sla_upper_bound"]),
        "--sla-num-runs", str(bench["sla_num_runs"]),
        "--sla-number-multiplier", str(bench["number_multiplier"]),
        "--warmup-num", str(bench["warmup_num"]),
        "--outputs-dir", out_dir,
    ]
    if bench.get("num_workers"):
        cmd += ["--num-workers", str(bench["num_workers"])]
    headers = build_headers(cfg)
    if headers:
        cmd += ["--headers"] + headers
    extra = bench.get("extra_args") or {}
    if extra:
        cmd += ["--extra-args", json.dumps(extra)]

    if sc["dataset"] == "random":
        # evalscope 在线生成随机 prompt；min=max=精确长度。
        # hit>0: 走 evalscope 原生多前缀（--prefix-length/--prefix-count，
        # 总输入 = 前缀 + 后缀，第 index 条请求挂第 index%K 个前缀），
        # 固定 seed 让前缀池跨次运行可复现；hit=0 保持无 seed（防跨次全 prompt 命中）
        if sc["hit_rate"] > 0:
            prefix_len = round(sc["input_len"] * sc["hit_rate"])
            suffix_len = sc["input_len"] - prefix_len
            cmd += [
                "--dataset", "random",
                "--min-prompt-length", str(suffix_len),
                "--max-prompt-length", str(suffix_len),
                "--prefix-length", str(prefix_len),
                "--prefix-count", str(sc["prefix_num"]),
            ]
            random_seed = cfg["dataset"].get("random_seed")
            if random_seed is not None:
                cmd += ["--seed", str(random_seed)]
        else:
            cmd += [
                "--dataset", "random",
                "--min-prompt-length", str(sc["input_len"]),
                "--max-prompt-length", str(sc["input_len"]),
                "--prefix-length", "0",
            ]
    else:
        # custom 数据集 + 过滤阈值（input_len + 模板开销）
        cmd += [
            "--dataset", "custom",
            "--dataset-path", dataset_path,
            "--max-prompt-length", str(sc["input_len"] + PROMPT_FILTER_MARGIN),
        ]
    return cmd


def health_probe_url(svc: dict) -> str:
    """健康探活地址: 服务 url 的 base + /v1/models

    注意不能只取 scheme://netloc——MaaS 网关的路径带应用 id
    (/v2/infer/<app-id>/v1/chat/completions), 丢掉前缀会探到一个网关不提供的
    路径, 压测期间误报"服务已挂"。
    """
    url = svc["url"].rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if url.endswith(suffix):
            url = url[: -len(suffix)]
            break
    return f"{url}/models"


def probe_alive(url: str) -> bool:
    """GET /v1/models 连通性检查: 任意 HTTP 状态码都算存活, URLError/超时算失败"""
    try:
        resp = _NO_PROXY_OPENER.open(url, timeout=5)
        resp.close()
        return True
    except urllib.error.HTTPError:
        return True  # 有 HTTP 响应（含 4xx/5xx）= 服务进程在, 只是该路径不可达
    except Exception:
        return False  # URLError / 超时 / 连接拒绝


def run_with_watchdog(cmd: list, cfg: dict) -> tuple:
    """执行压测命令, 支持预算看门狗 + 健康探活。返回 (killed, returncode)。

    - time_budget_min>0: 每 5s 轮询, 超预算 kill 后由调用方走 partial 判定
    - health_check_interval_s>0: 轮询期间按间隔探活 /v1/models, 连续失败只告警不杀进程
    - 两者都为 0 时保持原 subprocess.run 行为
    """
    bench = cfg["benchmark"]
    budget_min = bench.get("time_budget_min") or 0
    budget_s = budget_min * 60.0 if budget_min else 0
    itv_s = bench.get("health_check_interval_s") or 0
    if budget_s <= 0 and itv_s <= 0:
        return False, subprocess.run(cmd).returncode

    probe_url = health_probe_url(cfg["service"]) if itv_s > 0 else None
    t0 = time.time()
    p = subprocess.Popen(cmd)
    fail_n, next_probe = 0, t0 + itv_s
    while True:
        rc = p.poll()
        if rc is not None:
            return False, rc
        now = time.time()
        if budget_s > 0 and now - t0 >= budget_s:
            print(f"  [预算看门狗] 已达时间预算 {budget_min} 分钟, 终止该场景")
            p.kill()
            p.wait()
            return True, None
        if probe_url and now >= next_probe:
            if probe_alive(probe_url):
                fail_n = 0
            else:
                fail_n += 1
                print(f"  [健康探活] 服务无响应(连续 {fail_n} 次): {probe_url}")
                if fail_n >= 3:
                    print("  [健康探活] 疑似服务异常, 请检查服务端日志")
            next_probe = now + itv_s
        # 轮询步长 5s; 有探活/预算时对齐到下一个事件（下次探活 / 预算到期）, 保证间隔粒度
        wake = now + 5.0
        if probe_url:
            wake = min(wake, next_probe)
        if budget_s > 0:
            wake = min(wake, t0 + budget_s)
        time.sleep(max(0.1, wake - time.time()))


def _fnum(v):
    """安全转 float, 失败返回 None"""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _inum(v):
    """安全转 int, 失败返回 0"""
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def partial_verdict(results_dir: str, sid: str, sc: dict, budget_min) -> None:
    """预算到限被切断后的 partial 判定（借鉴 sunzhi sla_lib 规则）。

    逐档解析 <时间戳>/<model>/sla_tuning/sla_parallel_N_run_M/benchmark_summary.json:
    TTFT/TPOT 按 run 取算术平均, Success/Failed/Total 按 run 求和,
    缺 benchmark_summary.json 的档（被切断）跳过;
    达标 = Failed 之和为 0 且 TTFT/TPOT 双达标, selected = 达标档中最大 N（真实上限的下界）。
    全程不抛异常: 任何解析失败只打警告, 绝不影响主流程。
    """
    try:
        sdir = os.path.join(results_dir, sid)
        ts_dirs = sorted(d for d in glob.glob(os.path.join(sdir, "*")) if os.path.isdir(d))
        if not ts_dirs:
            print(f"  [partial] 未找到结果目录 {sdir}/<时间戳>/, 跳过 partial 判定")
            return
        mdir = ts_dirs[-1]  # 最新时间戳目录
        subs = sorted(d for d in glob.glob(os.path.join(mdir, "*")) if os.path.isdir(d))
        if subs:
            mdir = subs[0]  # 其下的模型目录（取第一个; 没有子目录就用本身）
        runs = {}
        for probe in sorted(glob.glob(os.path.join(mdir, "sla_tuning", "sla_parallel_*_run_*"))):
            mo = PROBE_DIR_RE.match(os.path.basename(probe))
            bs = os.path.join(probe, "benchmark_summary.json")
            if not mo or not os.path.isfile(bs):
                continue  # 无 benchmark_summary.json = 该档被切断, 跳过
            with open(bs, encoding="utf-8") as f:
                data = json.load(f)
            m = data if BS_TTFT in data else (data.get("metrics") or data)
            runs.setdefault(int(mo.group(1)), []).append({
                "ttft": _fnum(m.get(BS_TTFT)), "tpot": _fnum(m.get(BS_TPOT)),
                "succ": _inum(m.get(BS_OK)), "failed": _inum(m.get(BS_FAIL)),
                "total": _inum(m.get(BS_TOTAL)),
            })
        if not runs:
            print(f"  [partial] {mdir} 下没有已完成的探测档, 无 partial 结论")
            return
        probes = {}
        for n in sorted(runs):
            rs = runs[n]
            ttfts = [r["ttft"] for r in rs if r["ttft"] is not None]
            tpots = [r["tpot"] for r in rs if r["tpot"] is not None]
            ttft = round(statistics.fmean(ttfts), 1) if ttfts else None
            tpot = round(statistics.fmean(tpots), 2) if tpots else None
            failed = sum(r["failed"] for r in rs)
            probes[n] = {
                "ttft": ttft, "tpot": tpot,
                "succ": sum(r["succ"] for r in rs),
                "failed": failed,
                "total": sum(r["total"] for r in rs),
                "runs": len(rs),
                "pass": (failed == 0 and ttft is not None and tpot is not None
                         and ttft <= sc["ttft_sla_ms"] and tpot <= sc["tpot_sla_ms"]),
            }
        passing = [n for n, v in probes.items() if v["pass"]]
        selected = max(passing) if passing else None
        with open(os.path.join(sdir, "partial_summary.json"), "w", encoding="utf-8") as f:
            json.dump({"status": "partial", "budget_min": budget_min,
                       "selected": selected, "probes": probes},
                      f, ensure_ascii=False, indent=2)
        print(f"  [partial] 逐探测档判定 (SLA: TTFT<={sc['ttft_sla_ms']}ms / "
              f"TPOT<={sc['tpot_sla_ms']}ms)")
        print(f"    {'并发':>6} {'TTFT(ms)':>10} {'TPOT(ms)':>9} {'成功/总':>10} {'轮数':>4} {'判定':>6}")
        for n, v in probes.items():
            ttft_s = f"{v['ttft']:.0f}" if v["ttft"] is not None else "-"
            tpot_s = f"{v['tpot']:.2f}" if v["tpot"] is not None else "-"
            succ_s = f"{v['succ']}/{v['total']}"
            print(f"    {n:>6} {ttft_s:>10} {tpot_s:>9} {succ_s:>10} {v['runs']:>4} "
                  f"{'PASS' if v['pass'] else 'FAIL':>6}")
        sel = selected if selected is not None else "无"
        print(f"  摸高结论(partial): 最大达标并发={sel}"
              f"（真实上限的下界, 预算 {budget_min}min 到限终止）")
        print(f"  [partial] partial_summary.json 已写入: {os.path.join(sdir, 'partial_summary.json')}")
    except Exception as e:
        print(f"  [partial] 解析失败, 跳过 partial 判定: {e}")


def run_scenario(cfg: dict, sc: dict, dry_run: bool) -> int:
    sid = scenario_id(sc)
    print(f"\n{'=' * 70}\n[场景 {sid}]  {sc['raw']}")
    dataset_path = None
    if sc["dataset"] == "gsm8k":
        dataset_path = ensure_gsm8k_dataset(cfg, sc, dry_run)
    cmd = build_evalscope_cmd(cfg, sc, dataset_path)
    print("  命令: " + " ".join(shlex.quote(c) for c in cmd))
    if dry_run:
        return 0
    t0 = time.time()
    killed, rc = run_with_watchdog(cmd, cfg)
    dt = time.time() - t0
    if killed:
        # 预算到限被看门狗终止: 按已完成探测档算 partial 结论（真实上限的下界）
        print(f"  [{sid}] PARTIAL  耗时 {dt / 60:.1f} 分钟")
        partial_verdict(RESULTS_DIR, sid, sc, cfg["benchmark"].get("time_budget_min") or 0)
        return 0
    status = "OK" if rc == 0 else f"FAILED({rc})"
    print(f"  [{sid}] {status}  耗时 {dt / 60:.1f} 分钟")
    return rc


def main():
    ap = argparse.ArgumentParser(description="evalscope 摸高测试 runner")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    ap.add_argument("--list", action="store_true", help="列出启用场景后退出")
    ap.add_argument("--gen-datasets", action="store_true",
                    help="批量生成所有启用 gsm8k 场景的数据集（按文件名去重复用），不执行测试")
    ap.add_argument("--only", default=None,
                    help="只跑指定场景 ID（逗号分隔多个），如 random_1k_1k,gsm8k_64k_1k_hit30pct")
    # 兼容位: run.sh 用 -f/--foreground 决定前台/nohup 分支后原样透传, 此处仅接收不生效
    ap.add_argument("-f", "--foreground", action="store_true",
                    help="前台运行（实际由 run.sh 处理, runner 侧仅为兼容接收）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    check_tokenizer_path(cfg)
    scenarios = []
    for line in cfg["scenarios"]:
        line = str(line).strip()
        if not line or line.startswith("#"):
            continue
        scenarios.append(parse_scenario(line))
    if not scenarios:
        print("没有启用的场景（scenarios 段全被注释了）")
        return 0

    ids = [scenario_id(sc) for sc in scenarios]
    if args.list:
        for sid, sc in zip(ids, scenarios):
            print(f"{sid:40s}  {sc['raw']}")
        print(f"\n共 {len(ids)} 个启用场景")
        return 0
    if args.only:
        wanted = {w.strip() for w in args.only.split(",") if w.strip()}
        unknown = wanted - set(ids)
        if unknown:
            print(f"未知场景 ID: {unknown}\n可用 ID (--list 查看): {ids}", file=sys.stderr)
            return 2
        scenarios = [sc for sc in scenarios if scenario_id(sc) in wanted]

    os.makedirs(DATASETS_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)

    if args.gen_datasets:
        gsm8k_scenarios = [sc for sc in scenarios if sc["dataset"] == "gsm8k"]
        if not gsm8k_scenarios:
            print("没有启用的 gsm8k 场景，无需生成数据集")
            return 0
        seen = set()
        total = len(gsm8k_scenarios)
        done = 0
        skipped = 0
        for sc in gsm8k_scenarios:
            sid = scenario_id(sc)
            jsonl_path, prompts_path = gsm8k_dataset_paths(cfg, sc)
            key = os.path.basename(prompts_path)
            n = dataset_number(cfg, sc)
            if key in seen:
                print(f"  [复用] {sid} -> {key}")
                skipped += 1
                continue
            seen.add(key)
            if os.path.exists(prompts_path) and not cfg["dataset"].get("force_regenerate"):
                print(f"  [已存在] {sid} -> {key}  ({os.path.getsize(prompts_path)} bytes)")
                skipped += 1
                continue
            print(f"\n>>> 生成 {done + 1}/{total}: {sid}  (num={n})")
            try:
                ensure_gsm8k_dataset(cfg, sc, args.dry_run)
                done += 1
            except Exception as e:
                print(f"  [失败] {sid}: {e}")
        print(f"\n{'=' * 70}")
        print(f"数据集生成完成: 新生成 {done}，复用/已存在 {skipped}，共 {total} 个 gsm8k 场景")
        return 0

    print(f"目标服务: {cfg['service']['url']}  模型: {cfg['service']['model']}")
    print(f"启用场景: {len(scenarios)} 个")
    headers = build_headers(cfg)
    print(f"自定义请求头: {headers if headers else '（无）'}")

    t_start = time.time()
    failed = []
    for i, sc in enumerate(scenarios, 1):
        print(f"\n>>> 进度 {i}/{len(scenarios)}")
        try:
            rc = run_scenario(cfg, sc, args.dry_run)
        except Exception as e:
            print(f"  [异常] {e}")
            rc = 1
        if rc != 0:
            failed.append(scenario_id(sc))
    print(f"\n{'=' * 70}")
    print(f"全部完成，总耗时 {(time.time() - t_start) / 60:.1f} 分钟")
    if failed:
        print(f"失败场景 ({len(failed)}): {failed}")
        print("排查: results/<场景ID>/<时间戳>/<model>/sla_tuning/ 下各并发日志")
        return 1
    print("所有场景摸高完成。运行 python3 report.py 汇总结果。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
