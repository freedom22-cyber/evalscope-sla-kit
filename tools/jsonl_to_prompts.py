#!/usr/bin/env python3
"""Convert an aisbench-generated GSM8K jsonl dataset to plain-text prompts for evalscope.

evalscope's `custom` dataset plugin reads the file line by line and sends each
line as the prompt verbatim. The aisbench generator writes {"question": ..., "answer": ...}
JSON lines, which must be converted to one plain-text prompt per line first.

Usage:
    python3 jsonl_to_prompts.py <dataset.jsonl> [-o <output.prompts.txt>]
Default output: <dataset>.prompts.txt next to the input.
"""
import argparse
import json
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("dataset", help="aisbench-generated .jsonl dataset")
    ap.add_argument("-o", "--output", default=None,
                    help="output .prompts.txt path (default: <dataset>.prompts.txt)")
    args = ap.parse_args()

    out = args.output or (args.dataset.rsplit(".", 1)[0] + ".prompts.txt")
    n_in = n_out = n_bad = 0
    with open(args.dataset, encoding="utf-8") as fin, open(out, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            n_in += 1
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                if line.lstrip().startswith("{"):
                    # broken JSON record -> sending the fragment as a prompt
                    # would silently corrupt the benchmark. Skip and report.
                    n_bad += 1
                    continue
                prompt = line  # already plain text -> pass through
            else:
                if isinstance(item, dict):
                    prompt = item.get("question") or item.get("prompt") or ""
                else:
                    prompt = str(item)
            # evalscope reads line-by-line: embedded newlines would split one
            # prompt into two bogus prompts. Replace them, keep other whitespace.
            prompt = str(prompt).replace("\r", " ").replace("\n", " ").strip()
            if prompt:
                fout.write(prompt + "\n")
                n_out += 1

    print(f"read {n_in} lines -> wrote {n_out} prompts -> {out}")
    if n_bad:
        print(f"WARNING: skipped {n_bad} malformed JSON lines", file=sys.stderr)
    if n_out == 0:
        print("ERROR: no prompts written", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
