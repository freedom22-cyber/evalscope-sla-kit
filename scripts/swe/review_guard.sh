#!/bin/bash
# repo copy of /mnt/sfs_turbo/c00803992/swe_pro/review_guard.sh — live run uses the original
# Live-copy the active review rerun temp file while the eval writes it.
# Rationale: evalscope reviews are transactional -- a CRASHED eval calls
# discard_review_reruns() and deletes the temp WITH every row written so far
# (batch 102 lost 31 completed rows this way on 2026-10-30).  Committing
# instead does os.replace(temp, base), clobbering the previous batch.  This
# guard keeps a rolling copy of the temp file, so either ending is survivable.
# It copies only on growth (cheap stat checks, 5s cadence).
# 路径解析与 kit_paths.py 同口径: KIT 根 = 脚本上两级, 默认 kit 相对布局
KIT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="${REVIEW_GUARD_WORK:-$KIT/results/swe_pro/results/accuracy/swe_bench_pro}"
DEST="${REVIEW_GUARD_DEST:-$KIT/results/swe_pro/reviews_archive/live}"
LOG="${REVIEW_GUARD_LOG:-$KIT/results/swe_pro/review_guard.log}"
mkdir -p "$DEST"
declare -A last_size
echo "$(date +%F\ %H:%M:%S) review_guard start" >> "$LOG"
while :; do
    for f in "$WORK"/reviews/*/*.jsonl.rerun-*; do
        [ -f "$f" ] || continue
        sz=$(stat -c %s "$f" 2>/dev/null) || continue
        if [ "${last_size[$f]:-0}" != "$sz" ] && [ "$sz" -gt 0 ]; then
            cat "$f" > "$DEST/$(basename "$f")"
            last_size[$f]=$sz
        fi
    done
    # prune stale entries (eval gone -> file gone)
    sleep 5
done
