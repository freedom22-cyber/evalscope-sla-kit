#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalscope 精度测试 runner（迁移自 sunzhi_sla_eval run208.sh cmd_accuracy, 通用化）。

对 OpenAI 兼容推理服务跑 evalscope eval 数据集组（gsm8k / mmlu / gpqa_diamond /
aime25+aime26 …），逐组逐轮顺序执行并解析 accuracy 分数，汇总为 markdown 表。

精度口径 = pass@k（可配 k）：每组用 --repeats k 重复采样同一批题，evalscope 以
aggregation=mean_and_pass_at_k 聚合，报告里同时给出 mean（k 次采样平均）与
pass@1..pass@k（k 次里至少对一次的概率）。k=1 时退化为原来的单次 mean 口径。

用法:
    python3 accuracy_runner.py --list             # 列出所有启用精度数据集组
    python3 accuracy_runner.py --dry-run          # 只打印命令与 work-dir，不执行
    python3 accuracy_runner.py --only all5       # 只跑指定组（tag 见 --list）
    python3 accuracy_runner.py --k 4              # 临时覆盖 pass@k 的 k（覆盖 config）
    python3 accuracy_runner.py --report           # 不跑测试，只扫已有结果重打汇总表
    python3 accuracy_runner.py                    # 顺序执行所有启用组

执行流程:
    1. 解析 config.yaml accuracy.datasets（每组一行 6~7 字段:
       "tag, evalscope_datasets, rounds, effort, max_tokens, batch_size [, k]"）
    2. 构建 evalscope eval 命令（宿主机直跑，不走 docker——setup.sh 已装 evalscope），
       每组每轮独立 work-dir: results/accuracy/<tag>_r<N>（k>1 时为 <tag>_pass@k<k>_r<N>，
       与单次 mean 口径的结果目录隔开），--no-timestamp
    3. reasoning_effort 走 generation-config 的 extra_body（evalscope -> OpenAI SDK
       顶层展开进请求体，已抓包验证, 见 sunzhi 仓 docs/lessons_api208.md §4）;
       服务端接受 low/high/xhigh/max/none, "medium" 在部分部署上是已知 400 缺陷
    4. accuracy.proxy 非空时给子进程单独导出 http_proxy/https_proxy（评测集首次
       下载走外网），被测服务主机名自动进 no_proxy，不污染父进程环境
    5. k>1 时追加 --repeats k 与 --dataset-args '{"<ds>": {"aggregation":
       "mean_and_pass_at_k", "primary_metric": {"name": "accuracy",
       "aggregation": "pass_at_k", "dimensions": {"k": k}}}}'（每题采样 k 次，
       请求量 x k）; primary_metric 必须显式声明, 否则 evalscope 报告层判不出唯一
       主指标会抛 ValueError（见 tools/passk_evalscope.py 头注释）
    6. k>1 时入口换成 tools/passk_evalscope.py（dict 型 primary_metric 只有
       该 wrapper 能解析; 原生 evalscope 二进制会 AttributeError）
    7. --resume 时追加 --use-cache <work-dir> --rerun-review: 复用已有 predictions
       只补缺样本, 重算判分/报告（命令变过时缓存身份哈希对不上, --rerun-review
       必带; 见 README "断点续跑"）
    8. --limit（冒烟: 每个子集只评 N 条）时 work-dir 加 _limit<N> 后缀, 与全量结果
       隔开, 避免冒烟结果覆盖全量报告/污染 predictions 缓存
    9. 每组每轮跑完即解析 reports/<model>/<ds>.json 的 mean 与 pass@1..k 并打印;
       全部结束后追加写 results/accuracy/scores.md，多轮同组打印均值
"""
import argparse
import glob
import json
import os
import shlex
import shutil
import statistics
import subprocess
import sys
import time
from urllib.parse import urlsplit

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT_ROOT)

try:
    import yaml
except ImportError:
    print("缺少 pyyaml，请先运行: pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_CONFIG = os.path.join(KIT_ROOT, "config.yaml")
RESULTS_DIR = os.path.join(KIT_ROOT, "results")
ACC_DIR = os.path.join(RESULTS_DIR, "accuracy")
SCORES_MD = os.path.join(ACC_DIR, "scores.md")

# generation-config 固定采样参数（与 sunzhi run208.sh cmd_accuracy 一致）
GEN_TEMPERATURE = 1.0
GEN_TOP_P = 0.95
GEN_TOP_K = 20
GEN_TIMEOUT_MS = 3600000

# 本机 evalscope 装在独立 venv 里（不在 PATH 上）—— resolve_evalscope_bin 的兜底位置
KNOWN_EVALSCOPE_BINS = [
    "/mnt/sfs_turbo/c00803992/tools/evalscope/venv311/bin/evalscope",
]

# pass@k（k>1）专用的 evalscope 入口：原生二进制不认 dict 形式的 primary_metric，
# 报告阶段会抛 ValueError（详见 tools/passk_evalscope.py 头注释）。
PASSK_WRAPPER = os.path.join(KIT_ROOT, "tools", "passk_evalscope.py")


def parse_dataset_line(line: str) -> dict:
    """解析数据集组行: "tag, evalscope_datasets, rounds, effort, max_tokens, batch_size [, k]"

    evalscope_datasets 为一个字段内的空格分隔多值（如 "aime25 aime26"）。
    k = pass@k 的采样次数（可选，默认 1 = 单次 mean 口径，不加 --repeats）。
    """
    parts = [p.strip() for p in str(line).split(",")]
    if len(parts) not in (6, 7):
        raise ValueError(f"数据集组行格式错误（需 6 或 7 个逗号分隔字段）: {line!r}")
    tag, dss, rounds, effort, max_tokens, batch = parts[:6]
    k_s = parts[6] if len(parts) == 7 else "1"
    if not tag or not dss:
        raise ValueError(f"数据集组行 tag 与数据集名不能为空: {line!r}")
    try:
        rounds_i, maxtok_i, batch_i = int(rounds), int(max_tokens), int(batch)
        k_i = int(k_s)
    except ValueError:
        raise ValueError(f"数据集组行 rounds/max_tokens/batch_size/k 必须是整数: {line!r}")
    if rounds_i < 1:
        raise ValueError(f"数据集组行 rounds 必须 >= 1: {line!r}")
    if maxtok_i < 1 or batch_i < 1:
        raise ValueError(f"数据集组行 max_tokens/batch_size 必须 > 0: {line!r}")
    if k_i < 1:
        raise ValueError(f"数据集组行 k（pass@k 采样次数）必须 >= 1: {line!r}")
    if not effort:
        raise ValueError(f"数据集组行 effort 不能为空: {line!r}")
    return {
        "tag": tag,
        "datasets": dss.split(),
        "rounds": rounds_i,
        "effort": effort,
        "max_tokens": maxtok_i,
        "batch_size": batch_i,
        "k": k_i,
        "raw": line,
    }


def load_config(path: str) -> dict:
    """读 config.yaml；accuracy 段允许缺失（无组时友好退出），service 运行时校验"""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_groups(cfg: dict) -> list:
    """解析 accuracy.datasets 行列表; accuracy 段缺失/为空 -> []"""
    groups = []
    for line in (cfg.get("accuracy") or {}).get("datasets") or []:
        line = str(line).strip()
        if not line or line.startswith("#"):
            continue
        groups.append(parse_dataset_line(line))
    return groups


def resolve_evalscope_bin(cfg: dict) -> str:
    """evalscope 可执行文件: 环境变量 EVALSCOPE_BIN > config accuracy.evalscope_bin
    > PATH 上的 evalscope > 本机已知 venv 路径。

    缺失时打告警不阻断（与 sunzhi env.sh 的自检对齐; dry-run / 目标机外预检也能过）。
    """
    explicit = os.environ.get("EVALSCOPE_BIN") or (cfg.get("accuracy") or {}).get("evalscope_bin")
    bin_path = explicit or "evalscope"
    # 只有"命令名"（不含路径分隔符, 靠 PATH 找）才允许兜底到本机已知 venv 路径;
    # 显式给的路径找不到就照实报错——否则两台机器会静默跑不同版本的 evalscope,
    # 分数不可比
    is_path_spec = "/" in bin_path
    if not is_path_spec and not shutil.which(bin_path):
        for fallback in KNOWN_EVALSCOPE_BINS:
            if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
                bin_path = fallback
                break
    if not shutil.which(bin_path):
        print(f"警告: 未找到 evalscope 可执行文件: {bin_path} "
              f"(可用环境变量 EVALSCOPE_BIN 覆盖; 请先在目标机运行 bash setup.sh)",
              file=sys.stderr)
    return bin_path


def resolve_python_exe(evalscope_bin: str) -> str:
    """找出与 evalscope 同环境的 python 解释器（跑 tools/evalscope_passk_entry.py 用）。

    evalscope_bin 可能是绝对路径（/path/to/venv/bin/evalscope）也可能是 PATH 上的
    命令名。优先取同目录下的 python（venv / conda env 都符合这个布局），保证 wrapper
    用的 site-packages 和 evalscope 是同一份；找不到就退回当前解释器 sys.executable。
    """
    resolved = evalscope_bin
    if "/" not in resolved:
        resolved = shutil.which(resolved) or resolved
    cand = os.path.join(os.path.dirname(resolved), "python")
    if os.path.isfile(cand) and os.access(cand, os.X_OK):
        return cand
    return sys.executable


def tag_key(group: dict, limit: int = None) -> str:
    """结果行的分组 key: k>1 时带 _pass@k<k> 后缀（--limit 冒烟时再加 _limit<N>）。

    与 work_dir_of 的目录名口径一致——否则 k=1 与 k=4 的同 tag 结果会被当成
    两轮混在一起算均值, 且实时表与 --report 表的 tag 对不上。
    """
    k = group.get("k", 1)
    key = f"{group['tag']}_pass@k{k}" if k > 1 else group["tag"]
    return f"{key}_limit{limit}" if limit else key


def work_dir_of(group: dict, rnd: int, limit: int = None) -> str:
    """单组单轮 work-dir: results/accuracy/<tag>_r<N>

    k>1（pass@k 口径）时目录名带 _pass@k<k> 后缀，与单次 mean 口径的结果隔开，
    且 k 值写进目录名——不同 k 的结果不会互相覆盖/混用（evalscope 的 predictions
    是可复用的，同目录换 k 会污染结果）。--report 会把两种口径一起扫出来。

    --limit（冒烟：每个子集只评 N 条）时再加 _limit<N> 后缀：冒烟结果与全量结果
    必须分开存, 否则同一 work-dir 里的报告会被冒烟跑的 5 条结果覆盖掉全量结果。
    """
    k = group.get("k", 1)
    suffix = f"_pass@k{k}" if k > 1 else ""
    if limit:
        suffix += f"_limit{limit}"
    return os.path.join(ACC_DIR, f"{group['tag']}{suffix}_r{rnd}")



def build_generation_config(group: dict) -> dict:
    """generation-config: reasoning_effort 经 extra_body 顶层展开进请求体。

    服务端接受 low/high/xhigh/max/none（及整数 1~100）; "medium" 在部分部署上
    是已知 HTTP 400 缺陷（sunzhi 仓 F-03）。

    ⚠️ stream=True 是硬性要求（MaaS 网关 113.46.2.168 有 60s **首字节**超时:
    请求进网关后 60s 内后端没吐出任何字节就返回 504; 非流式的首字节=整个响应,
    长思考请求必然踩雷)。流式把首字节压到第一个 token, 实测可跑 1034s。
    详见 sunzhi 仓 accuracy/env.sh 与 CLAUDE.md §二.1。
    """
    return {
        "max_tokens": group["max_tokens"],
        "temperature": GEN_TEMPERATURE,
        "top_p": GEN_TOP_P,
        "top_k": GEN_TOP_K,
        "stream": True,
        "extra_body": {"reasoning_effort": group["effort"]},
        "timeout": GEN_TIMEOUT_MS,
    }


def build_dataset_args(group: dict) -> dict:
    """pass@k 的 --dataset-args: 每个数据集的 aggregation 换成 mean_and_pass_at_k。

    evalscope 侧 dataset_args 会合并进 BenchmarkMeta（registry.py），aggregation
    是可覆盖字段；mean_and_pass_at_k 会同时产出 mean 与 pass@1..k 两类指标
    （evalscope metrics/aggregators/aggregators.py::MeanPassAtK）。

    ⚠️ primary_metric 必须显式声明: 上面那 k+1 个 identity 全都叫 "accuracy"，
    而 evalscope 报告层（metrics/semantics/primary.py::select_primary）在没有
    显式声明时要求"有且仅有一个非诊断指标" → 5 个全算计分指标 → 歧义 →
    ValueError → 报告 json 永不落盘 → 外部看到的是"结果文件缺失"。
    声明成 dict 形式（带 aggregation + dimensions.k）由 tools/evalscope_passk_entry.py
    转成 MetricSelector；原生 evalscope 二进制只收字符串，而字符串只按名字选，
    5 个同名 identity 依然歧义 → 所以 k>1 必须走那个 wrapper 入口。

    口径是 pass@k（不是 mean），所以主指标取 dimensions.k = k。
    """
    k = group.get("k", 1)
    primary = {"name": "accuracy", "aggregation": "pass_at_k", "dimensions": {"k": k}}
    return {ds: {"aggregation": "mean_and_pass_at_k", "primary_metric": primary}
            for ds in group["datasets"]}


def build_cmd(cfg: dict, group: dict, rnd: int, evalscope_bin: str,
              limit: int = None, resume: bool = False) -> list:
    """构建单组单轮 evalscope eval 命令（宿主机直跑，不走 docker）"""
    svc = cfg["service"]
    api_key = (cfg.get("request") or {}).get("api_key") or "EMPTY"
    cmd = [
        evalscope_bin, "eval",
        "--model", svc["model"],
        "--model-id", svc["model"],
        "--eval-type", "openai_api",
        "--api-url", svc["url"],  # 完整 chat completions 路径, 原样透传
        "--api-key", api_key,
        # argv 列表执行: 每个数据集必须是独立元素; 拼成 "aime25 aime26" 一个
        # 元素会被 evalscope 当成单个基准名报 Benchmark not found（16.251 实测）
        "--datasets", *group["datasets"],
        "--generation-config", json.dumps(build_generation_config(group)),
        "--eval-batch-size", str(group["batch_size"]),
        "--work-dir", work_dir_of(group, rnd, limit),
        "--no-timestamp",
    ]
    k = group.get("k", 1)
    if k > 1:
        # pass@k: 每题重复采样 k 次, 并让 evalscope 按 mean_and_pass_at_k 聚合。
        # primary_metric 的 dict 需 wrapper 转 MetricSelector, 否则报告期必崩
        # (见 build_dataset_args 注释), 因此 k>1 时切到 wrapper 入口。
        cmd += ["--repeats", str(k),
                "--dataset-args", json.dumps(build_dataset_args(group))]
        # wrapper 用与 evalscope 同一个解释器跑, 保证 site-packages 一致
        # (wrapper 自带 shebang 指向别的环境时 import 的是另一份 evalscope)
        cmd[0:1] = [resolve_python_exe(evalscope_bin), PASSK_WRAPPER]
    if resume:
        # 断点续跑: 复用 work-dir 里已有 predictions, 只补缺的样本并重算 review/报告
        # (evalscope run.py: use_cache 即把 work_dir 指向该目录)。
        # 必须带 --rerun-review: 命令一变(如补 primary_metric)缓存身份哈希就对不上,
        # evalscope 会拒跑(ValueError: Cached evaluation identity does not match);
        # rerun_review 只重算 review/聚合(规则判分, 秒级), 昂贵的 predictions 照旧复用
        cmd += ["--use-cache", work_dir_of(group, rnd, limit), "--rerun-review"]
    if limit:  # 冒烟: 每个子集只评 limit 条
        cmd += ["--limit", str(limit)]
    return cmd


def build_subprocess_env(cfg: dict):
    """accuracy.proxy 非空时为子进程构造代理环境变量（仿 sunzhi run208.sh 写法）。

    http_proxy/https_proxy 指向代理供 evalscope 首次下载评测集; 被测服务主机名 +
    localhost/127.0.0.1 进 no_proxy/NO_PROXY, 避免评测请求被代理劫持。
    只作用于子进程（Popen/run 的 env 参数），不污染父进程 env。
    proxy 为空返回 None（= 子进程继承当前环境）。
    """
    proxy = str((cfg.get("accuracy") or {}).get("proxy") or "").strip()
    if not proxy:
        return None
    host = urlsplit(cfg["service"]["url"]).hostname or ""
    no_proxy = " ".join(x for x in (host, "localhost", "127.0.0.1") if x)
    env = os.environ.copy()
    env["http_proxy"] = env["https_proxy"] = proxy
    env["no_proxy"] = env["NO_PROXY"] = no_proxy
    return env


def parse_report(work_dir: str, model: str, datasets: list) -> dict:
    """解析一轮结果: <work_dir>/reports/<model>/<ds>.json 的 accuracy 分数。

    metrics[] 里每项都有 identity{name, aggregation, dimensions}:
      - aggregation=="mean"       -> mean 口径（k 次采样平均，k=1 时即单次准确率）
      - aggregation=="pass_at_k"  -> dimensions.k = n，score = pass@n
    mean 项取 identity.name=="accuracy" 的那条（照搬 sunzhi run208.sh 的解析口径;
    无该 identity 时退回 metrics[0]）；pass@k 项按 dimensions.k 收进 pass_at_k 字典。
    文件缺失/损坏时该数据集的 score/num 为 None（绝不抛异常），由调用方打缺失提示。
    返回 {ds: {"score":..., "num":..., "pass_at_k": {n: score}, "path":...}}
    """
    out = {}
    for ds in datasets:
        path = os.path.join(work_dir, "reports", model, f"{ds}.json")
        score, num = None, None
        pass_at_k = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            mets = data.get("metrics") or []
            acc = next((m for m in mets
                        if (m.get("identity") or {}).get("name") == "accuracy"
                        and (m.get("identity") or {}).get("aggregation") in (None, "mean")),
                       None)
            if acc is None:
                acc = next((m for m in mets
                            if (m.get("identity") or {}).get("name") == "accuracy"),
                           mets[0] if mets else {})
            score, num = acc.get("score"), acc.get("num")
            for m in mets:
                ident = m.get("identity") or {}
                if ident.get("aggregation") != "pass_at_k":
                    continue
                k_raw = (ident.get("dimensions") or {}).get("k")
                try:
                    pass_at_k[int(k_raw)] = m.get("score")
                except (TypeError, ValueError):
                    continue  # 没有 k 维度的畸形项: 跳过
        except (OSError, ValueError, AttributeError, TypeError):
            pass  # 文件缺失 / JSON 损坏 / 结构异常: 保持 None
        out[ds] = {"score": score, "num": num, "pass_at_k": pass_at_k, "path": path}
    return out


def format_pass_at_k(pass_at_k: dict) -> str:
    """pass_at_k 字典 -> "pass@1=0.41 pass@2=0.57 ..." 文本"""
    return "  ".join(f"pass@{k}={pass_at_k[k]}" for k in sorted(pass_at_k))


def print_scores(tag_r: str, scores: dict) -> None:
    """打印单轮分数; 结果文件缺失时给出路径提示"""
    for ds, v in scores.items():
        if v["score"] is None:
            print(f"  [{tag_r}] {ds}: 结果文件缺失 ({v['path']})")
        else:
            print(f"  [{tag_r}] {ds}: score={v['score']} (n={v['num']})")
            pak = v.get("pass_at_k") or {}
            if pak:
                print(f"  [{tag_r}] {ds}: {format_pass_at_k(pak)}")


def effective_line(group: dict) -> str:
    """按生效参数拼回数据集组行（--k 覆盖后与实际执行一致，便于看日志核对）"""
    return (f"{group['tag']}, {' '.join(group['datasets'])}, {group['rounds']}, "
            f"{group['effort']}, {group['max_tokens']}, {group['batch_size']}, "
            f"{group.get('k', 1)}")


def run_group_round(cfg: dict, group: dict, rnd: int, evalscope_bin: str,
                    env, dry_run: bool, limit: int = None,
                    resume: bool = False) -> tuple:
    """执行单组单轮; 返回 (returncode, scores)。dry-run 只打印不执行。"""
    tag_r = f"{group['tag']}_r{rnd}"
    work_dir = work_dir_of(group, rnd, limit)
    cmd = build_cmd(cfg, group, rnd, evalscope_bin, limit, resume)
    mode = f"pass@{group['k']}" if group.get("k", 1) > 1 else "mean"
    if limit:
        mode += f" / limit {limit}"
    if resume:
        mode += " / resume"
    print(f"\n{'=' * 70}\n[精度 {tag_r}]  {effective_line(group)}   (口径: {mode})")
    print("  命令: " + " ".join(shlex.quote(c) for c in cmd))
    print(f"  work-dir: {work_dir}")
    if dry_run:
        return 0, None
    t0 = time.time()
    try:
        rc = subprocess.run(cmd, env=env).returncode
    except Exception as e:
        print(f"  [异常] {e}")
        rc = 1
    status = "OK" if rc == 0 else f"FAILED({rc})"
    print(f"  [{tag_r}] {status}  耗时 {(time.time() - t0) / 60:.1f} 分钟")
    scores = parse_report(work_dir, cfg["service"]["model"], group["datasets"])
    print_scores(tag_r, scores)
    return rc, scores


def print_round_means(rows: list) -> None:
    """多轮同组打印均值（仅对收集到 >= 2 轮分数的 (tag, 数据集) 打印）

    mean 口径与 pass@k 口径都参与平均；pass@k 按 k 分桶平均。
    """
    by_key = {}
    for r in rows:
        if r["score"] is None:
            continue
        by_key.setdefault((r["tag"], r["ds"]), {"mean": [], "pak": {}})
        by_key[(r["tag"], r["ds"])]["mean"].append(float(r["score"]))
        for k, v in (r.get("pass_at_k") or {}).items():
            if v is None:
                continue
            by_key[(r["tag"], r["ds"])]["pak"].setdefault(k, []).append(float(v))
    for (tag, ds), agg in sorted(by_key.items()):
        if len(agg["mean"]) >= 2:
            print(f"  [{tag}] {ds}: {len(agg['mean'])} 轮均值 score="
                  f"{statistics.fmean(agg['mean']):.4f}")
            for k in sorted(agg["pak"]):
                vals = agg["pak"][k]
                if len(vals) >= 2:
                    print(f"  [{tag}] {ds}: {len(vals)} 轮均值 pass@{k}="
                          f"{statistics.fmean(vals):.4f}")


def write_scores_md(rows: list) -> None:
    """追加写 results/accuracy/scores.md（带时间戳头的 markdown 汇总表）

    表头按本批结果里出现过的最大 k 展开 pass@1..pass@k 列。
    """
    max_k = 0
    for r in rows:
        for k in (r.get("pass_at_k") or {}):
            max_k = max(max_k, int(k))
    header = "| tag | 轮 | 数据集 | mean | n |"
    sep = "|---|---|---|---|---|"
    for k in range(1, max_k + 1):
        header += f" pass@{k} |"
        sep += "---|"
    lines = [
        f"## 精度汇总 {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        header,
        sep,
    ]
    for r in rows:
        score_s = "-" if r["score"] is None else r["score"]
        num_s = "-" if r["num"] is None else r["num"]
        line = f"| {r['tag']} | {r['rnd']} | {r['ds']} | {score_s} | {num_s} |"
        pak = r.get("pass_at_k") or {}
        for k in range(1, max_k + 1):
            v = pak.get(k)
            line += f" {v if v is not None else '-'} |"
        lines.append(line)
    with open(SCORES_MD, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")
    print(f"\nscores.md 已追加: {SCORES_MD}")


def cmd_report(cfg: dict) -> int:
    """--report: 不跑测试, 扫 results/accuracy/*_r*/reports/<model>/*.json 重打汇总表"""
    model = (cfg.get("service") or {}).get("model") or ""
    if not model:
        print("config 缺少 service.model, 无法定位结果目录")
        return 1
    work_dirs = sorted(d for d in glob.glob(os.path.join(ACC_DIR, "*_r*"))
                       if os.path.isdir(d))
    rows = []
    found = False
    for wd in work_dirs:
        tag_r = os.path.basename(wd)
        tag, sep, rnd_s = tag_r.rpartition("_r")
        if not sep:
            tag, rnd_s = tag_r, "-"
        rep_dir = os.path.join(wd, "reports", model)
        dss = sorted(os.path.splitext(os.path.basename(p))[0]
                     for p in glob.glob(os.path.join(rep_dir, "*.json")))
        if not dss:
            continue
        found = True
        print(f"\n[{tag_r}]  来源: {rep_dir}")
        for ds, v in parse_report(wd, model, dss).items():
            if v["score"] is None:
                print(f"  {ds}: 结果缺失或解析失败 ({v['path']})")
            else:
                print(f"  {ds}: score={v['score']} (n={v['num']})")
                pak = v.get("pass_at_k") or {}
                if pak:
                    print(f"  {ds}: {format_pass_at_k(pak)}")
            rows.append({"tag": tag, "rnd": rnd_s, "ds": ds,
                         "score": v["score"], "num": v["num"],
                         "pass_at_k": v.get("pass_at_k") or {}})
    if not found:
        print(f"results/accuracy 下还没有精度结果（results/accuracy/*_r*/reports/{model}/ "
              f"无 *.json）, 先运行: bash run.sh --accuracy")
        return 0
    print(f"\n{'=' * 70}")
    print_round_means(rows)
    print(f"明细: results/accuracy/<tag>_r<N>/reports/{model}/<dataset>.json")
    return 0


def main():
    ap = argparse.ArgumentParser(description="evalscope 精度测试 runner（数据集组 x 轮）")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印命令不执行")
    ap.add_argument("--list", action="store_true", help="列出启用精度数据集组后退出")
    ap.add_argument("--only", default=None,
                    help="只跑指定数据集组 tag（逗号分隔多个），如 gsm8k,aime")
    ap.add_argument("--report", action="store_true",
                    help="不跑测试, 只扫 results/accuracy/ 已有结果重打汇总表")
    ap.add_argument("--limit", type=int, default=None,
                    help="每个子集只评 N 条（冒烟验证链路用; 不填=全量）")
    ap.add_argument("--k", type=int, default=None,
                    help="覆盖 pass@k 的采样次数 k（覆盖 config 里的 k; k=1 为单次 mean 口径）")
    ap.add_argument("--resume", action="store_true",
                    help="断点续跑: 复用 work-dir 已有 predictions/reviews, 只补缺样本并重出报告"
                         "（崩溃恢复用, 例: 上次死在报告聚合阶段）")
    # 兼容位: run.sh 用 -f/--foreground 决定前台/nohup 分支后原样透传, 此处仅接收不生效
    ap.add_argument("-f", "--foreground", action="store_true",
                    help="前台运行（实际由 run.sh 处理, runner 侧仅为兼容接收）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.report:
        return cmd_report(cfg)

    groups = resolve_groups(cfg)
    if not groups:
        print("没有启用的精度数据集组（config accuracy.datasets 缺失或为空）")
        return 0

    if args.k is not None:
        if args.k < 1:
            print(f"--k 必须 >= 1: {args.k}", file=sys.stderr)
            return 2
        for g in groups:
            g["k"] = args.k

    if args.list:
        for g in groups:
            mode = "pass@k" if g["k"] > 1 else "mean"
            print(f"{g['tag']:12s}  datasets={' '.join(g['datasets']):24s} rounds={g['rounds']}  "
                  f"effort={g['effort']:6s} max_tokens={g['max_tokens']}  batch={g['batch_size']}  "
                  f"k={g['k']} ({mode})")
        print(f"\n共 {len(groups)} 个启用精度数据集组")
        return 0

    if args.only:
        wanted = {w.strip() for w in args.only.split(",") if w.strip()}
        unknown = wanted - {g["tag"] for g in groups}
        if unknown:
            print(f"未知数据集组 tag: {sorted(unknown)}\n"
                  f"可用 tag (--list 查看): {[g['tag'] for g in groups]}", file=sys.stderr)
            return 2
        groups = [g for g in groups if g["tag"] in wanted]

    for key in ("url", "model"):
        if not (cfg.get("service") or {}).get(key):
            raise ValueError(f"config 缺少 service.{key}（精度测试必填）")

    evalscope_bin = resolve_evalscope_bin(cfg)
    env = build_subprocess_env(cfg)

    if not args.dry_run:
        os.makedirs(ACC_DIR, exist_ok=True)

    print(f"目标服务: {cfg['service']['url']}  模型: {cfg['service']['model']}")
    print(f"启用精度数据集组: {len(groups)} 个  evalscope: {evalscope_bin}")
    for g in groups:
        if g["k"] > 1:
            print(f"  [{g['tag']}] pass@k 模式: repeats={g['k']} "
                  f"(请求量 x{g['k']}, 输出 mean + pass@1..pass@{g['k']})")
    if env:
        print(f"子进程代理: http_proxy={env['http_proxy']}  no_proxy={env['no_proxy']}")

    t_start = time.time()
    failed = []
    no_score = []
    rows = []
    total = sum(g["rounds"] for g in groups)
    done = 0
    for g in groups:
        for rnd in range(1, g["rounds"] + 1):
            done += 1
            print(f"\n>>> 进度 {done}/{total}")
            rc, scores = run_group_round(cfg, g, rnd, evalscope_bin, env, args.dry_run,
                                         args.limit, args.resume)
            round_rows = []
            if scores:
                for ds, v in scores.items():
                    round_rows.append({"tag": tag_key(g, args.limit), "rnd": rnd, "ds": ds,
                                       "score": v["score"], "num": v["num"],
                                       "pass_at_k": v.get("pass_at_k") or {}})
            rows.extend(round_rows)
            # 每轮跑完立即落盘: 中途被打断也保留已完成轮次的结果
            if not args.dry_run and round_rows:
                write_scores_md(round_rows)
            if rc != 0:
                failed.append(f"{tag_key(g, args.limit)}_r{rnd}")
            elif not any(r["score"] is not None for r in round_rows):
                # 退出码 0 但没拿到分数 = 假通过（报告缺失/结构变了/指标为空）
                no_score.append(f"{tag_key(g, args.limit)}_r{rnd}")

    print(f"\n{'=' * 70}")
    print(f"全部完成，总耗时 {(time.time() - t_start) / 60:.1f} 分钟")
    if not args.dry_run:
        if rows:
            print_round_means(rows)
        else:
            print("没有可解析的分数结果")
    if failed:
        print(f"失败组 ({len(failed)}): {failed}")
        print("排查: results/accuracy/<tag>_r<N>/ 下 evalscope 日志与 reports/")
        return 1
    if no_score:
        print(f"无分数组 ({len(no_score)}): {no_score}")
        print("这些组 evalscope 退出码为 0 但没解析到分数, 按假通过处理（退出码 1）")
        print("排查: results/accuracy/<tag>_r<N>/reports/ 下是否有报告 json")
        return 1
    print("所有精度数据集组完成。汇总表: python3 accuracy_runner.py --report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
