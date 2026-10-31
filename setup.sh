#!/bin/bash
# ============================================================================
# 一键安装环境（Linux 目标主机 / 容器内执行）
# 安装: evalscope[perf] + 数据集生成依赖（torch/transformers 由镜像自带则跳过）
#       + BFCL v4 函数调用评测线的独立 venv（可选; 装失败仅告警, 不影响其余线）
#       + SWE-bench_Pro 线环境检查（可选; 只检查不修复, 完整准备见
#         tools/prepare_swe_bench_env.sh）
# 用法:
#   bash setup.sh              # 常规安装（清华 pip 源）
#   bash setup.sh --cn-proxy   # 内网需代理时先设置 http_proxy/https_proxy
# ============================================================================
set -e
PIP_INDEX="${PIP_INDEX:-https://pypi.org/simple}"
PY="${PYTHON:-python3}"
# BFCL 独立 venv 路径（与 config.yaml bfcl.venv_python / bfcl_runner.py 取值同源, 可覆盖）
BFCL_VENV="${BFCL_VENV:-/mnt/sfs_turbo/c00803992/bfcl-venv}"

echo "== [1/4] 检查 Python =="
"$PY" --version || { echo "未找到 python3"; exit 1; }

echo "== [2/4] 安装 evalscope[perf] + Web 依赖 =="
"$PY" -m pip install "evalscope[perf]" -i "$PIP_INDEX" 2>/dev/null \
  || "$PY" -m pip install "evalscope[perf]" -i "$PIP_INDEX" --no-build-isolation
# Web 前端后端（fastapi + uvicorn）
"$PY" -m pip install fastapi uvicorn -i "$PIP_INDEX" -q 2>/dev/null || true
# AgentX 智能体回放（SemiAnalysis agentx-harness; crick 需要 python3-devel+gcc）
"$PY" -c "import importlib.util; sys.exit(0 if importlib.util.find_spec('aiperf') else 1)" 2>/dev/null \
  || { echo "  安装 agentx-harness（crick 编译需要 python3-devel gcc）"; \
       "$PY" -m pip install 'agentx-harness==1.0.7' -i "$PIP_INDEX" 2>&1 | tail -2; }

echo "== [2.5/4] 补丁: custom 数据集插件支持 dataset_offset =="
# 官方 custom 插件忽略 --dataset-offset（random/multi_turn 插件都支持），多轮摸高换起点时
# 无法跳过前 N 行 -> 打补丁让 build_messages 按 offset 跳行。幂等：已打过则跳过。
"$PY" - <<'PYEOF'
import os, re, sys
try:
    import evalscope
except ImportError:
    sys.exit(0)  # evalscope 未装成功，前面步骤已报错
f = os.path.join(os.path.dirname(evalscope.__file__),
                 "perf", "plugin", "datasets", "custom.py")
if not os.path.exists(f):
    print(f"  [patch] 未找到 {f}，跳过")
    sys.exit(0)
src = open(f, encoding="utf-8").read()
if "dataset_offset" in src:
    print("  [patch] custom.py 已支持 dataset_offset，跳过")
    sys.exit(0)
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
    print("  [patch] 警告: custom.py 原文不匹配（evalscope 版本变了？），未打补丁")
    sys.exit(0)
open(f, "w", encoding="utf-8").write(src.replace(old, new, 1))
print(f"  [patch] 已为 {f} 打 dataset_offset 补丁")
PYEOF

echo "== [3/4] 修复已知依赖坑 =="
# 坑1: openai 版本不匹配 -> cannot import name 'APIStatusError'
"$PY" -c "import openai" 2>/dev/null \
  || "$PY" -m pip install --force-reinstall --no-deps openai -i "$PIP_INDEX"
# 坑2: pyarrow 需要 numpy>=2.0
"$PY" -c "import pyarrow" 2>/dev/null \
  || "$PY" -m pip install -U numpy -i "$PIP_INDEX"
# 坑3: yaml 解析 config
"$PY" -c "import yaml" 2>/dev/null \
  || "$PY" -m pip install pyyaml -i "$PIP_INDEX"

echo "== [3.5/4] BFCL 函数调用评测线（独立 venv, 可选） =="
# BFCL 依赖装在独立 venv（不与上面 perf 线共用环境, 也不碰 /tmp/bench-venv）。
# venv 的 bin/python 链到容器内解释器, 必须在 ais-bench 容器里可用, 故创建/装包
# 统一经 docker exec 派发; setup 跑在容器内时直跑（与 bfcl_runner.resolve_dispatch 同口径）。
if [ -f /.dockerenv ]; then
  BFCL_RUN=()                    # 已在容器内, 直接执行
else
  BFCL_RUN=(docker exec ais-bench)
fi
# 幂等: venv 已存在且关键包都能导入 → 跳过安装
#（docker exec 的 OCI 报错走 stdout, 探测命令需 >/dev/null 2>&1 才能完全静音）
if "${BFCL_RUN[@]}" "$BFCL_VENV/bin/python" -c "import evalscope, pyarrow, soundfile" >/dev/null 2>&1; then
  echo "  bfcl-venv 已就绪（evalscope / pyarrow / soundfile 导入 OK），跳过安装"
else
  # 缺 venv 或缺依赖 → 按 README「venv 重建」配方安装:
  #   - venv 建在 NFS 上, 容器重建不丢; 基础解释器用容器内 python3.12（不是宿主 $PY,
  #     对已存在的 venv 重跑 -m venv 只更新脚本不动 site-packages, 安全）
  #   - pyarrow==21.0.0 / soundfile 是上游未声明、必须手补的依赖（坑位见 README）
  #   - pip 源走 $PIP_INDEX（README 验证配方用清华源; 内网 pip 不通时
  #     PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple bash setup.sh）
  # 整条链在 if 条件里: 任一步失败（容器没起 / pip 不通）只落 else 告警, 不中断 setup
  if "${BFCL_RUN[@]}" /usr/local/python3.12.13/bin/python3.12 -m venv "$BFCL_VENV" \
     && "${BFCL_RUN[@]}" "$BFCL_VENV/bin/pip" install -i "$PIP_INDEX" \
          "evalscope==1.14.1" "bfcl-eval==2025.10.27.1" "pyarrow==21.0.0" soundfile \
     && "${BFCL_RUN[@]}" "$BFCL_VENV/bin/python" -c "import evalscope, pyarrow, soundfile"; then
    echo "  bfcl-venv 安装完成（evalscope / pyarrow / soundfile 导入 OK）"
  else
    echo "警告: bfcl-venv 安装/校验失败 —— BFCL 函数调用线不可用（其余测试线不受影响）"
  fi
fi

echo "== [3.6/4] SWE-bench_Pro 环境检查（可选线, 只检查不修复） =="
# SWE 线的评测环境（docker unix relay / qemu binfmt / 容器内 venv 补丁）不在
# setup 职责内——由 tools/prepare_swe_bench_env.sh（仓库内副本）幂等准备。
# 这里只做存在性检查: 失败/未就绪仅告警, 不中断安装（set -e 用 || 兜住）。
if "$PY" swe_runner.py env-check; then
  echo "  SWE 环境就绪（bash run.sh --swe status 可监控评测）"
else
  echo "警告: SWE 环境未就绪 —— SWE-bench_Pro 线不可用（其余测试线不受影响）"
  echo "  完整准备（宿主机执行, 幂等）: bash tools/prepare_swe_bench_env.sh"
fi

echo "== [4/4] 验证 =="
"$PY" -c "from evalscope.perf.main import run_perf_benchmark; print('evalscope.perf 导入 OK')"
"$PY" -c "import yaml; print('pyyaml OK')"
evalscope --version 2>/dev/null || "$PY" -c "import evalscope; print('evalscope', getattr(evalscope, '__version__', 'unknown'))"
# 数据集生成依赖（容器内一般已有 torch+transformers，缺失才提示）
"$PY" -c "import torch, transformers" 2>/dev/null \
  && echo "torch/transformers OK（可生成 gsm8k 数据集）" \
  || echo "警告: 缺 torch/transformers —— random 场景可跑，gsm8k 数据集生成不可用"
# BFCL venv 复验（可选线; [3.5/4] 已处理, 此处只报可用性）
"${BFCL_RUN[@]}" "$BFCL_VENV/bin/python" -c "import evalscope, pyarrow, soundfile" >/dev/null 2>&1 \
  && echo "bfcl-venv OK（bash run.sh --bfcl 函数调用评测可用）" \
  || echo "警告: bfcl-venv 不可用 —— BFCL 函数调用线装失败，其余测试线不受影响"

echo
echo "安装完成。下一步:"
echo "  1. 编辑 config.yaml（url / model / tokenizer_path / 场景开关）"
echo "  2. bash run.sh --dry-run   预检命令"
echo "  3. bash run.sh             开始摸高（nohup 后台）"
echo "  4. bash run.sh --bfcl      函数调用精度评测（可选线, bfcl-venv 就绪时可用）"
echo "  5. bash run.sh --swe status  SWE-bench_Pro 评测监控（可选线, 需先跑"
echo "     tools/prepare_swe_bench_env.sh 准备 relay/binfmt/venv 补丁）"
