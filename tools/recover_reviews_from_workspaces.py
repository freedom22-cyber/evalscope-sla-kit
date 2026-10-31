#!/usr/bin/env python3
"""Rebuild SWE-bench_Pro review rows from prediction cache + workspaces.

Why: evalscope's review files are transactional per eval process -- a normal
eval END does os.replace(temp, base) (clobbering the previous batch's rows)
and a CRASHED eval deletes its temp with all rows written so far.  Batch 102
ended via APITimeoutError: its 31 fully-written review rows were discarded.
The per-instance workspaces (output.json) and the append-safe prediction
cache (metadata with fail_to_pass/pass_to_pass) survive, so the score can
be recomputed exactly:

    tests   = output_json['tests']
    passed  = {t.name for t in tests if t.status == 'PASSED'}
    required= f2p | p2p
    resolved= bool(required) and required <= passed
    acc     = float(resolved)
(replicates evalscope/benchmarks/swe_bench_pro/utils.py:eval_instance)

Rows are written to <work>/reviews/<model>/aa_recovered.jsonl -- the 'aa_'
prefix keeps them EARLIER than the base/rerun files in sorted() order, so
the aggregator's keep-last dedupe prefers any future re-run results.

Usage: recover_reviews_from_workspaces.py <work-dir> [model]
"""
import ast
import json
import os
import sys

LOG_SUBDIR = 'swe_bench_pro_log'


def parse_test_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if not isinstance(value, str) or not value.strip():
        return []
    try:
        parsed = ast.literal_eval(value)
    except (ValueError, SyntaxError):
        try:
            parsed = json.loads(value)
        except (ValueError, SyntaxError):
            return []
    return list(parsed) if isinstance(parsed, (list, tuple)) else []


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    work = sys.argv[1]
    model = sys.argv[2] if len(sys.argv) > 2 else 'dsv4'

    # 1. prediction cache -> instance_id -> (index, metadata)
    meta_by_id = {}
    pred_path = os.path.join(work, 'predictions', model,
                             'swe_bench_pro_default.jsonl')
    if os.path.isfile(pred_path):
        with open(pred_path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                md = row.get('metadata') or {}
                iid = md.get('instance_id')
                if iid:
                    meta_by_id[iid] = (row.get('index', 0), md)
    print(f'prediction cache: {len(meta_by_id)} instances')

    # 2. workspaces -> rebuild rows
    log_root = os.path.join(work, LOG_SUBDIR)
    out_rows = []
    no_output = []
    for name in sorted(os.listdir(log_root)):
        if not name.startswith('instance_'):
            continue
        iid = name
        ws = os.path.join(log_root, name, 'workspace')
        out_json_path = os.path.join(ws, 'output.json')
        if iid not in meta_by_id:
            continue
        idx, md = meta_by_id[iid]
        if not os.path.isfile(out_json_path):
            no_output.append(iid)
            continue
        try:
            output_json = json.loads(open(out_json_path, encoding='utf-8').read())
        except (json.JSONDecodeError, OSError):
            no_output.append(iid)
            continue

        f2p = parse_test_list(md.get('fail_to_pass'))
        p2p = parse_test_list(md.get('pass_to_pass'))
        report = {'f2p': f2p, 'p2p': p2p}
        completed = False
        resolved = False
        if isinstance(output_json, dict):
            tests = output_json.get('tests') or []
            passed = {t.get('name') for t in tests if t.get('status') == 'PASSED'}
            required = set(f2p) | set(p2p)
            resolved = bool(required) and required.issubset(passed)
            report['tests'] = tests
            report['passed_count'] = len(passed)
            report['required_count'] = len(required)
            completed = True

        try:
            pred_text = open(os.path.join(ws, 'patch.diff'), encoding='utf-8').read()
        except OSError:
            pred_text = md.get('patch') or ''

        out_rows.append({
            'index': idx,
            'target': None,
            'messages': [],
            'agent_trace': None,
            'sample_score': {
                'score': {
                    'value': {'acc': float(resolved)},
                    'status': 'success',
                    'judge_summary': None,
                    'extracted_prediction': pred_text,
                    'prediction': pred_text,
                    'explanation': None,
                    'metadata': {
                        'completed': completed,
                        'resolved': resolved,
                        'report': report,
                    },
                },
                'sample_id': idx,
                'group_id': idx,
                'generation_index': 0,
                'sample_metadata': {
                    'instance_id': iid,
                    'repo': md.get('repo') or 'unknown',
                },
            },
        })

    out_path = os.path.join(work, 'reviews', model, 'aa_recovered.jsonl')
    with open(out_path, 'w', encoding='utf-8') as fh:
        for row in out_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + '\n')
    print(f'recovered: {len(out_rows)} rows -> {out_path}')
    if no_output:
        print(f'no output.json (need re-run): {len(no_output)}')
        for iid in no_output:
            print(f'  {iid}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
