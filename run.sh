#!/bin/bash
# ============================================================================
# 摸高测试入口
# 用法:
#   bash run.sh                     # 后台顺序跑 config.yaml 所有启用场景
#   bash run.sh -f                  # 前台跑（调试用，看实时输出）
#   bash run.sh --dry-run           # 只打印命令不执行
#   bash run.sh --list              # 列出启用场景 ID
#   bash run.sh --only <id[,id2]>   # 只跑指定场景
#   bash run.sh --report [--npu 16] # 汇总已有结果
# ============================================================================
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

case "${1:-}" in
  --report) shift; exec "$PY" report.py "$@" ;;
  *)        ;;
esac

TS=$(date +%Y%m%d_%H%M%S)
LOG="logs/run_${TS}.log"
mkdir -p logs

if [ "${1:-}" = "-f" ] || [ "${1:-}" = "--foreground" ] || [ "${1:-}" = "--dry-run" ]; then
  exec "$PY" runner.py "$@" 2>&1 | tee "$LOG"
fi

echo "后台启动摸高测试，日志: $LOG"
echo "  跟踪进度: tail -f $LOG"
echo "  中途汇总: python3 report.py"
nohup "$PY" runner.py "$@" > "$LOG" 2>&1 &
echo "PID: $!"
