#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SWE runner 最小单测: config 解析 + 各日志解析函数 + 补跑计划/双驱动护栏。

全部离线: 给 tmp fixture（假 batches.log / batch log 进度行 / eval_rc.log /
rerun_ids.txt）, pgrep 一律 mock, 不碰 /mnt/sfs_turbo/c00803992/swe_pro 真目录,
不发任何请求。

直接运行（不依赖 pytest）: python3 tests/test_swe.py
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import swe_runner  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILS.append(name)


# ---------------------------------------------------------------- 配置解析 --
def test_resolve_config():
    print("[config] resolve_swe_config")
    # 1) swe 段缺失: 全默认 + 派生路径跟随 work_dir
    sw = swe_runner.resolve_swe_config({})
    check("默认 work_dir", sw["work_dir"] == "/mnt/sfs_turbo/c00803992/swe_pro")
    check("默认 results_dir 跟随 work_dir",
          sw["results_dir"].endswith(
              "swe_pro/results/accuracy/swe_bench_pro_dsv4"))
    check("默认 rerun_ids 跟随 work_dir",
          sw["rerun_ids"].endswith("swe_pro/rerun_ids.txt"))
    check("默认 logs 派生", sw["batches_log"].endswith("swe_pro/logs/batches.log"))
    check("默认 model 不回退 service 段", sw["model"] == "dsv4")

    # 2) swe 段显式优先
    cfg = {"swe": {"work_dir": "/tmp/wd", "model": "m9",
                   "eval_batch_size": 8}}
    sw2 = swe_runner.resolve_swe_config(cfg)
    check("swe.model 覆盖", sw2["model"] == "m9")
    check("显式 work_dir 推导派生路径",
          sw2["results_dir"] == "/tmp/wd/results/accuracy/swe_bench_pro_dsv4"
          and sw2["rerun_ids"] == "/tmp/wd/rerun_ids.txt")
    check("eval_batch_size 覆盖", sw2["eval_batch_size"] == 8)

    # 3) --work-dir 覆盖 + 显式 results_dir 优先于推导
    sw3 = swe_runner.resolve_swe_config(
        {"swe": {"results_dir": "/keep/old"}}, work_dir="/other/wd")
    check("--work-dir 覆盖 work_dir", sw3["work_dir"] == "/other/wd")
    check("显式 results_dir 不被 work_dir 改写", sw3["results_dir"] == "/keep/old")
    check("--work-dir 推导 rerun_ids", sw3["rerun_ids"] == "/other/wd/rerun_ids.txt")


# ---------------------------------------------------------------- 批次日志 --
BATCHES_LOG = """[2026-10-30 16:08:35] window=32 concurrency=16 total=642 start_offset=38
[2026-10-30 16:08:35] === batch offset=38 count=32 ===
[2026-10-30 16:22:37] batch offset=38 finished (log 89942B)
[2026-10-30 16:22:37] pruned 2 images; disk: 281G free
[2026-10-30 21:15:49] === batch offset=102 count=32 ===
[2026-10-31 03:25:10] batch offset=102 finished (log 9097609B)
[2026-10-31 07:02:17] === batch offset=198 count=32 ===
"""


def test_parse_batches_log():
    print("[batches] parse_batches_log")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "batches.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write(BATCHES_LOG)
        b = swe_runner.parse_batches_log(p)
        check("window/total 解析", b["window"] == 32 and b["total"] == 642)
        check("concurrency/start_offset 解析",
              b["concurrency"] == 16 and b["start_offset"] == 38)
        check("完成批列表", b["finished_offsets"] == [38, 102]
              and b["finished_count"] == 2)
        check("当前批 offset=198 未完成",
              b["current"]["offset"] == 198 and b["current"]["count"] == 32
              and b["current"]["done"] is False)
        check("当前批开始时间", b["current"]["started"] == "2026-10-31 07:02:17")
        check("all_done 未置位", b["all_done"] is False)
        # 当前批已有 finished 行 → done=True + all done 标记
        with open(p, "a", encoding="utf-8") as f:
            f.write("[2026-10-31 12:00:00] batch offset=198 finished (log 1B)\n")
            f.write("[2026-10-31 12:00:01] all batches done\n")
        b2 = swe_runner.parse_batches_log(p)
        check("finished 行补齐 → done=True", b2["current"]["done"] is True)
        check("all_done 置位", b2["all_done"] is True)
        # 缺失文件
        b3 = swe_runner.parse_batches_log(os.path.join(td, "nope.log"))
        check("文件缺失 exists=False 且不抛",
              b3["exists"] is False and b3["current"] is None)


# ---------------------------------------------------------------- 进度解析 --
def test_parse_progress_line():
    print("[progress] parse_progress_line")
    raw = ("Evaluating[swe_bench_pro]:  94%|█████▇"
           "▍| 30/32 [3:55:05<20:46, 623.07s/it]")
    p = swe_runner.parse_progress_line(raw)
    check("tqdm 原始行: n/total", p["n"] == 30 and p["total"] == 32)
    check("tqdm 原始行: pct/elapsed/eta/rate",
          p["pct"] == 94 and p["elapsed"] == "3:55:05" and p["eta"] == "20:46"
          and abs(p["rate"] - 623.07) < 1e-9)
    check("tqdm 原始行: 单位 s/it 原样保留", p["rate_unit"] == "s/it")
    check("tqdm 原始行: 无日志时间戳", p["ts"] is None)

    info = ("2026-10-31 02:58:27 - evalscope - INFO: "
            "Evaluating[swe_bench_pro]  94%| 30/32 "
            "[Elapsed: 3:56:05 < Remaining: 20:46, 623.07s/it]")
    p2 = swe_runner.parse_progress_line(info)
    check("INFO 日志行: n/total", p2["n"] == 30 and p2["total"] == 32)
    check("INFO 日志行: eta 去 Remaining 前缀", p2["eta"] == "20:46")
    check("INFO 日志行: elapsed", p2["elapsed"] == "3:56:05")
    check("INFO 日志行: 提取时间戳", p2["ts"] == "2026-10-31 02:58:27")

    check("无关行返回 None",
          swe_runner.parse_progress_line("some unrelated log line") is None)
    check("残缺进度行返回 None",
          swe_runner.parse_progress_line("Evaluating[swe_bench_pro] 94%") is None)

    # 无 ETA 阶段（尚无完成实例）: tqdm 单位变 it/s、rate/eta 都是 "?"
    early = ("Evaluating[swe_bench_pro]:   0%|          | 0/68 "
             "[02:00<?, ?it/s]")
    p3 = swe_runner.parse_progress_line(early)
    check("无 ETA 行: n/total/rate",
          p3["n"] == 0 and p3["total"] == 68 and p3["rate"] == "?"
          and p3["eta"] == "?")
    check("无 ETA 行: 单位 it/s（展示层拼接用, 勿显示 ?s/it）",
          p3["rate_unit"] == "it/s")


def test_find_progress():
    print("[progress] find_progress（\\r 刷新行 + 取最后一条）")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "batch_198.log")
        payload = (b"[INFO:ms_enclave] pull image ok\r"
                   b"Evaluating[swe_bench_pro]:  93%|x| 29/32 "
                   b"[3:50:00<25:00, 610.0s/it]\r"
                   b"Evaluating[swe_bench_pro]:  94%|x| 30/32 "
                   b"[3:55:05<20:46, 623.07s/it]\n"
                   b"2026-10-31 02:58:27 - evalscope - INFO: "
                   b"Evaluating[swe_bench_pro]  94%| 30/32 "
                   b"[Elapsed: 3:56:05 < Remaining: 20:46, 623.07s/it]\n")
        with open(p, "wb") as f:
            f.write(payload)
        prog = swe_runner.find_progress(p)
        check("取最后一条（INFO 行胜出）",
              prog is not None and prog["n"] == 30 and prog["ts"] is not None)
        check("携带来源日志路径", prog["log"] == p)
        # 尾部就是 tqdm \r 原始行时也能取到
        p2 = os.path.join(td, "batch_2.log")
        with open(p2, "wb") as f:
            f.write(b"head line\n")
            f.write(b"Evaluating[swe_bench_pro]:  50%|x| 16/32 "
                    b"[1:00:00<1:00:00, 225.0s/it]")
        prog2 = swe_runner.find_progress(p2)
        check("尾部 \\r 原始 tqdm 行可解析",
              prog2 is not None and prog2["n"] == 16 and prog2["ts"] is None)
        check("缺失文件返回 None",
              swe_runner.find_progress(os.path.join(td, "nope.log")) is None)
        # 进度行被大量后续实例输出淹没（rerun 日志形态, ~320KB 噪声）:
        # 固定 200KB 尾窗会漏, 块回扫必须能找到
        p3 = os.path.join(td, "rerun_part1.log")
        with open(p3, "wb") as f:
            f.write(b"Evaluating[swe_bench_pro]:   0%| 0/68 "
                    b"[02:00<?, ?it/s]\n")
            f.write((b"[INFO:ms_enclave] " + b"x" * 80 + b"\n") * 4000)
        prog3 = swe_runner.find_progress(p3)
        check("进度行被噪声淹没仍可回扫找到",
              prog3 is not None and prog3["n"] == 0 and prog3["total"] == 68)


# ---------------------------------------------------------------- eval_rc --
def test_parse_eval_rc():
    print("[eval_rc] parse_eval_rc")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "eval_rc.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[eval] offset=166 rc=1 ended 07:00:48\n")
            f.write("[eval] offset=198 rc=0 ended 11:58:02\n")
        rc = swe_runner.parse_eval_rc(p)
        check("记录解析 2 条", len(rc["records"]) == 2)
        check("last = 正常结束批", rc["last"]["offset"] == 198
              and rc["last"]["rc"] == 0)
        check("崩溃批列表", rc["crash_offsets"] == [166])
        # rc!=0 在最后 → 崩溃标记
        with open(p, "a", encoding="utf-8") as f:
            f.write("[eval] offset=230 rc=1 ended 16:20:00\n")
        rc2 = swe_runner.parse_eval_rc(p)
        check("最后一条 rc!=0 → last 指向它",
              rc2["last"]["rc"] == 1 and rc2["last"]["offset"] == 230)
        # 文件不存在（当前 live 状态: 新版驱动脚本首批还没跑完）
        rc3 = swe_runner.parse_eval_rc(os.path.join(td, "nope.log"))
        check("文件缺失 exists=False 不抛", rc3["exists"] is False
              and rc3["last"] is None and rc3["crash_offsets"] == [])


# ---------------------------------------------------------------- rerun 清单 --
RERUN_TXT = """instance_a__x-111
instance_a__x-222
# nodebb qemu 下 mocha 段错误, parser 可解析
instance_b__y-333
instance_b__y-444
instance_b__y-555
# flipt go 模块拉取失败
instance_c__z-666
"""


def test_parse_rerun_ids():
    print("[rerun] parse_rerun_ids")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "rerun_ids.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(RERUN_TXT)
        rr = swe_runner.parse_rerun_ids(p, sample_n=2)
        check("非注释计数 6", rr["count"] == 6)
        check("注释 2 行", rr["comment_count"] == 2)
        check("分组摘要", rr["groups"] == [
            {"comment": "nodebb qemu 下 mocha 段错误, parser 可解析", "count": 3},
            {"comment": "flipt go 模块拉取失败", "count": 1}])
        check("样例截断 sample_n=2", rr["sample"] == ["instance_a__x-111",
                                                      "instance_a__x-222"])
        check("默认不含全量 ids", "ids" not in rr)
        rr2 = swe_runner.parse_rerun_ids(p, include_ids=True)
        check("include_ids 带全量", len(rr2["ids"]) == 6)
        rr3 = swe_runner.parse_rerun_ids(os.path.join(td, "nope.txt"))
        check("文件缺失 exists=False", rr3["exists"] is False
              and rr3["count"] == 0)


# ---------------------------------------------------------------- 补跑计划 --
def test_build_rerun_plan():
    print("[rerun] build_rerun_plan")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "rerun_ids.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(RERUN_TXT)
        sw = swe_runner.resolve_swe_config({}, work_dir=td)
        plan = swe_runner.build_rerun_plan(sw, batch_size=16, parts=2)
        check("总条数", plan["count"] == 6)
        check("均分 2 份 [3,3]", [len(c) for c in plan["chunks"]] == [3, 3])
        check("分片文件名", plan["files"][0].endswith("rerun_part1.txt"))
        check("limit 大于清单数（+8）",
              all(f"--limit {len(c) + 8}" in plan["shell"]
                  for c in plan["chunks"])
              and all(len(c) + 8 > len(c) for c in plan["chunks"]))
        check("串联顺序执行（; 而非 &）",
              "; " in plan["shell"] and " & " not in plan["shell"])
        check("OFFSET=0 + INSTANCE_FILE 在命令里",
              "EVALSCOPE_INSTANCE_FILE=" in plan["shell"]
              and "EVALSCOPE_DATASET_OFFSET=0" in plan["shell"])
        check("docker exec -d 形态", plan["docker_cmd"][:3]
              == ["docker", "exec", "-d"])
        check("cd 到 live 工作区", ("cd %s" % td) in plan["shell"])
        # 清单为空 → None
        empty = os.path.join(td, "empty.txt")
        open(empty, "w").close()
        sw2 = swe_runner.resolve_swe_config({}, work_dir=td)
        sw2["rerun_ids"] = empty
        check("空清单返回 None", swe_runner.build_rerun_plan(sw2, 16, 2) is None)


# ---------------------------------------------------------------- 双驱动护栏 --
def test_check_double_driver():
    print("[guard] check_double_driver（mock pgrep）")
    sw = swe_runner.resolve_swe_config({})
    orig_cp = swe_runner.container_pgrep
    orig_pg = swe_runner._pgrep
    orig_ic = swe_runner.IN_CONTAINER
    try:
        swe_runner.IN_CONTAINER = False   # 宿主机口径
        # 1) 容器内 evalscope 在跑 → 硬拒绝
        swe_runner.container_pgrep = lambda sw_, pat: ([111], "container")
        swe_runner._pgrep = lambda pat, container=None: [222]
        probs = swe_runner.check_double_driver(sw)
        check("eval 在跑 → 硬拒绝", probs and probs[0][0] is True
              and "evalscope eval" in probs[0][1])
        # 2) eval 空闲但宿主驱动在跑 → 硬拒绝
        swe_runner.container_pgrep = lambda sw_, pat: ([], "container")
        probs = swe_runner.check_double_driver(sw)
        check("驱动在跑 → 硬拒绝", probs and probs[0][0] is True
              and "run_all_batches" in probs[0][1])
        # 3) 全空闲 → 无硬拒绝
        swe_runner._pgrep = lambda pat, container=None: []
        probs = swe_runner.check_double_driver(sw)
        check("空闲 → 无硬拒绝项", not any(h for h, _ in probs))
        # 4) 探测不可用 → 软提示（不硬拒）
        swe_runner.container_pgrep = lambda sw_, pat: (None, "container")
        probs = swe_runner.check_double_driver(sw)
        check("探测不可用 → 仅提示", probs and probs[0][0] is False)
        # 5) 容器内模式: 宿主驱动不可见 → 软提示不拒绝
        swe_runner.IN_CONTAINER = True
        swe_runner.container_pgrep = lambda sw_, pat: ([], "container")
        probs = swe_runner.check_double_driver(sw)
        check("容器模式驱动不可见 → 仅提示", probs and probs[0][0] is False
              and "容器内" in probs[0][1])
    finally:
        swe_runner.container_pgrep = orig_cp
        swe_runner._pgrep = orig_pg
        swe_runner.IN_CONTAINER = orig_ic


def test_cmd_start_rerun_refuses():
    print("[guard] cmd_start_rerun 双驱动拒绝（不写文件不执行）")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "rerun_ids.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write(RERUN_TXT)
        sw = swe_runner.resolve_swe_config({}, work_dir=td)
        orig_cp = swe_runner.container_pgrep
        orig_ic = swe_runner.IN_CONTAINER
        try:
            swe_runner.IN_CONTAINER = False
            # eval 在跑: 即使 --yes 也拒绝, 且不写分片文件
            swe_runner.container_pgrep = lambda sw_, pat: ([67493], "container")
            swe_runner._pgrep = lambda pat, container=None: []
            rc = swe_runner.cmd_start_rerun(sw, yes=True, batch_size=16, parts=2)
            check("eval 在跑 → 退出码 3", rc == 3)
            check("拒绝时不写分片文件",
                  not os.path.exists(os.path.join(td, "rerun_part1.txt")))
            # 空闲 dry-run: 打印命令, 退出 0, 也不写文件
            swe_runner.container_pgrep = lambda sw_, pat: ([], "container")
            rc2 = swe_runner.cmd_start_rerun(sw, yes=False, batch_size=16,
                                             parts=2)
            check("空闲 dry-run → 退出码 0", rc2 == 0)
            check("dry-run 不写分片文件",
                  not os.path.exists(os.path.join(td, "rerun_part1.txt")))
        finally:
            swe_runner.container_pgrep = orig_cp
            swe_runner.IN_CONTAINER = orig_ic


# ---------------------------------------------------------------- 断点续跑 --
def test_compute_resume_offset():
    print("[resume] compute_resume_offset（web 启动表单默认断点）")
    with tempfile.TemporaryDirectory() as td:
        sw = swe_runner.resolve_swe_config({}, work_dir=td)
        # 1) 无日志 → 0
        r = swe_runner.compute_resume_offset(sw)
        check("无日志 → offset 0", r["offset"] == 0 and not r["interrupted"])
        # 2) 最后一个批无 finished 配对 → 续跑该批
        os.makedirs(os.path.join(td, "logs"), exist_ok=True)
        p = sw["batches_log"]
        with open(p, "w", encoding="utf-8") as f:
            f.write(BATCHES_LOG)   # 尾部: === batch offset=198 count=32 === 无配对
        r = swe_runner.compute_resume_offset(sw)
        check("中断批 → offset=198 interrupted", r["offset"] == 198
              and r["interrupted"] is True and "198" in r["note"])
        # 3) 全部配对完成 → 下一偏移
        with open(p, "a", encoding="utf-8") as f:
            f.write("[2026-10-31 12:00:00] batch offset=198 finished (log 1B)\n")
        r = swe_runner.compute_resume_offset(sw)
        check("全部完成 → 下一偏移 230", r["offset"] == 230
              and r["interrupted"] is False)
        # 4) all batches done → 封顶 total 不越界
        with open(p, "a", encoding="utf-8") as f:
            f.write("[2026-10-31 12:00:01] all batches done\n")
        r = swe_runner.compute_resume_offset(sw)
        check("all done 标记", r["all_done"] is True and r["offset"] == 230)
        # 5) 封顶: 手造最后一批 offset=640 count=32（>total 642）
        with open(p, "w", encoding="utf-8") as f:
            f.write("[2026-10-31 12:00:00] === batch offset=640 count=32 ===\n")
            f.write("[2026-10-31 12:00:01] batch offset=640 finished (log 1B)\n")
        r = swe_runner.compute_resume_offset(sw)
        check("下一偏移封顶 total=642", r["offset"] == 642)
        # 6) 无批行（空壳日志）→ 0
        with open(p, "w", encoding="utf-8") as f:
            f.write("[2026-10-30 16:08:35] window=32 concurrency=16 total=642 start_offset=0\n")
        r = swe_runner.compute_resume_offset(sw)
        check("无批行 → offset 0", r["offset"] == 0)


# ---------------------------------------------------------------- web 启停 --
def test_webapp_swe_run_validation():
    print("[web] /api/swe/run 参数校验（离线, 不发请求）")
    import webapp
    T = 642
    v, e = webapp._swe_validate_run_payload(
        {"mode": "full", "start_offset": 100, "window": 32, "concurrency": 16}, T)
    check("合法 full 通过", v is not None and e is None
          and v["start_offset"] == 100 and v["window"] == 32)
    for bad, why in ((-1, "负 offset"), (642, "offset=total"), ("x", "非整数 offset")):
        v, e = webapp._swe_validate_run_payload(
            {"mode": "full", "start_offset": bad}, T)
        check("%s 拒绝" % why, v is None and e)
    v, e = webapp._swe_validate_run_payload({"mode": "full", "window": 0}, T)
    check("window=0 拒绝", v is None)
    v, e = webapp._swe_validate_run_payload({"mode": "full", "window": 65}, T)
    check("window=65 拒绝", v is None)
    v, e = webapp._swe_validate_run_payload(
        {"mode": "full", "concurrency": 33}, T)
    check("concurrency=33 拒绝", v is None)
    v, e = webapp._swe_validate_run_payload({"mode": "weird"}, T)
    check("非法 mode 拒绝", v is None and "mode" in e)
    v, e = webapp._swe_validate_run_payload({"mode": "rerun", "concurrency": 8}, T)
    check("rerun 模式通过", v is not None and v["concurrency"] == 8
          and "start_offset" not in v)
    v, e = webapp._swe_validate_run_payload({"mode": "full", "keep_images": "yes"}, T)
    check("keep_images 非布尔拒绝", v is None)
    v = webapp._swe_fill_defaults({"mode": "full"}, swe_runner.resolve_swe_config({}))
    check("空参数补默认 window/concurrency",
          v["window"] == 32 and v["concurrency"] == 16)


def _load_bridge():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "tools", "swe_web_bridge.py")
    spec = importlib.util.spec_from_file_location("swe_web_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bridge_run_params():
    print("[bridge] parse_run_params（run.json 参数二道校验）")
    bridge = _load_bridge()
    p, e = bridge.parse_run_params({"mode": "full", "start_offset": 230,
                                    "window": 32, "concurrency": 16})
    check("合法 full", p is not None and p["start_offset"] == 230)
    p, e = bridge.parse_run_params({"mode": "nope"})
    check("非法 mode 拒绝", p is None and e)
    p, e = bridge.parse_run_params({"mode": "full", "start_offset": 642})
    check("offset 越界拒绝", p is None)
    p, e = bridge.parse_run_params({"mode": "full", "window": 0})
    check("window 越界拒绝", p is None)
    p, e = bridge.parse_run_params({"mode": "full", "concurrency": 99})
    check("concurrency 越界拒绝", p is None)
    p, e = bridge.parse_run_params({"mode": "rerun", "concurrency": 16})
    check("rerun 通过", p is not None and p["concurrency"] == 16)
    p, e = bridge.parse_run_params(["not", "a", "dict"])
    check("非对象拒绝", p is None)


def test_bridge_sandbox_filter():
    print("[bridge] sandbox 容器过滤（不误伤其它容器）")
    bridge = _load_bridge()
    lines = [
        "84e5b4cc566b dsv41-mmc",
        "191d39ad9689 aiserver-agent",
        "d012b60d01d1 ais-bench",
        "aaa111bbb222 sandbox-abc123",
        "ccc333ddd444 sandbox-xyz789",
        "eee555fff666 my-sandbox-not-prefix",
        "garbage-line-no-space",
        "ddd999000111 sandbox-",
    ]
    sel = bridge._select_sandbox_containers(lines)
    names = [n for _cid, n in sel]
    check("只选中 sandbox-* 前缀", names == ["sandbox-abc123", "sandbox-xyz789",
                                             "sandbox-"])
    check("保护容器全部排除",
          not any(n in ("dsv41-mmc", "aiserver-agent", "ais-bench") for n in names))
    check("含 sandbox 子串但非前缀不选", "my-sandbox-not-prefix" not in names)
    check("空输入安全", bridge._select_sandbox_containers([]) == [])


def test_bridge_find_interrupted_batch():
    print("[bridge] find_interrupted_batch（stop 抢救用）")
    bridge = _load_bridge()
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "batches.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write(BATCHES_LOG)   # 尾部 offset=198 无 finished
        check("最后一个未配对批", bridge.find_interrupted_batch(p) == (198, 32))
        with open(p, "a", encoding="utf-8") as f:
            f.write("[2026-10-31 12:00:00] batch offset=198 finished (log 1B)\n")
        check("配对齐 → None", bridge.find_interrupted_batch(p) is None)
        check("文件缺失 → None",
              bridge.find_interrupted_batch(os.path.join(td, "nope.log")) is None)
        empty = os.path.join(td, "empty.log")
        open(empty, "w").close()
        check("空文件 → None", bridge.find_interrupted_batch(empty) is None)


def test_bridge_append_rerun_ids():
    print("[bridge] append_rerun_ids（去重追加）")
    bridge = _load_bridge()
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "rerun_ids.txt")
        with open(p, "w", encoding="utf-8") as f:
            f.write("# 已有分组\ninstance_a__x-111\ninstance_a__x-222\n")
        n = bridge.append_rerun_ids(p, ["instance_a__x-111", "instance_b__y-333",
                                        "instance_b__y-333"],
                                    comment="2026-10-31 12:00:00 停止时批 offset=198 中断")
        check("只追加去重后的新 id（1 条）", n == 1)
        with open(p, encoding="utf-8") as f:
            text = f.read()
        check("注释行写入", "停止时批 offset=198" in text)
        check("新 id 写入", "instance_b__y-333" in text)
        check("既有 id 不重复",
              text.count("instance_a__x-111") == 1
              and text.count("instance_b__y-333") == 1)
        # 全重复 → 0 且不写注释
        before = os.path.getsize(p)
        n2 = bridge.append_rerun_ids(p, ["instance_a__x-111"], comment="再停一次")
        check("全重复 → 0 条不追加", n2 == 0 and os.path.getsize(p) == before)
        check("空清单 → 0", bridge.append_rerun_ids(p, [], comment="空") == 0)


def test_bridge_state_roundtrip():
    print("[bridge] state.json 原子写 + 解析")
    bridge = _load_bridge()
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "webctl", "state.json")
        data = {"ok": True, "bridge_pid": 123, "updated_at": "2026-10-31 12:00:00",
                "last_action": {"type": "stop", "at": "2026-10-31 12:00:00",
                                "detail": "多行\n详情"}}
        bridge.atomic_write_json(p, data)
        check("原子写后可读回", os.path.isfile(p)
              and json.load(open(p, encoding="utf-8")) == data)
        check("无残留 tmp 文件",
              not [f for f in os.listdir(os.path.dirname(p)) if f.endswith(".tmp")])
        # write_state 附带 epoch 字段（宿主/容器跨时区的在线判据）
        orig_state_path = bridge.STATE_PATH
        try:
            bridge.STATE_PATH = p
            bridge._STATE.clear()
            bridge._STATE.update(data)
            bridge.write_state()
            d = json.load(open(p, encoding="utf-8"))
            check("write_state 带 epoch",
                  isinstance(d.get("updated_at_ts"), float)
                  and abs(d["updated_at_ts"] - time.time()) < 5)
        finally:
            bridge.STATE_PATH = orig_state_path


def test_webapp_swe_webctl_commands():
    """webapp run/stop 端点: 命令文件写入 + 桥接在线判定（全部离线 mock）。"""
    print("[web] /api/swe/run|stop 命令文件写入（mock 桥接, 不真启动）")
    import webapp
    orig_cfg, orig_state = webapp.CONFIG_PATH, webapp.STATE_PATH
    with tempfile.TemporaryDirectory() as td:
        work = os.path.join(td, "swe_pro")
        webctl = os.path.join(work, "webctl")
        cfg_path = os.path.join(td, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            # runner.load_config 要求 service/request/benchmark/dataset/
            # scenarios 段存在（值可空）
            f.write("service: {}\nrequest: {}\nbenchmark: {}\ndataset: {}\n"
                    "scenarios: []\nswe:\n  work_dir: %s\n" % work)
        try:
            webapp.CONFIG_PATH = cfg_path
            webapp.STATE_PATH = os.path.join(td, "web_state.json")
            # 1) 桥接未起（无 state.json）→ run/stop 明确报错（不写命令文件）
            r = webapp.api_swe_run({"mode": "full", "start_offset": 0})
            check("桥接离线 run → 503", r.status_code == 503
                  and "桥接" in json.loads(r.body.decode())["error"]
                  and not os.path.exists(os.path.join(webctl, "run.json")))
            r = webapp.api_swe_stop()
            check("桥接离线 stop → 503", r.status_code == 503
                  and not os.path.exists(os.path.join(webctl, "stop")))
            # 2) 伪造在线桥接（epoch 刚刚）→ run 写 run.json（带 uuid）
            #    （宿主 CST / 容器 UTC 时区不同，在线判据走 updated_at_ts）
            os.makedirs(webctl, exist_ok=True)
            with open(os.path.join(webctl, "state.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"ok": True,
                           "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "updated_at_ts": time.time(),
                           "driver_running": False, "eval_running": False}, f)
            r = webapp.api_swe_run({"mode": "full", "start_offset": 230,
                                    "window": 32, "concurrency": 16})
            check("在线 run → accepted", isinstance(r, dict) and r["ok"] is True
                  and r["accepted"] is True)
            cmd = json.load(open(os.path.join(webctl, "run.json"),
                                 encoding="utf-8"))
            check("run.json 带 uuid 防重放", len(cmd.get("uuid") or "") >= 8)
            check("run.json 透传参数", cmd["mode"] == "full"
                  and cmd["start_offset"] == 230 and cmd["window"] == 32)
            # 3) 桥接显示在跑 → 409 拒绝
            with open(os.path.join(webctl, "state.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"ok": True, "updated_at_ts": time.time(),
                           "driver_running": True, "eval_running": False}, f)
            r = webapp.api_swe_run({"mode": "full", "start_offset": 0})
            check("桥接显示在跑 → 409", r.status_code == 409)
            check("409 不写新命令文件",
                  cmd["uuid"] == json.load(open(os.path.join(webctl, "run.json"),
                                                encoding="utf-8"))["uuid"])
            # 4) 在线 stop → 写空标志文件
            with open(os.path.join(webctl, "state.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"ok": True, "updated_at_ts": time.time(),
                           "driver_running": False, "eval_running": False}, f)
            r = webapp.api_swe_stop()
            check("在线 stop → accepted", isinstance(r, dict) and r["ok"] is True)
            check("stop 标志文件写入（空）",
                  os.path.isfile(os.path.join(webctl, "stop"))
                  and os.path.getsize(os.path.join(webctl, "stop")) == 0)
            # 4b) 心跳过期（epoch 60s 前）→ 判离线 503
            with open(os.path.join(webctl, "state.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"ok": True,
                           "updated_at_ts": time.time() - 60,
                           "driver_running": False, "eval_running": False}, f)
            r = webapp.api_swe_stop()
            check("桥接心跳过期 → 503", r.status_code == 503)
            # 恢复在线心跳（后面的 GET config 要看到 bridge 在线）
            with open(os.path.join(webctl, "state.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"ok": True, "updated_at_ts": time.time(),
                           "driver_running": False, "eval_running": False}, f)
            # 5) 非法参数 → 400（在线也先拒）
            r = webapp.api_swe_run({"mode": "full", "start_offset": 642})
            check("非法参数 → 400", r.status_code == 400)
            # 6) 参数保存进 web_state（不碰 config.yaml）
            r = webapp.api_swe_set_config({"mode": "full", "start_offset": 38,
                                           "window": 16})
            check("config 保存返回 ok", isinstance(r, dict) and r["ok"] is True)
            st = json.load(open(webapp.STATE_PATH, encoding="utf-8"))
            check("swe_run_params 落 web_state",
                  st["swe_run_params"]["start_offset"] == 38
                  and st["swe_run_params"]["window"] == 16)
            with open(cfg_path, encoding="utf-8") as f:
                check("config.yaml 未被改写", "work_dir" in f.read())
            # 7) GET config: 默认断点 + saved_params
            cfg = webapp.api_swe_config()
            check("GET config 返回 saved_params",
                  cfg["saved_params"]["start_offset"] == 38)
            check("GET config 默认值齐全",
                  cfg["defaults"]["window"] == 32
                  and cfg["total_instances"] == 642
                  and cfg["default_start_offset"] == 0
                  and cfg["bridge"]["online"] is True)
        finally:
            webapp.CONFIG_PATH, webapp.STATE_PATH = orig_cfg, orig_state


# ---------------------------------------------------------------- 主入口 --
def main():
    test_resolve_config()
    test_parse_batches_log()
    test_parse_progress_line()
    test_find_progress()
    test_parse_eval_rc()
    test_parse_rerun_ids()
    test_build_rerun_plan()
    test_check_double_driver()
    test_cmd_start_rerun_refuses()
    test_compute_resume_offset()
    test_webapp_swe_run_validation()
    test_bridge_run_params()
    test_bridge_sandbox_filter()
    test_bridge_find_interrupted_batch()
    test_bridge_append_rerun_ids()
    test_bridge_state_roundtrip()
    test_webapp_swe_webctl_commands()

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项: {FAILS}")
        return 1
    print("SWE runner 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
