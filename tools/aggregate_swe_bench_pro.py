#!/usr/bin/env python3
"""Aggregate SWE-bench_Pro review files into one accuracy summary.

Reads every review jsonl under <work-dir>/reviews/<model>/ and reports
resolved/total plus a per-repo breakdown.

Usage: aggregate_swe_bench_pro.py <work-dir> [model]
"""
import json
import os
import sys
from collections import defaultdict


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    work = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else None

    reviews_root = os.path.join(work, 'reviews')
    if not os.path.isdir(reviews_root):
        print(f'no reviews dir: {reviews_root}')
        return 1

    model_dirs = [os.path.join(reviews_root, model)] if model else [
        os.path.join(reviews_root, d) for d in sorted(os.listdir(reviews_root))
    ]

    rows = []
    for md in model_dirs:
        if not os.path.isdir(md):
            continue
        for fn in sorted(os.listdir(md)):
            if not fn.endswith(('.jsonl', '.jsonl.rerun')) and '.jsonl' not in fn:
                continue
            path = os.path.join(md, fn)
            with open(path, encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append((fn, json.loads(line)))
                    except json.JSONDecodeError:
                        continue

    # de-duplicate by instance_id, keep the last occurrence
    by_inst = {}
    for fn, d in rows:
        md = (d.get('sample_score') or {}).get('sample_metadata') or d.get('sample_metadata') or {}
        iid = md.get('instance_id') or f"idx{d.get('sample_id')}"
        by_inst[iid] = (fn, d)

    per_repo = defaultdict(lambda: [0, 0])
    resolved = scored = 0
    unresolved = []
    for iid, (fn, d) in sorted(by_inst.items()):
        sc = d.get('sample_score') or {}
        md = sc.get('sample_metadata') or d.get('sample_metadata') or {}
        repo = md.get('repo') or 'unknown'
        # score payload: sample_score.score.{value,metadata}
        inner = sc.get('score') or sc
        val = inner.get('value') or {}
        meta = inner.get('metadata') or {}
        acc = val.get('acc')
        ok = meta.get('resolved')
        per_repo[repo][1] += 1
        if acc is not None:
            scored += 1
            per_repo[repo][0] += float(acc)
            if ok:
                resolved += 1
            else:
                rep = meta.get('report') or {}
                unresolved.append((iid, repo, rep.get('passed_count'),
                                   rep.get('required_count'),
                                   len(rep.get('tests') or [])))

    total = len(by_inst)
    print(f'work-dir: {work}')
    print(f'instances scored: {scored}/{total}   resolved: {resolved}'
          f'   acc: {resolved / scored if scored else 0:.4f}')
    print()
    print(f'{"repo":22s} {"resolved":>9s} {"total":>6s} {"acc":>8s}')
    for repo, (r, t) in sorted(per_repo.items(), key=lambda kv: -kv[1][1]):
        print(f'{repo:22s} {int(r):9d} {t:6d} {r / t:8.4f}')
    if unresolved:
        print()
        print('unresolved (instance, repo, passed, required, tests_ran):')
        for row in unresolved[:40]:
            print('  ', row)
        if len(unresolved) > 40:
            print(f'   ... and {len(unresolved) - 40} more')
    return 0


if __name__ == '__main__':
    sys.exit(main())
