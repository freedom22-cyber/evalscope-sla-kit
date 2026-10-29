#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""汇总所有场景的 evalscope 摸高结果，输出并发-指标总表 + 每场景最大达标并发。

含 SLA 判定（失败请求零容忍: Failed==0 且 TTFT/TPOT 双达标 -> PASS），
判据取自 config.yaml 对应场景行; config 不可用时优雅降级为不打印判定列。

用法:
    python3 report.py                     # 汇总 results/ 下全部场景
    python3 report.py --npu 16            # 按卡数折算单卡吞吐列
    python3 report.py --out report.md     # 额外写 markdown 报告文件

结果目录结构: results/<场景ID>/<时间戳>/<model>/sla_summary.json
  键 parallel_N -> {metrics: {...}, percentiles: {...}}
"""
import argparse
import glob
import json
import os
import sys
import time

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(KIT_ROOT, "results")
sys.path.insert(0, KIT_ROOT)

# SLA 判据表 {场景ID: (ttft_sla_ms, tpot_sla_ms)}，从 config.yaml 场景行解析;
# config 缺失/不可用时 SLA_MAP 为空，报告优雅降级（不打印判定列）。
# 兜底 SystemExit: runner.py 缺 pyyaml 时 import 阶段就会 sys.exit
try:
    from runner import parse_scenario, scenario_id

    import yaml
    with open(os.path.join(KIT_ROOT, "config.yaml"), encoding="utf-8") as f:
        _cfg = yaml.safe_load(f) or {}
    SLA_MAP = {}
    for _line in _cfg.get("scenarios") or []:
        _line = str(_line).strip()
        if not _line or _line.startswith("#"):
            continue
        try:
            _sc = parse_scenario(_line)
            SLA_MAP[scenario_id(_sc)] = (_sc["ttft_sla_ms"], _sc["tpot_sla_ms"])
        except (TypeError, ValueError):
            continue
except (Exception, SystemExit):
    SLA_MAP = {}

# evalscope 指标键名（perf_constants.py 中的标准名）
K_TTFT = "Avg TTFT (ms)"
K_TPOT = "Avg TPOT (ms)"
K_LAT = "Avg Latency (s)"
K_OK = "Success Requests"
K_FAIL = "Failed Requests"
K_TOTAL = "Total Requests"
K_OUT_TPS = "Output Throughput (tok/s)"
K_TOTAL_TPS = "Total Throughput (tok/s)"
K_IN_TPS = "Input Throughput (tok/s)"


def find_latest_summary(scenario_dir: str):
    """找场景目录下最新时间戳的 sla_summary.json"""
    paths = sorted(glob.glob(os.path.join(scenario_dir, "*", "*", "sla_summary.json")))
    return paths[-1] if paths else None


def load_scenario_sla(scenario_dir: str):
    """读 sla_summary.json，返回 {parallel: metrics_dict}。

    坏 JSON / 非对象 JSON 只告警跳过，不让单个坏文件打断整个汇总。
    """
    path = find_latest_summary(scenario_dir)
    if not path:
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [warn] {path} 解析失败, 跳过: {e}")
        return None, None
    if not isinstance(data, dict):
        print(f"  [warn] {path} 不是 JSON 对象, 跳过")
        return None, None
    rows = {}
    for k, v in data.items():
        if not k.startswith("parallel"):
            continue
        try:
            par = int(k.split("_")[1])
        except (IndexError, ValueError):
            continue
        m = v.get("metrics", {}) if isinstance(v, dict) else {}
        rows[par] = m or {}
    return rows, path


def fmt(v, nd=1):
    if v is None:
        return "-"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def sla_verdict(m: dict, lims: tuple) -> str:
    """失败请求零容忍: Failed==0 且 avg TTFT/TPOT 双达标 -> PASS, 否则 FAIL"""
    try:
        ttft, tpot = m.get(K_TTFT), m.get(K_TPOT)
        ok = ((m.get(K_FAIL) or 0) == 0
              and ttft is not None and tpot is not None
              and float(ttft) <= lims[0] and float(tpot) <= lims[1])
        return "PASS" if ok else "FAIL"
    except (TypeError, ValueError):
        return "FAIL"


def main():
    ap = argparse.ArgumentParser(description="evalscope 摸高结果汇总")
    ap.add_argument("--npu", type=int, default=0,
                    help="NPU 卡数（>0 时输出单卡吞吐列 = 总吞吐/卡数）")
    ap.add_argument("--out", default=None, help="额外输出 markdown 文件路径")
    args = ap.parse_args()

    # 只列摸高场景目录：跳过 accuracy/agentx/functional/_hixl_warmup 等非场景目录
    _NON_SCENARIO = {"accuracy", "agentx", "functional", "_hixl_warmup"}
    scenario_dirs = sorted(
        d for d in glob.glob(os.path.join(RESULTS_DIR, "*"))
        if os.path.isdir(d) and os.path.basename(d) not in _NON_SCENARIO)
    if not scenario_dirs:
        print(f"results/ 下没有场景结果目录，先跑 runner.py")
        return 1

    md_lines = []
    summary_rows = []

    print(f"evalscope 摸高结果汇总  ({time.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"{'=' * 110}")
    for sdir in scenario_dirs:
        sid = os.path.basename(sdir)
        rows, path = load_scenario_sla(sdir)
        if not rows:
            print(f"\n### {sid}: 未找到 sla_summary.json（场景未跑或未完成）")
            continue

        # 场景名解析: <dataset>_<in>_<out>_hit<pct>
        parts = sid.split("_")
        dset = parts[0]
        in_len = parts[1] if len(parts) > 1 else "?"
        out_len = parts[2] if len(parts) > 2 else "?"

        print(f"\n### {sid}")
        print(f"    来源: {path}")
        lims = SLA_MAP.get(sid)  # 该场景的 (ttft_sla_ms, tpot_sla_ms), 不在 config 则 None
        header = (f"    {'并发':>6} {'TTFT(ms)':>10} {'TPOT(ms)':>9} {'延迟(s)':>8} "
                  f"{'成功/总':>10} {'输出tok/s':>10} {'总tok/s':>10}")
        if args.npu:
            header += f" {'单卡出tok/s':>11} {'单卡总tok/s':>11}"
        if lims:
            header += f" {'判定':>6}"
        print(header)
        md_lines.append(f"\n## {sid}\n")
        md_header = "| 并发 | TTFT(ms) | TPOT(ms) | 延迟(s) | 成功/总 | 输出吞吐(tok/s) | 总吞吐(tok/s) |"
        if args.npu:
            md_header += " 单卡输出(tok/s) | 单卡总(tok/s) |"
        if lims:
            md_header += " 判定 |"
        md_lines.append(md_header)
        md_lines.append("|---|---|---|---|---|---|---|" + ("---|---|" if args.npu else "")
                        + ("---|" if lims else ""))

        for par in sorted(rows):
            m = rows[par]
            ok = m.get(K_OK) or 0
            total = m.get(K_TOTAL) or 0
            verdict = sla_verdict(m, lims) if lims else None
            line = (f"    {par:>6} {fmt(m.get(K_TTFT), 0):>10} {fmt(m.get(K_TPOT), 2):>9} "
                    f"{fmt(m.get(K_LAT), 1):>8} {f'{ok:.0f}/{total:.0f}':>10} "
                    f"{fmt(m.get(K_OUT_TPS), 1):>10} {fmt(m.get(K_TOTAL_TPS), 1):>10}")
            md_row = (f"| {par} | {fmt(m.get(K_TTFT), 0)} | {fmt(m.get(K_TPOT), 2)} | "
                      f"{fmt(m.get(K_LAT), 1)} | {ok:.0f}/{total:.0f} | "
                      f"{fmt(m.get(K_OUT_TPS), 1)} | {fmt(m.get(K_TOTAL_TPS), 1)} |")
            if args.npu:
                # 吞吐缺失显示 '-' 而不是 0.0
                out_ps = m.get(K_OUT_TPS)
                tot_ps = m.get(K_TOTAL_TPS)
                line += (f" {fmt(out_ps / args.npu if out_ps is not None else None, 1):>11} "
                         f"{fmt(tot_ps / args.npu if tot_ps is not None else None, 1):>11}")
                md_row += (f" {fmt(out_ps / args.npu if out_ps is not None else None, 1)} | "
                           f"{fmt(tot_ps / args.npu if tot_ps is not None else None, 1)} |")
            if lims:
                line += f" {verdict:>6}"
                md_row += f" {verdict} |"
            print(line)
            md_lines.append(md_row)
            summary_rows.append((sid, par, m))

        # SLA 结论: 最大达标并发（失败请求零容忍口径, 与 runner.py partial 判定一致）
        if lims:
            passing = [par for par in sorted(rows) if sla_verdict(rows[par], lims) == "PASS"]
            concl = (f"SLA 判定: 最大达标并发 = {max(passing)}" if passing
                     else "SLA 判定: 无达标档（Failed at lower bound）")
        else:
            concl = "SLA 判定: 跳过（config.yaml 无该场景或不可用）"
        print(f"    {concl}")
        md_lines.append("")
        md_lines.append(concl)

        print(f"    （详细百分位见 {os.path.dirname(path)}）")

    # 总览表：每场景最大并发（sla_summary 中已含 evalscope 的选择结果，
    # 这里按指标重算最大达标并发作为交叉验证）
    print(f"\n{'=' * 110}")
    print("说明: 每场景的 SLA 判据与达标并发以 evalscope 日志中的 Selections 表为准；")
    print("      上表为各并发的原始指标，可用 --npu 折算单卡吞吐。")
    if args.out:
        out_path = os.path.abspath(args.out)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"# evalscope 摸高结果汇总\n\n生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("\n".join(md_lines) + "\n")
        print(f"\nmarkdown 报告已写入: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
