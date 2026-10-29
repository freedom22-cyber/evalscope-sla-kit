#!/bin/bash
# ============================================================================
# pass@k 监督守护: 服务闪断/进程被杀(OOM)后自动断点续跑, 直到报告全出
#
# 背景(实测): 长精度评测期间服务可能临时 503(与 acs-bench 压测共享同一 ModelArts
# 服务时尤其常见)或本机 OOM 杀进程; evalscope 只快速重试 5 次(~秒级)即放弃退出,
# 深夜没人拉起就白白损失墙钟时间。本守护把"拉起"自动化:
#   健康检查 -> accuracy_runner --resume(复用 predictions, 不丢进度)
#   -> 未完成(退出非 0 或报告缺失) -> 等服务恢复 -> 再续跑
#   -> 全部 reports/<model>/<ds>.json 生成 -> 正常退出
#
# 用法 (detached 启动):
#   setsid nohup bash tools/supervise_passk.sh > logs/acc_supervise.log 2>&1 &
#
# 可调环境变量 (与 wait_service_passk.sh 同一套约定):
#   TAG / K       config 数据集组与 pass@k 的 k (默认 all5 / 4)
#   CONFIG        配置文件 (默认 kit 根目录 config.yaml)
#   POLL_SECS     服务不健康时的轮询间隔 (默认 120s)
#   MAX_RETRY     总尝试次数上限 (默认 50, 防无限循环)
#
# 注意:
#   - 完成判定 = 组内所有数据集的 reports/<model>/<ds>.json 都存在
#     (runner 退出码 0 但报告缺失的"假通过"也会被逮住继续跑)
#   - 锁目录防双跑; 启动时若已有精度进程在跑则退出(避免并发写同一 work-dir)
#   - curl 一律 --noproxy '*': 本机 http_proxy 不通, 不绕开则健康探测永远失败
# ============================================================================
set -u
cd "$(dirname "$0")/.."   # kit 根目录

CONFIG="${CONFIG:-$PWD/config.yaml}"
PY_BIN="${PYTHON:-python3}"
K="${K:-4}"
TAG="${TAG:-all5}"
POLL_SECS="${POLL_SECS:-120}"
MAX_RETRY="${MAX_RETRY:-50}"
LOCK_DIR="${LOCK_DIR:-$PWD/.passk_supervise.lock}"

# ---- 从 config.yaml 取服务地址/模型/该组数据集清单 --------------------------
eval "$("$PY_BIN" - "$CONFIG" "$TAG" <<'PYEOF'
import re, shlex, sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
svc = cfg.get("service") or {}
url = svc.get("url") or ""
model = svc.get("model") or ""
base = re.sub(r"/(?:v[0-9a-z]+)?/?(?:chat/)?completions$", "", url).rstrip("/")
datasets = ""
for line in (cfg.get("accuracy") or {}).get("datasets") or []:
    if line.strip() and line.split(",")[0].strip() == sys.argv[2]:
        datasets = line.split(",")[1].strip()
        break
print(f"BASE_URL={shlex.quote(base)}")
print(f"MODEL_NAME={shlex.quote(model)}")
print(f"DATASETS={shlex.quote(datasets)}")
PYEOF
)"

if [ -z "${BASE_URL:-}" ] || [ -z "${DATASETS:-}" ]; then
  echo "无法从 $CONFIG 读到 service.url 或数据集组 $TAG, 退出"
  exit 2
fi

WORK_DIR="$PWD/results/accuracy/${TAG}_pass@k${K}_r1"
REPORT_DIR="$WORK_DIR/reports/$MODEL_NAME"

log() { echo "[$(date '+%F %T')] $*"; }

healthy() {
  local code
  code=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 20 "$BASE_URL/v1/models" 2>/dev/null)
  [ "$code" = "200" ] || return 1
  code=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 90 -X POST "$BASE_URL/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"${MODEL_NAME}\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"max_tokens\":16,\"stream\":true}" 2>/dev/null)
  [ "$code" = "200" ]
}

all_reports_done() {
  local ds
  for ds in $DATASETS; do
    [ -f "$REPORT_DIR/${ds}.json" ] || return 1
  done
  return 0
}

# ---- 防双跑: 锁目录 + 已有精度进程 ------------------------------------------
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "已有监督守护在跑 (锁目录 $LOCK_DIR 存在), 本守护退出"
  exit 0
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT

if pgrep -f "accuracy_runner.py" > /dev/null 2>&1; then
  log "已有精度进程在跑, 本守护退出(不并发双跑); 需要接管请先结束它"
  exit 0
fi

# 续跑参数: work-dir 有 predictions 就带 --resume(幂等, 已完成的组秒级回放)
RESUME_ARG=""
if [ -d "$WORK_DIR/predictions" ]; then
  RESUME_ARG="--resume"
  log "检测到已有 predictions, 全程以 --resume 断点续跑: $WORK_DIR"
fi

log "监督守护启动: TAG=$TAG K=$K 目标服务=$BASE_URL 模型=$MODEL_NAME"
log "  work-dir: $WORK_DIR  数据集: $DATASETS"
log "  完成判定: $REPORT_DIR/ 下上述数据集 .json 全存在"

attempt=0
while [ "$attempt" -lt "$MAX_RETRY" ]; do
  if all_reports_done; then
    log "全部报告已生成, 监督正常结束"
    log "  结果: $REPORT_DIR/  汇总: python3 accuracy_runner.py --report"
    exit 0
  fi
  attempt=$((attempt + 1))

  if ! healthy; then
    log "(第 $attempt 次) 服务不健康(503/闪断?), ${POLL_SECS}s 后再探"
    sleep "$POLL_SECS"
    continue
  fi

  TS=$(date +%Y%m%d_%H%M%S)
  LOG="logs/acc_passk_${TS}.log"
  log "(第 $attempt 次) 服务健康, 启动/续跑精度评测, 日志: $LOG"
  "$PY_BIN" accuracy_runner.py --only "$TAG" --k "$K" $RESUME_ARG > "$LOG" 2>&1
  rc=$?

  if all_reports_done; then
    log "(第 $attempt 次) 评测退出 rc=$rc 且报告齐全, 监督正常结束"
    log "  结果: $REPORT_DIR/  汇总: python3 accuracy_runner.py --report"
    exit 0
  fi
  log "(第 $attempt 次) 未完成(rc=$rc, 服务闪断或进程被杀), ${POLL_SECS}s 后重试"
  sleep "$POLL_SECS"
done

log "已达最大尝试次数 $MAX_RETRY, 放弃自动续跑; 请人工检查服务与日志"
exit 1
