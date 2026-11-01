#!/usr/bin/env python3
"""Cap Go toolchain parallelism in SWE-bench_Pro run scripts.

Why: Go families (flipt-io, ...) invoke bare `go test -v ./...`.  The go
command's default build/test parallelism (-p, -parallel) is GOMAXPROCS, and
qemu-user passes the host's 320 CPUs through -> the scoring container runs
~320 parallel compile/test processes -> 8g cgroup OOM kills `compile`
(observed on flipt-io, 2026-10-31 03:39, batch 166).  Same failure class as
the ansible pytest-xdist and element-hq jest worker storms.

Fix: `export GOMAXPROCS=2` right after `set -e` in every run_script that
invokes go tooling.  sed-style rewrite creates a new inode so a bash that
already started reading the old file is unaffected -- safe while a batch is
running (staged copies must still be patched before their bash starts; use
the staged watcher for that).

Usage: python3 patch_swe_bench_pro_go.py <SWE-bench_Pro-os-repo-dir> \
           [eval-work-dir]
"""
import os
import sys

MARKER = '# env-repair: cap go parallelism for qemu'
GOPROXY_MARKER = '# env-repair: use reachable go module proxy'
EXPORTS = [
    f'export GOMAXPROCS=2  {MARKER}',
    f'export GOPROXY=https://goproxy.cn,direct  {GOPROXY_MARKER}',
]
GO_TOKENS = ('go test', 'go build', 'go vet', 'go run', 'go generate')


def patch_text(src: str):
    """Return (patched_src, n_inserted) -- insert missing go env exports."""
    n = 0
    lines = src.split('\n')
    if not any(t in src for t in GO_TOKENS):
        return src, 0
    out = []
    # find anchor line index (first `set -e`, else shebang, else 0)
    anchor = None
    for i, line in enumerate(lines):
        if line.strip() == 'set -e' or line.startswith('#!'):
            anchor = i
            break
    if anchor is None:
        anchor = -1
    for i, line in enumerate(lines):
        out.append(line)
        if i == anchor:
            for export in EXPORTS:
                if 'GOMAXPROCS' in export and 'GOMAXPROCS' in src:
                    continue
                if 'GOPROXY' in export and 'GOPROXY' in src:
                    continue
                out.append(export)
                n += 1
    return '\n'.join(out), n


def patch_tree(root: str, label: str, workspace_level: bool = False):
    """Patch run_script.sh files.  repo run_scripts layout is
    <root>/<name>/run_script.sh; eval work layout is
    <root>/<instance>/workspace/run_script.sh (set workspace_level)."""
    total = changed = 0
    if not os.path.isdir(root):
        print(f'{label}: no dir {root}')
        return
    for name in sorted(os.listdir(root)):
        rel = [name, 'workspace', 'run_script.sh'] if workspace_level \
            else [name, 'run_script.sh']
        path = os.path.join(root, *rel)
        if not os.path.isfile(path):
            continue
        src = open(path, encoding='utf-8').read()
        if not any(t in src for t in GO_TOKENS):
            continue
        total += 1
        new, n = patch_text(src)
        if n:
            # atomic replace: a bash starting up concurrently reads either
            # the old or the new full file -- never a truncated write
            # (2026-10-31: non-atomic rewrite gave three flipt scripts
            # transient partial content -> "syntax error near fi")
            tmp = path + '.patchtmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                fh.write(new)
            os.replace(tmp, path)
            changed += 1
    print(f'{label}: {changed}/{total} go-using files patched')


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    repo = sys.argv[1]
    work = sys.argv[2] if len(sys.argv) > 2 else None

    patch_tree(os.path.join(repo, 'run_scripts'), 'repo run_scripts')

    if work:
        patch_tree(os.path.join(work, 'swe_bench_pro_log'), 'staged workspaces',
                   workspace_level=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
