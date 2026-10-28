#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AgentX 智能体工作负载回放 runner（SemiAnalysis AgentX v1.0）。

用 agentx-harness 对 OpenAI 兼容服务回放公开的 Claude Code 会话 trace
（393 个真实智能体编码会话，含多轮/子智能体/前缀复用/KV cache 命中模式），
测的是真实智能体工作负载下的性能（TTFT/吞吐/缓存命中），不是精度。

用法:
    python3 agentx_runner.py --list             # 列出启用的 AgentX 数据集变体
    python3 agentx_runner.py --dry-run          # 只打印命令，不执行
    python3 agentx_runner.py --only 256k        # 只跑指定变体
    python3 agentx_runner.py --concurrency 16    # 覆盖并发会话数
    python3 agentx_runner.py                    # 顺序执行所有启用变体

执行流程:
    1. 解析 config.yaml agentx.datasets（每行一个变体:
       "tag, public_dataset, concurrency [, tokenizer]"）
    2. 构建 agentx profile 命令（宿主机直跑，setup.sh 已装 agentx-harness）
    3. url 取 service.url（去掉 /v1/chat/completions 后缀），endpoint 固定
       /v1/chat/completions；tokenizer 取 service.tokenizer_path（本地路径）
       或行内第 4 字段（HuggingFace 名）
    4. 每个变体独立输出目录: results/agentx/<tag>_<时间戳>/
    5. 跑完解析 output-artifact-dir 里的 summary/metrics json 并打印关键指标

AgentX 数据集变体（HuggingFace semianalysisai/cc-traces-weka-*）:
    full   semianalysis_cc_traces_weka_062126          # 全量 1M ctx, 135k 请求
    256k   semianalysis_cc_traces_weka_with_subagents_256k  # 256k ctx, 68k 请求
"""
import argparse
import glob
import json
import os
import shlex
import subprocess
import sys
import time
from urllib.parse import urlsplit

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT_ROOT)

import yaml  # noqa: E402

DEFAULT_CONFIG = os.path.join(KIT_ROOT, "config.yaml")
AGENTX_DIR = os.path.join(KIT_ROOT, "results", "agentx")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_variant_line(line: str) -> dict:
    """解析变体行: "tag, public_dataset, concurrency [, tokenizer]"（tokenizer 可选）。"""
    parts = [p.strip() for p in str(line).split(",")]
    if len(parts) not in (3, 4):
        raise ValueError(f"变体行格式错误（需 3-4 个逗号分隔字段）: {line!r}")
    tag, dataset, conc = parts[0], parts[1], parts[2]
    tokenizer = parts[3] if len(parts) == 4 else ""
    if not tag or not dataset:
        raise ValueError(f"变体行 tag 与 public_dataset 不能为空: {line!r}")
    try:
        conc_i = int(conc)
    except ValueError:
        raise ValueError(f"变体行 concurrency 必须是整数: {line!r}")
    if conc_i < 1:
        raise ValueError(f"变体行 concurrency 必须 >= 1: {line!r}")
    return {"tag": tag, "dataset": dataset, "concurrency": conc_i,
            "tokenizer": tokenizer, "raw": line}


def resolve_variants(cfg: dict) -> list:
    ax = cfg.get("agentx") or {}
    lines = ax.get("datasets") or []
    variants = []
    for line in lines:
        line = str(line).strip()
        if not line or line.startswith("#"):
            continue
        variants.append(parse_variant_line(line))
    return variants


def base_url_of(url: str) -> str:
    """service.url 是完整 chat completions 路径，agentx 要 base + endpoint 分开传。"""
    u = url.rstrip("/")
    for suffix in ("/chat/completions", "/completions"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    return u


def work_dir_of(tag: str) -> str:
    return os.path.join(AGENTX_DIR, f"{tag}_{time.strftime('%Y%m%d_%H%M%S')}")


def build_cmd(cfg: dict, v: dict, concurrency_override: int = None) -> tuple:
    """构建 agentx profile 命令；返回 (cmd, work_dir)。"""
    svc = cfg["service"]
    req = cfg.get("request") or {}
    conc = concurrency_override or v["concurrency"]
    work_dir = work_dir_of(v["tag"])
    # tokenizer: 行内字段 > service.tokenizer_path（本地路径直接用）
    tokenizer = v["tokenizer"] or svc.get("tokenizer_path") or ""
    cmd = [
        "agentx", "profile",
        "--url", base_url_of(svc["url"]),
        "--endpoint", "/v1/chat/completions",
        "--model", svc["model"],
        "--tokenizer", tokenizer,
        "--concurrency", str(conc),
        "--public-dataset", v["dataset"],
        "--output-artifact-dir", work_dir,
    ]
    # api_key: agentx --api-key 自动加 Authorization: Bearer
    api_key = req.get("api_key") or ""
    if api_key:
        cmd += ["--api-key", api_key]
    return cmd, work_dir


def parse_results(work_dir: str) -> dict:
    """解析 output-artifact-dir 里的指标 json，返回关键指标 dict。"""
    out = {}
    # 找 summary/metrics 类 json
    for pat in ("**/*summary*.json", "**/*metrics*.json", "**/*result*.json"):
        for p in glob.glob(os.path.join(work_dir, pat), recursive=True):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                out[os.path.basename(p)] = data
            except Exception:
                continue
        if out:
            break
    return out


def flatten_metrics(data: dict) -> list:
    """把嵌套的 metrics json 摊平成 (key, value) 列表（只取数值/字符串叶子）。"""
    rows = []

    def walk(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{prefix}.{ k}" if prefix else k)
        elif isinstance(obj, (int, float, str, bool)):
            rows.append((prefix, obj))

    walk(data)
    return rows


def run_variant(cfg: dict, v: dict, dry_run: bool, concurrency: int = None) -> int:
    cmd, work_dir = build_cmd(cfg, v, concurrency)
    print(f"\n{'=' * 70}")
    print(f"[AgentX {v['tag']}]  {v['raw']}   (并发: {concurrency or v['concurrency']})")
    print("  命令: " + " ".join(shlex.quote(c) for c in cmd))
    print(f"  输出目录: {work_dir}")
    if dry_run:
        return 0
    os.makedirs(work_dir, exist_ok=True)
    r = subprocess.run(cmd, cwd=KIT_ROOT)
    if r.returncode != 0:
        print(f"  [失败] exit={r.returncode}")
        return r.returncode
    # 解析并打印关键指标
    data = parse_results(work_dir)
    if data:
        print(f"\n  关键指标（{list(data.keys())}）:")
        for fname, d in data.items():
            for k, val in flatten_metrics(d)[:40]:
                print(f"    {fname}: {k} = {val}")
    else:
        print("  警告: 未找到 summary/metrics json（查看输出目录）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="AgentX 智能体工作负载回放")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    ap.add_argument("--list", action="store_true", help="列出启用变体后退出")
    ap.add_argument("--only", default=None, help="只跑指定变体 tag（逗号分隔）")
    ap.add_argument("--concurrency", type=int, default=None,
                    help="覆盖并发会话数（覆盖 config）")
    # 兼容位: run.sh 用 -f/--foreground 决定前台/nohup 分支后原样透传
    ap.add_argument("-f", "--foreground", action="store_true",
                    help="前台运行（实际由 run.sh 处理, 此处仅为兼容接收）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    variants = resolve_variants(cfg)
    if not variants:
        print("没有启用的 AgentX 变体（config.yaml agentx.datasets 全被注释了）")
        return 0

    if args.list:
        for v in variants:
            print(f"{v['tag']:12s}  dataset={v['dataset']}  concurrency={v['concurrency']}")
        print(f"\n共 {len(variants)} 个启用变体")
        return 0
    if args.only:
        wanted = {w.strip() for w in args.only.split(",") if w.strip()}
        unknown = wanted - {v["tag"] for v in variants}
        if unknown:
            print(f"未知变体: {unknown}\n可用 tag (--list 查看): "
                  f"{[v['tag'] for v in variants]}", file=sys.stderr)
            return 2
        variants = [v for v in variants if v["tag"] in wanted]

    svc = cfg.get("service") or {}
    print(f"目标服务: {svc.get('url')}  模型: {svc.get('model')}")
    print(f"启用 AgentX 变体: {len(variants)} 个")

    t_start = time.time()
    failed = []
    for v in variants:
        rc = run_variant(cfg, v, args.dry_run, args.concurrency)
        if rc != 0:
            failed.append(v["tag"])
    print(f"\n{'=' * 70}")
    print(f"全部完成，总耗时 {(time.time() - t_start) / 60:.1f} 分钟")
    if failed:
        print(f"失败变体 ({len(failed)}): {failed}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
