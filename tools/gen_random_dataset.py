#!/usr/bin/env python3
"""
随机定长数据集生成器（复刻 evalscope random 数据集的语料逻辑）。

与 gsm8k 的区别：
  - gsm8k  : 从 GSM8K.jsonl 抽数学题文本，截断/重复到定长
  - random : 从 tokenizer 词表随机抽 token id（排除 special / byte-fallback token），
             解码成文本，再用 re-encode 校正到精确定长——内容是随机 token 序列，
             不是自然语言。

支持前缀命中率：hit_rate>0 时，prefix_num 个不同随机前缀被 N 条样本按
i % prefix_num 复用，命中率 = prefix_len / input_len。

输出与 runner.py 约定的文件名一致，直接落盘到 --save_path：
  hit==0 : RANDOM-in{in_len}-num{n}-{tok}{pN}.jsonl
  hit>0  : RANDOM-in{in_len}-num{n}-{tok}{pN}-repeatRate{rr}.jsonl
           且前缀文件 prefix-RANDOM-in{prefix_len}-num{dp*prefix_num}-{tok}.jsonl

用法：
  python3 gen_random_dataset.py --tokenizer_path /path --input_len 1024 \
      --number 1000 --save_path ../datasets/random
  # 带前缀命中率 90%
  python3 gen_random_dataset.py --tokenizer_path /path --input_len 1024 \
      --number 1000 --save_path ../datasets/random \
      --hit_rate 0.9 --prefix_num 1 --dp 1 --seed 1
"""

import argparse
import json
import logging
import os
import re
import sys

import numpy as np

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def get_allowed_tokens(tokenizer):
    """全词表 token，排除 special token 与 byte-fallback token（<0xHH>）。

    与 evalscope random_dataset.py 的 allowed_tokens 逻辑一致：
    byte-fallback token 解码成裸字节，服务端 re-encode 会按 Latin-1 误读，
    导致 CJK/多字节字符 token 数膨胀 3-5 倍，因此排除。
    """
    byte_pattern = re.compile(r'^<0x[0-9A-Fa-f]{2}>$')
    prohibited = set(tokenizer.all_special_ids)
    try:
        vocab = tokenizer.get_vocab()  # {token_str: id}
        for tok_str, tid in vocab.items():
            if byte_pattern.match(tok_str):
                prohibited.add(tid)
    except Exception:
        for i in range(len(tokenizer)):
            try:
                ts = tokenizer.convert_ids_to_tokens(i)
                if ts and byte_pattern.match(ts):
                    prohibited.add(i)
            except Exception:
                pass
    allowed = [i for i in range(len(tokenizer)) if i not in prohibited]
    return np.array(allowed, dtype=np.int64)


def decode_to_exact_len(tokenizer, token_ids, target_len, allowed_tokens, rng):
    """把 token 序列解码成文本，并校正 re-encode 后恰好 target_len 个 token。

    随机 token 序列 decode→encode 通常不等长（合并/特殊字符），因此循环
    截断或补随机 token 直到稳定。
    """
    text = tokenizer.decode(token_ids, skip_special_tokens=True)
    ids = tokenizer.encode(text, add_special_tokens=False)
    guard = 0
    while len(ids) != target_len and guard < 20:
        guard += 1
        if len(ids) > target_len:
            ids = ids[:target_len]
        else:
            need = target_len - len(ids)
            extra = allowed_tokens[rng.integers(0, len(allowed_tokens), size=need)]
            ids = list(ids) + extra.tolist()
        text = tokenizer.decode(ids, skip_special_tokens=True)
        ids = tokenizer.encode(text, add_special_tokens=False)
    if len(ids) != target_len:
        # 兜底：直接按目标长度截断 token 再解码，不再校正（极少数边界情况）
        if len(ids) > target_len:
            ids = ids[:target_len]
        text = tokenizer.decode(ids, skip_special_tokens=True)
    return text


def gen_tokens(allowed_tokens, length, rng):
    if length <= 0:
        return []
    return allowed_tokens[rng.integers(0, len(allowed_tokens), size=length)].tolist()


def write_data(path, dataset):
    with open(path, "w", encoding="utf-8") as f:
        for q in dataset:
            f.write(json.dumps({"question": q, "answer": "none"}, ensure_ascii=False))
            f.write("\n")


def main():
    ap = argparse.ArgumentParser(description="随机定长数据集生成器（复刻 evalscope random）")
    ap.add_argument("--tokenizer_path", required=True)
    ap.add_argument("--input_len", type=int, required=True)
    ap.add_argument("--number", type=int, required=True)
    ap.add_argument("--save_path", required=True)
    ap.add_argument("--hit_rate", type=float, default=0.0, help="前缀命中率 [0,1]")
    ap.add_argument("--prefix_num", type=int, default=1, help="前缀种类数")
    ap.add_argument("--dp", type=int, default=1)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--force_regenerate", action="store_true")
    args = ap.parse_args()

    os.makedirs(args.save_path, exist_ok=True)
    base_name = os.path.basename(os.path.normpath(args.tokenizer_path))
    pnum_s = f"-pN{args.prefix_num}" if args.prefix_num > 1 else ""

    if args.hit_rate == 0:
        out = os.path.join(args.save_path, f"RANDOM-in{args.input_len}-num{args.number}-{base_name}{pnum_s}.jsonl")
        prefix_out = None
    else:
        rr = args.hit_rate if args.hit_rate < 1 else 1
        out = os.path.join(
            args.save_path,
            f"RANDOM-in{args.input_len}-num{args.number}-{base_name}{pnum_s}-repeatRate{rr}.jsonl")
        prefix_len = int(args.input_len * args.hit_rate)
        prefix_out = os.path.join(
            args.save_path,
            f"prefix-RANDOM-in{prefix_len}-num{args.dp * args.prefix_num}-{base_name}.jsonl")

    if os.path.exists(out) and not args.force_regenerate and (
            prefix_out is None or os.path.exists(prefix_out)):
        logging.info(f"数据集已存在，跳过: {out}")
        return

    logging.info("=" * 60)
    logging.info(f"随机数据集: input_len={args.input_len} number={args.number} "
                 f"hit_rate={args.hit_rate} prefix_num={args.prefix_num} seed={args.seed}")
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_path)
    allowed = get_allowed_tokens(tokenizer)
    logging.info(f"allowed_tokens: {len(allowed)} / {len(tokenizer)}（排除 special/byte-fallback）")

    rng = np.random.default_rng(args.seed)

    if args.hit_rate == 0:
        dataset = []
        for _ in range(args.number):
            toks = gen_tokens(allowed, args.input_len, rng)
            dataset.append(decode_to_exact_len(tokenizer, toks, args.input_len, allowed, rng))
        write_data(out, dataset)
        logging.info(f"✅ 随机数据集: {out}（{len(dataset)} 条）")
        return

    prefix_len = int(args.input_len * args.hit_rate)
    suffix_len = args.input_len - prefix_len
    # 生成 prefix_num 个不同随机前缀
    prefix_texts = []
    for _ in range(args.prefix_num):
        toks = gen_tokens(allowed, prefix_len, rng)
        prefix_texts.append(decode_to_exact_len(tokenizer, toks, prefix_len, allowed, rng))
    # 前缀文件（dp 展开）
    prefix_lines = []
    for i in range(args.prefix_num):
        for _ in range(args.dp):
            prefix_lines.append(prefix_texts[i])
    write_data(prefix_out, prefix_lines)

    # 主数据集: prefix[i % prefix_num] + 随机后缀
    dataset = []
    for i in range(args.number):
        toks = gen_tokens(allowed, suffix_len, rng)
        suffix = decode_to_exact_len(tokenizer, toks, suffix_len, allowed, rng)
        dataset.append(prefix_texts[i % args.prefix_num] + suffix)
    write_data(out, dataset)
    logging.info(f"✅ 随机数据集: {out}（{len(dataset)} 条）")
    logging.info(f"✅ 前缀数据集: {prefix_out}（{len(prefix_lines)} 条）")


if __name__ == "__main__":
    main()
