#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sla_summary.json / 结果目录的"最新一轮"选择共享工具。

webapp / report.py / compare_pools.py / runner.py 四处同口径, 防止各写各的
选法再漂移。背景: 结果目录名时间戳（YYYYMMDD_HHMMSS）有两个时钟来源——
宿主 CST（CLI 直跑）与容器 UTC（webui 触发）, 同一真实时刻两种命名相差
8 小时, 按目录名字典序选"最新"会把旧一轮选中（2026-10-31 实锤: 11:58 的
3 条冒烟盖住 15:45 的 50 条全量; 池对照也曾因此拿旧轮当最新）。NFS 上
mtime 来自同一服务器时钟、与入口无关, 故一律按 mtime 选。
"""
import glob
import os
import re

_TS_RE = re.compile(r"\d{8}_\d{6}")


def latest_by_mtime(paths):
    """多条路径里按 mtime 取最新; 空列表返回 None。"""
    paths = list(paths)
    return max(paths, key=os.path.getmtime) if paths else None


def latest_summary_path(scenario_dir):
    """场景目录下最新的 sla_summary.json（results/<sid>/<时间戳>/<model>/sla_summary.json）。"""
    return latest_by_mtime(
        glob.glob(os.path.join(scenario_dir, "*", "*", "sla_summary.json")))


def latest_ts_dir(sdir):
    """场景目录下最新的时间戳目录（只认 YYYYMMDD_HHMMSS, 跳过 prefix_warmup 等非结果目录）。

    字典序里 prefix_warmup > 2026… 会把最新时间戳顶掉（runner partial 判定
    曾因此静默丢失）, 故先过滤再按 mtime 取。
    """
    ts_dirs = [d for d in glob.glob(os.path.join(sdir, "*"))
               if os.path.isdir(d) and _TS_RE.fullmatch(os.path.basename(d))]
    return latest_by_mtime(ts_dirs)
