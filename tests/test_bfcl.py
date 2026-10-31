#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BFCL runner 最小单测: config 解析 + Overall 公式 + 报告解析。

直接运行（不依赖 pytest）: python3 tests/test_bfcl.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bfcl_runner  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
    else:
        print(f"  FAIL {name}  {detail}")
        FAILS.append(name)


def approx(a, b, eps=1e-9):
    return abs(a - b) < eps


# ---------------------------------------------------------------- 配置解析 --
def test_resolve_config():
    print("[config] resolve_bfcl_config / resolve_subsets")
    # 1) bfcl 段缺失: 全默认 + 回退 service/request
    cfg = {"service": {"url": "http://x:9/v1/chat/completions", "model": "m1"},
           "request": {"api_key": "K"}}
    bf = bfcl_runner.resolve_bfcl_config(cfg)
    check("url 回退 service", bf["url"] == "http://x:9/v1/chat/completions")
    check("model 回退 service", bf["model"] == "m1")
    check("api_key 回退 request", bf["api_key"] == "K")
    check("默认离线子集 17 个",
          len(bf["enabled_subsets"]) == 17, f"got {len(bf['enabled_subsets'])}")
    check("默认不开 web_search/memory",
          not bf["enable_web_search"] and not bf["enable_memory"])
    subs = bfcl_runner.resolve_subsets(bf)
    check("resolve_subsets 默认 = 离线全集", len(subs) == 17 and "web_search_base" not in subs)
    check("live_relevance 默认在列（跑但不计分）", "live_relevance" in subs)

    # 2) bfcl 段显式优先 + 开关追加
    cfg2 = {"service": {"url": "http://svc/v1/chat/completions", "model": "svc_model"},
            "bfcl": {"url": "http://bfcl:18020/v1", "model": "dsv4",
                     "enable_web_search": True, "enable_memory": True, "limit": 5}}
    bf2 = bfcl_runner.resolve_bfcl_config(cfg2)
    check("bfcl.url 覆盖 service", bf2["url"] == "http://bfcl:18020/v1")
    check("bfcl.model 覆盖 service", bf2["model"] == "dsv4")
    check("api_key 缺省 EMPTY", bf2["api_key"] == "EMPTY")
    subs2 = bfcl_runner.resolve_subsets(bf2)
    check("开关追加 web_search 2 + memory 3", len(subs2) == 22,
          f"got {len(subs2)}")
    check("limit 生效", bf2["limit"] == 5)

    # 3) --only 显式指定与未知子集
    only = bfcl_runner.resolve_subsets(bf, "simple_python,irrelevance")
    check("--only 按显式列表", only == ["simple_python", "irrelevance"])
    try:
        bfcl_runner.resolve_subsets(bf, "no_such_subset")
        check("未知子集应报错", False)
    except ValueError:
        check("未知子集应报错", True)

    # 4) work_dir 命名: 默认 tag=模型名, --limit 加后缀
    check("work_dir 默认 tag", bfcl_runner.work_dir_of(bf, None, 0)
          .endswith("/results/bfcl/m1"))
    check("work_dir limit 后缀", bfcl_runner.work_dir_of(bf, "t", 10)
          .endswith("/results/bfcl/t_limit10"))


# ---------------------------------------------------------------- 聚合公式 --
def _doc_scores(n=10):
    """evalscope 官方文档 bfcl_v4 页的输出示例分数（qwen-plus, limit=10）"""
    return {
        "simple_python": 1.0, "simple_java": 0.5, "simple_javascript": 0.6,
        "multiple": 0.9, "parallel": 0.9, "parallel_multiple": 0.9,
        "irrelevance": 1.0,
        "live_simple": 1.0, "live_multiple": 0.8, "live_parallel": 0.9,
        "live_parallel_multiple": 0.7, "live_irrelevance": 0.9,
        "live_relevance": 0.8,   # 不计分, 放进去验证被排除
        "multi_turn_base": 0.5, "multi_turn_miss_func": 0.1,
        "multi_turn_miss_param": 0.2, "multi_turn_long_context": 0.7,
        "web_search_base": 0.7, "web_search_no_snippet": 0.4,
        "memory_kv": 0.1, "memory_vector": 0.1, "memory_rec_sum": 0.3,
    } | {k: {"score": v, "num": n} for k, v in {}.items()}


def test_compute_aggregates():
    print("[formula] compute_aggregates")
    scores = {k: {"score": v, "num": 10} for k, v in _doc_scores().items()}
    aggs = bfcl_runner.compute_aggregates(scores)
    # simple_ast = mean(1.0, 0.5, 0.6) = 0.7
    # non_live  = mean(0.7, 0.9, 0.9, 0.9) = 0.85        (文档 NON_LIVE=0.85)
    check("non_live = 0.85", approx(aggs["non_live"]["score"], 0.85))
    # live 按样本数加权: 4 个子集各 10 条 -> mean(1, .8, .9, .7) = 0.85 (文档 LIVE=0.85)
    check("live = 0.85", approx(aggs["live"]["score"], 0.85))
    # hallucination = mean(live_irrelevance=0.9, irrelevance=1.0) = 0.95 (文档 0.95)
    check("hallucination = 0.95", approx(aggs["hallucination"]["score"], 0.95))
    # multi_turn = mean(0.5, 0.1, 0.2, 0.7) = 0.375       (文档 MULTI_TURN=0.375)
    check("multi_turn = 0.375", approx(aggs["multi_turn"]["score"], 0.375))
    # agentic = (mean(web_search) + mean(memory)) / 2
    #         = ((0.7+0.4)/2 + (0.1+0.1+0.3)/3) / 2 = 0.35833... (文档 AGENTIC=0.3584)
    check("agentic = 0.35833", approx(aggs["agentic"]["score"], 0.35833333333333334))
    # overall = 40%*0.35833 + 30%*0.375 + 10%*0.85 + 10%*0.85 + 10%*0.95
    #         = 0.52083... (文档 OVERALL=0.5209, 差异为文档表 4 位显示舍入)
    check("overall = 0.52083", approx(aggs["overall"]["score"], 0.5208333333333334))
    check("overall num = 210", aggs["overall"]["num"] == 210)
    check("全类别都有样本时 missing 为空", aggs["missing"] == [])
    check("live_relevance 不进任何聚合",
          approx(aggs["overall"]["score"], 0.5208333333333334))  # 上面已验证, 显式再断言一次

    # live 是按 num 加权的: 两个子集 n 不同 -> micro 平均
    scores2 = {"live_simple": {"score": 1.0, "num": 30},
               "live_multiple": {"score": 0.0, "num": 10}}
    aggs2 = bfcl_runner.compute_aggregates(scores2)
    check("live 按样本数加权 (30*1 + 10*0)/40 = 0.75",
          approx(aggs2["live"]["score"], 0.75), f"got {aggs2['live']['score']}")
    check("live num 合计 40", aggs2["live"]["num"] == 40)

    # 部分运行: 只跑 simple_python + irrelevance -> 缺项类别按 0 计权
    scores3 = {"simple_python": {"score": 1.0, "num": 10},
               "irrelevance": {"score": 1.0, "num": 10}}
    aggs3 = bfcl_runner.compute_aggregates(scores3)
    check("non_live 缺项子集不计入均值 (只 simple_ast)",
          approx(aggs3["non_live"]["score"], 1.0))
    check("missing 含 3 个类别",
          sorted(aggs3["missing"]) == ["agentic", "live", "multi_turn"])
    # overall = 10%*1.0 + 10%*1.0 = 0.2（缺项 0 分计权, 权重不重归一化）
    check("缺项按 0 计权 overall = 0.2", approx(aggs3["overall"]["score"], 0.2))
    check("overall num = 20", aggs3["overall"]["num"] == 20)

    # 空输入: 全 0, 不崩
    aggs4 = bfcl_runner.compute_aggregates({})
    check("空输入 overall = 0", aggs4["overall"]["score"] == 0.0)
    check("空输入 missing = 全部 5 类", len(aggs4["missing"]) == 5)


# ---------------------------------------------------------------- 报告解析 --
def test_parse_report():
    print("[report] parse_report")
    report = {
        "metrics": [
            {"identity": {"name": "acc", "aggregation": "mean"},
             "categories": [
                 {"name": ["default"], "subsets": [
                     {"name": "simple_python", "score": 1.0, "num": 10, "is_aggregate": False},
                     {"name": "irrelevance", "score": 1.0, "num": 10, "is_aggregate": False}]},
                 {"name": ["-"], "subsets": [
                     {"name": "NON_LIVE", "score": 1.0, "num": 10, "is_aggregate": True},
                     {"name": "HALLUCINATION", "score": 1.0, "num": 10, "is_aggregate": True},
                     {"name": "OVERALL", "score": 0.2, "num": 20, "is_aggregate": True}]},
             ]}],
        "model_name": "dsv4", "dataset_name": "bfcl_v4"}
    with tempfile.TemporaryDirectory() as td:
        wd = os.path.join(td, "dsv4_limit10")
        os.makedirs(os.path.join(wd, "reports", "dsv4"))
        path = os.path.join(wd, "reports", "dsv4", "bfcl_v4.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f)
        parsed = bfcl_runner.parse_report(wd)
        check("subsets 解析 2 个", set(parsed["subsets"]) == {"simple_python", "irrelevance"})
        check("聚合行分离 3 个", set(parsed["aggregate"]) == {"NON_LIVE", "HALLUCINATION", "OVERALL"})
        check("OVERALL score/num", parsed["aggregate"]["OVERALL"]["score"] == 0.2
              and parsed["aggregate"]["OVERALL"]["num"] == 20)
        check("report_path 命中", parsed["report_path"] == path)
        # 缺报告: 返回空不抛
        empty = bfcl_runner.parse_report(os.path.join(td, "nope"))
        check("报告缺失返回空", not empty["subsets"] and not empty["aggregate"])


# ---------------------------------------------------------------- 命令构建 --
def test_build_cmd():
    print("[cmd] build_cmd")
    bf = bfcl_runner.resolve_bfcl_config(
        {"service": {"url": "http://192.168.0.9:18020/v1", "model": "dsv4"}})
    cmd = bfcl_runner.build_cmd(bf, ["simple_python", "irrelevance"],
                                "/tmp/wd_bfcl", limit=10, resume=True)
    check("首元素是派发前缀或 venv python", cmd[0] in ("docker", bf["venv_python"]))
    # 宿主机侧带 docker exec <容器> 三元素前缀; 直接剥掉后看内部命令
    inner = cmd[3:] if cmd[0] == "docker" else cmd
    joined = " ".join(inner)
    check("入口脚本在命令里", "tools/bfcl_eval_entry.py" in joined)
    check("subsets 逗号拼接", "--subsets simple_python,irrelevance" in joined)
    check("limit 透传", "--limit 10" in joined)
    check("resume 带 use-cache + rerun", "--use-cache /tmp/wd_bfcl" in joined
          and "--resume" in joined)
    check("extra_params 含 is_fc_model/underscore_to_dot",
          '"is_fc_model": true' in joined and '"underscore_to_dot": true' in joined)
    # 无 limit/resume 时不带这些旗标
    cmd2 = bfcl_runner.build_cmd(bf, ["simple_python"], "/tmp/wd_bfcl2")
    joined2 = " ".join(cmd2)
    check("默认不带 --limit/--use-cache", "--limit" not in joined2
          and "--use-cache" not in joined2)


def main():
    test_resolve_config()
    test_compute_aggregates()
    test_parse_report()
    test_build_cmd()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项: {FAILS}")
        return 1
    print("BFCL runner 单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
