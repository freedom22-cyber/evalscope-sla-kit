#!/bin/bash
# repo copy of /mnt/sfs_turbo/c00803992/swe_pro/run_swe_bench_pro.sh — live run uses the original
# ============================================================================
# SWE-bench_Pro 精度评测（dsv4 服务）—— 在 ais-bench 容器 venv 内执行
#
# 参考文档: https://evalscope.readthedocs.io/zh-cn/latest/benchmarks/swe_bench_pro.html
#
# 用法（宿主机; 路径默认从脚本自身位置推导: tools/ dockersock/ swe_bench_pro_os/
# 是 swe_pro/ 的兄弟目录, 环境变量 DOCKER_HOST/REPO_PATH/WORK_DIR/VENV 可覆盖）:
#   bash tools/prepare_swe_bench_env.sh    # 幂等环境准备
#   docker exec -d ais-bench bash -c "cd swe_pro && bash run_swe_bench_pro.sh --limit 2"
#   日志: logs/swe_bench_pro_run1.log
#
# 环境要点（2026-10-30 踩坑记录）:
#  1) 容器内无 /var/run/docker.sock —— 沙箱要按实例拉起 Docker 容器，
#     走宿主机 unix relay: DOCKER_HOST=unix:///mnt/sfs_turbo/c00803992/dockersock/docker.sock
#     （relay 脚本 /mnt/sfs_turbo/c00803992/tools/docker_unix_relay.py）
#     不要用 TCP 代理: docker-py 走 hijack(101) 后直接读裸 socket，
#     TCP 分段会把帧头/帧体拆开，导致 exec 输出丢字节（实测 b''）。
#  2) 宿主机是 aarch64, sweap-images 只有 amd64 —— 走 qemu-x86_64 binfmt 模拟。
#     条目由 tools/ensure_qemu_binfmt.sh 重建（flags 必须 OCF，不能用 POCF：
#     P 会把原 argv[0] 当脚本参数传给目标程序，bash 报 "cannot execute binary file"）。
#  3) SWE-bench_Pro-os 仓库需固定在 commit ca10a60，已克隆到
#     /mnt/sfs_turbo/c00803992/swe_bench_pro_os/repo（extra_params 指定，免重复 clone）。
#  4) 数据集走 ModelScope: ScaleAI/SWE-bench_Pro（HF 直连不通），实际 642 条。
#  5) 每个实例一个独立 Docker 镜像(jefzda/sweap-images)，镜像层不共享，
#     642 实例全量需分批 + prune。
#  6) evalscope 适配器默认不把金标 test_patch 放进容器 /tests，会导致全部 0 分；
#     已由 tools/patch_swe_bench_pro_tests.py 修补（容器重建后需重打）。
# ============================================================================
set -eu
# KIT 根 = 脚本位置上两级（脚本在 scripts/swe/ 或 results/swe_pro/ 都成立）,
# 与 kit_paths.py 的默认布局（.cache/swe-venv, results/swe_pro）对齐
KIT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$(dirname "$0")"
mkdir -p logs results/accuracy

# 机器相关路径全部从环境变量取（由 swe_runner / kit_paths.py 注入）
export DOCKER_HOST="${DOCKER_HOST:-unix:///var/run/docker.sock}"
export PATH="${SWE_VENV:-$KIT/.cache/swe-venv}/bin:$PATH"

API_URL="${API_URL:-}"
MODEL="${MODEL:-}"
REPO_PATH="${REPO_PATH:-$KIT/.cache/swe_bench_pro_os/repo}"
WORK_DIR="${WORK_DIR:-$KIT/results/swe_pro/results/accuracy/swe_bench_pro}"

if [ -z "$API_URL" ] || [ -z "$MODEL" ]; then
  echo "错误: API_URL 和 MODEL 必须由环境变量或 swe_runner 注入" >&2; exit 1
fi

# max_steps: 默认 250（官方默认）。内存/CPU 限制避免 NodeBB 等实例被 OOM kill。
AGENT_CONFIG="${AGENT_CONFIG:-{\"mode\":\"native\",\"strategy\":\"swe_bench_toolcall\",\"max_steps\":250}}"
SANDBOX="${SANDBOX:-{\"enabled\":true,\"engine\":\"docker\",\"default_config\":{\"platform\":\"linux/amd64\",\"memory_limit\":\"8g\",\"cpu_limit\":4.0}}}"

# set -e would abort the script on a crashed eval (e.g. straggler
# APITimeoutError) -- which silently skipped the post-eval snapshot and let
# the evalscope rerun-temp deletion eat the whole batch's review rows
# (batch 134, 2026-10-31).  Capture the rc and keep going instead.
set +e
"${SWE_VENV:-$KIT/.cache/swe-venv}/bin/evalscope" eval \
  --model "$MODEL" --model-id "$MODEL" --eval-type openai_api \
  --api-url "$API_URL" \
  --api-key EMPTY \
  --datasets swe_bench_pro \
  --agent-config "$AGENT_CONFIG" \
  --sandbox "$SANDBOX" \
  --work-dir "$WORK_DIR" --no-timestamp \
  --dataset-args "{
    \"swe_bench_pro\": {
      \"extra_params\": {
        \"swe_bench_pro_repo_path\": \"$REPO_PATH\",
        \"dockerhub_username\": \"jefzda\",
        \"eval_timeout\": 3600
      }
    }
  }" \
  "$@"
EVAL_RC=$?
set -e
SWE_LOGS_DIR="${SWE_LOGS_DIR:-$KIT/results/swe_pro/logs}"
mkdir -p "$SWE_LOGS_DIR" "$SWE_LOGS_DIR/../reviews_archive" || true
echo "[eval] offset=${EVALSCOPE_DATASET_OFFSET:-0} rc=$EVAL_RC ended $(date +%H:%M:%S)" \
  >> "$SWE_LOGS_DIR/eval_rc.log" || true

# ---- post-eval snapshot (run_all_batches re-reads this file fresh per batch).
# evalscope reviews are transactional: a normal eval end os.replace()s the
# rerun temp over the base file (destroying the previous batch's rows); a
# crashed eval deletes the temp instead.  Freeze whatever survived so no
# batch boundary can lose review data.  review_guard.sh additionally
# live-copies the temp file while the eval writes it.
snap_dst="$(dirname "$SWE_LOGS_DIR")/reviews_archive/post_${EVALSCOPE_DATASET_OFFSET:-0}_$(date +%H%M%S)"
mkdir -p "$snap_dst" || true
for f in "$WORK_DIR"/reviews/*/*.jsonl*; do
  [ -f "$f" ] && cat "$f" > "$snap_dst/$(basename "$f")" || true
done
echo "[snapshot] $(date +%H:%M:%S) offset=${EVALSCOPE_DATASET_OFFSET:-0}: $(ls "$snap_dst" 2>/dev/null | wc -l) files, $(cat "$snap_dst"/* 2>/dev/null | wc -l) rows" >> "$SWE_LOGS_DIR/snapshots.log" || true
