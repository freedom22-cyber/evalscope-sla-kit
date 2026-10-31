#!/bin/bash
# ============================================================================
# 启动 Web 控制台（ais-bench 容器内 /tmp/bench-venv）
# ----------------------------------------------------------------------------
# 用法:
#   bash scripts/start_web.sh            # 默认 0.0.0.0:8300
#   bash scripts/start_web.sh --port 8301
#
# 说明:
#   - evalscope 客户端只装在 ais-bench 容器的 /tmp/bench-venv（evalscope 1.14.1），
#     宿主机 /usr/local/bin/evalscope 是指向已不存在目录的坏 wrapper，不要用。
#   - 容器网络模式是 host，控制台监听 0.0.0.0:<port> 即宿主机同端口。
#   - 幂等: 容器已在跑则不重启；控制台已在监听则直接退出。
# ============================================================================
set -u
cd "$(dirname "$0")/.."

CONTAINER="${CONTAINER:-ais-bench}"
VENV="${VENV:-/tmp/bench-venv}"
PORT=8300
HOST=0.0.0.0
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    *) echo "未知参数: $1"; exit 2 ;;
  esac
done

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "启动容器 $CONTAINER ..."
  docker start "$CONTAINER" >/dev/null
  sleep 3
fi

# 宿主机主 IPv4（控制台网络模式=host, 容器内外同端口）
HOST_IP="$(ip -4 addr show scope global 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1 | grep -E '^[0-9]' | head -1)"
[ -z "$HOST_IP" ] && HOST_IP="127.0.0.1"

# dataset_offset 补丁: evalscope 的 custom 数据集插件默认忽略该参数,
# 多轮摸高换起点时会打空数据集。幂等。
docker exec -i "$CONTAINER" "$VENV/bin/python" - <<'PYEOF'
import os
import evalscope
f = os.path.join(os.path.dirname(evalscope.__file__), "perf", "plugin", "datasets", "custom.py")
src = open(f, encoding="utf-8").read()
if "dataset_offset" in src:
    print("[patch] custom.py 已支持 dataset_offset")
else:
    old = '''    def build_messages(self) -> Iterator[List[Dict]]:
        for item in self.dataset_line_by_line(self.query_parameters.dataset_path):
            prompt = item.strip()'''
    new = '''    def build_messages(self) -> Iterator[List[Dict]]:
        offset = self.query_parameters.dataset_offset

        for index, item in enumerate(
            self.dataset_line_by_line(self.query_parameters.dataset_path)
        ):
            if index < offset:
                continue

            prompt = item.strip()'''
    if old not in src:
        print("[patch] 警告: custom.py 原文不匹配（evalscope 版本变了？）")
    else:
        open(f, "w", encoding="utf-8").write(src.replace(old, new, 1))
        print("[patch] 已为 custom.py 打 dataset_offset 补丁")
PYEOF

# SWE web 启停桥接守护（宿主机跑）：容器内 webapp 无 docker CLI、看不到宿主
# 驱动进程，/api/swe/run|stop 的命令文件由它轮询共享盘 swe_pro/webctl/ 执行。
# 幂等: 已在跑则不重复拉起。
if pgrep -f 'swe_web_bridg[e]' > /dev/null 2>&1; then
  echo "SWE 桥接守护已在运行"
else
  mkdir -p logs
  setsid nohup python3 "$PWD/tools/swe_web_bridge.py" > "$PWD/logs/swe_web_bridge.log" 2>&1 &
  echo "SWE 桥接守护已启动 (日志: logs/swe_web_bridge.log)"
fi

if curl -s -m 3 -o /dev/null "http://127.0.0.1:$PORT/"; then
  echo "控制台已在运行: http://${HOST_IP}:$PORT"
  exit 0
fi

mkdir -p logs
LOG="$PWD/logs/webapp_${PORT}.log"
# AgentX 公开语料从 HuggingFace 拉；容器内 huggingface.co 直连不通（hf-mirror.com 可用）
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
docker exec -d "$CONTAINER" bash -c "cd $PWD && PATH=$VENV/bin:\$PATH HF_ENDPOINT=$HF_ENDPOINT nohup $VENV/bin/python webapp.py --host $HOST --port $PORT > $LOG 2>&1"
sleep 4
if curl -s -m 5 -o /dev/null "http://127.0.0.1:$PORT/"; then
  echo "控制台已启动: http://${HOST_IP}:$PORT   日志: $LOG"
else
  echo "启动失败, 看日志: $LOG"; tail -20 "$LOG"; exit 1
fi
