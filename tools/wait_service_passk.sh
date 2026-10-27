#!/bin/bash
# ============================================================================
# 等待 ModelArts 服务恢复健康后自动启动 pass@k 测试
# 背景: all5_r1 跑完后服务 STOPPED (ModelArts.4203), 09:33 首次 pass@k 启动失败
#
# 用法 (detached 启动):
#   setsid nohup bash tools/wait_service_passk.sh > logs/acc_passk_queue.log 2>&1 &
#
# 健康判定 (连续 2 次, 间隔 30s, 防启动中途误触发):
#   1) GET  /v1/models 返回 200
#   2) POST 极小流式对话返回 200 (流式避开网关 60s 首字节超时)
#
# 可调环境变量:
#   POLL_SECS     轮询间隔 (默认 120s)
#   MAX_WAIT_SECS 最长等待 (默认 259200s = 3 天, 超时放弃)
#   K             pass@k 的 k (默认 4, 与 config accuracy.datasets 的 k 一致)
#   TAG           config 里的数据集组 tag (默认 all5)
#   CONFIG        配置文件 (默认 kit 根目录的 config.yaml)
#
# 注意:
#   - 服务地址/模型从 config.yaml 的 service 段读, 不写死（换目标不用改脚本）
#   - curl 一律 --noproxy '*': 本机的 http_proxy(192.168.16.146:3128) 连不通,
#     不绕过代理的话健康探测永远拿不到 200, 守护会空等 3 天
#   - 锁目录防双跑（两个守护同时起来会并发写同一个 work-dir）
# ============================================================================
set -u
cd "$(dirname "$0")/.."   # kit 根目录

CONFIG="${CONFIG:-$PWD/config.yaml}"
POLL_SECS="${POLL_SECS:-120}"
MAX_WAIT_SECS="${MAX_WAIT_SECS:-259200}"
PY_BIN="${PYTHON:-python3}"
K="${K:-4}"                 # pass@k 的 k (与 config accuracy.datasets 的 k 一致)
TAG="${TAG:-all5}"         # config 里的数据集组 tag
LOCK_DIR="${LOCK_DIR:-$PWD/.passk_wait.lock}"

# ---- 从 config.yaml 取服务地址与模型 ----------------------------------------
eval "$("$PY_BIN" - "$CONFIG" <<'PYEOF'
import re, shlex, sys, yaml
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
svc = cfg.get("service") or {}
url = svc.get("url") or ""
model = svc.get("model") or ""
# base = chat completions 完整路径去掉尾部（版本段一起去掉）
base = re.sub(r"/(?:v[0-9a-z]+)?/?(?:chat/)?completions$", "", url).rstrip("/")
print(f"BASE_URL={shlex.quote(base)}")
print(f"MODEL_NAME={shlex.quote(model)}")
PYEOF
)"

if [ -z "${BASE_URL:-}" ]; then
  echo "无法从 $CONFIG 读到 service.url, 退出"
  exit 2
fi

WORK_DIR="$PWD/results/accuracy/${TAG}_pass@k${K}_r1"

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

# ---- 防双跑: 锁目录 + 已有精度进程 -------------------------------------------
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  log "已有等待守护在跑 (锁目录 $LOCK_DIR 存在), 本守护退出"
  exit 0
fi
cleanup() { rmdir "$LOCK_DIR" 2>/dev/null || true; }
trap cleanup EXIT

if pgrep -f "accuracy_runner.py" > /dev/null 2>&1; then
  log "检测到已有精度进程在跑, 本守护退出(不重复启动)"
  exit 0
fi

log "等待服务恢复: $BASE_URL (每 ${POLL_SECS}s 检查, 连续 2 次通过才启动, 最长等 $((MAX_WAIT_SECS/3600))h)"
waited=0
ok_count=0
while [ "$waited" -lt "$MAX_WAIT_SECS" ]; do
  if healthy; then
    ok_count=$((ok_count + 1))
    log "健康检查通过 ($ok_count/2)"
    [ "$ok_count" -ge 2 ] && break
    sleep 30; waited=$((waited + 30))
  else
    ok_count=0
    sleep "$POLL_SECS"; waited=$((waited + POLL_SECS))
  fi
done

if [ "$ok_count" -lt 2 ]; then
  log "等待超时(${MAX_WAIT_SECS}s), 放弃自动启动; 服务恢复后可手动重跑本脚本"
  exit 1
fi

# 清理上次失败残留(仅 configs/空 predictions, 失败日志仍在 logs/acc_passk_*.log)
if [ -d "$WORK_DIR" ]; then
  FAILED_DIR="${WORK_DIR}_failed_$(date +%m%d_%H%M)"
  mv "$WORK_DIR" "$FAILED_DIR" && log "失败残留已移至 $FAILED_DIR"
fi

mkdir -p logs
TS=$(date +%Y%m%d_%H%M%S)
LOG="logs/acc_passk_${TS}.log"
log "服务已恢复, 启动 pass@k 测试 (repeats=$K, x$K 请求量), 日志: $LOG"
log "  work-dir: $WORK_DIR"

# 精度命令统一由 accuracy_runner.py 生成 (k>1 时自动带 --repeats/--dataset-args)
"$PY_BIN" accuracy_runner.py --only "$TAG" --k "$K" > "$LOG" 2>&1
rc=$?
log "pass@k 测试结束 rc=$rc"
log "  结果: $WORK_DIR/reports/  (metrics 里 aggregation=pass_at_k, dimensions.k=1..$K; mean 也在)"
log "  汇总: python3 accuracy_runner.py --report"
