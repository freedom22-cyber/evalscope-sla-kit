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
       "tag, public_dataset, concurrency [, tokenizer]"；
       public_dataset 字段也可写 local:<语料目录> 回放本地裁剪语料,
       目录由 tools/curate_weka_traces.py 生成）
    2. 构建 agentx profile 命令（宿主机直跑，setup.sh 已装 agentx-harness）
    3. url 取 service.url（去掉 /v1/chat/completions 后缀），endpoint 固定
       /v1/chat/completions；tokenizer 取 service.tokenizer_path（本地路径）
       或行内第 4 字段（HuggingFace 名）
    4. 每个变体独立输出目录: results/agentx/<tag>_<时间戳>/
    5. 跑完解析 output-artifact-dir 里的 summary/metrics json 并打印关键指标

AgentX 数据集变体（HuggingFace semianalysisai/cc-traces-weka-*）:
    full   semianalysis_cc_traces_weka_062126          # 全量 1M ctx, 135k 请求
    256k   semianalysis_cc_traces_weka_with_subagents_256k  # 256k ctx, 68k 请求
    本地裁剪语料（tools/curate_weka_traces.py 生成, local:<目录>）:
    weka8  local:datasets/weka8    # 80 会话 x 8 轮 x ~65k 输入 x ~80% 缓存命中
                                    # （glm5.3 实测: 810 请求, ISL p50 65.4k,
                                    #  理论前缀命中 77.75%, 详见 README AgentX 章节）
"""
import argparse
import glob
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

import yaml  # noqa: E402

DEFAULT_CONFIG = os.path.join(KIT_ROOT, "config.yaml")
AGENTX_DIR = os.path.join(KIT_ROOT, "results", "agentx")


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def parse_variant_line(line: str) -> dict:
    """解析变体行: "tag, public_dataset | local:<目录>, concurrency [, tokenizer]"。

    用正则而非裸 split(","): local: 语料目录路径可能含逗号, 硬切会错位。
    dataset 贪婪匹配到「, 整数(并发)」之前; tokenizer 为可选尾字段。
    """
    m = re.match(r"^\s*([^,]+?)\s*,\s*(.+?)\s*,\s*(\d+)\s*(?:,\s*(.+?)\s*)?$", str(line))
    if not m:
        raise ValueError(f"变体行格式错误（需 tag, dataset, concurrency [, tokenizer]）: {line!r}")
    tag, dataset, conc, tokenizer = m.group(1), m.group(2), m.group(3), (m.group(4) or "")
    if not tag or not dataset:
        raise ValueError(f"变体行 tag 与 public_dataset 不能为空: {line!r}")
    try:
        conc_i = int(conc)
    except ValueError:
        raise ValueError(f"变体行 concurrency 必须是整数: {line!r}")
    if conc_i < 1:
        raise ValueError(f"变体行 concurrency 必须 >= 1: {line!r}")
    # local:<目录> = 本地裁剪语料（tools/curate_weka_traces.py 生成）
    local_dir = dataset[len("local:"):] if dataset.startswith("local:") else ""
    return {"tag": tag, "dataset": dataset, "local_dir": local_dir,
            "concurrency": conc_i, "tokenizer": tokenizer, "raw": line}


def resolve_local_corpus(v: dict) -> dict:
    """校验 local: 语料并推导回放参数（entries / request-count 上限）。

    entries = 目录里会话 JSON 文件数; request-count 上限取自裁剪工具写的
    <目录>_stats.json 的 total_requests —— 没有上限时车道会把跑完的短会话
    回收重放, 挤掉裁剪窗口里的原始轮次。
    """
    d = v["local_dir"]
    if not os.path.isabs(d):
        d = os.path.join(KIT_ROOT, d)
    if not os.path.isdir(d):
        raise ValueError(f"[{v['tag']}] 本地语料目录不存在: {d}"
                         f"（先用 tools/curate_weka_traces.py 生成）")
    entries = len(glob.glob(os.path.join(d, "*.json")))
    if entries == 0:
        raise ValueError(f"[{v['tag']}] 本地语料目录没有会话 JSON: {d}")
    stats_path = d.rstrip("/") + "_stats.json"
    if not os.path.isfile(stats_path):
        raise ValueError(f"[{v['tag']}] 缺少 {stats_path}（request-count 上限取自其中"
                         f" total_requests; 请用 tools/curate_weka_traces.py 生成语料）")
    with open(stats_path, encoding="utf-8") as f:
        total = int(json.load(f).get("total_requests") or 0)
    if total < 1:
        raise ValueError(f"[{v['tag']}] {stats_path} 的 total_requests 无效: {total}")
    return {"dir": d, "entries": entries, "request_count": total}


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
        # tokenizer 写 HuggingFace 名时需要（官方 README 用法; 本地路径不受影响）
        "--tokenizer-trust-remote-code",
        "--concurrency", str(conc),
    ]
    if v["local_dir"]:
        corpus = resolve_local_corpus(v)
        # 本地裁剪语料固定用 inferencex-agentx-mvp 场景: 默认 agentx 场景会注入
        # warmup_requests_per_lane=10 的加速预热 —— 预热回放会真实消耗轮次
        # （8 轮短语料直接被吃光）, 且高并发时 ~N 条零间隔大 prefill 同时打出,
        # 排队网关（如 ModelArts）会杀掉排队过久的请求, 单条失败即整体中止。
        # mvp 场景无预设注入但保留 AGENTIC_REPLAY 回放时序, 下面显式补齐原预设值。
        cmd += [
            "--scenario", "inferencex-agentx-mvp",
            # 本地目录不是场景锁校验的 pinned 公共语料, 必须 override
            # （聚合结果会标记 submission_valid=false）
            "--unsafe-override",
            "--custom-dataset-type", "weka_trace",
            "--input-file", corpus["dir"],
            "--num-dataset-entries", str(corpus["entries"]),
            # 车道回收重放会挤掉裁剪轮次, 用裁剪工具算出的总量封顶
            "--request-count", str(corpus["request_count"]),
            # 从第 0 轮回放整个裁剪窗口（预设 0.25~0.75 会跳过短语料大部分轮次）
            "--trajectory-start-min-ratio", "0",
            "--trajectory-start-max-ratio", "0",
            # 不传 ramp 时 80 条车道 t=0 同时派发第 0 轮（全新 ~65k prefill）,
            # 与预热同款的零间隔突发, 排队网关会杀; 120s 斜坡错开车道起步
            "--concurrency-ramp-duration", "120",
            # 回放全长受会话跨度限制（weka8 实测 ~11 分钟, 最长会话 987s）;
            # 3600 与实测命令一致并留足慢端点余量（mvp 场景不传时自动填 1800）
            "--benchmark-duration", "3600",
            "--random-seed", "42",
            "--failed-request-threshold", "0.10",
            "--trace-idle-gap-cap-seconds", "300",
            "--use-server-token-count",
        ]
    else:
        cmd += ["--public-dataset", v["dataset"]]
    # 服务上下文上限（如 glm5.3 的 204800; 不设置则不传, 由服务端自行拒绝超长）
    max_ctx = svc.get("max_context_length")
    if max_ctx:
        cmd += ["--max-context-length", str(int(max_ctx))]
    cmd += ["--output-artifact-dir", work_dir]
    # Host 头: 过 LB/网关按域名路由时需要（agentx --header Header:Value）
    if req.get("add_host_header"):
        host_val = req.get("host_header") or ""
        if not host_val:
            try:
                from urllib.parse import urlsplit
                host_val = urlsplit(svc.get("url", "")).hostname or ""
            except Exception:
                host_val = ""
        if host_val:
            cmd += ["--header", f"Host:{host_val}"]
    # api_key: agentx --api-key 自动加 Authorization: Bearer
    api_key = req.get("api_key") or ""
    if api_key:
        cmd += ["--api-key", api_key]
    return cmd, work_dir


def parse_results(work_dir: str) -> dict:
    """解析 output-artifact-dir 里的指标 json，返回关键指标 dict。"""
    out = {}
    # aiperf(agentx-harness) 1.0.7 的指标文件名是 profile_export_aiperf.json
    # （实测产物里没有 *summary*/*metrics* 命名的文件, 不匹配会漏掉全部指标）
    for pat in ("**/profile_export_aiperf.json", "**/*summary*.json",
                "**/*metrics*.json", "**/*result*.json"):
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
