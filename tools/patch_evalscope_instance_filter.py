#!/usr/bin/env python3
"""Add an env-var driven INSTANCE FILTER to evalscope's dataset loader.

Why: batching gives contiguous windows only.  Re-running the instances that
failed for environment reasons (cgroup OOM, lost relay, ...) or the ones a
killed batch never reached needs an instance-level selection:

    EVALSCOPE_INSTANCE_FILE=/path/to/ids.txt evalscope eval ... --limit N

The file holds one instance_id per line; rows whose instance_id is not
listed are dropped.  Inert unless the env var is set and the file exists.

Patches the same three limit-blocks the offset patch touched
(patch_evalscope_dataset_offset.py must already be applied).

Usage: python3 patch_evalscope_instance_filter.py <path-to-loader.py>
"""
import sys

MARKER = '# evalscope-instance-filter'

HF_ANCHOR = """        # # evalscope-batched-eval-offset: skip the first N samples (batched evaluation)
        _offset = int(os.environ.get('EVALSCOPE_DATASET_OFFSET', '0') or 0)
        if _offset:
            dataset = dataset.select(range(_offset, len(dataset)))
"""

HF_FILTER = """
        # %s: keep only the listed instances (re-run of env-failed ones)
        _instance_file = os.environ.get('EVALSCOPE_INSTANCE_FILE')
        if _instance_file and os.path.exists(_instance_file):
            _wanted = {line.strip() for line in open(_instance_file) if line.strip()}
            dataset = dataset.filter(
                lambda row, _w=_wanted: row['instance_id'] in _w)
""" % MARKER

LIST_ANCHOR = """        # # evalscope-batched-eval-offset: skip the first N samples (batched evaluation)
        _offset = int(os.environ.get('EVALSCOPE_DATASET_OFFSET', '0') or 0)
        if _offset:
            dataset = dataset[_offset:]
"""

LIST_FILTER = """
        # %s: keep only the listed instances (re-run of env-failed ones)
        _instance_file = os.environ.get('EVALSCOPE_INSTANCE_FILE')
        if _instance_file and os.path.exists(_instance_file):
            _wanted = {line.strip() for line in open(_instance_file) if line.strip()}
            dataset = [row for row in dataset if row.get('instance_id') in _wanted]
""" % MARKER


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    src = open(path, encoding='utf-8').read()

    if MARKER in src:
        print('already patched:', path)
        return 0

    if HF_ANCHOR not in src:
        print('ERROR: HF offset block not found (apply the offset patch first)')
        return 1
    src = src.replace(HF_ANCHOR, HF_ANCHOR + HF_FILTER, 1)

    n = src.count(LIST_ANCHOR)
    if n == 0:
        print('ERROR: list offset blocks not found')
        return 1
    src = src.replace(LIST_ANCHOR, LIST_ANCHOR + LIST_FILTER)

    open(path, 'w', encoding='utf-8').write(src)
    print(f'patched: {path} (list blocks: {n})')
    return 0


if __name__ == '__main__':
    sys.exit(main())
