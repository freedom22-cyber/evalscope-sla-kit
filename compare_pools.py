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

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(KIT_ROOT, "results")
sys.path.insert(0, KIT_ROOT)

from sla_paths import latest_summary_path  # noqa: E402  # "最新一轮"选择（mtime 口径）

K_TTFT = "Avg TTFT (ms)"
K_TPOT = "Avg TPOT (ms)"
K_OK = "Success Requests"
K_FAIL = "Failed Requests"
K_TOTAL = "Total Requests"
K_OUT_TPS = "Output Throughput (tok/s)"
K_TOTAL_TPS = "Total Throughput (tok/s)"


def fmt(v, nd=1):
    """None / evalscope 失败哨兵（负值, 如全挂轮的 -1000）显示 '-' 而非负数指标。"""
    if v is None:
        return "-"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "-"
    return "-" if v < 0 else f"{v:.{nd}f}"


def load_pool(pool_dir):
    """{sid: {parallel: metrics}}"""
    out = {}
    for sid_dir in sorted(glob.glob(os.path.join(pool_dir, "gsm8k_*"))):
        sid = os.path.basename(sid_dir)
        # mtime 选最新: 结果目录名时间戳混用宿主 CST / 容器 UTC 两个时钟,
        # 按目录名字典序会把旧一轮选中（口径见 sla_paths 模块说明）
        path = latest_summary_path(sid_dir)
        if not path:
            out[sid] = None
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] {path} 解析失败: {e}")
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
    """与 report.py sla_verdict 同口径: Failed==0 且 TTFT/TPOT 双达标 -> PASS
    （evalscope 失败哨兵 -1000 视为无指标, 不参与达标判定）"""
    if not m:
        return "-"
    failed = m.get(K_FAIL) or 0
    ttft, tpot = m.get(K_TTFT), m.get(K_TPOT)
    if failed:
        return "FAIL"
    if ttft is None or tpot is None:
        return "-"
    try:
        ttft, tpot = float(ttft), float(tpot)
    except (TypeError, ValueError):
        return "-"
    if ttft < 0 or tpot < 0:
        return "-"   # 全挂哨兵: 无指标, 而非达标
    return "PASS" if (ttft <= ttft_sla and tpot <= tpot_sla) else "FAIL"


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

    # SLA 判据从 config.dsv41.yaml 读——与 report.py 同源同口径:
    # 场景 ID 推导复用 runner.parse_scenario / scenario_id（含 _pN{prefix_num}
    # 前缀数后缀与 29.5%→29p5pct 高精度取整）, 手写推导丢后缀会让 SLA 列整列失配。
    sla_map = {}
    cfg = os.path.join(KIT_ROOT, "config.dsv41.yaml")
    try:
        import yaml
        from runner import parse_scenario, scenario_id
        with open(cfg, encoding="utf-8") as f:
            scenarios = (yaml.safe_load(f) or {}).get("scenarios") or []
        # 逐行隔离: 单条坏场景行只跳过自己, 不让后面的行跟着丢 SLA 列
        for line in scenarios:
            line = str(line).strip()
            if not line or line.startswith("#"):
                continue
            try:
                sc = parse_scenario(line)
            except (TypeError, ValueError) as e:
                print(f"  [warn] 场景行解析失败, 跳过该行: {line!r} ({e})")
                continue
            sla_map[scenario_id(sc)] = (sc["ttft_sla_ms"], sc["tpot_sla_ms"])
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
