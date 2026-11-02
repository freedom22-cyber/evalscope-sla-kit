#!/bin/bash
# ============================================================================
# SWE-bench_Pro 线全新部署 bootstrap（宿主机执行, 幂等, 可重复跑）
#
# 在 freshly cloned 的 evalscope-sla-kit 仓库根目录跑本脚本, 得到与本机
# NFS 部署（/mnt/sfs_turbo/c00803992/...）同构的仓库内布局:
#
#   <repo>/swe_pro/                     # 工作目录（= NFS 布局的 swe_pro）
#       logs/ webctl/ reviews_archive/ results/accuracy/swe_bench_pro_dsv4/
#       run_all_batches.sh 等（从 scripts/swe/ 拷入, 不覆盖已有）
#   <repo>/swe_bench_pro_os/repo/       # SWE-bench_Pro-os @ ca10a60
#   <repo>/dockersock/                  # docker unix relay socket
#   容器内 /tmp/bench-venv              # evalscope==1.14.1 + modelscope + docker
#
# swe_pro 的父目录就是仓库根——tools/ dockersock/ swe_bench_pro_os/ 全是
# swe_pro/ 的兄弟目录, 所以 scripts/swe/*.sh 的 $(dirname $0)/.. 推导在
# 两种布局下都成立。
#
# 前置条件（无法由本脚本满足, 需部署侧保证）:
#   * 宿主机有 docker CLI 且当前用户可用; 容器 $CONTAINER（默认 ais-bench）存在
#   * 容器把仓库所在目录以相同路径 bind-mount 进容器（relay socket 与
#     swe_pro/ 脚本都要求容器内路径一致: -v <repo>:<repo>）
#   * 宿主机是 aarch64 时, sweap-images(amd64) 走 qemu binfmt——
#     prepare_swe_bench_env.sh 会建条目, 需要 binfmt_misc 可写
#
# 用法:  bash tools/bootstrap_swe_pro.sh
#   环境变量: CONTAINER(默认 ais-bench) VENV(默认 /tmp/bench-venv)
#             PIP_INDEX(pip 源) SWE_OS_REF(默认 ca10a60)
# ============================================================================
set -u
KIT="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${CONTAINER:-ais-bench}"
VENV="${VENV:-/tmp/bench-venv}"
PIP_INDEX="${PIP_INDEX:-https://pypi.org/simple}"
SWE_OS_REF="${SWE_OS_REF:-ca10a60}"
SWE_OS_URL="${SWE_OS_URL:-https://github.com/scaleapi/SWE-bench_Pro-os.git}"

echo "== 0. 前置检查 =="
command -v docker > /dev/null || { echo "ERROR: 宿主机无 docker CLI"; exit 1; }
docker inspect "$CONTAINER" > /dev/null 2>&1 \
  || { echo "ERROR: 容器 $CONTAINER 不存在（CONTAINER=xxx 可覆盖）"; exit 1; }
# 容器内必须看到与本脚本相同的仓库路径（relay socket / swe_pro 脚本依赖）
if docker exec "$CONTAINER" test -d "$KIT/tools"; then
  echo "  容器内可见 $KIT —— 布局一致 OK"
else
  echo "ERROR: 容器 $CONTAINER 内看不到 $KIT（路径不一致）。"
  echo "  需要容器把仓库目录以相同路径挂载, 例如:"
  echo "    docker run ... -v $KIT:$KIT ..."
  exit 1
fi

echo "== 1. swe_pro 工作目录 =="
mkdir -p "$KIT/swe_pro/logs" \
         "$KIT/swe_pro/webctl" \
         "$KIT/swe_pro/reviews_archive" \
         "$KIT/swe_pro/results/accuracy/swe_bench_pro_dsv4"
mkdir -p "$KIT/dockersock"
# 驱动脚本拷入 swe_pro/（不覆盖: 已有 = 用户改过, 保留）
for f in "$KIT"/scripts/swe/*.sh; do
  dst="$KIT/swe_pro/$(basename "$f")"
  if [ -e "$dst" ]; then
    echo "  保留已有 $(basename "$f")"
  else
    cp "$f" "$dst"
    echo "  拷入 $(basename "$f")"
  fi
done

echo "== 2. SWE-bench_Pro-os 仓库 @$SWE_OS_REF =="
if [ -d "$KIT/swe_bench_pro_os/repo/.git" ]; then
  echo "  已存在, 跳过（如需更新: git -C $KIT/swe_bench_pro_os/repo fetch && checkout）"
else
  mkdir -p "$KIT/swe_bench_pro_os"
  git clone "$SWE_OS_URL" "$KIT/swe_bench_pro_os/repo" \
    || { echo "ERROR: clone 失败（网络?）; SWE_OS_URL 可覆盖"; exit 1; }
  git -C "$KIT/swe_bench_pro_os/repo" checkout "$SWE_OS_REF" \
    || { echo "ERROR: checkout $SWE_OS_REF 失败"; exit 1; }
fi

echo "== 3. 容器内 bench venv =="
if docker exec "$CONTAINER" "$VENV/bin/python" -c "import evalscope, modelscope, docker" >/dev/null 2>&1; then
  echo "  $VENV 已就绪（evalscope / modelscope / docker 导入 OK）"
else
  # 基础解释器: 镜像自带 python3.12 固定路径优先, 否则 PATH 里的 python3
  VENV_PY="${VENV_PY:-/usr/local/python3.12.13/bin/python3.12}"
  docker exec "$CONTAINER" "$VENV_PY" --version >/dev/null 2>&1 || VENV_PY=python3
  docker exec "$CONTAINER" "$VENV_PY" -m venv "$VENV" \
    || { echo "ERROR: venv 创建失败"; exit 1; }
  docker exec "$CONTAINER" "$VENV/bin/pip" install -i "$PIP_INDEX" \
      "evalscope==1.14.1" "modelscope==1.40.2" "docker==7.2.0" \
    || { echo "ERROR: pip 安装失败（换源: PIP_INDEX=... bash 本脚本）"; exit 1; }
  docker exec "$CONTAINER" "$VENV/bin/python" -c "import evalscope, modelscope, docker" \
    && echo "  $VENV 安装完成" || { echo "ERROR: 安装后导入校验失败"; exit 1; }
fi

echo "== 4. relay / binfmt / evalscope 补丁（幂等） =="
bash "$KIT/tools/prepare_swe_bench_env.sh" \
  || { echo "ERROR: prepare 失败, 按上方输出排查"; exit 1; }

echo "== 5. ModelScope 数据集预热（可选, 失败仅告警） =="
# 数据集 642 条, 首次 MsDataset.load 会下载缓存; 评测时也会自动拉, 这里
# 只是提前触发避免首批评测等待。网络受限时可跳过。
if docker exec "$CONTAINER" "$VENV/bin/python" -c "
from modelscope.msdatasets import MsDataset
rows = MsDataset.load('ScaleAI/SWE-bench_Pro', subset_name='default', split='test')
print(f'  dataset rows: {len(rows)}')"; then
  :
else
  echo "警告: 数据集预热失败 —— 评测启动时会再试, 若持续失败检查 ModelScope 网络"
fi

echo
echo "== bootstrap 完成 =="
echo "  冒烟（2 实例）:  docker exec -d $CONTAINER bash -c \\"
echo "    \"cd $KIT/swe_pro && bash run_swe_bench_pro.sh --limit 2\""
echo "  全量分批:        cd $KIT/swe_pro && nohup bash run_all_batches.sh > logs/batches.log 2>&1 &"
echo "  监控:            bash run.sh --swe status"
echo "  注意: config.yaml swe 段若指向不存在的绝对路径, swe_runner 会自动"
echo "        回退到本仓库内布局（stderr 有提示）, 无需手改。"
