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
# 用法（宿主机; 路径默认从脚本自身位置推导, 也可 SWE_PRO/TOOLS 环境变量覆盖）:
#   bash tools/prepare_swe_bench_env.sh
#   nohup bash swe_pro/run_all_batches.sh > swe_pro/logs/batches.log 2>&1 &
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

# 路径可移植化: 默认从脚本位置推导（脚本应位于 swe_pro/ 内, tools/ 是其
# 兄弟目录——仓库内 bootstrap 布局与本机 NFS 布局同构）; 环境变量可覆盖。
# SWE_PRO 覆盖到别处而那里没有兄弟 tools/ 时, TOOLS 兜底回脚本自身位置的推导。
BASE="${SWE_PRO:-$(cd "$(dirname "$0")" && pwd)}"
TOOLS="${TOOLS:-$(cd "$BASE/.." && pwd)/tools}"
[ -d "$TOOLS" ] || TOOLS="$(cd "$(dirname "$0")/.." && pwd)/tools"
ROOT="$(cd "$TOOLS/.." && pwd)"     # dockersock/ 所在（relay socket 根）
VENV="${VENV:-/tmp/bench-venv}"
DISK_MOUNT="${DISK_MOUNT:-/mnt/docker}"
LOGDIR="$BASE/logs"
WORK="$BASE/results/accuracy/swe_bench_pro_dsv4"
IMAGES_TRACKED="$LOGDIR/pruned_images.txt"
CONTAINER="${CONTAINER:-ais-bench}"

mkdir -p "$LOGDIR"
touch "$IMAGES_TRACKED"

log() { echo "[$(date '+%F %T')] $*"; }

log "window=$WINDOW concurrency=$CONCURRENCY total=$TOTAL start_offset=$START_OFFSET"

for ((off=START_OFFSET; off<TOTAL; off+=WINDOW)); do
    n=$WINDOW
    if ((off + n > TOTAL)); then n=$((TOTAL - off)); fi

    log "=== batch offset=$off count=$n ==="

    # 环境自愈：relay / binfmt / 容器内 evalscope 补丁
    if ! bash "$TOOLS/prepare_swe_bench_env.sh" > "$LOGDIR/prepare_$off.log" 2>&1; then
        log "ERROR: prepare failed, see $LOGDIR/prepare_$off.log"
        exit 1
    fi

    # 记录本批开始前已存在的镜像（结束后只 prune 这些 + 本批新增的）
    docker images --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
        | grep '^jefzda/sweap-images:' >> "$IMAGES_TRACKED"
    sort -u "$IMAGES_TRACKED" -o "$IMAGES_TRACKED"

    # 并行预拉镜像：ms_enclave 自己串行拉（~0.25MB/s/张），这里先并发拉好
    docker cp "$TOOLS/prepull_swe_bench_images.py" \
        "$CONTAINER:$VENV/prepull_swe_bench_images.py" > /dev/null
    log "pre-pulling $n images (threads=$PULL_THREADS)"
    if ! docker exec "$CONTAINER" bash -c \
        "DOCKER_HOST=unix://$ROOT/dockersock/docker.sock \
         $VENV/bin/python $VENV/prepull_swe_bench_images.py $off $n $PULL_THREADS" \
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
        log "pruned $removed images; disk: $(df -h "$DISK_MOUNT" | tail -1 | awk '{print $4}') free"
    fi

    df -h "$DISK_MOUNT" | tail -1 | awk '{print "  disk: " $4 " free (" $5 " used)"}'
done

log "all batches done"
