#!/bin/bash
# repo copy of /mnt/sfs_turbo/c00803992/swe_pro/run_all_batches.sh — live run uses the original
# ============================================================================
# SWE-bench_Pro 全量分批运行（dsv4 服务）
#
# 数据集实际 642 条；每个实例一个独立 Docker 镜像（jefzda/sweap-images），
# 镜像层不共享（每个 ~2.07GB 展开）→ 全量需 ~1.3TB，必须按窗口分批并在
# 每批结束后 prune 镜像。
#
# 分批切片靠 evalscope dataset loader 的 offset 补丁
# （tools/patch_evalscope_dataset_offset.py，EVALSCOPE_DATASET_OFFSET 环境变量），
# 配合 --limit WINDOW 取 [offset, offset+WINDOW)。
#
# 用法（宿主机）:
#   bash /mnt/sfs_turbo/c00803992/tools/prepare_swe_bench_env.sh
#   nohup bash /mnt/sfs_turbo/c00803992/swe_pro/run_all_batches.sh > \
#       /mnt/sfs_turbo/c00803992/swe_pro/logs/batches.log 2>&1 &
#
# 环境变量: WINDOW(每批实例数, 默认32) CONCURRENCY(批内并发, 默认16)
#            TOTAL(总实例数, 默认642) KEEP_IMAGES(1=不 prune)
# ============================================================================
set -u
WINDOW="${WINDOW:-32}"
CONCURRENCY="${CONCURRENCY:-16}"
TOTAL="${TOTAL:-642}"
START_OFFSET="${START_OFFSET:-0}"
KEEP_IMAGES="${KEEP_IMAGES:-0}"
PULL_THREADS="${PULL_THREADS:-16}"

BASE=/mnt/sfs_turbo/c00803992/swe_pro
LOGDIR="$BASE/logs"
WORK="$BASE/results/accuracy/swe_bench_pro_dsv4"
IMAGES_TRACKED="$LOGDIR/pruned_images.txt"
CONTAINER=ais-bench

mkdir -p "$LOGDIR"
touch "$IMAGES_TRACKED"

log() { echo "[$(date '+%F %T')] $*"; }

log "window=$WINDOW concurrency=$CONCURRENCY total=$TOTAL start_offset=$START_OFFSET"

for ((off=START_OFFSET; off<TOTAL; off+=WINDOW)); do
    n=$WINDOW
    if ((off + n > TOTAL)); then n=$((TOTAL - off)); fi

    log "=== batch offset=$off count=$n ==="

    # 环境自愈：relay / binfmt / 容器内 evalscope 补丁
    if ! bash /mnt/sfs_turbo/c00803992/tools/prepare_swe_bench_env.sh > "$LOGDIR/prepare_$off.log" 2>&1; then
        log "ERROR: prepare failed, see $LOGDIR/prepare_$off.log"
        exit 1
    fi

    # 记录本批开始前已存在的镜像（结束后只 prune 这些 + 本批新增的）
    docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
        | grep '^jefzda/sweap-images:' >> "$IMAGES_TRACKED"
    sort -u "$IMAGES_TRACKED" -o "$IMAGES_TRACKED"

    # 并行预拉镜像：ms_enclave 自己串行拉（~0.25MB/s/张），这里先并发拉好
    docker cp /mnt/sfs_turbo/c00803992/tools/prepull_swe_bench_images.py \
        "$CONTAINER:/tmp/bench-venv/prepull_swe_bench_images.py" > /dev/null
    log "pre-pulling $n images (threads=$PULL_THREADS)"
    if ! docker exec "$CONTAINER" bash -c \
        "/tmp/bench-venv/bin/python /tmp/bench-venv/prepull_swe_bench_images.py $off $n $PULL_THREADS" \
        > "$LOGDIR/prepull_$off.log" 2>&1; then
        log "WARN: pre-pull reported failures (see $LOGDIR/prepull_$off.log)"
    fi
    log "pre-pull done: $(grep -c '^\[.*\] ok' "$LOGDIR/prepull_$off.log" 2>/dev/null || echo 0)/$n ok"

    docker exec -d "$CONTAINER" bash -c \
        "cd $BASE && EVALSCOPE_DATASET_OFFSET=$off bash run_swe_bench_pro.sh --limit $n --eval-batch-size $CONCURRENCY > $LOGDIR/batch_$off.log 2>&1"

    # 等待本批结束（评测进程退出）
    while docker exec "$CONTAINER" pgrep -f "evalscope eval" > /dev/null 2>&1; do
        sleep 60
    done

    # 校验本批真的跑完了（防止秒退被当成完成，把 642 实例空转掉）
    batch_log="$LOGDIR/batch_$off.log"
    size=$(wc -c < "$batch_log" 2>/dev/null || echo 0)
    if [ "$size" -lt 2000 ]; then
        log "ERROR: batch offset=$off log is only $size bytes -- eval did not run"
        tail -20 "$batch_log" 2>/dev/null
        exit 1
    fi
    log "batch offset=$off finished (log ${size}B)"

    if [ "$KEEP_IMAGES" != "1" ]; then
        # 删除所有 sweap 镜像（正在被容器使用的会被 docker 拒绝）
        docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
            | grep '^jefzda/sweap-images:' | sort -u > "$LOGDIR/images_to_prune.txt"
        removed=0
        while read -r img; do
            [ -z "$img" ] && continue
            if docker image rm -f "$img" > /dev/null 2>&1; then
                removed=$((removed + 1))
            fi
        done < "$LOGDIR/images_to_prune.txt"
        log "pruned $removed images; disk: $(df -h /mnt/docker | tail -1 | awk '{print $4}') free"
    fi

    df -h /mnt/docker | tail -1 | awk '{print "  disk: " $4 " free (" $5 " used)"}'
done

log "all batches done"
