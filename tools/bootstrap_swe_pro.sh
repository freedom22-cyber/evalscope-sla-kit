#!/bin/bash
# ============================================================================
# SWE-bench_Pro 线全新部署 bootstrap（宿主机执行, 幂等, 可重复跑）
#
# 在 freshly cloned 的 evalscope-sla-kit 仓库根目录跑本脚本, 建立 kit_paths.py
# 默认的 kit 相对布局（config.yaml 不写死机器路径, 全部环境变量/kit 默认解析）:
#
#   <kit>/results/swe_pro/              # 工作区（驱动/logs/reviews/results）
#       logs/ webctl/ reviews_archive/ results/accuracy/swe_bench_pro/
#       run_all_batches.sh 等（从 scripts/swe/ 拷入, 不覆盖已有）
#   <kit>/.cache/swe_bench_pro_os/repo/ # SWE-bench_Pro-os @ ca10a60
#   <kit>/.cache/swe-venv/              # evalscope==1.14.1 + modelscope + docker
#   <kit>/dockersock/                   # docker unix relay socket（容器无
#                                       # /var/run/docker.sock 时才需要）
#
# 前置条件（无法由本脚本满足, 需部署侧保证）:
#   * 宿主机有 docker CLI 且当前用户可用
#   * 评测经 docker exec 进容器跑（SWE_CONTAINER, 默认 ais-bench）时, 容器
#     需存在, 且把 kit 目录以相同路径挂进去（relay socket / venv / 驱动脚本
#     都要求容器内外路径一致: docker run -v <kit>:<kit>）
#   * aarch64 宿主跑 amd64 sweap 镜像走 qemu binfmt——prepare 会建条目
#
# 用法:  bash tools/bootstrap_swe_pro.sh
#   环境变量: SWE_CONTAINER(默认 ais-bench; 空=容器内直跑本机装)
#             SWE_VENV(默认 <kit>/.cache/swe-venv)
#             PIP_INDEX / SWE_OS_REF(默认 ca10a60) / SWE_OS_URL
# ============================================================================
set -u
KIT="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${SWE_CONTAINER:-ais-bench}"
VENV="${SWE_VENV:-$KIT/.cache/swe-venv}"
PIP_INDEX="${PIP_INDEX:-https://pypi.org/simple}"
SWE_OS_REF="${SWE_OS_REF:-ca10a60}"
SWE_OS_URL="${SWE_OS_URL:-https://github.com/scaleapi/SWE-bench_Pro-os.git}"

echo "== 0. 前置检查 =="
command -v docker > /dev/null || { echo "ERROR: 宿主机无 docker CLI"; exit 1; }
if [ -n "$CONTAINER" ]; then
  docker inspect "$CONTAINER" > /dev/null 2>&1 \
    || { echo "ERROR: 容器 $CONTAINER 不存在（SWE_CONTAINER=xxx 可覆盖）"; exit 1; }
  # 容器内必须看到与本脚本相同的 kit 路径（relay socket / venv / 驱动脚本依赖）
  if docker exec "$CONTAINER" test -d "$KIT/tools"; then
    echo "  容器内可见 $KIT —— 布局一致 OK"
  else
    echo "ERROR: 容器 $CONTAINER 内看不到 $KIT（路径不一致）。"
    echo "  需要容器把 kit 目录以相同路径挂载, 例如:"
    echo "    docker run ... -v $KIT:$KIT ..."
    exit 1
  fi
  RUN=(docker exec "$CONTAINER")
else
  RUN=()   # 容器内直跑模式, 本机执行
fi

echo "== 1. swe_pro 工作目录 =="
mkdir -p "$KIT/results/swe_pro/logs" \
         "$KIT/results/swe_pro/webctl" \
         "$KIT/results/swe_pro/reviews_archive" \
         "$KIT/results/swe_pro/results/accuracy/swe_bench_pro"
# 驱动脚本拷入 results/swe_pro/（不覆盖: 已有 = 用户改过, 保留）
for f in "$KIT"/scripts/swe/*.sh; do
  dst="$KIT/results/swe_pro/$(basename "$f")"
  if [ -e "$dst" ]; then
    echo "  保留已有 $(basename "$f")"
  else
    cp "$f" "$dst"
    echo "  拷入 $(basename "$f")"
  fi
done

echo "== 2. SWE-bench_Pro-os 仓库 @$SWE_OS_REF =="
if [ -d "$KIT/.cache/swe_bench_pro_os/repo/.git" ]; then
  echo "  已存在, 跳过（如需更新: git -C ... fetch && checkout）"
else
  mkdir -p "$KIT/.cache/swe_bench_pro_os"
  git clone "$SWE_OS_URL" "$KIT/.cache/swe_bench_pro_os/repo" \
    || { echo "ERROR: clone 失败（网络?）; SWE_OS_URL 可覆盖"; exit 1; }
  git -C "$KIT/.cache/swe_bench_pro_os/repo" checkout "$SWE_OS_REF" \
    || { echo "ERROR: checkout $SWE_OS_REF 失败"; exit 1; }
fi

echo "== 3. evalscope venv =="
if "${RUN[@]}" "$VENV/bin/python" -c "import evalscope, modelscope, docker" >/dev/null 2>&1; then
  echo "  $VENV 已就绪（evalscope / modelscope / docker 导入 OK）"
else
  "${RUN[@]}" python3 -m venv "$VENV" \
    || { echo "ERROR: venv 创建失败"; exit 1; }
  "${RUN[@]}" "$VENV/bin/pip" install -i "$PIP_INDEX" \
      "evalscope==1.14.1" "modelscope==1.40.2" "docker==7.2.0" \
    || { echo "ERROR: pip 安装失败（换源: PIP_INDEX=... 重跑）"; exit 1; }
  "${RUN[@]}" "$VENV/bin/python" -c "import evalscope, modelscope, docker" \
    && echo "  $VENV 安装完成" || { echo "ERROR: 安装后导入校验失败"; exit 1; }
fi

echo "== 4. relay / binfmt / evalscope 补丁（幂等） =="
# 容器内已有 /var/run/docker.sock 时用直连, 否则 prepare 会起 unix relay
# （socket 落 $KIT/dockersock/docker.sock, 容器经相同路径访问）
bash "$KIT/tools/prepare_swe_bench_env.sh" \
  || { echo "ERROR: prepare 失败, 按上方输出排查"; exit 1; }

echo "== 5. ModelScope 数据集预热（可选, 失败仅告警） =="
# 数据集 642 条, 首次 MsDataset.load 会下载缓存; 评测时也会自动拉, 这里
# 只是提前触发避免首批评测等待。网络受限时可跳过。
if "${RUN[@]}" "$VENV/bin/python" -c "
from modelscope.msdatasets import MsDataset
rows = MsDataset.load('ScaleAI/SWE-bench_Pro', subset_name='default', split='test')
print(f'  dataset rows: {len(rows)}')"; then
  :
else
  echo "警告: 数据集预热失败 —— 评测启动时会再试, 若持续失败检查 ModelScope 网络"
fi

echo
echo "== bootstrap 完成 =="
echo "  冒烟（2 实例, 记得注入被测服务）:  SWE_CONTAINER=$CONTAINER \\"
echo "    API_URL=http://<host>:<port>/v1/chat/completions MODEL=<model> \\"
echo "    docker exec -d $CONTAINER bash -c \\"
echo "      \"cd $KIT/results/swe_pro && bash run_swe_bench_pro.sh --limit 2\""
echo "  全量分批:  cd $KIT/results/swe_pro && nohup bash run_all_batches.sh > logs/batches.log 2>&1 &"
echo "  监控:      bash run.sh --swe status"
echo "  机器路径的解析优先级: 环境变量 > config 显式值 > kit 默认（kit_paths.py）"
