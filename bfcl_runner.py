#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BFCL v4 函数调用评测 runner（第五条测试线）。

对 OpenAI 兼容推理服务跑 Berkeley Function-Calling Leaderboard v4
（evalscope 1.14.1 内置 benchmarks/bfcl/v4 适配器 + bfcl-eval 2025.10.27.1）,
解析 per-subset acc, 并按官方公式计算类别分与 Overall:

    Overall = Agentic(web_search+memory) × 40% + Multi-Turn × 30%
            + Live × 10% + Non-Live × 10% + Hallucination × 10%

聚合口径与 evalscope 内置 compute_aggregate_subsets 完全一致（互相校验）:
    simple_ast   = mean(simple_python, simple_java, simple_javascript)
    non_live     = mean(simple_ast, multiple, parallel, parallel_multiple)
    live         = 按 n 加权 mean(live_simple, live_multiple, live_parallel, live_parallel_multiple)
    hallucination= mean(live_irrelevance, irrelevance)
    multi_turn   = mean(4 个 multi_turn_*)
    web_search   = mean(web_search_base, web_search_no_snippet)
    memory       = mean(memory_kv, memory_vector, memory_rec_sum)
    agentic      = mean(web_search, memory)
    live_relevance 不计入任何类别; 类别缺项按 0 分计权（权重不重归一化）。

运行位置: 评测进程跑在 ais-bench 容器内的独立 venv
/mnt/sfs_turbo/c00803992/bfcl-venv（NFS 共享盘, 容器重建不丢; 绝不往
/tmp/bench-venv 里装东西——那是摸高/精度线的 evalscope 客户端环境）。
本 runner 自身可在两种命名空间下运行, 自动选择派发方式:
    - 容器内（webapp 调起）: /.dockerenv 存在且 venv python 可执行 → 直接执行
    - 宿主机（bash run.sh --bfcl）: 经 docker exec ais-bench 进容器执行
（kit 根目录是 NFS 共享盘, 宿主/容器同路径, results/ 两侧都能读）

用法:
    bash run.sh --bfcl                     # 后台跑 config bfcl 段启用子集
    bash run.sh --bfcl -f                  # 前台跑
    python3 bfcl_runner.py --list          # 列出启用子集
    python3 bfcl_runner.py --dry-run       # 只打印命令不执行
    python3 bfcl_runner.py --only simple_python,irrelevance   # 只跑指定子集
    python3 bfcl_runner.py --limit 10      # 冒烟: 每个子集只评 10 条
    python3 bfcl_runner.py --report        # 不跑测试, 重打已有结果汇总表
    python3 bfcl_runner.py --resume        # 断点续跑（复用 work-dir 已有 predictions）

结果:
    results/bfcl/<tag>/        work-dir（predictions/reviews/reports; tag 默认=模型名, --limit 加 _limit<N> 后缀）
    results/bfcl/scores.md     汇总表（时间戳头, 追加写）
"""
import argparse
import glob
import json
import os
import shlex
import subprocess
import sys
import time

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT_ROOT)

try:
    import yaml
except ImportError:
    print("缺少 pyyaml，请先运行: pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_CONFIG = os.path.join(KIT_ROOT, "config.yaml")
RESULTS_DIR = os.path.join(KIT_ROOT, "results")
BFCL_DIR = os.path.join(RESULTS_DIR, "bfcl")
SCORES_MD = os.path.join(BFCL_DIR, "scores.md")

# venv 评测入口（在 bfcl-venv 解释器下运行, 组装 TaskConfig）
BFCL_ENTRY = os.path.join(KIT_ROOT, "tools", "bfcl_eval_entry.py")

# 是否在容器命名空间内（webapp 在 ais-bench 容器里跑 → 直接执行 venv python,
# 无需也无法再 docker exec）
IN_CONTAINER = os.path.exists("/.dockerenv")

# 22 个子集按官方口径分组（顺序即展示顺序）
SUBSET_CATEGORIES = [
    # (类别名, 子集列表, 备注)
    ("Non-Live", ["simple_python", "simple_java", "simple_javascript",
                  "multiple", "parallel", "parallel_multiple"], ""),
    ("Live", ["live_simple", "live_multiple", "live_parallel",
              "live_parallel_multiple"], ""),
    ("Relevance", ["irrelevance", "live_irrelevance", "live_relevance"],
     "live_relevance 不计入总分"),
    ("Multi-Turn", ["multi_turn_base", "multi_turn_miss_func",
                    "multi_turn_miss_param", "multi_turn_long_context"], ""),
    ("Web Search", ["web_search_base", "web_search_no_snippet"],
     "需 SERPAPI_API_KEY, 否则该类得 0"),
    ("Memory", ["memory_kv", "memory_vector", "memory_rec_sum"],
     "memory_vector 需从 HF 下载模型（本机 HF 直连不通）"),
]
ALL_SUBSETS = [s for _, subs, _ in SUBSET_CATEGORIES for s in subs]
# 默认启用: 离线可跑的全集（Non-Live + Live + Relevance + Multi-Turn）;
# web_search/memory 需外部依赖, 由 enable_web_search / enable_memory 显式开启
OFFLINE_SUBSETS = [s for cat, subs, _ in SUBSET_CATEGORIES
                   if cat in ("Non-Live", "Live", "Relevance", "Multi-Turn")
                   for s in subs]
WEB_SEARCH_SUBSETS = [s for cat, subs, _ in SUBSET_CATEGORIES if cat == "Web Search" for s in subs]
MEMORY_SUBSETS = [s for cat, subs, _ in SUBSET_CATEGORIES if cat == "Memory" for s in subs]

# Overall 公式的 5 个计分类别（名称 -> 权重%）; 前置聚合层次见 compute_aggregates
CATEGORY_WEIGHTS = [("agentic", 40), ("multi_turn", 30), ("non_live", 10),
                    ("live", 10), ("hallucination", 10)]
CATEGORY_WEIGHTS_DICT = dict(CATEGORY_WEIGHTS)

# bfcl 段缺省值（config.yaml 可逐键覆盖）
DEFAULTS = {
    "venv_python": "/mnt/sfs_turbo/c00803992/bfcl-venv/bin/python",
    "container": "ais-bench",
    "url": "",          # 留空 = 用 service.url
    "model": "",        # 留空 = 用 service.model
    "api_key": "",      # 留空 = 用 request.api_key（再缺省 EMPTY）
    "enabled_subsets": list(OFFLINE_SUBSETS),
    "enable_web_search": False,
    "enable_memory": False,
    "serpapi_api_key": "",
    "limit": 0,             # 每个子集只评 N 条（冒烟）; 0 = 全量
    "eval_batch_size": 10,
    "temperature": 0.0,     # BFCL 只支持 temperature
    "is_fc_model": True,    # true = 原生 function calling 模型; false = 提示词绕过模式
    "underscore_to_dot": True,
}


def load_config(path: str) -> dict:
    """读 config.yaml; bfcl 段允许缺失（全用默认值）"""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_bfcl_config(cfg: dict) -> dict:
    """合并 config bfcl 段与默认值; url/model/api_key 留空回退 service/request 段"""
    bf = dict(DEFAULTS)
    bf.update(cfg.get("bfcl") or {})
    svc = cfg.get("service") or {}
    req = cfg.get("request") or {}
    if not str(bf.get("url") or "").strip():
        bf["url"] = (svc.get("url") or "").strip()
    if not str(bf.get("model") or "").strip():
        bf["model"] = (svc.get("model") or "").strip()
    if not str(bf.get("api_key") or "").strip():
        bf["api_key"] = str(req.get("api_key") or "").strip() or "EMPTY"
    # 全新克隆可移植性: 配置的 venv 不存在而仓库内 bfcl-venv 就绪（setup.sh 的
    # 默认安装位置）→ 回退。本机绝对路径配置存在时优先, live 布局不受影响。
    venv_py = str(bf.get("venv_python") or "")
    if venv_py and not os.path.isfile(venv_py):
        local = os.path.join(KIT_ROOT, "bfcl-venv", "bin", "python")
        if os.path.isfile(local) and os.access(local, os.X_OK):
            print(f"提示: config bfcl.venv_python={venv_py} 不存在, "
                  f"回退仓库内 {local}", file=sys.stderr)
            bf["venv_python"] = local
    return bf


def resolve_subsets(bf: dict, only: str = None) -> list:
    """确定本次要跑的子集列表。

    --only 显式指定时原样使用（可勾 web_search/memory 子集 = 显式开启外部依赖）;
    否则 = enabled_subsets + enable_web_search/enable_memory 追加的组。
    """
    if only:
        wanted = [s.strip() for s in only.split(",") if s.strip()]
        unknown = [s for s in wanted if s not in ALL_SUBSETS]
        if unknown:
            raise ValueError(f"未知 BFCL 子集: {unknown}\n可用子集: {ALL_SUBSETS}")
        if not wanted:
            raise ValueError("--only 不能为空")
        return wanted
    subs = [str(s).strip() for s in bf.get("enabled_subsets") or []]
    unknown = [s for s in subs if s not in ALL_SUBSETS]
    if unknown:
        raise ValueError(f"config bfcl.enabled_subsets 含未知子集: {unknown}")
    if bf.get("enable_web_search"):
        subs += [s for s in WEB_SEARCH_SUBSETS if s not in subs]
    if bf.get("enable_memory"):
        subs += [s for s in MEMORY_SUBSETS if s not in subs]
    if not subs:
        raise ValueError("没有启用的 BFCL 子集（config bfcl.enabled_subsets 为空）")
    return subs


def work_dir_of(bf: dict, tag: str, limit: int) -> str:
    """work-dir: results/bfcl/<tag>（--limit 冒烟时加 _limit<N>, 与全量结果隔开）"""
    t = tag or str(bf.get("model") or "bfcl")
    if limit:
        t += f"_limit{limit}"
    return os.path.join(BFCL_DIR, t)


# --------------------------------------------------------------- 聚合公式 ----
def _avg(entries: list) -> tuple:
    """unweighted_average_from_subsets 的等价实现: 只算 num>0 的项, 返回 (均值, n 合计)"""
    vals = [(float(s), int(n or 0)) for s, n in entries if int(n or 0) > 0]
    if not vals:
        return 0.0, 0
    return sum(s for s, _ in vals) / len(vals), sum(n for _, n in vals)


def _weighted(entries: list) -> tuple:
    """weighted_average_from_subsets 的等价实现: 按 n 加权（num=0 的项自然出局）"""
    total = sum(float(s) * int(n or 0) for s, n in entries)
    cnt = sum(int(n or 0) for _, n in entries)
    return (total / cnt if cnt > 0 else 0.0), cnt


def compute_aggregates(subset_scores: dict) -> dict:
    """从 per-subset acc 计算类别分与 Overall（镜像 evalscope compute_aggregate_subsets）。

    subset_scores: {子集名: {"score": float, "num": int}}（只含真实子集）。
    返回 {"agentic": {"score","num"}, ..., "overall": {...}, "missing": [缺项类别名]}
    —— missing 记录 n=0 的计分类别, 供输出提示（这些类别按 0 分计权）。
    """
    def get(name):
        v = subset_scores.get(name) or {}
        return (float(v.get("score") or 0.0), int(v.get("num") or 0))

    simple_ast = _avg([get("simple_python"), get("simple_java"), get("simple_javascript")])
    non_live = _avg([(simple_ast[0], simple_ast[1]), get("multiple"), get("parallel"),
                     get("parallel_multiple")])
    live = _weighted([get("live_simple"), get("live_multiple"),
                      get("live_parallel"), get("live_parallel_multiple")])
    hallucination = _avg([get("live_irrelevance"), get("irrelevance")])
    multi_turn = _avg([get("multi_turn_base"), get("multi_turn_miss_func"),
                       get("multi_turn_miss_param"), get("multi_turn_long_context")])
    web_search = _avg([get("web_search_base"), get("web_search_no_snippet")])
    memory = _avg([get("memory_kv"), get("memory_vector"), get("memory_rec_sum")])
    agentic = _avg([(web_search[0], web_search[1]), (memory[0], memory[1])])

    out = {name: {"score": score, "num": num} for name, (score, num) in
           [("agentic", agentic), ("multi_turn", multi_turn), ("non_live", non_live),
            ("live", live), ("hallucination", hallucination)]}
    # live_relevance 不进任何类别; 缺项类别按 0 计权, 权重不重归一化（与官方一致）
    missing = [name for name, v in out.items() if v["num"] == 0]
    overall = sum(CATEGORY_WEIGHTS_DICT[name] * out[name]["score"]
                  for name, _ in CATEGORY_WEIGHTS) / 100.0
    out["overall"] = {"score": overall, "num": sum(v["num"] for v in out.values())}
    out["missing"] = missing
    return out


# --------------------------------------------------------------- 命令构建 ----
def resolve_dispatch(bf: dict) -> list:
    """评测子进程的派发前缀: 容器内直接执行; 宿主机 docker exec 进容器。"""
    venv_py = str(bf.get("venv_python") or "")
    if IN_CONTAINER and os.path.isfile(venv_py) and os.access(venv_py, os.X_OK):
        return []
    return ["docker", "exec", str(bf.get("container") or "ais-bench")]


def build_extra_params(bf: dict, subsets: list) -> dict:
    """dataset_args.extra_params: underscore_to_dot / is_fc_model / SERPAPI_API_KEY"""
    extra = {"underscore_to_dot": bool(bf.get("underscore_to_dot")),
             "is_fc_model": bool(bf.get("is_fc_model")),
             "SERPAPI_API_KEY": str(bf.get("serpapi_api_key") or "") or None}
    if any(s in WEB_SEARCH_SUBSETS for s in subsets) and not extra["SERPAPI_API_KEY"]:
        print("警告: 本次包含 web_search 子集但未配置 bfcl.serpapi_api_key, "
              "该类别得分会全为 0", file=sys.stderr)
    if any(s in MEMORY_SUBSETS for s in subsets):
        print("提示: memory 子集会先跑预填充推理（耗时）; memory_vector 首次运行需从 "
              "HF 下载模型（本机 HF 直连不通, 需配 HF_ENDPOINT=hf-mirror.com 或代理）",
              file=sys.stderr)
    return extra


def build_cmd(bf: dict, subsets: list, work_dir: str, limit: int = None,
              resume: bool = False) -> list:
    """构建完整评测命令（含容器派发前缀; dry-run 打印的就是它, 可直接复现）"""
    limit = int(limit if limit is not None else (bf.get("limit") or 0))
    extra = build_extra_params(bf, subsets)
    inner = [str(bf.get("venv_python")), BFCL_ENTRY,
             "--model", str(bf["model"]),
             "--api-url", str(bf["url"]),
             "--api-key", str(bf.get("api_key") or "EMPTY"),
             "--subsets", ",".join(subsets),
             "--extra-params", json.dumps(extra),
             "--batch-size", str(int(bf.get("eval_batch_size") or 10)),
             "--temperature", str(float(bf.get("temperature") or 0)),
             "--work-dir", work_dir]
    if limit and limit > 0:
        inner += ["--limit", str(limit)]
    if resume:
        # 断点续跑: work-dir 里已有 predictions 时复用只补缺样本; --resume 让入口
        # 带 rerun_review（命令变过时跳过缓存身份哈希核对, 同精度线 --resume）
        inner += ["--use-cache", work_dir, "--resume"]
    return resolve_dispatch(bf) + inner


def precheck_dispatch(bf: dict) -> str:
    """非 dry-run 前的轻量预检: venv python 在派发侧可用（否则给中文原因）。"""
    prefix = resolve_dispatch(bf)
    venv_py = str(bf.get("venv_python"))
    if not prefix:
        return None
    try:
        r = subprocess.run(["docker", "exec", str(bf.get("container") or "ais-bench"),
                            "test", "-x", venv_py], capture_output=True, timeout=30)
        if r.returncode != 0:
            return (f"容器 {bf.get('container')} 里找不到可执行的 {venv_py}。\n"
                    f"请先创建 bfcl-venv（见 README 'BFCL 函数调用评测'）: "
                    f"docker exec {bf.get('container')} /usr/local/python3.12.13/bin/python3.12 "
                    f"-m venv {os.path.dirname(os.path.dirname(venv_py))} 并安装依赖")
    except FileNotFoundError:
        return "宿主机没有 docker 命令（本 runner 需要经 docker exec 进 ais-bench 容器跑评测）"
    except subprocess.TimeoutExpired:
        return "docker exec 预检超时（容器是否在运行?）"
    return None


# --------------------------------------------------------------- 结果解析 ----
def parse_report(work_dir: str) -> dict:
    """解析 <work_dir>/reports/<model_id>/bfcl_v4.json（model_id 目录名不写死）。

    结构: {"metrics": [{"identity":..., "categories": [{"name":...,
    "subsets": [{"name", "score", "num", "is_aggregate"}]}]}]}。
    聚合行（AGENTIC/OVERALL 等）在 name='-' 的类别下, is_aggregate=True。
    返回 {"subsets": {名字: {"score","num"}}, "aggregate": {大写名: {...}},
          "report_path": path}; 文件缺失/损坏返回空（不抛, 调用方给提示）。
    """
    out = {"subsets": {}, "aggregate": {}, "report_path": None}
    paths = sorted(glob.glob(os.path.join(work_dir, "reports", "*", "*.json")))
    if not paths:
        return out
    out["report_path"] = paths[-1]
    try:
        with open(paths[-1], encoding="utf-8") as f:
            data = json.load(f)
        for met in data.get("metrics") or []:
            for cat in met.get("categories") or []:
                for sub in cat.get("subsets") or []:
                    name = str(sub.get("name") or "")
                    if not name:
                        continue
                    entry = {"score": sub.get("score"), "num": sub.get("num")}
                    if sub.get("is_aggregate"):
                        out["aggregate"][name] = entry
                    else:
                        out["subsets"][name] = entry
    except (OSError, ValueError, AttributeError, TypeError):
        pass  # JSON 损坏/结构变化: 保持空, 上层按"无分数"处理
    return out


def _fmt(x) -> str:
    return "-" if x is None else f"{float(x):.4f}"


def overall_formula_str(aggs: dict) -> str:
    """Overall 的展开式（对照公式, 便于人工核对）"""
    parts = [f"{w}%×{name}({_fmt(aggs[name]['score'])})"
             for name, w in CATEGORY_WEIGHTS]
    return " + ".join(parts) + f" = {_fmt(aggs['overall']['score'])}"


def write_scores_md(bf: dict, subsets: list, parsed: dict, aggs: dict,
                    work_dir: str, limit: int) -> None:
    """追加写 results/bfcl/scores.md（时间戳头 + 子集表 + 聚合行 + Overall）"""
    lines = [f"## BFCL 函数调用汇总 {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    lim_s = f", limit={limit}" if limit else ""
    lines.append(f"- 模型: {bf['model']}  端点: {bf['url']}")
    lines.append(f"- 子集: {', '.join(subsets)}{lim_s}  "
                 f"(batch={bf.get('eval_batch_size')}, temperature={bf.get('temperature')})")
    lines.append(f"- work-dir: {work_dir}")
    if aggs.get("missing"):
        lines.append(f"- 注: 未跑/无样本的计分类别按 0 分计权: "
                     f"{', '.join(aggs['missing'])}（与 evalscope 官方口径一致）")
    if aggs.get("overall") is not None and parsed.get("aggregate", {}).get("OVERALL") is not None:
        ev = parsed["aggregate"]["OVERALL"]
        lines.append(f"- evalscope 报告 OVERALL = {_fmt(ev['score'])} (n={ev['num']})")
    lines += ["", "| 子集 | 类别 | n | acc |", "|---|---|---|---|"]
    cat_of = {}
    for cat, subs, _ in SUBSET_CATEGORIES:
        for s in subs:
            cat_of[s] = cat
    for name, v in parsed.get("subsets", {}).items():
        lines.append(f"| {name} | {cat_of.get(name, '-')} | {v['num']} | {_fmt(v['score'])} |")
    for name, v in parsed.get("aggregate", {}).items():
        if v.get("num"):
            lines.append(f"| **{name}** | **汇总** | {v['num']} | **{_fmt(v['score'])}** |")
    if aggs.get("overall") is not None and aggs["overall"].get("num"):
        lines.append(f"| **OVERALL** | **总分** | {aggs['overall']['num']} "
                     f"| **{_fmt(aggs['overall']['score'])}** |")
    lines.append("")
    with open(SCORES_MD, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nscores.md 已追加: {SCORES_MD}")


# --------------------------------------------------------------- 主流程 ----
def run_eval(cfg: dict, bf: dict, subsets: list, limit: int, tag: str,
             resume: bool, dry_run: bool) -> tuple:
    """执行一次评测; 返回 (returncode, 解析结果/聚合结果)。dry-run 只打印。"""
    work_dir = work_dir_of(bf, tag, limit)
    cmd = build_cmd(bf, subsets, work_dir, limit, resume)
    lim_s = f" / limit {limit}" if limit else ""
    res_s = " / resume" if resume else ""
    print(f"\n{'=' * 70}\n[BFCL] 模型 {bf['model']}  子集 {len(subsets)} 个{lim_s}{res_s}")
    print("  命令: " + " ".join(shlex.quote(c) for c in cmd))
    print(f"  work-dir: {work_dir}")
    if dry_run:
        return 0, None
    err = precheck_dispatch(bf)
    if err:
        print(err, file=sys.stderr)
        return 2, None
    t0 = time.time()
    try:
        rc = subprocess.run(cmd).returncode
    except Exception as e:
        print(f"  [异常] {e}")
        rc = 1
    status = "OK" if rc == 0 else f"FAILED({rc})"
    print(f"  [BFCL] {status}  耗时 {(time.time() - t0) / 60:.1f} 分钟")
    parsed = parse_report(work_dir)
    if not parsed["subsets"] and not parsed["aggregate"]:
        print(f"  [BFCL] 未解析到分数（报告缺失: {work_dir}/reports/ 下无 *.json）")
        return rc, None
    missing_req = [s for s in subsets if s not in parsed["subsets"]]
    if missing_req:
        print(f"  [BFCL] 警告: 请求的子集在报告中缺失: {missing_req}")
    aggs = compute_aggregates(parsed["subsets"])
    for name, v in parsed["subsets"].items():
        print(f"  {name}: acc={_fmt(v['score'])} (n={v['num']})")
    for name, v in parsed["aggregate"].items():
        if v.get("num"):
            print(f"  {name}: score={_fmt(v['score'])} (n={v['num']})")
    if aggs.get("missing"):
        print(f"  缺项类别按 0 计权: {', '.join(aggs['missing'])}")
    print(f"  Overall = {overall_formula_str(aggs)}")
    ev_overall = parsed["aggregate"].get("OVERALL")
    if ev_overall is not None and ev_overall.get("score") is not None:
        mine = aggs["overall"]["score"]
        if abs(float(ev_overall["score"]) - float(mine)) > 1e-3:
            print(f"  警告: 本地公式 Overall={_fmt(mine)} 与 evalscope 报告 "
                  f"OVERALL={_fmt(ev_overall['score'])} 不一致（以 evalscope 为准）")
        else:
            print(f"  evalscope 报告 OVERALL = {_fmt(ev_overall['score'])}（与本地公式一致）")
    return rc, (parsed, aggs)


def cmd_report(cfg: dict) -> int:
    """--report: 扫 results/bfcl/*/ 已有结果重打汇总表"""
    work_dirs = sorted(d for d in glob.glob(os.path.join(BFCL_DIR, "*"))
                       if os.path.isdir(d))
    if not work_dirs:
        print(f"results/bfcl 下还没有结果（{BFCL_DIR}/<tag>/reports/ 无报告）, "
              f"先运行: bash run.sh --bfcl")
        return 0
    found = False
    for wd in work_dirs:
        parsed = parse_report(wd)
        if not parsed["subsets"]:
            continue
        found = True
        print(f"\n[{os.path.basename(wd)}]  来源: {parsed['report_path']}")
        for name, v in parsed["subsets"].items():
            print(f"  {name}: acc={_fmt(v['score'])} (n={v['num']})")
        for name, v in parsed["aggregate"].items():
            if v.get("num"):
                print(f"  {name}: score={_fmt(v['score'])} (n={v['num']})")
        aggs = compute_aggregates(parsed["subsets"])
        if aggs.get("missing"):
            print(f"  缺项类别按 0 计权: {', '.join(aggs['missing'])}")
        print(f"  Overall = {overall_formula_str(aggs)}")
    if not found:
        print("有结果目录但没有可解析的报告（reports/<model>/*.json 缺失）")
        return 1
    print(f"\n明细: results/bfcl/<tag>/reports/<model>/bfcl_v4.json")
    return 0


def main():
    ap = argparse.ArgumentParser(description="BFCL v4 函数调用评测 runner")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    ap.add_argument("--list", action="store_true", help="列出启用子集后退出")
    ap.add_argument("--only", default=None,
                    help="只跑指定子集（逗号分隔, 如 simple_python,irrelevance）")
    ap.add_argument("--limit", type=int, default=None,
                    help="每个子集只评 N 条（覆盖 config; 冒烟用）")
    ap.add_argument("--batch-size", type=int, default=None,
                    help="覆盖 config 的 eval_batch_size（评测并发）")
    ap.add_argument("--temperature", type=float, default=None,
                    help="覆盖 config 的 temperature（BFCL 只支持温度）")
    ap.add_argument("--tag", default=None,
                    help="结果目录名（默认=模型名; limit>0 时加 _limit<N> 后缀）")
    ap.add_argument("--report", action="store_true",
                    help="不跑测试, 扫 results/bfcl/ 已有结果重打汇总表")
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑: 复用 work-dir 已有 predictions, 只补缺样本重出报告")
    ap.add_argument("-f", "--foreground", action="store_true",
                    help="前台运行（实际由 run.sh 处理, runner 侧仅为兼容接收）")
    args = ap.parse_args()
    if args.limit is not None and args.limit < 1:
        print("--limit 必须 >= 1（0 会被当成无限量，非预期）", file=sys.stderr)
        return 2

    cfg = load_config(args.config)
    if args.report:
        return cmd_report(cfg)

    bf = resolve_bfcl_config(cfg)
    # CLI 参数覆盖 config（不回写, 只影响本次运行）
    if args.batch_size is not None:
        bf["eval_batch_size"] = args.batch_size
    if args.temperature is not None:
        bf["temperature"] = args.temperature
    try:
        subsets = resolve_subsets(bf, args.only)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2

    if args.list:
        print(f"目标服务: {bf['url']}  模型: {bf['model']}")
        print(f"venv: {bf['venv_python']}  "
              f"({'容器内直接执行' if not resolve_dispatch(bf) else 'docker exec ' + str(bf['container'])})")
        for cat, subs, note in SUBSET_CATEGORIES:
            marks = "".join("x" if s in subsets else " " for s in subs)
            print(f"  {cat:12s} {marks}  {', '.join(subs)}"
                  + (f"  # {note}" if note else ""))
        print(f"共启用 {len(subsets)}/{len(ALL_SUBSETS)} 个子集 "
              f"(limit={bf.get('limit') or '全量'}, batch={bf.get('eval_batch_size')}, "
              f"temperature={bf.get('temperature')}, is_fc_model={bf.get('is_fc_model')}, "
              f"underscore_to_dot={bf.get('underscore_to_dot')})")
        return 0

    for key in ("url", "model"):
        if not bf.get(key):
            print(f"config 缺少 bfcl.{key} 且 service 段也没有 {key}（BFCL 测试必填）",
                  file=sys.stderr)
            return 2

    limit = args.limit if args.limit is not None else int(bf.get("limit") or 0)
    if not args.dry_run:
        os.makedirs(BFCL_DIR, exist_ok=True)

    rc, result = run_eval(cfg, bf, subsets, limit, args.tag, args.resume, args.dry_run)
    if result is not None:
        parsed, aggs = result
        if not args.dry_run:
            write_scores_md(bf, subsets, parsed, aggs, work_dir_of(bf, args.tag, limit), limit)
    if rc != 0:
        return 1
    if result is None and not args.dry_run:
        # evalscope 退出码 0 但没解析到分数 = 假通过（报告缺失/结构变化）
        print("退出码 0 但没解析到分数, 按失败处理（排查 work-dir 下 evalscope 日志）")
        return 1
    print("\nBFCL 评测完成。汇总表: python3 bfcl_runner.py --report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
