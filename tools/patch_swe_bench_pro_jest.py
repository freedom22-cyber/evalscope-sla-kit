#!/usr/bin/env python3
"""Cap jest worker count and per-test timeout in SWE-bench_Pro run scripts.

Why: element-hq's minimal run_script template invokes `npx jest` with no
--maxWorkers.  jest's default worker count is os.cpu_count()-1 and qemu-user
passes the host's 320 CPUs through -> the scoring container tries to spawn
~320 jest workers (each a qemu node process) -> 8g cgroup OOM -> "Killed"
before a single test runs (observed on element-web d58f71ea, 2026-10-30).
The big template already passes --maxWorkers=2.

jest's default testTimeout is 5000 ms; under qemu (~6x slowdown) that turns
1-second native tests into spurious failures, so every jest invocation gets
--testTimeout=30000 as well (same rationale as the shell `timeout` scaling).

Works on the repo run_scripts and (hot) on already-staged workspace copies:
scoring containers bind-mount the host workspace, and `sed`-style rewrite
creates a new inode so a bash that already started reading the old file is
unaffected -- safe while a batch is running.

Usage: python3 patch_swe_bench_pro_jest.py <SWE-bench_Pro-os-repo-dir> \
           [eval-work-dir]
"""
import os
import sys

MARKER = '# env-repair: cap jest workers/timeout for qemu'
FLAGS = '--maxWorkers=2 --testTimeout=30000'


TEST_CMD_TOKENS = ('npx jest ', 'yarn jest ', 'npx vitest ', 'yarn test ')


def patch_text(src: str):
    """Return (patched_src, n_lines_changed)."""
    out = []
    changed = 0
    for line in src.split('\n'):
        new = line
        invokes_test = any(t in line for t in TEST_CMD_TOKENS) \
            or line.strip().endswith('jest') and 'node_modules' not in line
        if invokes_test and 'install' not in line:
            if '--maxWorkers' not in new:
                for token in TEST_CMD_TOKENS:
                    if token in new:
                        new = new.replace(token, token + FLAGS + ' ', 1)
                        break
                else:
                    # bare `jest ...` line: append flags
                    if new.rstrip().endswith('2>&1'):
                        new = new.rstrip()[:-4].rstrip() + ' ' + FLAGS + ' 2>&1'
                    else:
                        new = new.rstrip() + ' ' + FLAGS
            elif '--testTimeout' not in new:
                # capped already (big template) -- still fix the 5s default
                if new.rstrip().endswith('2>&1'):
                    new = new.rstrip()[:-4].rstrip() + ' --testTimeout=30000 2>&1'
                else:
                    new = new.rstrip() + ' --testTimeout=30000'
        if new != line:
            changed += 1
        out.append(new)
    return '\n'.join(out), changed


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    repo = sys.argv[1]
    work = sys.argv[2] if len(sys.argv) > 2 else None

    run_scripts = os.path.join(repo, 'run_scripts')
    total = changed = 0
    for name in sorted(os.listdir(run_scripts)):
        path = os.path.join(run_scripts, name, 'run_script.sh')
        if not os.path.isfile(path):
            continue
        src = open(path, encoding='utf-8').read()
        if 'jest' not in src and 'vitest' not in src \
                and 'yarn test' not in src and 'npx jest' not in src:
            continue
        total += 1
        new, n = patch_text(src)
        if n:
            # atomic replace -- a bash starting up concurrently must never
            # read a truncated write (2026-10-31 flipt syntax-error race)
            tmp = path + '.patchtmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                fh.write(new)
            os.replace(tmp, path)
            changed += 1
    print(f'repo run_scripts: {changed}/{total} jest-using files patched')

    if work:
        log_root = os.path.join(work, 'swe_bench_pro_log')
        staged = fixed = 0
        if os.path.isdir(log_root):
            for name in sorted(os.listdir(log_root)):
                path = os.path.join(log_root, name, 'workspace', 'run_script.sh')
                if not os.path.isfile(path):
                    continue
                src = open(path, encoding='utf-8').read()
                if 'jest' not in src and 'vitest' not in src \
                        and 'yarn test' not in src and 'npx jest' not in src:
                    continue
                staged += 1
                new, n = patch_text(src)
                if n:
                    # atomic replace, same reason as above
                    tmp = path + '.patchtmp'
                    with open(tmp, 'w', encoding='utf-8') as fh:
                        fh.write(new)
                    os.replace(tmp, path)
                    fixed += 1
        print(f'staged workspaces: {fixed}/{staged} jest-using files patched')
    return 0


if __name__ == '__main__':
    sys.exit(main())
