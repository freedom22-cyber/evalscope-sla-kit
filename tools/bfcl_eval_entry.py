#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BFCL v4 评测入口（在 bfcl-venv 解释器下运行, 由 bfcl_runner.py 调起）。

组装 evalscope TaskConfig(datasets=['bfcl_v4']) 并 run_task()。独立成脚本的
原因: TaskConfig 只能在装了 evalscope + bfcl-eval 的 bfcl-venv 里构造
（该 venv 与 kit 宿主环境隔离, 见 bfcl_runner.py 头注释）。

参数一律显式传入、不读 config.yaml: bfcl_runner 解析配置后转成命令行参数,
保证两侧职责单一、--dry-run 打印的命令可完整复现。

与 evalscope 官方文档的对应关系
（https://evalscope.readthedocs.io/zh-cn/latest/third_party/bfcl_v4.html）:
    TaskConfig(model, api_url, api_key, eval_type='openai_api',
               datasets=['bfcl_v4'], eval_batch_size, dataset_args={'bfcl_v4':
               {'subset_list': [...], 'extra_params': {underscore_to_dot,
               is_fc_model, SERPAPI_API_KEY}}}, generation_config={'temperature'},
               use_cache, limit) -> run_task()
"""
import argparse
import json
import sys


def main():
    ap = argparse.ArgumentParser(description="BFCL v4 评测入口（bfcl-venv 内运行）")
    ap.add_argument("--model", required=True, help="被测模型名")
    ap.add_argument("--api-url", required=True, help="OpenAI 兼容端点（chat completions 完整路径或 base, 适配器会自动剥掉 /chat/completions）")
    ap.add_argument("--api-key", default="EMPTY")
    ap.add_argument("--subsets", required=True, help="逗号分隔的子集列表, 如 simple_python,irrelevance")
    ap.add_argument("--extra-params", default="{}", help="JSON: underscore_to_dot / is_fc_model / SERPAPI_API_KEY")
    ap.add_argument("--batch-size", type=int, default=10, help="评测并发（eval_batch_size）")
    ap.add_argument("--temperature", type=float, default=0.0, help="采样温度（BFCL 只支持 temperature）")
    ap.add_argument("--limit", type=int, default=0, help="每个子集只评 N 条（0=不限）")
    ap.add_argument("--work-dir", required=True, help="结果目录（predictions/reviews/reports 落这里）")
    ap.add_argument("--use-cache", default=None, help="复用旧 predictions 的目录（断点续跑）")
    ap.add_argument("--resume", action="store_true", help="配 use_cache 时放弃缓存身份哈希核对, 复用 predictions 重算判分")
    args = ap.parse_args()

    try:
        from evalscope import TaskConfig, run_task
    except ImportError as e:
        print(f"bfcl-venv 里没有 evalscope: {e}\n"
              f"请先在 ais-bench 容器内安装: {sys.executable} -m pip install evalscope==1.14.1 bfcl-eval==2025.10.27.1",
              file=sys.stderr)
        return 1

    subsets = [s.strip() for s in args.subsets.split(",") if s.strip()]
    if not subsets:
        print("--subsets 不能为空", file=sys.stderr)
        return 2
    try:
        extra_params = json.loads(args.extra_params or "{}")
    except ValueError as e:
        print(f"--extra-params 不是合法 JSON: {e}", file=sys.stderr)
        return 2

    task_cfg = TaskConfig(
        model=args.model,
        model_id=args.model,
        api_url=args.api_url,
        api_key=args.api_key,
        eval_type="openai_api",          # 仅支持 API 模型评测
        datasets=["bfcl_v4"],
        eval_batch_size=args.batch_size,
        dataset_args={"bfcl_v4": {"subset_list": subsets, "extra_params": extra_params}},
        generation_config={"temperature": args.temperature},  # 只支持 temperature, 其他参数会被忽略
        work_dir=args.work_dir,
        no_timestamp=True,               # 同 tag 重跑写同一目录（predictions 可复用）
        use_cache=args.use_cache or None,
        rerun_review=args.resume,        # 命令变过时跳过缓存身份哈希核对（同 accuracy 线 --resume）
        limit=args.limit or None,
        seed=42,
    )
    run_task(task_cfg=task_cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
