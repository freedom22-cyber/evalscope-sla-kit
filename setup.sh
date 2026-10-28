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
