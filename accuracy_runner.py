#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalscope 精度测试 runner（迁移自 sunzhi_sla_eval run208.sh cmd_accuracy, 通用化）。

对 OpenAI 兼容推理服务跑 evalscope eval 数据集组（gsm8k / mmlu / gpqa_diamond /
aime25+aime26 …），逐组逐轮顺序执行并解析 accuracy 分数，汇总为 markdown 表。

用法:
    python3 accuracy_runner.py --list             # 列出所有启用精度数据集组
    python3 accuracy_runner.py --dry-run          # 只打印命令与 work-dir，不执行
    python3 accuracy_runner.py --only gsm8k,aime  # 只跑指定组（tag 见 --list）
    python3 accuracy_runner.py --report           # 不跑测试，只扫已有结果重打汇总表
    python3 accuracy_runner.py                    # 顺序执行所有启用组

执行流程:
    1. 解析 config.yaml accuracy.datasets（每组一行 6 字段:
       "tag, evalscope_datasets, rounds, effort, max_tokens, batch_size"）
    2. 构建 evalscope eval 命令（宿主机直跑，不走 docker——setup.sh 已装 evalscope），
       每组每轮独立 work-dir: results/accuracy/<tag>_r<N>（--no-timestamp）
    3. reasoning_effort 走 generation-config 的 extra_body（evalscope -> OpenAI SDK
       顶层展开进请求体，已抓包验证, 见 sunzhi 仓 docs/lessons_api208.md §4）;
       服务端接受 low/high/xhigh/max/none, "medium" 在部分部署上是已知 400 缺陷
    4. accuracy.proxy 非空时给子进程单独导出 http_proxy/https_proxy（评测集首次
       下载走外网），被测服务主机名自动进 no_proxy，不污染父进程环境
    5. 每组每轮跑完即解析 reports/<model>/<ds>.json 的 accuracy 分数并打印;
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


def parse_dataset_line(line: str) -> dict:
    """解析数据集组行: "tag, evalscope_datasets, rounds, effort, max_tokens, batch_size"

    evalscope_datasets 为一个字段内的空格分隔多值（如 "aime25 aime26"）。
    """
    parts = [p.strip() for p in str(line).split(",")]
    if len(parts) != 6:
        raise ValueError(f"数据集组行格式错误（需 6 个逗号分隔字段）: {line!r}")
    tag, dss, rounds, effort, max_tokens, batch = parts
    if not tag or not dss:
        raise ValueError(f"数据集组行 tag 与数据集名不能为空: {line!r}")
    try:
        rounds_i, maxtok_i, batch_i = int(rounds), int(max_tokens), int(batch)
    except ValueError:
        raise ValueError(f"数据集组行 rounds/max_tokens/batch_size 必须是整数: {line!r}")
    if rounds_i < 1:
        raise ValueError(f"数据集组行 rounds 必须 >= 1: {line!r}")
    if maxtok_i < 1 or batch_i < 1:
        raise ValueError(f"数据集组行 max_tokens/batch_size 必须 > 0: {line!r}")
    if not effort:
        raise ValueError(f"数据集组行 effort 不能为空: {line!r}")
    return {
        "tag": tag,
        "datasets": dss.split(),
        "rounds": rounds_i,
        "effort": effort,
        "max_tokens": maxtok_i,
        "batch_size": batch_i,
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
    """evalscope 可执行文件: 环境变量 EVALSCOPE_BIN > config accuracy.evalscope_bin。

    缺失时打告警不阻断（与 sunzhi env.sh 的自检对齐; dry-run / 目标机外预检也能过）。
    """
    bin_path = (os.environ.get("EVALSCOPE_BIN")
                or (cfg.get("accuracy") or {}).get("evalscope_bin")
                or "evalscope")
    if not shutil.which(bin_path):
        print(f"警告: 未找到 evalscope 可执行文件: {bin_path} "
              f"(可用环境变量 EVALSCOPE_BIN 覆盖; 请先在目标机运行 bash setup.sh)",
              file=sys.stderr)
    return bin_path


def work_dir_of(group: dict, rnd: int) -> str:
    """单组单轮 work-dir: results/accuracy/<tag>_r<N>"""
    return os.path.join(ACC_DIR, f"{group['tag']}_r{rnd}")


def build_generation_config(group: dict) -> dict:
    """generation-config: reasoning_effort 经 extra_body 顶层展开进请求体。

    服务端接受 low/high/xhigh/max/none（及整数 1~100）; "medium" 在部分部署上
    是已知 HTTP 400 缺陷（sunzhi 仓 F-03）。
    """
    return {
        "max_tokens": group["max_tokens"],
        "temperature": GEN_TEMPERATURE,
        "top_p": GEN_TOP_P,
        "top_k": GEN_TOP_K,
        "extra_body": {"reasoning_effort": group["effort"]},
        "timeout": GEN_TIMEOUT_MS,
    }


def build_cmd(cfg: dict, group: dict, rnd: int, evalscope_bin: str) -> list:
    """构建单组单轮 evalscope eval 命令（宿主机直跑，不走 docker）"""
    svc = cfg["service"]
    api_key = (cfg.get("request") or {}).get("api_key") or "EMPTY"
    return [
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
        "--work-dir", work_dir_of(group, rnd),
        "--no-timestamp",
    ]


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

    取 metrics 里 identity.name=="accuracy" 的 score/num（照搬 sunzhi run208.sh
    的解析口径; 无该 identity 时退回 metrics[0]）。文件缺失/损坏时该数据集的
    score/num 为 None（绝不抛异常），由调用方打缺失提示。
    返回 {ds: {"score": ..., "num": ..., "path": ...}}
    """
    out = {}
    for ds in datasets:
        path = os.path.join(work_dir, "reports", model, f"{ds}.json")
        score, num = None, None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            mets = data.get("metrics") or []
            acc = next((m for m in mets
                        if (m.get("identity") or {}).get("name") == "accuracy"),
                       mets[0] if mets else {})
            score, num = acc.get("score"), acc.get("num")
        except (OSError, ValueError, AttributeError, TypeError):
            pass  # 文件缺失 / JSON 损坏 / 结构异常: 保持 None
        out[ds] = {"score": score, "num": num, "path": path}
    return out


def print_scores(tag_r: str, scores: dict) -> None:
    """打印单轮分数; 结果文件缺失时给出路径提示"""
    for ds, v in scores.items():
        if v["score"] is None:
            print(f"  [{tag_r}] {ds}: 结果文件缺失 ({v['path']})")
        else:
            print(f"  [{tag_r}] {ds}: score={v['score']} (n={v['num']})")


def run_group_round(cfg: dict, group: dict, rnd: int, evalscope_bin: str,
                    env, dry_run: bool) -> tuple:
    """执行单组单轮; 返回 (returncode, scores)。dry-run 只打印不执行。"""
    tag_r = f"{group['tag']}_r{rnd}"
    work_dir = work_dir_of(group, rnd)
    cmd = build_cmd(cfg, group, rnd, evalscope_bin)
    print(f"\n{'=' * 70}\n[精度 {tag_r}]  {group['raw']}")
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
    """多轮同组打印均值（仅对收集到 >= 2 轮分数的 (tag, 数据集) 打印）"""
    by_key = {}
    for tag, rnd, ds, score, _num in rows:
        if score is None:
            continue
        by_key.setdefault((tag, ds), []).append(float(score))
    for (tag, ds), vals in sorted(by_key.items()):
        if len(vals) >= 2:
            print(f"  [{tag}] {ds}: {len(vals)} 轮均值 score={statistics.fmean(vals):.4f}")


def write_scores_md(rows: list) -> None:
    """追加写 results/accuracy/scores.md（带时间戳头的 markdown 汇总表）"""
    lines = [
        f"## 精度汇总 {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "| tag | 轮 | 数据集 | score | n |",
        "|---|---|---|---|---|",
    ]
    for tag, rnd, ds, score, num in rows:
        score_s = "-" if score is None else score
        num_s = "-" if num is None else num
        lines.append(f"| {tag} | {rnd} | {ds} | {score_s} | {num_s} |")
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
    rows = []  # (tag, rnd, ds, score, num)
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
            rows.append((tag, rnd_s, ds, v["score"], v["num"]))
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

    if args.list:
        for g in groups:
            print(f"{g['tag']:12s}  datasets={' '.join(g['datasets']):24s} rounds={g['rounds']}  "
                  f"effort={g['effort']:6s} max_tokens={g['max_tokens']}  batch={g['batch_size']}")
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
    if env:
        print(f"子进程代理: http_proxy={env['http_proxy']}  no_proxy={env['no_proxy']}")

    t_start = time.time()
    failed = []
    rows = []  # (tag, rnd, ds, score, num)
    total = sum(g["rounds"] for g in groups)
    done = 0
    for g in groups:
        for rnd in range(1, g["rounds"] + 1):
            done += 1
            print(f"\n>>> 进度 {done}/{total}")
            rc, scores = run_group_round(cfg, g, rnd, evalscope_bin, env, args.dry_run)
            if scores:
                for ds, v in scores.items():
                    rows.append((g["tag"], rnd, ds, v["score"], v["num"]))
            if rc != 0:
                failed.append(f"{g['tag']}_r{rnd}")

    print(f"\n{'=' * 70}")
    print(f"全部完成，总耗时 {(time.time() - t_start) / 60:.1f} 分钟")
    if not args.dry_run:
        if rows:
            print_round_means(rows)
            write_scores_md(rows)
        else:
            print("没有可解析的分数结果")
    if failed:
        print(f"失败组 ({len(failed)}): {failed}")
        print("排查: results/accuracy/<tag>_r<N>/ 下 evalscope 日志与 reports/")
        return 1
    print("所有精度数据集组完成。汇总表: python3 accuracy_runner.py --report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
