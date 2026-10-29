#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cc-traces-weka-062126 本地裁剪语料生成工具。

从 HuggingFace `semianalysisai/cc-traces-weka-062126` 快照的 traces.jsonl 里
裁剪出目标画像的回放语料: 每个会话只保留前 N 个主轮次（窗口内派生的子智能体
会话原样保留）, 再按「平均输入长度 / 理论前缀缓存命中率」最接近目标值选出
最好的 M 个会话, 输出为每会话一个 JSON 的目录 —— agentx_runner.py 里用
`local:<目录>` 变体回放（--custom-dataset-type weka_trace --input-file）。

典型用法（80 会话 x 8 轮, 平均输入 65k, 缓存命中 80%, glm5.3 的 204.8k 上下文）:
    python3 tools/curate_weka_traces.py \
        --src <HF快照>/traces.jsonl \
        --out-dir datasets/weka8 \
        --main-turns 8 --sessions 80 \
        --target-isl 65000 --target-hit 0.80 \
        --max-context 204800 --idle-cap 300

输出:
    datasets/weka8/<会话id>.json    每会话一个文件（weka_trace 回放格式）
    datasets/weka8_stats.json       汇总（含 total_requests, 即回放 --request-count 上限）

说明:
    - in/out 字段是 KV cache 块数（block_size 默认 64）, 不是精确 token 数;
      --max-context 按记录峰值(峰值 in+out)过滤, 偏保守。
    - 理论缓存命中率由 hash_ids 指纹集合差推得: 每请求中未见过的块 = 未命中
      前缀, 1 - 新鲜块/全部输入块 即回放的理论前缀命中率。
    - --idle-cap 与回放的 --trace-idle-gap-cap-seconds 对齐（默认 300s）,
      会话内超过该值的轮间空档在回放时会被压缩。
"""
import argparse
import glob
import json
import os
import shutil
import statistics
import sys


def parse_args():
    ap = argparse.ArgumentParser(
        description="裁剪 cc-traces-weka-062126 为目标画像的本地回放语料")
    ap.add_argument("--src", required=True,
                    help="输入 traces.jsonl（HF 快照里的原始全量文件）")
    ap.add_argument("--out-dir", required=True,
                    help="输出语料目录（每会话一个 JSON; 汇总写到 <目录>_stats.json）")
    ap.add_argument("--main-turns", type=int, default=8,
                    help="每会话保留的主轮次数（默认 8）")
    ap.add_argument("--sessions", type=int, default=80,
                    help="选出的会话数（默认 80）")
    ap.add_argument("--target-isl", type=int, default=65000,
                    help="目标平均输入长度（token, 默认 65000）")
    ap.add_argument("--target-hit", type=float, default=0.80,
                    help="目标理论前缀缓存命中率（默认 0.80）")
    ap.add_argument("--max-context", type=int, default=204800,
                    help="丢弃裁剪后峰值超过该值的会话（默认 204800, glm5.3 上下文）")
    ap.add_argument("--idle-cap", type=float, default=300.0,
                    help="轮间空档压缩上限秒（默认 300, 对齐回放 --trace-idle-gap-cap-seconds）")
    args = ap.parse_args()
    if args.main_turns < 1 or args.sessions < 1:
        ap.error("--main-turns 与 --sessions 必须为正整数")
    if args.target_isl < 1:
        ap.error("--target-isl 必须为正整数")
    if not 0.0 < args.target_hit <= 1.0:
        ap.error("--target-hit 需在 (0, 1] 区间")
    if args.max_context < 1:
        ap.error("--max-context 必须为正整数")
    if args.idle_cap < 0:
        ap.error("--idle-cap 不能为负")
    return args


def events_of(requests):
    """全部可派发请求 [(start, end, req, is_sub), ...]（子请求时间相对派生点）。"""
    out = []
    for r in requests:
        if r.get("type") == "subagent":
            t0 = r.get("t", 0.0)
            for ir in r.get("requests", []):
                s = t0 + ir.get("t", 0.0)
                out.append((s, s + ir.get("api_time", 0.0), ir, True))
        else:
            s = r.get("t", 0.0)
            out.append((s, s + r.get("api_time", 0.0), r, False))
    out.sort(key=lambda e: e[0])
    return out


def truncate_trace(trace, main_turns):
    """会话裁剪: 保留前 main_turns 个主轮次, 窗口内派生的子会话整体保留。

    主轮次不足 main_turns 返回 None（调用方按“轮次过短”跳过）。
    """
    requests = trace["requests"]
    mains = [r for r in requests if r.get("type") != "subagent"]
    if len(mains) < main_turns:
        return None
    last = mains[main_turns - 1]
    cut = last.get("t", 0.0) + last.get("api_time", 0.0)
    kept, seen_mains = [], 0
    for r in requests:
        if r.get("type") == "subagent":
            if r.get("t", 0.0) <= cut:  # 窗口内派生: 子会话保持完整
                kept.append(r)
        elif seen_mains < main_turns:
            kept.append(r)
            seen_mains += 1
    out = dict(trace)
    out["requests"] = kept
    return out


def stats_of(trace, idle_cap):
    """裁剪后会话画像: 请求数/平均输入/主轮输入/峰值/理论命中率/有效跨度。"""
    bs = trace.get("block_size", 64)
    evs = events_of(trace["requests"])
    ins = [e[2].get("in", 0) for e in evs]
    outs = [e[2].get("out", 0) for e in evs]
    # 理论前缀命中率: hash_ids 指纹在本会话内首次出现 = 未命中块
    seen, fresh_tot, in_tot = set(), 0, 0
    for _, _, r, _ in evs:
        hs, i = r.get("hash_ids"), r.get("in", 0)
        if not hs or i <= 0:
            continue
        fresh = sum(1 for h in hs if h not in seen)
        seen.update(hs)
        fresh_tot += min(fresh * bs, i)
        in_tot += i
    hit = 1.0 - fresh_tot / in_tot if in_tot else 1.0
    # 有效跨度: 主轮之间超过 idle_cap 的空档按 idle_cap 计（对齐回放侧压缩）
    clock, prev_end = 0.0, None
    for s, e, _, is_sub in evs:
        if is_sub:
            continue
        clock += min(s - (prev_end or 0.0), idle_cap) + (e - s)
        prev_end = e
    return {
        "n_req": len(evs),
        "n_sub": sum(1 for e in evs if e[3]),
        "mean_isl": sum(ins) / len(ins) if ins else 0,
        "main_isl": statistics.fmean(e[2].get("in", 0) for e in evs if not e[3]),
        "peak": max((i + o for i, o in zip(ins, outs)), default=0),
        "hit": hit,
        "span": clock,
    }


def score_of(st, target_isl, target_hit):
    """与目标画像的距离: ISL 与命中率各按目标归一后相加（越小越接近）。"""
    return (abs(st["mean_isl"] - target_isl) / target_isl
            + abs(st["hit"] - target_hit) / target_hit)


def main():
    args = parse_args()
    srcs = sorted(glob.glob(args.src)) if any(c in args.src for c in "*?[") else [args.src]
    candidates, short, over, total = [], 0, 0, 0
    for src in srcs:
        with open(src, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                total += 1
                trace = json.loads(line)
                cut = truncate_trace(trace, args.main_turns)
                if cut is None:
                    short += 1
                    continue
                st = stats_of(cut, args.idle_cap)
                if st["peak"] > args.max_context:
                    over += 1
                    continue
                candidates.append((score_of(st, args.target_isl, args.target_hit), cut, st))
    print(f"解析 {total} 条 trace: {short} 条主轮次不足 {args.main_turns}, "
          f"{over} 条裁剪后峰值超 {args.max_context}, 候选 {len(candidates)} 条")
    # 稳定排序: 同分保持文件顺序, 保证可复现
    candidates.sort(key=lambda c: c[0])
    selected = candidates[:args.sessions]

    out_dir = args.out_dir.rstrip("/")
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    rows = []
    for score, cut, st in selected:
        with open(os.path.join(out_dir, f"{cut['id']}.json"), "w", encoding="utf-8") as f:
            json.dump(cut, f, separators=(",", ":"))
        rows.append({"id": cut["id"], "score": round(score, 4),
                     "n_req": st["n_req"], "n_sub": st["n_sub"],
                     "mean_isl": round(st["mean_isl"]), "main_isl": round(st["main_isl"]),
                     "peak": st["peak"], "hit": round(st["hit"], 3),
                     "span_s": round(st["span"])})
    isls = [r["mean_isl"] for r in rows]
    hits = [r["hit"] for r in rows]
    spans = [r["span_s"] for r in rows]
    summary = {
        "src": args.src, "n_candidates": len(candidates), "n_selected": len(rows),
        "total_requests": sum(r["n_req"] for r in rows),
        "mean_isl": round(statistics.fmean(isls)) if isls else 0,
        "isl_min": min(isls) if isls else 0, "isl_max": max(isls) if isls else 0,
        "mean_hit": round(statistics.fmean(hits), 3) if hits else 0.0,
        "hit_min": min(hits) if hits else 0.0, "hit_max": max(hits) if hits else 0.0,
        "main_isl_mean": round(statistics.fmean(r["main_isl"] for r in rows)) if rows else 0,
        "span_p50": round(statistics.median(spans)) if spans else 0,
        "span_max": max(spans) if spans else 0,
        "idle_cap_s": args.idle_cap,
        "target": {"isl": args.target_isl, "hit": args.target_hit,
                   "main_turns": args.main_turns, "max_context": args.max_context},
        "traces": rows,
    }
    stats_path = out_dir + "_stats.json"
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)

    print(f"\n语料: {len(rows)} 会话 -> {out_dir}")
    print(f"  总请求数: {summary['total_requests']}"
          f"（其中子智能体 {sum(r['n_sub'] for r in rows)}）")
    print(f"  平均输入(全部/仅主轮): {summary['mean_isl']} / {summary['main_isl_mean']}")
    print(f"  理论缓存命中率: 均值 {summary['mean_hit']}"
          f"（范围 {summary['hit_min']} ~ {summary['hit_max']}）")
    print(f"  有效跨度: p50 {summary['span_p50']}s / 最大 {summary['span_max']}s"
          f"（空档压缩 {args.idle_cap:.0f}s）")
    print(f"  汇总: {stats_path}")
    print(f"\n回放配置（agentx_runner.py 变体行）:")
    print(f'    - "weka8, local:{out_dir}, 80"')
    print(f"  request-count 上限 = total_requests = {summary['total_requests']}"
          f"（agentx_runner 自动从 {stats_path} 读取）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
