#!/usr/bin/env python3
"""Patch evalscope's swe_bench_pro adapter so the gold test patch reaches the container.

Why: the adapter takes the dataset's `before_repo_set_cmd`, whose last line is

    git apply --verbose /tests/test_patch.patch

but nothing ever puts `test_patch` into /tests inside the sandbox container.
The run therefore applies no test patch, the gold fail_to_pass tests do not
exist, `output.json` comes back with an empty test list and every instance
scores 0 regardless of the model's patch.

The patch makes the adapter
  1. write the dataset's `test_patch` into the eval workspace as
     `test_patch.patch` (the workspace is bind-mounted at /workspace), and
  2. copy it to /tests inside the container before `before_repo_set_cmd` runs.

Usage: python3 patch_swe_bench_pro_tests.py <path-to-evalscope-utils.py>
"""
import re
import sys

MARKER = '# swe-bench-pro-testpatch'


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    path = sys.argv[1]
    src = open(path, encoding='utf-8').read()

    if MARKER in src:
        print('already patched:', path)
        return 0

    # 1) ship the gold test patch into the sandbox workspace
    old_files = """    files = {
        'patch.diff': cleaned_patch,
        'run_script.sh': resources['run_script'],
        'parser.py': resources['parser_script'],
        'entryscript.sh': build_entry_script(metadata, resources),
    }"""
    new_files = """    files = {
        'patch.diff': cleaned_patch,
        'run_script.sh': resources['run_script'],
        'parser.py': resources['parser_script'],
        'entryscript.sh': build_entry_script(metadata, resources),
        # %s: the gold test patch, applied inside the container by
        # before_repo_set_cmd ("git apply /tests/test_patch.patch")
        'test_patch.patch': metadata.get('test_patch') or '',
    }""" % MARKER
    if old_files not in src:
        print('ERROR: eval_instance file-map block not found; patch not applied')
        return 1
    src = src.replace(old_files, new_files, 1)

    # 2) put it at /tests before before_repo_set_cmd runs
    old_run = """git apply -v /workspace/patch.diff
{before_repo_set_cmd}"""
    new_run = """git apply -v /workspace/patch.diff
# %s: place the gold test patch where before_repo_set_cmd expects it
mkdir -p /tests && cp /workspace/test_patch.patch /tests/test_patch.patch
{before_repo_set_cmd}""" % MARKER
    if old_run not in src:
        print('ERROR: entryscript template block not found; patch not applied')
        return 1
    src = src.replace(old_run, new_run, 1)

    open(path, 'w', encoding='utf-8').write(src)
    print('patched:', path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
