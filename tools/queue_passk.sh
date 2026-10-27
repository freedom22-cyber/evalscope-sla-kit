#!/bin/bash
# ============================================================================
# pass@k 排队守护: 等当前精度评测退出后自动启动 pass@k 测试
#
# 用法 (detached 启动, 不随会话退出而终止):
#   setsid nohup bash tools/queue_passk.sh > logs/acc_passk_queue.log 2>&1 &
#
# 可调环境变量:
#   WATCH_PID   额外要等待的进程 PID (可选; 不设=只看"有没有精度进程在跑")
#   GRACE_SECS  旧进程退出后的缓冲秒数, 等它写完 reports (默认 60)
#   K           pass@k 的 k (默认 4, 与 config accuracy.datasets 的 k 一致)
#   TAG         config 里的数据集组 tag (默认 all5)
#
# 注意:
#   - 等待条件是"没有精度/评测进程在跑", 不是写死的 PID: 写死的 PID 一旦失效
#     (进程已退出或 PID 被复用), 守护会立刻放行, 与仍在跑的评测并发写同一个
#     work-dir, 结果互相污染
#   - 锁目录防双跑（两个守护同时起来会并发跑同一份 pass@k）
#
# 日志:
#   - 守护自身输出: logs/acc_passk_queue.log (重定向由启动命令决定)
#   - pass@k 评测输出: logs/acc_passk_<时间戳>.log (启动时生成)
# 结果:
#   results/accuracy/<tag>_pass@k<k>_r1/reports/<model>/<ds>.json
#   (mean + pass@1..pass@k; accuracy_runner.py 已能解析两者, --report 可直接看)
# ============================================================================
set -u
cd "$(dirname "$0")/.."   # kit 根目录

WATCH_PID="${WATCH_PID:-}"
GRACE_SECS="${GRACE_SECS:-60}"
PY_BIN="${PYTHON:-python3}"
K="${K:-4}"                 # pass@k 的 k (与 config accuracy.datasets 的 k 一致)
TAG="${TAG:-all5}"         # config 里的数据集组 tag
WORK_DIR="$PWD/results/accuracy/${TAG}_pass@k${K}_r1"
LOCK_DIR="${LOCK_DIR:-$PWD/.passk_queue.lock}"

log() { echo "[$(date '+%F %T')] $*"; }

# 是否有精度/评测进程在跑（不依赖写死的 PID）
eval_running() {
  pgrep -f "accuracy_runner.py" > /dev/null 2>&1 && return 0
  pgrep -f "evalscope eval" > /dev/null 2>&1 && return 0
  return 1
}

watch_pid_alive() {
  [ -n "$WATCH_PID" ] || return 1
  kill -0 "$WATCH_PID" 2>/dev/null || return 1
  # PID 复用防护: cmdline 仍是 evalscope 才算还在跑
  grep -qa evalscope "/proc/$WATCH_PID/cmdline" 2>/dev/null
}

# ---- 防双跑: 锁目录 ----------------------------------------------------------
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "已有排队守护在跑 (锁目录 $LOCK_DIR 存在), 本守护退出"
  exit 0
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT

if [ -n "$WATCH_PID" ]; then
  log "pass@k 排队守护启动, 等待 PID $WATCH_PID 退出"
  log "  被监控命令: $(tr '\0' ' ' < "/proc/$WATCH_PID/cmdline" 2>/dev/null | cut -c1-120)..."
fi
log "  同时等待: 没有精度/评测进程在跑"

while eval_running || watch_pid_alive; do
  sleep 60
done
log "前序精度评测已退出"
log "缓冲 ${GRACE_SECS}s 等待 reports 落盘..."
sleep "$GRACE_SECS"

mkdir -p logs
TS=$(date +%Y%m%d_%H%M%S)
LOG="logs/acc_passk_${TS}.log"
log "启动 pass@k 测试 (repeats=$K, x$K 请求量), 日志: $LOG"
log "  work-dir: $WORK_DIR"

# 前台执行(守护自身已 detached), 输出重定向到独立日志
# 精度命令统一由 accuracy_runner.py 生成 (k>1 时自动带 --repeats/--dataset-args)
"$PY_BIN" accuracy_runner.py --only "$TAG" --k "$K" > "$LOG" 2>&1
rc=$?
log "pass@k 测试结束 rc=$rc"
log "  结果: $WORK_DIR/reports/  汇总请看各 <ds>.json 的 metrics (aggregation=pass_at_k, dimensions.k=1..$K)"
log "  汇总: python3 accuracy_runner.py --report"
