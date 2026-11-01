#!/bin/bash
# repo copy of /mnt/sfs_turbo/c00803992/swe_pro/staged_watch.sh — live run uses the original
# Fast staged-copy hot-patch (0.2s busy loop), COMBINED jest + go patcher.
# The eval process holds UNPATCHED run_scripts in memory for the batch it
# started with; staged copies must be patched between file-creation and the
# scoring bash reading them -- measured window 1-3s.  Only files modified in
# the last 90s are re-read, keeping a full pass under ~0.1s.
# jest cap: --maxWorkers=2 --testTimeout=30000 (element-hq OOM)
# go cap:   GOMAXPROCS=2 (flipt-io & other Go families' compile OOM)
TOOLS_PY=/mnt/sfs_turbo/c00803992/tools
LOG=/mnt/sfs_turbo/c00803992/swe_pro/staged_watch.log
echo "$(date '+%m-%d %H:%M:%S') watcher v6 start (jest+go, double-read + atomic replace, survives batch gaps)" >> "$LOG"
# survive batch boundaries: the eval process vanishes for minutes between
# batches (image prune/prepare) -- only exit after 30min with no eval at
# all (i.e. the whole run finished).  Previously the watcher suicided at
# every boundary and had to be manually relaunched (missed once: batch
# 166 second half ran unprotected).
miss_since=0
while :; do
    if pgrep -f 'evalscope eval' > /dev/null 2>&1; then
        miss_since=0
    else
        now=$(date +%s)
        if [ "$miss_since" -eq 0 ]; then
            miss_since=$now
            echo "$(date '+%m-%d %H:%M:%S') eval process gone, waiting (30min timeout)" >> "$LOG"
        elif [ $((now - miss_since)) -ge 1800 ]; then
            echo "$(date '+%m-%d %H:%M:%S') no eval process for 30min, exiting" >> "$LOG"
            exit 0
        fi
    fi
    python3 - >> "$LOG" 2>&1 <<'PYEOF'
import sys, time, os, glob, tempfile
sys.path.insert(0, '/mnt/sfs_turbo/c00803992/tools')
from patch_swe_bench_pro_jest import patch_text as jest_patch
from patch_swe_bench_pro_go import patch_text as go_patch
now = time.time()
for path in glob.glob('/mnt/sfs_turbo/c00803992/swe_pro/results/'
                      'accuracy/swe_bench_pro_dsv4/swe_bench_pro_log/'
                      'instance_*/workspace/run_script.sh'):
    try:
        if now - os.stat(path).st_mtime > 90:
            continue
    except FileNotFoundError:
        continue
    try:
        # double-read stability check: the stager writes the file in one
        # go, but a read can land mid-write -> a read-modify-write cycle
        # would then truncate the tail (observed 2026-10-31: three flipt
        # run_scripts cut to 52 lines, bash "syntax error near fi").
        data1 = open(path, 'rb').read()
        time.sleep(0.1)
        data2 = open(path, 'rb').read()
        if data1 != data2:
            continue  # still being written -- retry next loop
        src = data2.decode('utf-8', errors='replace')
    except OSError:
        continue
    cur, tags = src, []
    if ('jest' in src or 'vitest' in src or 'yarn test' in src
            or 'npx jest' in src):
        new, k = jest_patch(cur)
        if k:
            cur = new
            tags.append(f'jest({k})')
    new, n = go_patch(cur)
    if n:
        cur = new
        tags.append('go')
    if tags:
        # atomic replace: bash sees either the old or the new full file,
        # never a partially-written one
        tmp = path + '.patchtmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            fh.write(cur)
        os.replace(tmp, path)
        print(f'{time.strftime("%m-%d %H:%M:%S")} patched {"+".join(tags)}: '
              f'{path.rsplit("/",2)[-2]}', flush=True)
PYEOF
    sleep 0.2
done
