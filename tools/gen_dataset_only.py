#!/usr/bin/env python3
"""
独立数据集生成脚本 —— 只生成数据集，不跑 benchmark。

用法示例：
  # 普通数据集
  python3 gen_dataset_only.py --tokenizer_path /path/to/model --input_len 8192 --number 160

  # 前缀缓存数据集（前缀重复率90%，1个前缀，dp=1）
  python3 gen_dataset_only.py --tokenizer_path /path/to/model --input_len 8192 --number 160 \
      --prefix_flag 1 --repeat_rate 0.9 --prefix_num 1 --seed 0 --dp 1

  # 多前缀缓存数据集（前缀重复率90%，3个前缀轮转挂载，文件名带 -pfx3）
  python3 gen_dataset_only.py --tokenizer_path /path/to/model --input_len 8192 --number 160 \
      --prefix_flag 1 --repeat_rate 0.9 --prefix_num 3 --seed 0 --dp 1

  # 强制重新生成
  python3 gen_dataset_only.py --tokenizer_path /path/to/model --input_len 8192 --number 160 --force_regenerate
"""

import argparse
import os
import sys
import logging

# 确保能 import 同目录下的模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from generate_dataset import create_multi_prefix_dataset, parse_prefix_ratio

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def main():
    parser = argparse.ArgumentParser(
        description="独立生成 aisbench 数据集（不跑 benchmark）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--tokenizer_path", type=str, required=True,
                        help="模型权重路径（用于加载 tokenizer）")
    parser.add_argument("--input_len", type=int, required=True,
                        help="每条样本的 token 长度")
    parser.add_argument("--number", type=int, required=True,
                        help="样本条数")
    parser.add_argument("--save_path", type=str, default=None,
                        help="数据集保存目录（默认: ../datasets）")
    parser.add_argument("--prefix_flag", type=int, default=0, choices=[0, 1],
                        help="0=普通数据集, 1=前缀缓存数据集（默认 0）")
    parser.add_argument("--dp", type=int, default=1,
                        help="DP 域数量（默认 1）")
    parser.add_argument("--repeat_rate", type=str, default="0",
                        help="前缀重复率，如 0.9 或 90%%（默认 0）")
    parser.add_argument("--seed", type=int, default=1,
                        help="随机种子（默认 1）")
    parser.add_argument("--prefix_num", type=int, default=1,
                        help="前缀种类数（默认 1）")
    parser.add_argument("--force_regenerate", action="store_true", default=False,
                        help="强制重新生成（忽略已有数据集）")

    args = parser.parse_args()

    # 默认保存路径 = 包根目录/datasets
    if args.save_path is None:
        tool_dir = os.path.dirname(os.path.abspath(__file__))
        pkg_root = os.path.dirname(tool_dir)
        args.save_path = os.path.join(pkg_root, "datasets")

    if not os.path.exists(args.save_path):
        os.makedirs(args.save_path)
        logging.info(f"创建保存目录: {args.save_path}")

    repeat_rate = parse_prefix_ratio(args.repeat_rate)

    logging.info("=" * 60)
    logging.info("数据集生成参数：")
    logging.info(f"  tokenizer_path : {args.tokenizer_path}")
    logging.info(f"  input_len      : {args.input_len}")
    logging.info(f"  number         : {args.number}")
    logging.info(f"  save_path      : {args.save_path}")
    logging.info(f"  prefix_flag    : {args.prefix_flag} ({'前缀缓存' if args.prefix_flag else '普通'})")
    logging.info(f"  dp             : {args.dp}")
    logging.info(f"  repeat_rate    : {repeat_rate}")
    logging.info(f"  seed           : {args.seed}")
    logging.info(f"  prefix_num     : {args.prefix_num}")
    logging.info(f"  force_regenerate: {args.force_regenerate}")
    logging.info("=" * 60)

    prefix_path, dataset_path = create_multi_prefix_dataset(
        tokenizer_path=args.tokenizer_path,
        input_len=args.input_len,
        number=args.number,
        save_path=args.save_path,
        prefix_flag=args.prefix_flag,
        dp=args.dp,
        repeat_rate=repeat_rate,
        seed=args.seed,
        prefix_num=args.prefix_num,
        force_regenerate=args.force_regenerate,
    )

    logging.info("=" * 60)
    logging.info("✅ 数据集生成完成！")
    if prefix_path:
        logging.info(f"  前缀文件: {prefix_path}")
    logging.info(f"  数据文件: {dataset_path}")
    logging.info("=" * 60)


if __name__ == "__main__":
    main()
