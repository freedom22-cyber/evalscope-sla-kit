#!/bin/bash
# Prepare the host for a SWE-bench_Pro run:
#   1. docker unix-socket relay  (the ais-bench container has no /var/run/docker.sock)
#   2. qemu-user binfmt entry   (host is aarch64, sweap-images are amd64 only)
#   3. evalscope adapter patch  (gold test patch must reach the container)
#
# Every step is idempotent: this is safe to re-run before each evaluation.
#
# 路径解析与 kit_paths.py 同口径: 环境变量优先（SWE_CONTAINER/SWE_VENV/
# SWE_REPO_PATH/SWE_RESULTS/RELAY_SOCK/TOOLS）, 默认 kit 相对布局——
# 工作区 results/swe_pro, evalscope venv .cache/swe-venv, OS 仓库 .cache/。
set -u
KIT="$(cd "$(dirname "$0")/.." && pwd)"
TOOLS="${TOOLS:-$KIT/tools}"
CONTAINER="${SWE_CONTAINER:-ais-bench}"
VENV="${SWE_VENV:-$KIT/.cache/swe-venv}"
RELAY_SOCK="${RELAY_SOCK:-${SWE_RELAY_SOCKET:-$KIT/dockersock/docker.sock}}"
OS_REPO="${SWE_REPO_PATH:-$KIT/.cache/swe_bench_pro_os/repo}"
SWE_RESULTS="${SWE_RESULTS:-$KIT/results/swe_pro/results/accuracy/swe_bench_pro}"

echo "== 1. docker unix relay =="
if pgrep -f "docker_unix_relay[.]py" > /dev/null; then
    echo "relay already running: $(pgrep -f 'docker_unix_relay[.]py' | tr '\n' ' ')"
else
    setsid nohup python3 "$TOOLS/docker_unix_relay.py" "$RELAY_SOCK" \
        > "$TOOLS/docker_unix_relay.log" 2>&1 < /dev/null &
    sleep 2
    pgrep -f "docker_unix_relay[.]py" > /dev/null \
        && echo "relay started" || { echo "ERROR: relay failed to start"; exit 1; }
fi
timeout 10 curl -s --unix-socket "$RELAY_SOCK" http://localhost/version > /dev/null \
    && echo "relay reachable" || { echo "ERROR: relay not reachable"; exit 1; }

echo "== 2. qemu-x86_64 binfmt =="
bash "$TOOLS/ensure_qemu_binfmt.sh" || { echo "ERROR: binfmt setup failed"; exit 1; }

echo "== 3. container + evalscope adapter patch =="
docker inspect "$CONTAINER" > /dev/null 2>&1 || { echo "ERROR: no container $CONTAINER"; exit 1; }
if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
    echo "starting $CONTAINER"
    docker start "$CONTAINER" > /dev/null || { echo "ERROR: cannot start container"; exit 1; }
    sleep 3
fi
docker cp "$TOOLS/patch_swe_bench_pro_tests.py" "$CONTAINER:$VENV/patch_swe_bench_pro_tests.py"
docker exec "$CONTAINER" bash -c \
    "SP=\$(ls -d $VENV/lib/python3*/site-packages | head -1); \
     $VENV/bin/python $VENV/patch_swe_bench_pro_tests.py \$SP/evalscope/benchmarks/swe_bench_pro/utils.py"
docker cp "$TOOLS/patch_evalscope_instance_filter.py" "$CONTAINER:/tmp/patch_evalscope_instance_filter.py"
docker exec "$CONTAINER" bash -c \
    "SP=\$(ls -d $VENV/lib/python3*/site-packages | head -1); \
     $VENV/bin/python /tmp/patch_evalscope_instance_filter.py \$SP/evalscope/api/dataset/loader.py"

echo "== 4. run_script worker cap (qemu OOM) + module aliases =="
python3 "$TOOLS/patch_swe_bench_pro_workers.py" "$OS_REPO" "$SWE_RESULTS" || true
python3 "$TOOLS/patch_swe_bench_pro_jest.py" "$OS_REPO" "$SWE_RESULTS" || true

echo "== ready =="
