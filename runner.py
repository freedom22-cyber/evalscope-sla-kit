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
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
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
    if len(parts) != 7:
        raise ValueError(f"场景行格式错误（需 7 个逗号分隔字段）: {line!r}")
    dset, in_len, out_len, hit, pnum, ttft, tpot = parts
    if dset not in ("random", "gsm8k"):
        raise ValueError(f"数据集类型必须是 random/gsm8k: {line!r}")
    hit_rate = parse_hit_rate(hit)
    if dset == "random" and hit_rate != 0:
        raise ValueError(f"random 数据集不支持缓存命中率: {line!r}")
    return {
        "dataset": dset,
        "input_len": parse_len(in_len),
        "output_len": parse_len(out_len),
        "hit_rate": hit_rate,
        "prefix_num": int(pnum),
        "ttft_sla_ms": int(ttft),
        "tpot_sla_ms": int(tpot),
        "raw": line,
    }


def scenario_id(sc: dict) -> str:
    """场景 ID: random_1k_1k / gsm8k_64k_1k_hit30pct"""
    def kfmt(n: int) -> str:
        return f"{n // 1024}k" if n % 1024 == 0 else str(n)
    hit = "0" if sc["hit_rate"] == 0 else f"{sc['hit_rate']:.0%}".replace("%", "pct")
    return f"{sc['dataset']}_{kfmt(sc['input_len'])}_{kfmt(sc['output_len'])}_hit{hit}"


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    for key in ("service", "request", "benchmark", "dataset", "scenarios"):
        if key not in cfg:
            raise ValueError(f"config 缺少必需段: {key}")
    return cfg


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


def gsm8k_dataset_paths(cfg: dict, sc: dict) -> tuple:
    """返回 (jsonl_path, prompts_path)；文件名规则与 gen_dataset_only.py 一致"""
    tok_name = os.path.basename(os.path.normpath(cfg["service"]["tokenizer_path"]))
    n = cfg["dataset"]["gsm8k_number"]
    in_len = sc["input_len"]
    hit = sc["hit_rate"]
    if hit == 0:
        jsonl = os.path.join(DATASETS_DIR, f"GSM8K-in{in_len}-num{n}-{tok_name}.jsonl")
    else:
        rr = hit if hit < 1 else 1
        jsonl = os.path.join(
            DATASETS_DIR, f"GSM8K-in{in_len}-num{n}-{tok_name}-repeatRate{rr}.jsonl")
    prompts = jsonl.rsplit(".", 1)[0] + ".prompts.txt"
    return jsonl, prompts


def ensure_gsm8k_dataset(cfg: dict, sc: dict, dry_run: bool = False) -> str:
    """确保 gsm8k 数据集存在，返回 prompts.txt 路径。不存在则生成。"""
    jsonl_path, prompts_path = gsm8k_dataset_paths(cfg, sc)
    if os.path.exists(prompts_path) and not cfg["dataset"].get("force_regenerate"):
        return prompts_path
    if dry_run:
        print(f"    [dry-run] 将生成数据集: {prompts_path}")
        return prompts_path

    ds_cfg = cfg["dataset"]
    cmd = [
        sys.executable, os.path.join(TOOL_DIR, "gen_dataset_only.py"),
        "--tokenizer_path", cfg["service"]["tokenizer_path"],
        "--input_len", str(sc["input_len"]),
        "--number", str(ds_cfg["gsm8k_number"]),
        "--save_path", DATASETS_DIR,
        "--seed", str(ds_cfg["gsm8k_seed"]),
        "--prefix_num", str(sc["prefix_num"] or ds_cfg.get("gsm8k_prefix_num", 1)),
        "--dp", str(ds_cfg["gsm8k_dp"]),
    ]
    if sc["hit_rate"] > 0:
        cmd += ["--prefix_flag", "1", "--repeat_rate", str(sc["hit_rate"])]
    if ds_cfg.get("force_regenerate"):
        cmd.append("--force_regenerate")
    print(f"    生成数据集: {os.path.basename(jsonl_path)}")
    # gen_dataset_only.py 必须在 tool/ 目录下跑（GSM8K.jsonl 是相对路径）
    r = subprocess.run(cmd, cwd=TOOL_DIR)
    if r.returncode != 0:
        raise RuntimeError(f"数据集生成失败: {' '.join(cmd)}")
    if not os.path.exists(jsonl_path):
        raise RuntimeError(f"数据集生成后未找到: {jsonl_path}")

    # jsonl -> 纯文本 prompts（evalscope custom 插件要求）
    r = subprocess.run([sys.executable, os.path.join(TOOL_DIR, "jsonl_to_prompts.py"), jsonl_path])
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
        # evalscope 在线生成随机 prompt；min=max=精确长度
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
    r = subprocess.run(cmd)
    dt = time.time() - t0
    status = "OK" if r.returncode == 0 else f"FAILED({r.returncode})"
    print(f"  [{sid}] {status}  耗时 {dt / 60:.1f} 分钟")
    return r.returncode


def main():
    ap = argparse.ArgumentParser(description="evalscope 摸高测试 runner")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    ap.add_argument("--list", action="store_true", help="列出启用场景后退出")
    ap.add_argument("--only", default=None,
                    help="只跑指定场景 ID（逗号分隔多个），如 random_1k_1k,gsm8k_64k_1k_hit30pct")
    args = ap.parse_args()

    cfg = load_config(args.config)
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
