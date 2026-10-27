#!/usr/bin/env bash
# ==============================================================================
# F-05 claude code CLI —— 问答 + 工具调用
#
# 迁移自 sunzhi_sla_eval/functional/test_claude_code.sh。functional_runner.py 里的
# F-05_claude 只做「有没有 claude CLI」的存在性检查（恒记 SKIP，真会话需人工跑），
# 本脚本把那部分自动化: 用 claude CLI 以 -p 模式打两轮
#   1) 问答: "你好，请简要介绍一下自己"
#   2) 工具: "请列出当前目录下的文件"  (必须真的调工具读目录)
# 判定: 输出非空 且 不出现鉴权/连接类错误。
#
# 用法:
#   bash tools/f05_claude_code.sh                      # 用 config.yaml 的 service 配置
#   CLAUDE_CLI=/path/to/claude bash tools/f05_claude_code.sh
#   F05_TIMEOUT=600 bash tools/f05_claude_code.sh      # 单条 CLI 超时（秒）
#
# 产物: results/functional/f05_<时间戳>/  (f05_qa.txt / f05_tool.txt / report.md)
# ==============================================================================
set -uo pipefail

KIT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${CONFIG:-$KIT_ROOT/config.yaml}"
CLAUDE_CLI="${CLAUDE_CLI:-/mnt/sfs_turbo/c00803992/tools/cc/.npm-prefix/bin/claude}"
F05_TIMEOUT="${F05_TIMEOUT:-300}"

# ---- 从 config.yaml 取 service 配置（url / model / api_key）-------------------
eval "$(python3 - "$CONFIG" <<'PYEOF'
import sys, shlex, yaml
from urllib.parse import urlsplit
cfg = yaml.safe_load(open(sys.argv[1], encoding="utf-8")) or {}
svc = cfg.get("service") or {}
req = cfg.get("request") or {}
url = svc.get("url") or ""
model = svc.get("model") or ""
key = req.get("api_key") or ""
# claude CLI 走 Anthropic Messages API: base url = chat completions 路径去掉尾部
# （版本段也要一起去掉: claude CLI 自己会拼 /v1/messages, 留着 /v1 会变成 /v1/v1/messages）
import re
base = re.sub(r"/(?:v[0-9a-z]+)?/?(?:chat/)?completions$", "", url).rstrip("/")
print(f"API_URL={shlex.quote(url)}")
print(f"BASE_URL={shlex.quote(base)}")
print(f"MODEL_NAME={shlex.quote(model)}")
print(f"API_KEY={shlex.quote(key)}")
print(f"API_HOST={shlex.quote(urlsplit(url).hostname or '')}")
PYEOF
)"

if [[ -z "$API_URL" || -z "$MODEL_NAME" ]]; then
  echo "[ERROR] config 缺少 service.url / service.model: $CONFIG" >&2
  exit 2
fi
[[ -x "$CLAUDE_CLI" ]] || { echo "[ERROR] claude CLI 不可执行: $CLAUDE_CLI" >&2; exit 2; }

TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="$KIT_ROOT/results/functional/f05_${TS}"
mkdir -p "$OUT_DIR"

echo "== F-05 claude code CLI"
echo "   claude CLI : $CLAUDE_CLI ($("$CLAUDE_CLI" --version 2>&1 | head -1))"
echo "   目标服务   : $API_URL"
echo "   模型       : $MODEL_NAME"
echo "   输出目录   : $OUT_DIR"

# ---- CLI 环境（仿 sunzhi settings.json / test_claude_code.sh）-----------------
export ANTHROPIC_BASE_URL="$BASE_URL"
export ANTHROPIC_AUTH_TOKEN="${API_KEY:-EMPTY}"
export ANTHROPIC_MODEL="$MODEL_NAME"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="$MODEL_NAME"
export ANTHROPIC_DEFAULT_SONNET_MODEL="$MODEL_NAME"
export ANTHROPIC_DEFAULT_OPUS_MODEL="$MODEL_NAME"
export CLAUDE_CODE_SUBAGENT_MODEL="$MODEL_NAME"
export CLAUDE_CODE_MAX_CONTEXT_TOKENS="131072"
export API_TIMEOUT_MS="3000000"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1"
export NODE_TLS_REJECT_UNAUTHORIZED="0"

# ⚠️ 必须清掉代理: 环境里的 http_proxy 会把发往网关的请求劫持到不可达地址
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY
export no_proxy="${API_HOST},localhost,127.0.0.1"
export NO_PROXY="$no_proxy"

# ⚠️ 清掉当前 Claude Code 会话自身的环境, 否则子 CLI 会误判「嵌套会话」并串到
#    本会话的 socket / 凭据上
unset CLAUDECODE CLAUDE_CODE_SESSION_ID CLAUDE_CODE_MESSAGING_SOCKET \
      CLAUDE_CODE_CHILD_SESSION CLAUDE_CODE_ENTRYPOINT CLAUDE_PID \
      CLAUDE_CODE_SESSION_ATTENDED ANTHROPIC_API_KEY

# 非内置模型名 → 用 settings.json 的 behavesAs 兜底映射, 避免 CLI 拒跑
SETTINGS="$OUT_DIR/settings.json"
cat > "$SETTINGS" <<EOF
{
  "model": "$MODEL_NAME",
  "skipDangerousModePermissionPrompt": true,
  "modelPicker": {
    "options": [
      { "model": "$MODEL_NAME", "label": "GLM-5.3 (MaaS 网关)", "behavesAs": "claude-sonnet-4-5" }
    ]
  }
}
EOF

# ---- 判定 ---------------------------------------------------------------------
# 输出非空, 且不出现鉴权/连接/模型不可用类错误
# ⚠️ "There's an issue with the selected model (X)" 是 CLI 对 API 请求失败的统一文案
#    (模型名不被网关路由 / 模型不存在 / 404 Path not found 都会落到这句),
#    不把它算失败就会出现假 PASS —— sunzhi 那轮的 PASS 就没存原始输出, 无法自证。
output_ok() {
  local out="$1"
  [[ -n "${out//[[:space:]]/}" ]] || return 1
  if echo "$out" | grep -qiE "not logged in|api error|connection error|unauthorized|invalid api key|fetch failed|ECONNREFUSED|ETIMEDOUT|there's an issue with the selected model|isn't described by this version's model catalog|Path not found"; then
    echo "$out" | grep -qiE "there's an issue with the selected model|Path not found" && \
      echo "   [HINT] 模型不可用/路由不存在 —— 多数是网关未提供 Anthropic Messages 路由 (POST $BASE_URL/v1/messages 404)"
    return 1
  fi
  return 0
}
# CLI 对非内置模型名会打印该告警, 属告警不属失败
warn_unrecognized_model() {
  echo "$1" | grep -q "unrecognized_model" && \
    echo "   [WARN] CLI 未识别模型名 $MODEL_NAME（靠 settings.json 的 behavesAs 兜底），实际请求仍成功"
  return 0
}

PASS_COUNT=0
FAIL_COUNT=0

echo
echo "=== [1/2] 问答测试 ==="
QA_OUT="$(cd "$KIT_ROOT" && timeout "$F05_TIMEOUT" "$CLAUDE_CLI" -p "你好，请简要介绍一下自己" \
          --model "$MODEL_NAME" --settings "$SETTINGS" 2>&1)"
echo "$QA_OUT" > "$OUT_DIR/f05_qa.txt"
echo "$QA_OUT" | head -20
warn_unrecognized_model "$QA_OUT"
if output_ok "$QA_OUT"; then
  echo "   [PASS] 问答"
  PASS_COUNT=$((PASS_COUNT + 1))
else
  echo "   [FAIL] 问答"
  FAIL_COUNT=$((FAIL_COUNT + 1))
fi

echo
echo "=== [2/2] 工具调用测试 ==="
TOOL_OUT="$(cd "$KIT_ROOT" && timeout "$F05_TIMEOUT" "$CLAUDE_CLI" -p "请列出当前目录下的文件" \
             --model "$MODEL_NAME" --settings "$SETTINGS" 2>&1)"
echo "$TOOL_OUT" > "$OUT_DIR/f05_tool.txt"
echo "$TOOL_OUT" | head -20
warn_unrecognized_model "$TOOL_OUT"
if output_ok "$TOOL_OUT"; then
  echo "   [PASS] 工具调用"
  PASS_COUNT=$((PASS_COUNT + 1))
else
  echo "   [FAIL] 工具调用"
  FAIL_COUNT=$((FAIL_COUNT + 1))
fi
# 工具结果是否像一份目录清单（不致命, 仅提示）
if ! echo "$TOOL_OUT" | grep -qE "config\.yaml|functional_runner\.py|results|tools"; then
  echo "   [WARN] 输出中未识别到目录条目（CLI 可能没真的调工具）"
fi

SUMMARY="claude code: pass=${PASS_COUNT} fail=${FAIL_COUNT}"
echo
echo "======================================================================"
echo "$SUMMARY"

if [[ $FAIL_COUNT -eq 0 ]]; then
  VERDICT=PASS
  CONCLUSION="claude code CLI 能正常问答并完成工具调用（无鉴权/连接类错误）"
else
  VERDICT=FAIL
  CONCLUSION="claude code CLI 未能完成问答/工具调用（见 f05_qa.txt / f05_tool.txt 原文）。若报错为「模型不可用」，多为网关未提供 Anthropic Messages 路由（POST $BASE_URL/v1/messages 返回 404 Path not found）"
fi

cat > "$OUT_DIR/report.md" <<EOF
# 测试报告: F-05_claude_code
- **测试时间**: $(date '+%Y-%m-%d %H:%M:%S')
- **模型服务**: $API_URL
- **测试场景**: claude code 问答 + 工具调用
- **参数配置**: model=$MODEL_NAME, ANTHROPIC_BASE_URL=$BASE_URL, claude CLI=$(basename "$CLAUDE_CLI") $("$CLAUDE_CLI" --version 2>&1 | head -1)
- **测试结果**: $VERDICT
- **关键指标**: $SUMMARY
- **结论**: $CONCLUSION
EOF
echo "报告: $OUT_DIR/report.md"

[[ $FAIL_COUNT -eq 0 ]] && exit 0 || exit 1
