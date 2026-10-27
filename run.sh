#!/bin/bash
# ============================================================================
# 摸高/精度测试入口
# 用法:
#   bash run.sh                     # 后台顺序跑 config.yaml 所有启用场景
#   bash run.sh -f                  # 前台跑（调试用，看实时输出）
#   bash run.sh --dry-run           # 只打印命令不执行
#   bash run.sh --list              # 列出启用场景 ID
#   bash run.sh --gen-datasets      # 批量预生成所有启用 gsm8k 场景的数据集
#   bash run.sh --only <id[,id2]>   # 只跑指定场景
#   bash run.sh --report [--npu 16] # 汇总已有摸高结果
#   bash run.sh --accuracy          # 后台跑精度（accuracy_runner.py, 日志 acc_*.log）
#   bash run.sh --accuracy -f       # 前台跑精度
#   bash run.sh --functional        # 后台跑功能检查（scripts/run_api_e2e.py, 日志 func_*.log）
#   bash run.sh --functional -f     # 前台跑功能检查
# ============================================================================
set -u
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

# 精度/功能分发: 与默认摸高走同一套 前台/nohup 分支, 仅换 runner 与日志前缀（防混）
RUNNER="runner.py"
PREFIX="run"
case "${1:-}" in
  --report)     shift; exec "$PY" report.py "$@" ;;
  --accuracy)   shift; RUNNER="accuracy_runner.py"; PREFIX="acc" ;;
  --functional) shift; RUNNER="scripts/run_api_e2e.py"; PREFIX="func" ;;
  *)            ;;
esac

TS=$(date +%Y%m%d_%H%M%S)
# 日志目录: LOG_DIR 环境变量可改（默认 kit 内 logs/）。日志走本地盘/共享盘时用:
#   LOG_DIR=/path/to/logs bash run.sh --accuracy
LOG_DIR="${LOG_DIR:-$PWD/logs}"
LOG="${LOG_DIR}/${PREFIX}_${TS}.log"
mkdir -p "$LOG_DIR"

if [ "${1:-}" = "-f" ] || [ "${1:-}" = "--foreground" ] || [ "${1:-}" = "--dry-run" ] || [ "${1:-}" = "--gen-datasets" ] || [ "${1:-}" = "--list" ]; then
  # 注意: exec 在管道里不会终止脚本（只替换管道子进程）, 前台跑完必须显式 exit,
  # 否则会继续落到下方 nohup 分支把整套测试再后台跑一遍
  "$PY" "$RUNNER" "$@" 2>&1 | tee "$LOG"
  exit "${PIPESTATUS[0]}"
fi

echo "后台启动摸高测试，日志: $LOG"
echo "  跟踪进度: tail -f $LOG"
echo "  中途汇总: python3 report.py"
nohup "$PY" "$RUNNER" "$@" > "$LOG" 2>&1 &
echo "PID: $!"
