#!/usr/bin/env python3
"""compare_pools.py —— mooncake / memcache 两种外置 KV 池的摸高结果对照表。

用法:
    python3 compare_pools.py                      # 对比 results/<pool>/ 下的所有池
    python3 compare_pools.py mooncake memcache    # 指定池顺序

结果目录约定: 每个池一轮跑完后把 results/<sid>/ 整目录移到 results/<pool>/<sid>/，
本脚本读 results/<pool>/<sid>/<时间戳>/<model>/sla_summary.json。
"""
import glob
import json
import os
import sys

RESULTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

K_TTFT = "Avg TTFT (ms)"
K_TPOT = "Avg TPOT (ms)"
K_OK = "Success Requests"
K_FAIL = "Failed Requests"
K_TOTAL = "Total Requests"
K_OUT_TPS = "Output Throughput (tok/s)"
K_TOTAL_TPS = "Total Throughput (tok/s)"


def fmt(v, nd=1):
    if v is None:
        return "-"
    try:
        return f"{float(v):.{nd}f}"
    except (TypeError, ValueError):
        return "-"


def load_pool(pool_dir):
    """{sid: {parallel: metrics}}"""
    out = {}
    for sid_dir in sorted(glob.glob(os.path.join(pool_dir, "gsm8k_*"))):
        sid = os.path.basename(sid_dir)
        paths = sorted(glob.glob(os.path.join(sid_dir, "*", "*", "sla_summary.json")))
        if not paths:
            out[sid] = None
            continue
        try:
            data = json.load(open(paths[-1], encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] {paths[-1]} 解析失败: {e}")
            out[sid] = None
            continue
        rows = {}
        for k, v in data.items():
            if not k.startswith("parallel"):
                continue
            try:
                par = int(k.split("_")[1])
            except (IndexError, ValueError):
                continue
            rows[par] = (v.get("metrics", {}) if isinstance(v, dict) else {}) or {}
        out[sid] = rows
    return out


def verdict(m, ttft_sla, tpot_sla):
    """与 report.py 同口径: Failed==0 且 TTFT/TPOT 双达标 -> PASS"""
    if not m:
        return "-"
    failed = m.get(K_FAIL) or 0
    ttft = m.get(K_TTFT)
    tpot = m.get(K_TPOT)
    if failed:
        return "FAIL"
    if ttft is None or tpot is None:
        return "-"
    ok = ttft <= ttft_sla and tpot <= tpot_sla
    return "PASS" if ok else "FAIL"


def main():
    pools = sys.argv[1:] or ["mooncake", "memcache"]
    data = {}
    for p in pools:
        pdir = os.path.join(RESULTS, p)
        if not os.path.isdir(pdir):
            print(f"[warn] 池目录不存在: {pdir}")
            continue
        data[p] = load_pool(pdir)
    if not data:
        print("没有可对比的池结果")
        return 1

    # SLA 判据从 config.dsv41.yaml 读
    sla_map = {}
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.dsv41.yaml")
    try:
        import yaml
        for line in yaml.safe_load(open(cfg, encoding="utf-8"))["scenarios"]:
            parts = [x.strip() for x in line.split(",")]
            kfmt = (lambda n: f"{n//1024}k" if n % 1024 == 0 else str(n))
            ilen = int(parts[1][:-1]) * 1024 if parts[1].endswith("k") else int(parts[1])
            olen = int(parts[2][:-1]) * 1024 if parts[2].endswith("k") else int(parts[2])
            hit = 0 if float(parts[3]) == 0 else f"{round(float(parts[3])*100)}pct"
            sla_map[f"{parts[0]}_{kfmt(ilen)}_{kfmt(olen)}_hit{hit}"] = (int(parts[5]), int(parts[6]))
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] 读 config.dsv41.yaml 失败，SLA 判定列跳过: {e}")

    all_sids = sorted({sid for d in data.values() for sid in d})
    print("=" * 108)
    print("Mooncake vs Memcache —— 外置 KV 池摸高对照（DSV41 A3 1P1D，片上 prefix cache 关闭）")
    print("=" * 108)
    for sid in all_sids:
        lims = sla_map.get(sid)
        print(f"\n### {sid}   SLA: TTFT<={lims[0]}ms TPOT<={lims[1]}ms" if lims else f"\n### {sid}")
        hdr = f"{'并发':>6} | " + " | ".join(
            f"{p:^34}" for p in data)
        print(hdr)
        print(f"{'':>6} | " + " | ".join(f"{'TTFT(ms) TPOT(ms) tok/s 判定':^34}" for _ in data))
        pars = sorted({p for d in data.values() for rows in d.values() if rows for p in rows})
        if not pars:
            for p in data:
                print(f"  [{p}] 无 sla_summary.json")
            continue
        for par in pars:
            cells = []
            for p in data:
                rows = data[p].get(sid)
                m = rows.get(par) if rows else None
                if not m:
                    cells.append(f"{'-':^34}")
                    continue
                cells.append(
                    f"{fmt(m.get(K_TTFT),0):>9} {fmt(m.get(K_TPOT),1):>8} "
                    f"{fmt(m.get(K_OUT_TPS),1):>9} {verdict(m, *lims) if lims else '-':>4}")
            print(f"{par:>6} | " + " | ".join(cells))
        # 结论: 最大达标并发
        for p in data:
            rows = data[p].get(sid)
            if not rows:
                continue
            if lims:
                ok = [q for q in sorted(rows) if verdict(rows[q], *lims) == "PASS"]
                concl = f"最大达标并发 = {max(ok)}" if ok else "无达标档"
            else:
                concl = "-"
            print(f"    [{p}] {concl}")
    print("\n" + "=" * 108)
    return 0


if __name__ == "__main__":
    sys.exit(main())
