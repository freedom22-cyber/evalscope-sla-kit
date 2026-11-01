#!/usr/bin/env python3
"""Cap pytest-xdist worker count in SWE-bench_Pro ansible run scripts.

Why: the ansible-test `units` runner invokes `pytest --boxed -r a -n auto`.
`auto` resolves through psutil to the HOST cpu count (320 on this machine --
qemu-user passes it through), so each scoring container tries to spawn
hundreds of xdist workers, each a full qemu process.  The container memory
limit (8g) is blown -> cgroup OOM -> SIGKILL (exit -9) -> no test output ->
score 0 even when the model patch is correct.  Every zero-score ansible
instance so far died this way (dmesg: "Memory cgroup out of memory").

Fix: insert a small sed step right after `cd /app` in every ansible
run_script.sh.  It rewrites the two known cmd constructions to a fixed
worker count of 4 (matches the container cpu_limit):

  new layout:  runner/lib/executor.py          '-n', 'auto'   -> '-n', '4'
  old layout:  lib/ansible_test/_internal/
               units/__init__.py               str(args.num_workers)
                                                 if args.num_workers
                                                 else 'auto'    -> '4'

Also patches the already-staged workspace copies
(<work>/swe_bench_pro_log/<inst>/workspace/run_script.sh) so instances whose
agent phase is still running get the fix before their scoring container is
created (scoring mounts the host-side workspace).

Usage: python3 patch_swe_bench_pro_workers.py <SWE-bench_Pro-os-repo-dir> \
           [eval-work-dir]
"""
import os
import sys

MARKER = '# env-repair: cap xdist workers'

SNIPPET = """
  # %s: `-n auto` under qemu sees 320 host CPUs -> hundreds of xdist workers
  # -> cgroup OOM (exit -9, no results).  Cap workers to 4 (cpu_limit is 4.0).
  grep -rl -- "'auto'" /app/test/lib/ansible_test /app/test/runner 2>/dev/null | while read -r _f; do
    sed -i "s/'-n', 'auto'/'-n', '4'/g; s/str(args.num_workers) if args.num_workers else 'auto'/'4'/g" "$_f" 2>/dev/null || true
  done
""" % MARKER

ANCHOR = '  cd /app\n'
# some ansible templates have no `cd /app` (they rely on the entry script's
# cwd); the snippet only touches absolute paths, so any early spot works
FALLBACK_ANCHOR = 'set -e\n'


def patch_file(path: str) -> str:
    """Insert SNIPPET after every ANCHOR.  Returns 'patched'/'skip'/'noanchor'."""
    with open(path, encoding='utf-8') as fh:
        src = fh.read()
    if MARKER in src:
        return 'skip'
    if ANCHOR in src:
        src = src.replace(ANCHOR, ANCHOR + SNIPPET)
    elif FALLBACK_ANCHOR in src:
        src = src.replace(FALLBACK_ANCHOR, FALLBACK_ANCHOR + SNIPPET, 1)
    else:
        return 'noanchor'
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(src)
    return 'patched'


def iter_scripts(root: str):
    run_scripts = os.path.join(root, 'run_scripts')
    if not os.path.isdir(run_scripts):
        print(f'ERROR: no run_scripts dir under {root}')
        return
    for name in sorted(os.listdir(run_scripts)):
        if not name.startswith('instance_ansible__ansible-'):
            continue
        path = os.path.join(run_scripts, name, 'run_script.sh')
        if os.path.isfile(path):
            yield path


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    repo = sys.argv[1]
    work = sys.argv[2] if len(sys.argv) > 2 else None

    counts = {'patched': 0, 'skip': 0, 'noanchor': 0}
    for path in iter_scripts(repo):
        counts[patch_file(path)] += 1
    print(f"repo run_scripts: {counts['patched']} patched, "
          f"{counts['skip']} already, {counts['noanchor']} without anchor")

    if work:
        log_root = os.path.join(work, 'swe_bench_pro_log')
        staged = {'patched': 0, 'skip': 0, 'noanchor': 0}
        if os.path.isdir(log_root):
            for name in sorted(os.listdir(log_root)):
                path = os.path.join(log_root, name, 'workspace', 'run_script.sh')
                if os.path.isfile(path):
                    staged[patch_file(path)] += 1
        print(f"staged workspaces: {staged['patched']} patched, "
              f"{staged['skip']} already, {staged['noanchor']} without anchor")
    return 0


if __name__ == '__main__':
    sys.exit(main())
