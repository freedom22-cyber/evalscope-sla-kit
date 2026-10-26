#!/bin/bash
# ============================================================================
# 一键安装环境（Linux 目标主机 / 容器内执行）
# 安装: evalscope[perf] + 数据集生成依赖（torch/transformers 由镜像自带则跳过）
# 用法:
#   bash setup.sh              # 常规安装（清华 pip 源）
#   bash setup.sh --cn-proxy   # 内网需代理时先设置 http_proxy/https_proxy
# ============================================================================
set -e
PIP_INDEX="${PIP_INDEX:-https://pypi.org/simple}"
PY="${PYTHON:-python3}"

echo "== [1/4] 检查 Python =="
"$PY" --version || { echo "未找到 python3"; exit 1; }

echo "== [2/4] 安装 evalscope[perf] + Web 依赖 =="
"$PY" -m pip install "evalscope[perf]" -i "$PIP_INDEX" 2>/dev/null \
  || "$PY" -m pip install "evalscope[perf]" -i "$PIP_INDEX" --no-build-isolation
# Web 前端后端（fastapi + uvicorn）
"$PY" -m pip install fastapi uvicorn -i "$PIP_INDEX" -q 2>/dev/null || true

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

echo "== [4/4] 验证 =="
"$PY" -c "from evalscope.perf.main import run_perf_benchmark; print('evalscope.perf 导入 OK')"
"$PY" -c "import yaml; print('pyyaml OK')"
evalscope --version 2>/dev/null || "$PY" -c "import evalscope; print('evalscope', getattr(evalscope, '__version__', 'unknown'))"
# 数据集生成依赖（容器内一般已有 torch+transformers，缺失才提示）
"$PY" -c "import torch, transformers" 2>/dev/null \
  && echo "torch/transformers OK（可生成 gsm8k 数据集）" \
  || echo "警告: 缺 torch/transformers —— random 场景可跑，gsm8k 数据集生成不可用"

echo
echo "安装完成。下一步:"
echo "  1. 编辑 config.yaml（url / model / tokenizer_path / 场景开关）"
echo "  2. bash run.sh --dry-run   预检命令"
echo "  3. bash run.sh             开始摸高（nohup 后台）"
