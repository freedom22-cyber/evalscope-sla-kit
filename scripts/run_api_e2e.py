#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simaas 风格 API E2E 功能测试（evalscope-sla-kit 统一框架版）。

对被测服务跑一组 API 端到端检查，覆盖四种接口协议族:

    openai     OpenAI 兼容  (/v1/models, /v1/chat/completions, /v1/completions,
                             /v1/responses, 流式, 结构化输出, 多轮/长上下文召回, usage)
    messages   Anthropic Messages 风格 (/v1/messages, 流式, tool use, JSON 输出)
    gemini     Gemini 风格 (:generateContent, :streamGenerateContent,
                            function calling, JSON 输出)
    embedding  /v1/embeddings

判定口径统一为 PASS / FAIL / WARN / SKIP:

    PASS  正常
    FAIL  失败（计入退出码）
    WARN  已知缺陷 / 可接受异常（如 reasoning_effort=medium 的 400 已知缺陷、
          可选端点未实现）——不计失败
    SKIP  主动跳过（config functional.skip / 该协议族未启用）

F-01~F-05 专有检查（GLM 侧沉淀）已迁移进本框架, 判定口径沿用原 functional_runner.py:

    F-01a chat 非流式 200+content   -> openai/compatibility  "chat completions"
    F-01b chat 流式 data:/[DONE]     -> openai/streaming      "chat streaming"
    F-01c completions 非流式         -> openai/compatibility  "completions"
    F-01d max_tokens=8 截断          -> openai/robustness     "F-01d max tokens truncation"
    F-01e stop 参数                  -> openai/robustness     "F-01e stop parameter"
    F-01f temperature=0 两次一致     -> openai/robustness     "F-01f determinism"
    F-02a thinking 开(reasoning 字段) -> openai/capability    "F-02a thinking on"
    F-02b thinking 关(effort=none)    -> openai/capability    "F-02b thinking off"
    F-03  effort 矩阵(low/high/...)   -> openai/capability    "F-03 <effort>"
    F-03_medium 已知缺陷              -> openai/capability    "F-03 medium" (恒 WARN)
    F-04  tools 触发 tool_calls       -> openai/capability    "F-04 tool call"
    F_err 错误模型名返回 4xx          -> openai/robustness     "F_err unknown model 4xx"
    F-05  claude code 会话            -> openai/session        "F-05 claude code session" (恒 SKIP)

用法:
    python3 scripts/run_api_e2e.py --list             # 列出全部用例
    python3 scripts/run_api_e2e.py --dry-run          # 只打印计划, 不发请求
    python3 scripts/run_api_e2e.py                    # 按 config.yaml 跑
    python3 scripts/run_api_e2e.py --base-url https://116.204.8.3/infer7 --model 'glm-5' \
        --header 'Host: sz.glm5.com' --skip-bench --capability-repeat 10 --insecure
    python3 scripts/run_api_e2e.py --interface-types openai,messages

配置来源: config.yaml 为主（service / request / functional 段）, 命令行参数与环境变量覆盖。
"""
import argparse
import datetime as dt
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

KIT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG = os.path.join(KIT_ROOT, "config.yaml")

try:
    import yaml
except ImportError:  # pragma: no cover - 纯 CLI 用法时允许没有 pyyaml
    yaml = None


STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_WARN = "WARN"
STATUS_SKIP = "SKIP"

INTERFACE_OPENAI = "openai"
INTERFACE_MESSAGES = "messages"
INTERFACE_GEMINI = "gemini"
INTERFACE_EMBEDDING = "embedding"
INTERFACE_TYPES = [
    INTERFACE_OPENAI,
    INTERFACE_MESSAGES,
    INTERFACE_GEMINI,
    INTERFACE_EMBEDDING,
]

DEFAULT_API_VERSION = "v1"
DEFAULT_GEMINI_API_VERSION = "v1beta"
DEFAULT_TIMEOUT_SECONDS = 300

# 流式响应累计读取上限（1 MiB）: 防异常服务无限输出拖死检查
STREAM_MAX_BYTES = 1 << 20

# capability 类用例（category 以 /capability 结尾）受 --capability-repeat 影响:
# 重复跑 N 轮用于抖动检测——单次能过不代表稳定。
CAPABILITY_SUFFIX = "/capability"


@dataclass
class Config:
    base_url: str
    api_key: str
    model: str
    interface_types: List[str]
    openai_api_version: str
    messages_api_version: str
    gemini_api_version: str
    embedding_api_version: str
    gemini_interactions_api_revision: str
    headers: Dict[str, str]
    timeout_seconds: int
    report_path: Path
    print_logs: bool
    log_max_chars: int
    verify_ssl: bool
    skip_long_context_api: bool
    long_context_api_tokens: int
    # ---- kit 扩展 ----
    skip_cases: List[str] = field(default_factory=list)
    efforts: List[str] = field(default_factory=lambda: ["low", "high", "xhigh", "max"])
    max_tokens: int = 2048          # 思考型模型的输出预算（太小会把 max_tokens 吃光, 正文为空）
    capability_repeat: int = 1
    skip_bench: bool = False
    output_dir: Path = Path("results/functional")
    claude_session_hint: str = "tools/f05_claude_code.sh"


@dataclass
class HttpResponse:
    status: int
    body: bytes
    headers: Dict[str, str]
    error: str = ""
    latency_ms: int = 0

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text())


@dataclass
class ApiCaseResult:
    category: str
    name: str
    endpoint: str
    status: str
    http_status: Optional[int] = None
    latency_ms: Optional[int] = None
    issue: str = ""
    detail: str = ""
    run_index: int = 1
    run_total: int = 1


@dataclass
class Case:
    """一条可执行的用例声明。

    tag 沿用 F-xx 编号（--only 可按 tag 或前缀组过滤, 如 --only F-03）;
    optional=True 的用例在端点未实现（404/405）时记 WARN 而非 FAIL;
    runner 非空时由自定义执行函数接管（如需要连发两次的确定性检查）。
    """
    category: str
    name: str
    endpoint: str
    method: str = "POST"
    payload: Optional[Dict[str, Any]] = None
    validate: Optional[Callable[[HttpResponse], Tuple[str, str, str]]] = None
    extra_headers: Optional[Dict[str, str]] = None
    stream: bool = False
    extractor: Optional[Callable[[Any], str]] = None
    tags: List[str] = field(default_factory=list)
    optional: bool = False
    always_validate: bool = False
    require_done: bool = False   # 缺 [DONE] 结束标记时记 FAIL（沿用 F-01b 旧口径）
    slug: str = ""
    skip_always: bool = False
    skip_reason: str = ""
    runner: Optional[Callable[["Config", "Case", Optional[str]], ApiCaseResult]] = None


def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("base URL is required. Use --base-url, BASE_URL, or OPENAI_BASE_URL.")
    return value


def normalize_api_version(value: str) -> str:
    value = value.strip().strip("/")
    if not value:
        raise ValueError("API version cannot be empty.")
    return value


_BASE_URL_STRIP_RE = re.compile(r"/(?:v[0-9a-z]+)?/?(?:chat/)?completions$")


def base_url_of(chat_url: str) -> str:
    """完整 chat completions 路径 -> base URL（去掉尾部 /v1/chat/completions 等）。

    config.yaml 的 service.url 是 chat completions 完整路径, 而框架按
    base + /{api_version}/{endpoint} 拼地址, 所以这里要把版本段一起还原成 base。
    例: http://h/v2/infer/app/v1/chat/completions -> http://h/v2/infer/app
    """
    url = (chat_url or "").strip().rstrip("/")
    return _BASE_URL_STRIP_RE.sub("", url).rstrip("/")


def endpoint_url(base_url: str, endpoint: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...<truncated {len(text) - max_chars} chars>"


def parse_header(value: str) -> Tuple[str, str]:
    if ":" in value:
        key, raw = value.split(":", 1)
    elif "=" in value:
        key, raw = value.split("=", 1)
    else:
        raise argparse.ArgumentTypeError("Header must be formatted as 'Name: value' or 'Name=value'.")
    key = key.strip()
    if not key:
        raise argparse.ArgumentTypeError("Header name cannot be empty.")
    return key, raw.strip()


def parse_interface_types(values: Sequence[str]) -> List[str]:
    items: List[str] = []
    for value in values:
        for item in value.split(","):
            normalized = item.strip().lower()
            if not normalized:
                continue
            if normalized not in INTERFACE_TYPES:
                allowed = ", ".join(INTERFACE_TYPES)
                raise ValueError(f"Unsupported interface type: {normalized}. Allowed: {allowed}.")
            if normalized not in items:
                items.append(normalized)
    return items or [INTERFACE_OPENAI]


def request_headers(config: Config, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = dict(config.headers)
    if config.api_key:
        headers.setdefault("Authorization", f"Bearer {config.api_key}")
    if extra:
        headers.update(extra)
    return headers


def ssl_context(config: Config) -> Optional[ssl.SSLContext]:
    if config.verify_ssl:
        return None
    return ssl._create_unverified_context()


# 直连 opener（等价 curl --noproxy '*'）: 被测服务常是内网地址,
# 不走 http_proxy 环境变量, 避免功能请求被代理劫持。
# 注: OpenerDirector.open() 不接受 context 参数, SSL 上下文要挂到 HTTPSHandler 上。
def build_opener(config: "Config") -> urllib.request.OpenerDirector:
    handlers: List[Any] = [urllib.request.ProxyHandler({})]
    ctx = ssl_context(config)
    if ctx is not None:
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def make_request(
    config: Config,
    method: str,
    endpoint: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    extra_headers: Optional[Dict[str, str]] = None,
) -> HttpResponse:
    url = endpoint_url(config.base_url, endpoint)
    data = None
    headers = request_headers(config, extra_headers)
    if payload is not None:
        data = compact_json(payload).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")

    request = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    started = time.time()
    try:
        with build_opener(config).open(
            request,
            timeout=config.timeout_seconds,
        ) as resp:
            body = resp.read()
            latency_ms = int((time.time() - started) * 1000)
            return HttpResponse(
                status=resp.status,
                body=body,
                headers=dict(resp.headers.items()),
                latency_ms=latency_ms,
            )
    except urllib.error.HTTPError as exc:
        body = exc.read()
        latency_ms = int((time.time() - started) * 1000)
        return HttpResponse(
            status=exc.code,
            body=body,
            headers=dict(exc.headers.items()),
            error=str(exc),
            latency_ms=latency_ms,
        )
    except Exception as exc:
        latency_ms = int((time.time() - started) * 1000)
        return HttpResponse(status=0, body=b"", headers={}, error=str(exc), latency_ms=latency_ms)


def make_stream_request(
    config: Config,
    endpoint: str,
    payload: Dict[str, Any],
    *,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Tuple[HttpResponse, List[str], int]:
    url = endpoint_url(config.base_url, endpoint)
    headers = request_headers(config, extra_headers)
    headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url,
        data=compact_json(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    started = time.time()
    deadline = started + config.timeout_seconds   # 墙钟总时限: 防"每个 chunk 都来但一直不断"的慢速流
    first_event_ms = -1
    lines: List[str] = []
    total_bytes = 0
    truncated = False
    try:
        with build_opener(config).open(
            request,
            timeout=config.timeout_seconds,
        ) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                if first_event_ms < 0:
                    first_event_ms = int((time.time() - started) * 1000)
                lines.append(line)
                total_bytes += len(raw_line)
                if line == "[DONE]" or line == "data: [DONE]":
                    break                     # 收到结束标记立即收流, 不等对端关闭连接
                if total_bytes >= STREAM_MAX_BYTES:
                    truncated = True
                    break
                if time.time() > deadline:
                    truncated = True
                    break
            latency_ms = int((time.time() - started) * 1000)
            body = "\n".join(lines).encode("utf-8")
            return (
                HttpResponse(resp.status, body, dict(resp.headers.items()),
                             error="stream truncated (byte cap or deadline reached)" if truncated else "",
                             latency_ms=latency_ms),
                lines,
                first_event_ms,
            )
    except urllib.error.HTTPError as exc:
        body = exc.read()
        latency_ms = int((time.time() - started) * 1000)
        return (
            HttpResponse(exc.code, body, dict(exc.headers.items()), error=str(exc), latency_ms=latency_ms),
            [],
            first_event_ms,
        )
    except Exception as exc:
        latency_ms = int((time.time() - started) * 1000)
        # 关键: 出错也要把已经收到的 SSE 行带回去（丢弃就等于没有现场证据）
        return (HttpResponse(0, "\n".join(lines).encode("utf-8"), {},
                             error=str(exc), latency_ms=latency_ms),
                lines, first_event_ms)


def summarize_body(resp: HttpResponse, max_len: int = 500) -> str:
    if resp.error and not resp.body:
        return resp.error
    text = resp.text().strip()
    return truncate_text(text, max_len)


def extract_text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def collect_strings(value: Any, out: List[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, list):
        for item in value:
            collect_strings(item, out)
    elif isinstance(value, dict):
        for item in value.values():
            collect_strings(item, out)


def collect_key_text(value: Any, keys: Iterable[str], out: List[str]) -> None:
    keyset = set(keys)
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keyset and isinstance(item, str):
                out.append(item)
            else:
                collect_key_text(item, keyset, out)
    elif isinstance(value, list):
        for item in value:
            collect_key_text(item, keyset, out)


def find_key_recursive(value: Any, keys: Iterable[str]) -> Optional[Any]:
    keyset = set(keys)
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keyset:
                return item
            found = find_key_recursive(item, keyset)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_key_recursive(item, keyset)
            if found is not None:
                return found
    return None


def extract_response_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    texts: List[str] = []

    for choice in data.get("choices", []) or []:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            texts.append(extract_text_from_content(message.get("content")))
            tool_calls = message.get("tool_calls")
            if tool_calls:
                texts.append(compact_json(tool_calls))
        texts.append(extract_text_from_content(choice.get("text")))

    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            for content in item.get("content", []) or []:
                if isinstance(content, dict):
                    texts.append(extract_text_from_content(content.get("text")))
                    texts.append(extract_text_from_content(content.get("content")))
            if item.get("type") in {"function_call", "tool_call"}:
                texts.append(compact_json(item))

    for key in ("output_text", "text", "content"):
        value = data.get(key)
        if isinstance(value, str):
            texts.append(value)

    return "\n".join(text for text in texts if text).strip()


def extract_messages_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    return extract_text_from_content(data.get("content")) or extract_response_text(data)


def extract_gemini_text(data: Any) -> str:
    texts: List[str] = []
    collect_key_text(data, keys=("text", "output_text"), out=texts)
    return "\n".join(texts).strip()


def extract_usage(data: Any) -> Optional[Dict[str, Any]]:
    usage = find_key_recursive(data, {"usage", "usageMetadata", "usage_metadata"})
    return usage if isinstance(usage, dict) else None


def first_numeric(usage: Dict[str, Any], names: Sequence[str]) -> Optional[float]:
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def validate_usage_consistency(data: Any) -> Tuple[str, str, str]:
    usage = extract_usage(data)
    if not usage:
        return STATUS_WARN, "usage fields are missing.", ""

    prompt = first_numeric(usage, ("prompt_tokens", "input_tokens", "promptTokenCount", "inputTokenCount"))
    completion = first_numeric(
        usage,
        ("completion_tokens", "output_tokens", "completionTokenCount", "candidatesTokenCount", "outputTokenCount"),
    )
    total = first_numeric(usage, ("total_tokens", "totalTokens", "totalTokenCount"))
    details = [
        f"prompt={prompt:g}" if prompt is not None else "prompt=?",
        f"completion={completion:g}" if completion is not None else "completion=?",
        f"total={total:g}" if total is not None else "total=?",
    ]
    if total is not None and prompt is not None and completion is not None:
        if abs(total - prompt - completion) > 2:
            return STATUS_WARN, "usage total is not consistent with prompt + completion.", ", ".join(details)
    return STATUS_PASS, "", ", ".join(details)


def strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[-1].strip().startswith("```"):
            return "\n".join(lines[1:-1]).strip()
    return text


def parse_json_from_text(text: str) -> Tuple[Optional[Any], str]:
    text = strip_code_fence(text)
    try:
        return json.loads(text), ""
    except Exception:
        pass
    start = min([idx for idx in (text.find("{"), text.find("[")) if idx >= 0], default=-1)
    end = max(text.rfind("}"), text.rfind("]"))
    if start >= 0 and end > start:
        snippet = text[start : end + 1]
        try:
            return json.loads(snippet), ""
        except Exception as exc:
            return None, str(exc)
    return None, "no JSON object or array found in model output."


# ---------------------------------------------------------------------------
# 校验器（validator）: (HttpResponse) -> (status, issue, detail)
# ---------------------------------------------------------------------------

def validate_non_empty_text(resp: HttpResponse, extractor: Callable[[Any], str] = extract_response_text) -> Tuple[str, str, str]:
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    text = extractor(data)
    if not text:
        return STATUS_FAIL, "model output text is empty.", summarize_body(resp)
    return STATUS_PASS, "", f"text_len={len(text)}"


def validate_contains_token(token: str, extractor: Callable[[Any], str] = extract_response_text) -> Callable[[HttpResponse], Tuple[str, str, str]]:
    def _validate(resp: HttpResponse) -> Tuple[str, str, str]:
        try:
            data = resp.json()
        except Exception as exc:
            return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
        text = extractor(data)
        if token.lower() not in text.lower():
            return STATUS_FAIL, f"expected token {token!r} was not found.", truncate_text(text, 300)
        return STATUS_PASS, "", f"found={token}"

    return _validate


def validate_json_output(extractor: Callable[[Any], str] = extract_response_text) -> Callable[[HttpResponse], Tuple[str, str, str]]:
    def _validate(resp: HttpResponse) -> Tuple[str, str, str]:
        try:
            data = resp.json()
        except Exception as exc:
            return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
        text = extractor(data)
        parsed, issue = parse_json_from_text(text)
        if parsed is None:
            return STATUS_FAIL, f"model output is not parseable JSON: {issue}", truncate_text(text, 300)
        if isinstance(parsed, dict) and "city" in parsed:
            return STATUS_PASS, "", f"json_keys={','.join(sorted(parsed.keys()))}"
        return STATUS_PASS, "", f"json_type={type(parsed).__name__}"

    return _validate


def validate_usage_response(resp: HttpResponse) -> Tuple[str, str, str]:
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    return validate_usage_consistency(data)


def validate_embedding_response(resp: HttpResponse) -> Tuple[str, str, str]:
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    vectors = find_key_recursive(data, {"embedding"})
    if not isinstance(vectors, list) or not vectors:
        return STATUS_FAIL, "embedding vector was not found.", summarize_body(resp)
    return STATUS_PASS, "", f"embedding_dim={len(vectors)}"


# ---- F-01~F-05 迁移过来的校验器 --------------------------------------------

def validate_answer_text(resp: HttpResponse) -> Tuple[str, str, str]:
    """正文非空。

    思考型模型（GLM-5.3 强制思考）在 max_tokens 偏小时会把预算吃光, 正文为空——
    那是预算问题不是协议问题, 记 WARN; 正文与 reasoning 都空才记 FAIL。
    """
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    content = str(message.get("content") or "")
    reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "")
    if content.strip():
        return STATUS_PASS, "", f"text_len={len(content)}"
    if reasoning.strip():
        return STATUS_WARN, ("answer text is empty: the reasoning chain consumed the whole "
                             "max_tokens budget (raise functional.max_tokens)."), f"reasoning_len={len(reasoning)}"
    return STATUS_FAIL, "model output text is empty (no content and no reasoning).", summarize_body(resp)


def validate_completion_response(resp: HttpResponse) -> Tuple[str, str, str]:
    """completions 端点: 2xx 且 choices 存在即通过（旧口径）; 正文为空记 WARN。"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return STATUS_FAIL, "response has no choices array.", summarize_body(resp)
    first = choices[0] or {}
    text = str(first.get("text") or "")
    if text.strip():
        return STATUS_PASS, "", f"text_len={len(text)}"
    return (STATUS_WARN,
            "completion text is empty (a thinking model may have consumed the whole max_tokens budget).",
            f"finish_reason={first.get('finish_reason')}")


def validate_completion_tokens_capped(resp: HttpResponse, cap: int = 8) -> Tuple[str, str, str]:
    """F-01d: max_tokens=8 截断后 usage.completion_tokens <= 8（解析不到记 FAIL）"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    usage = extract_usage(data) or {}
    value = usage.get("completion_tokens")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return STATUS_FAIL, "usage.completion_tokens is missing.", compact_json(usage)
    if value > cap:
        return STATUS_FAIL, f"completion_tokens={value} exceeds cap {cap}.", compact_json(usage)
    return STATUS_PASS, "", f"completion_tokens={value}"


def validate_thinking_on(resp: HttpResponse) -> Tuple[str, str, str]:
    """F-02a: 默认 thinking 开 -> 响应含 reasoning 内容"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "")
    if reasoning.strip():
        return STATUS_PASS, "", f"reasoning_len={len(reasoning)}"
    return STATUS_FAIL, "response has no reasoning content (thinking not enabled?).", truncate_text(compact_json(message), 300)


def validate_thinking_off(resp: HttpResponse) -> Tuple[str, str, str]:
    """F-02b: reasoning_effort=none -> reasoning 内容为空"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "").strip()
    if reasoning:
        return STATUS_FAIL, "reasoning content is not empty although effort=none.", truncate_text(reasoning, 300)
    return STATUS_PASS, "", "reasoning content empty"


def validate_tool_calls(resp: HttpResponse) -> Tuple[str, str, str]:
    """F-04: tools 触发后响应含 tool_calls"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    tool_calls = message.get("tool_calls")
    if not tool_calls:
        return STATUS_FAIL, "tool_calls not found in response.", truncate_text(compact_json(message), 300)
    try:
        name = ((tool_calls[0] or {}).get("function") or {}).get("name")
    except (IndexError, AttributeError, TypeError):
        name = None
    return STATUS_PASS, "", f"tool_call={name}"


def validate_4xx(resp: HttpResponse) -> Tuple[str, str, str]:
    """F_err: 错误模型名必须 4xx（2xx/5xx/网络失败都算 FAIL）"""
    if 400 <= resp.status < 500:
        return STATUS_PASS, "", f"http={resp.status}"
    return STATUS_FAIL, f"expected 4xx for unknown model, got http={resp.status}.", summarize_body(resp)


def validate_effort_level(resp: HttpResponse) -> Tuple[str, str, str]:
    """F-03 档位: 2xx 且有 reasoning 内容。

    网关存在"非法档位静默接受"的行为（minimal/medium/xhigh/none 全返 200,
    且可能被映射成不思考）——只看 HTTP 码会把这种降级误判成 PASS,
    所以这里额外要求 reasoning 非空, 没有就记 WARN 提示档位可能被静默降级。
    """
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
    reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "")
    if reasoning.strip():
        return STATUS_PASS, "", f"reasoning_len={len(reasoning)}"
    return (STATUS_WARN,
            "no reasoning content: this effort level may be silently downgraded to no-thinking.",
            "reasoning_len=0")


def validate_status_ok(resp: HttpResponse) -> Tuple[str, str, str]:
    """只要 2xx 就过（用于 stop 参数 / effort 矩阵这类"不报错即可"的检查）"""
    return STATUS_PASS, "", f"http={resp.status}"


def validate_effort_known_defect(resp: HttpResponse) -> Tuple[str, str, str]:
    """F-03 medium: 已知缺陷, 记 WARN（不计失败）——但服务异常必须 FAIL。

    400 = 缺陷仍在（部分部署 chat template 校验拒绝 reasoning_effort=medium）；
    200 = 非法档位被静默接受（网关不报错, 需要人工确认该档位是否真的生效）；
    其它状态（5xx/网络异常）= 服务真有问题, 记 FAIL。
    """
    if resp.status == 400:
        return STATUS_WARN, "known defect: reasoning_effort=medium returns 400.", f"http={resp.status}"
    if 200 <= resp.status < 300:
        return (STATUS_WARN,
                "medium is silently accepted (illegal effort level returns 200 without error) "
                "- confirm manually whether the level took effect.",
                f"http={resp.status}")
    return STATUS_FAIL, f"unexpected status {resp.status} (expected 400 known defect or 200 silent accept).", summarize_body(resp, 200)


def run_determinism_case(config: Config, case: Case, save_path: Optional[str] = None) -> ApiCaseResult:
    """F-01f: temperature=0 连发两次, 响应文本应一致（参考项, 不一致记 FAIL）"""
    payload = case.payload or {}
    t0 = time.time()
    r1 = make_request(config, case.method, case.endpoint, payload)
    r2 = make_request(config, case.method, case.endpoint, payload)
    latency = int((time.time() - t0) * 1000)
    if save_path:
        save_body(save_path, r1.body)

    def _text(resp: HttpResponse) -> str:
        try:
            data = resp.json()
        except Exception:
            return ""
        choices = data.get("choices") or []
        if not choices:
            return ""
        first = choices[0] or {}
        return str(first.get("text") or ((first.get("message") or {}).get("content") or ""))

    if not (200 <= r1.status < 300 and 200 <= r2.status < 300):
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_FAIL,
                             r1.status, latency,
                             f"non-2xx response: {r1.status}/{r2.status}", "")
    text1, text2 = _text(r1), _text(r2)
    if not text1 or not text2:
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_FAIL,
                             r1.status, latency, "response text is empty.", "")
    if text1 != text2:
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_FAIL,
                             r1.status, latency, "temperature=0 responses differ.",
                             truncate_text(f"a={text1!r} b={text2!r}", 300))
    return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_PASS,
                          r1.status, latency, "", f"text_len={len(text1)}")


def parse_stream_payload(line: str) -> str:
    line = line.strip()
    if line.startswith("data:"):
        return line[5:].strip()
    return line


def extract_stream_text(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    texts: List[str] = []
    for choice in data.get("choices", []) or []:
        if isinstance(choice, dict):
            delta = choice.get("delta")
            if isinstance(delta, dict):
                texts.append(extract_text_from_content(delta.get("content")))
            texts.append(extract_text_from_content(choice.get("text")))
    collect_key_text(data, keys=("delta", "text"), out=texts)
    return "".join(texts)


def aggregate_stream_events(events: List[Any]) -> Dict[str, Any]:
    """把 SSE 事件聚合成 chat completion 形状的对象。

    这样流式用例可以直接复用非流式那套校验器（content / reasoning /
    tool_calls / usage / finish_reason 都聚合到 message 上）。
    """
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: Dict[int, Dict[str, Any]] = {}
    usage: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None
    for data in events:
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("usage"), dict):
            usage = data["usage"]
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                delta = choice
            text = delta.get("content")
            if isinstance(text, str):
                content_parts.append(text)
            elif isinstance(text, list):
                content_parts.append(extract_text_from_content(text))
            legacy = choice.get("text")
            if isinstance(legacy, str):
                content_parts.append(legacy)
            for key in ("reasoning", "reasoning_content"):
                value = delta.get(key)
                if isinstance(value, str):
                    reasoning_parts.append(value)
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                idx = call.get("index")
                if not isinstance(idx, int):
                    idx = len(tool_calls)
                slot = tool_calls.setdefault(idx, {"id": None, "type": "function",
                                                  "function": {"name": "", "arguments": ""}})
                if call.get("id"):
                    slot["id"] = call["id"]
                fn = call.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if isinstance(fn.get("arguments"), str):
                    slot["function"]["arguments"] += fn["arguments"]
    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if reasoning_parts:
        message["reasoning"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    out: Dict[str, Any] = {
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
    }
    if usage:
        out["usage"] = usage
    return out


def is_done_event(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    if data.get("done") is True:
        return True
    if data.get("type") in {"message_stop", "done", "response.completed"}:
        return True
    return False


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

def save_body(path: Optional[str], body: bytes) -> None:
    if not path:
        return
    try:
        with open(path, "wb") as f:
            f.write(body)
    except OSError as exc:
        print(f"  [警告] 响应落盘失败 {path}: {exc}")


def run_case(
    config: Config,
    case: Case,
    *,
    run_index: int = 1,
    run_total: int = 1,
    save_path: Optional[str] = None,
) -> ApiCaseResult:
    resp = make_request(config, case.method, case.endpoint, case.payload,
                         extra_headers=case.extra_headers)
    status = STATUS_PASS if 200 <= resp.status < 300 else STATUS_FAIL
    issue = "" if status == STATUS_PASS else summarize_body(resp)
    detail = ""
    if case.optional and 400 <= resp.status < 500:
        # 能力探测用例: 端点/特性不被该服务支持 -> WARN（不计失败）
        status = STATUS_WARN
        issue = f"not supported by this service (http={resp.status})."
    if resp.status == 0:
        # 传输层失败（连接拒绝/超时/DNS）: 一律 FAIL, 不交给校验器改判
        status = STATUS_FAIL
        issue = resp.error or "request failed at transport layer"
    elif case.validate is not None and (status == STATUS_PASS or case.always_validate):
        try:
            status, issue, detail = case.validate(resp)
        except Exception as exc:
            status = STATUS_FAIL
            issue = f"validator error: {exc}"
    save_body(save_path, resp.body)

    if config.print_logs:
        print(f"\n[{status}] {case.category}/{case.name} {case.method} {case.endpoint} "
              f"HTTP={resp.status} {resp.latency_ms}ms")
        if case.payload is not None:
            print(truncate_text(compact_json(case.payload), config.log_max_chars))
        print(truncate_text(resp.text(), config.log_max_chars))

    return ApiCaseResult(
        category=case.category,
        name=case.name,
        endpoint=case.endpoint,
        status=status,
        http_status=resp.status,
        latency_ms=resp.latency_ms,
        issue=issue,
        detail=detail,
        run_index=run_index,
        run_total=run_total,
    )


def run_stream_case(
    config: Config,
    case: Case,
    *,
    run_index: int = 1,
    run_total: int = 1,
    save_path: Optional[str] = None,
) -> ApiCaseResult:
    payload = case.payload or {}
    resp, lines, first_event_ms = make_stream_request(
        config, case.endpoint, payload, extra_headers=case.extra_headers)
    status = STATUS_PASS if 200 <= resp.status < 300 else STATUS_FAIL
    issue = "" if status == STATUS_PASS else summarize_body(resp)
    if case.optional and 400 <= resp.status < 500:
        status = STATUS_WARN
        issue = f"not supported by this service (http={resp.status})."
    text_parts: List[str] = []
    events: List[Any] = []
    done = False
    json_events = 0
    if status == STATUS_PASS:
        for line in lines:
            payload_text = parse_stream_payload(line)
            if payload_text == "[DONE]":
                done = True
                continue
            try:
                data = json.loads(payload_text)
            except Exception:
                continue
            json_events += 1
            events.append(data)
            if is_done_event(data):
                done = True
            extractor = case.extractor or extract_stream_text
            text_parts.append(extractor(data))
        aggregated = aggregate_stream_events(events)
        message = (aggregated.get("choices") or [{}])[0].get("message") or {}
        content = str(message.get("content") or "")
        reasoning = str(message.get("reasoning") or "")
        tool_calls = message.get("tool_calls") or []
        if json_events == 0:
            status = STATUS_FAIL
            issue = "stream returned no JSON data events."
        elif not content.strip() and not reasoning.strip() and not tool_calls:
            # 正文/思考/工具调用全空: 流是空的。正文为空但有思考链的情况交给
            # 校验器判（那是 max_tokens 预算问题, 不是协议问题）
            status = STATUS_FAIL
            issue = "stream returned no text deltas."
        if not done and status != STATUS_FAIL:
            # F-01b 沿用旧口径: 必须有 [DONE] 结束标记, 缺了记 FAIL;
            # 框架标准流式用例缺结束标记只记 WARN
            if case.require_done:
                status = STATUS_FAIL
                issue = "stream completed without an explicit [DONE] marker."
            elif status == STATUS_PASS:
                status = STATUS_WARN
                issue = "stream completed without an explicit done marker."
        if case.validate is not None:
            synthetic = HttpResponse(resp.status, compact_json(aggregated).encode("utf-8"),
                                     resp.headers, latency_ms=resp.latency_ms)
            try:
                v_status, v_issue, v_detail = case.validate(synthetic)
            except Exception as exc:
                v_status, v_issue, v_detail = STATUS_FAIL, f"validator error: {exc}", ""
            if severity(v_status) > severity(status):
                status, issue = v_status, v_issue
            elif v_status == STATUS_PASS and status == STATUS_PASS:
                issue = ""
            if v_detail:
                detail_extra = v_detail
            else:
                detail_extra = ""
    else:
        aggregated = {}
        detail_extra = ""

    detail = (
        f"events={json_events}, lines={len(lines)}, ttft_ms={first_event_ms}, "
        f"text_len={len(''.join(text_parts).strip())}"
    )
    if detail_extra:
        detail = f"{detail}; {detail_extra}"
    save_body(save_path, resp.body)
    if config.print_logs:
        print(f"\n[{status}] {case.category}/{case.name} POST {case.endpoint} "
              f"HTTP={resp.status} {resp.latency_ms}ms")
        print(truncate_text("\n".join(lines) or resp.text(), config.log_max_chars))
    return ApiCaseResult(
        case.category, case.name, case.endpoint, status, resp.status, resp.latency_ms,
        issue, detail, run_index, run_total,
    )


# ---------------------------------------------------------------------------
# 用例注册表
# ---------------------------------------------------------------------------

def api_path(config: Config, version: str, suffix: str) -> str:
    """拼 API 路径: base 已含版本段时不再重复加。

    例: base="http://h/v1"（OPENAI_BASE_URL 常见写法）-> "/chat/completions"
        base="http://h/v2/infer/app" -> "/v1/chat/completions"
    """
    suffix = suffix.lstrip("/")
    base = config.base_url.rstrip("/")
    if base.endswith("/" + version):
        return f"/{suffix}"
    return f"/{version}/{suffix}"


def openai_endpoint(config: Config, suffix: str) -> str:
    return api_path(config, config.openai_api_version, suffix)


def messages_endpoint(config: Config) -> str:
    return api_path(config, config.messages_api_version, "messages")


def embedding_endpoint(config: Config) -> str:
    return api_path(config, config.embedding_api_version, "embeddings")


def gemini_model_path(model: str) -> str:
    if model.startswith("models/"):
        return model
    return f"models/{model}"


def gemini_generate_endpoint(config: Config) -> str:
    model = urllib.parse.quote(gemini_model_path(config.model), safe="/")
    return api_path(config, config.gemini_api_version, f"{model}:generateContent")


def gemini_stream_endpoint(config: Config) -> str:
    model = urllib.parse.quote(gemini_model_path(config.model), safe="/")
    return api_path(config, config.gemini_api_version, f"{model}:streamGenerateContent")


def gemini_interactions_endpoint(config: Config) -> str:
    return api_path(config, config.gemini_api_version, "interactions")


def chat_payload(config: Config, messages: List[Dict[str, Any]], max_tokens: int = None,
                 **extra) -> Dict[str, Any]:
    """chat/completions 请求体: max_tokens 走 functional.max_tokens（思考型模型预算）。"""
    payload: Dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "max_tokens": int(max_tokens or config.max_tokens),
        "temperature": 0,
    }
    payload.update(extra)
    return payload


def stream_payload(config: Config, messages: List[Dict[str, Any]], max_tokens: int = None,
                   **extra) -> Dict[str, Any]:
    """流式 chat 请求体。

    ⚠️ 必须流式: MaaS 网关 113.46.2.168 有 60s **首字节**超时——非流式的首字节
    等于整个响应, 思考型模型的长回答必然踩雷(网关返 504)。流式把首字节压到第一个
    token, 实测可跑 1034s（见 accuracy_runner.py build_generation_config 的注释）。
    """
    payload = chat_payload(config, messages, max_tokens, **extra)
    payload["stream"] = True
    return payload


def run_usage_case(config: Config, case: Case, save_path: Optional[str] = None) -> ApiCaseResult:
    """chat usage: 优先流式 + stream_options.include_usage。

    网关若不认 stream_options 会回 4xx, 此时回落到非流式再取 usage。
    """
    payload = dict(case.payload or {})
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    resp, lines, _ = make_stream_request(config, case.endpoint, payload)
    if 200 <= resp.status < 300:
        events: List[Any] = []
        for line in lines:
            text = parse_stream_payload(line)
            if text == "[DONE]":
                continue
            try:
                events.append(json.loads(text))
            except Exception:
                continue
        aggregated = aggregate_stream_events(events)
        if aggregated.get("usage"):
            synthetic = HttpResponse(200, compact_json(aggregated).encode("utf-8"),
                                     resp.headers, latency_ms=resp.latency_ms)
            status, issue, detail = validate_usage_response(synthetic)
            return ApiCaseResult(case.category, case.name, case.endpoint, status, 200,
                                 resp.latency_ms, issue, detail)
    # 回落到非流式
    resp2 = make_request(config, case.method, case.endpoint, dict(case.payload or {}))
    save_body(save_path, resp2.body)
    if not (200 <= resp2.status < 300):
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_FAIL,
                             resp2.status, resp2.latency_ms, summarize_body(resp2), "")
    status, issue, detail = validate_usage_response(resp2)
    return ApiCaseResult(case.category, case.name, case.endpoint, status, resp2.status,
                          resp2.latency_ms, issue, detail)


def build_openai_cases(config: Config) -> List[Case]:
    """OpenAI 兼容用例（含 F-01a/b/c、thinking、effort 矩阵、tool call 等专有检查）。

    chat 类用例一律走流式（见 stream_payload 的网关首字节超时说明）;
    /v1/completions 的三个短用例（截断/stop/确定性）保持非流式——它们
    max_tokens 很小, 响应很快, 历史上该端点非流式一直正常。
    """
    chat_ep = openai_endpoint(config, "chat/completions")
    comp_ep = openai_endpoint(config, "completions")
    cases: List[Case] = [
        Case(
            category="openai/compatibility",
            name="models list",
            endpoint=openai_endpoint(config, "models"),
            method="GET",
            validate=lambda resp: (STATUS_PASS, "", f"bytes={len(resp.body)}"),
            slug="models_list",
        ),
        Case(
            category="openai/compatibility",
            name="chat completions [F-01a]",
            endpoint=chat_ep,
            payload=stream_payload(config, [
                {"role": "user", "content": "Reply with exactly: api-ok"}]),
            stream=True,
            validate=validate_answer_text,
            tags=["F-01a", "F-01a_chat"],
            slug="f01a_chat",
        ),
        Case(
            category="openai/compatibility",
            name="completions [F-01c]",
            endpoint=comp_ep,
            payload={"model": config.model, "prompt": "Reply with api-ok",
                     "max_tokens": 128, "temperature": 0},
            validate=validate_completion_response,
            tags=["F-01c", "F-01c_completions"],
            slug="f01c_completions",
        ),
        Case(
            category="openai/compatibility",
            name="responses",
            endpoint=openai_endpoint(config, "responses"),
            payload={"model": config.model, "input": "Reply with api-ok",
                     "max_output_tokens": 64, "temperature": 0},
            validate=validate_answer_text,
            optional=True,  # 能力探测: 4xx 记 WARN（该服务未实现）, 不计失败
            slug="responses",
        ),
        Case(
            category="openai/streaming",
            name="chat streaming [F-01b]",
            endpoint=chat_ep,
            payload=stream_payload(config, [
                {"role": "user", "content": "Reply with a short sentence."}]),
            stream=True,
            require_done=True,   # 沿用 F-01b 旧口径: 必须有 [DONE] 结束标记
            validate=validate_answer_text,
            tags=["F-01b", "F-01b_stream"],
            slug="f01b_stream",
        ),
        Case(
            category="openai/structured-output",
            name="json object",
            endpoint=chat_ep,
            payload=stream_payload(config, [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user",
                 "content": 'Return {"city":"Shanghai","temperature_c":23,"condition":"clear"}'},
            ], response_format={"type": "json_object"}),
            stream=True,
            validate=validate_json_output(),
            optional=True,      # 能力探测: 网关不支持 response_format 时记 WARN
            slug="json_object",
        ),
        Case(
            category="openai/context",
            name="multi-turn recall",
            endpoint=chat_ep,
            payload=stream_payload(config, [
                {"role": "system", "content": "You recall exact secret tokens."},
                {"role": "user", "content": "Remember this secret token: SIMAAS_OPENAI_SECRET"},
                {"role": "assistant", "content": "I will remember it."},
                {"role": "user", "content": "Return only the secret token."},
            ]),
            stream=True,
            validate=validate_contains_token("SIMAAS_OPENAI_SECRET"),
            slug="multi_turn_recall",
        ),
        Case(
            category="openai/finops",
            name="chat usage",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user", "content": "Say hello in three words."}]),
            runner=run_usage_case,
            slug="chat_usage",
        ),
        # ---- F-01d/e/f robustness（/v1/completions, max_tokens 小 -> 响应快） ----
        Case(
            category="openai/robustness",
            name="F-01d max tokens truncation",
            endpoint=comp_ep,
            payload={"model": config.model, "prompt": "从1数到100", "max_tokens": 8},
            validate=validate_completion_tokens_capped,
            tags=["F-01d", "F-01d_maxtok"],
            slug="f01d_maxtok",
        ),
        Case(
            category="openai/robustness",
            name="F-01e stop parameter",
            endpoint=comp_ep,
            payload={"model": config.model,
                     "prompt": "把单词 banana 重复输出 20 遍，用空格分隔",
                     "max_tokens": 128, "stop": ["banana"]},
            validate=validate_completion_response,
            tags=["F-01e", "F-01e_stop"],
            slug="f01e_stop",
        ),
        Case(
            category="openai/robustness",
            name="F-01f determinism",
            endpoint=comp_ep,
            payload={"model": config.model, "prompt": "1+1等于几", "max_tokens": 16, "temperature": 0},
            tags=["F-01f", "F-01f_determ"],
            slug="f01f_determ",
            runner=run_determinism_case,
        ),
        # ---- F-02 thinking 开关 / F-03 effort 矩阵 / F-04 tool call ----
        Case(
            category="openai/capability",
            name="F-02a thinking on",
            endpoint=chat_ep,
            payload=stream_payload(config, [
                {"role": "user", "content": "12*17=?"}]),
            stream=True,
            validate=validate_thinking_on,
            tags=["F-02a", "F-02a_think_on"],
            slug="f02a_think_on",
        ),
        Case(
            category="openai/capability",
            name="F-02b thinking off",
            endpoint=chat_ep,
            payload=stream_payload(config, [
                {"role": "user", "content": "12*17=?"}], reasoning_effort="none"),
            stream=True,
            validate=validate_thinking_off,
            tags=["F-02b", "F-02b_think_off"],
            slug="f02b_think_off",
        ),
        Case(
            category="openai/capability",
            name="F-04 tool call",
            endpoint=chat_ep,
            payload=stream_payload(config, [
                {"role": "user", "content": "北京今天天气如何?"}],
                tools=[{"type": "function", "function": {
                    "name": "get_weather", "description": "查询城市天气",
                    "parameters": {"type": "object",
                                   "properties": {"city": {"type": "string"}},
                                   "required": ["city"]}}}],
                tool_choice="auto"),
            stream=True,
            validate=validate_tool_calls,
            tags=["F-04", "F-04_toolcall"],
            slug="f04_toolcall",
        ),
        Case(
            category="openai/robustness",
            name="F_err unknown model 4xx",
            endpoint=chat_ep,
            payload={"model": "not-exist-model",
                     "messages": [{"role": "user", "content": "hi"}],
                     "max_tokens": 16},
            validate=validate_4xx,
            tags=["F_err", "F_err_4xx"],
            always_validate=True,
            slug="ferr_4xx",
        ),
        Case(
            category="openai/session",
            name="F-05 claude code session",
            endpoint="(manual)",
            method="-",
            payload=None,
            validate=None,
            tags=["F-05", "F-05_claude"],
            slug="f05_claude",
            skip_always=True,
            skip_reason=f"需人工/CLI 会话, 见 {config.claude_session_hint}",
        ),
    ]

    # F-03 effort 矩阵（config functional.efforts 展开）
    for effort in config.efforts:
        cases.append(Case(
            category="openai/capability",
            name=f"F-03 reasoning_effort={effort}",
            endpoint=chat_ep,
            payload=stream_payload(config, [
                {"role": "user", "content": "1+1=?"}], reasoning_effort=effort),
            stream=True,
            validate=validate_effort_level,
            tags=[f"F-03_{effort}"],
            slug=f"f03_{effort}",
        ))
    # F-03 medium 已知缺陷（400=缺陷仍在 / 200=被静默接受, 都记 WARN; 服务异常记 FAIL）
    cases.append(Case(
        category="openai/capability",
        name="F-03 reasoning_effort=medium (known defect)",
        endpoint=chat_ep,
        payload=stream_payload(config, [
            {"role": "user", "content": "1+1=?"}], reasoning_effort="medium"),
        stream=True,
        validate=validate_effort_known_defect,
        tags=["F-03_medium", "F-03"],
        always_validate=True,
        slug="f03_medium",
    ))

    # 长上下文召回（默认按 config 决定是否跑）
    if config.skip_long_context_api:
        cases.append(Case(
            category="openai/context",
            name="long-context recall",
            endpoint=chat_ep,
            method="GET",
            tags=["long_context"],
            slug="long_context_recall",
            skip_always=True,
            skip_reason="disabled by --skip-long-context-api / config functional.skip_long_context_api",
        ))
    else:
        needle = "SIMAAS_LONG_CONTEXT_NEEDLE"
        filler = " ".join(["context filler"] * max(1, config.long_context_api_tokens // 2))
        cases.append(Case(
            category="openai/context",
            name="long-context recall",
            endpoint=chat_ep,
            payload=stream_payload(config, [
                {"role": "user",
                 "content": f"{filler}\nNeedle: {needle}\n{filler}\nReturn only the needle token."}]),
            stream=True,
            validate=validate_contains_token(needle),
            slug="long_context_recall",
        ))
    return cases


def build_messages_cases(config: Config) -> List[Case]:
    endpoint = messages_endpoint(config)
    base_payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": "Reply with exactly: messages-ok"}],
        "max_tokens": 64,
        "temperature": 0,
    }
    stream_payload = dict(base_payload)
    stream_payload["stream"] = True
    return [
        Case(
            category="messages/compatibility",
            name="messages",
            endpoint=endpoint,
            payload=base_payload,
            validate=lambda resp: validate_non_empty_text(resp, extract_messages_text),
            slug="messages",
        ),
        Case(
            category="messages/streaming",
            name="messages streaming",
            endpoint=endpoint,
            payload=stream_payload,
            stream=True,
            slug="messages_streaming",
        ),
        Case(
            category="messages/capability",
            name="tool use",
            endpoint=endpoint,
            payload={
                "model": config.model,
                "messages": [{"role": "user", "content": "Call the weather tool for Shanghai."}],
                "tools": [
                    {
                        "name": "get_weather",
                        "description": "Get weather.",
                        "input_schema": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    }
                ],
                "tool_choice": {"type": "tool", "name": "get_weather"},
                "max_tokens": 128,
                "temperature": 0,
            },
            validate=lambda resp: validate_non_empty_text(resp, extract_messages_text),
            slug="messages_tool_use",
        ),
        Case(
            category="messages/structured-output",
            name="json output",
            endpoint=endpoint,
            payload={
                "model": config.model,
                "messages": [{"role": "user", "content": 'Return only JSON: {"city":"Shanghai","temperature_c":23}'}],
                "max_tokens": 128,
                "temperature": 0,
            },
            validate=validate_json_output(extract_messages_text),
            slug="messages_json",
        ),
        Case(
            category="messages/context",
            name="context recall",
            endpoint=endpoint,
            payload={
                "model": config.model,
                "messages": [
                    {"role": "user", "content": "Remember this secret token: SIMAAS_MESSAGES_SECRET"},
                    {"role": "assistant", "content": "I will remember it."},
                    {"role": "user", "content": "Return only the secret token."},
                ],
                "max_tokens": 64,
                "temperature": 0,
            },
            validate=validate_contains_token("SIMAAS_MESSAGES_SECRET", extract_messages_text),
            slug="messages_recall",
        ),
        Case(
            category="messages/finops",
            name="usage",
            endpoint=endpoint,
            payload=base_payload,
            validate=validate_usage_response,
            slug="messages_usage",
        ),
    ]


def gemini_payload(model: str, text: str, max_tokens: int = 128) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens},
    }
    lowered = model.lower()
    if "gemini-2.5" in lowered:
        payload["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 0}
    elif "gemini-3" in lowered:
        payload["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
    return payload


def build_gemini_cases(config: Config) -> List[Case]:
    generate_endpoint = gemini_generate_endpoint(config)
    structured = gemini_payload(config.model, 'Return only JSON: {"city":"Shanghai","temperature_c":23}', 128)
    structured["generationConfig"]["responseMimeType"] = "application/json"
    interaction_headers = {}
    if config.gemini_interactions_api_revision:
        interaction_headers["Api-Revision"] = config.gemini_interactions_api_revision
    return [
        Case(
            category="gemini/api",
            name="generateContent",
            endpoint=generate_endpoint,
            payload=gemini_payload(config.model, "Reply with exactly: gemini-ok", 128),
            validate=lambda resp: validate_non_empty_text(resp, extract_gemini_text),
            slug="gemini_generate",
        ),
        Case(
            category="gemini/streaming",
            name="streamGenerateContent",
            endpoint=gemini_stream_endpoint(config),
            payload=gemini_payload(config.model, "Reply with a short sentence.", 128),
            stream=True,
            extractor=extract_gemini_text,
            slug="gemini_stream",
        ),
        Case(
            category="gemini/capability",
            name="function calling",
            endpoint=generate_endpoint,
            payload={
                "contents": [{"role": "user", "parts": [{"text": "Call get_weather for Shanghai."}]}],
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": "get_weather",
                                "description": "Get weather.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"city": {"type": "string"}},
                                    "required": ["city"],
                                },
                            }
                        ]
                    }
                ],
                "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["get_weather"]}},
            },
            validate=lambda resp: validate_non_empty_text(resp, extract_gemini_text),
            slug="gemini_function_call",
        ),
        Case(
            category="gemini/structured-output",
            name="json output",
            endpoint=generate_endpoint,
            payload=structured,
            validate=validate_json_output(extract_gemini_text),
            slug="gemini_json",
        ),
        Case(
            category="gemini/context",
            name="context recall",
            endpoint=generate_endpoint,
            payload=gemini_payload(
                config.model,
                "Remember this secret token: SIMAAS_GEMINI_SECRET\nNow return only the secret token.",
                64,
            ),
            validate=validate_contains_token("SIMAAS_GEMINI_SECRET", extract_gemini_text),
            slug="gemini_recall",
        ),
        Case(
            category="gemini/finops",
            name="usage",
            endpoint=generate_endpoint,
            payload=gemini_payload(config.model, "Say hello in three words.", 64),
            validate=validate_usage_response,
            slug="gemini_usage",
        ),
        Case(
            category="gemini/api",
            name="interactions",
            endpoint=gemini_interactions_endpoint(config),
            payload={
                "model": config.model,
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "Reply with interaction-ok"}]}],
            },
            extra_headers=interaction_headers,
            validate=lambda resp: validate_non_empty_text(resp, extract_gemini_text),
            slug="gemini_interactions",
        ),
    ]


def build_embedding_cases(config: Config) -> List[Case]:
    return [
        Case(
            category="embedding/compatibility",
            name="embeddings",
            endpoint=embedding_endpoint(config),
            payload={"model": config.model, "input": "hello embedding"},
            validate=validate_embedding_response,
            slug="embeddings",
        )
    ]


def build_cases(config: Config) -> List[Case]:
    cases: List[Case] = []
    for interface_type in config.interface_types:
        if interface_type == INTERFACE_OPENAI:
            cases.extend(build_openai_cases(config))
        elif interface_type == INTERFACE_MESSAGES:
            cases.extend(build_messages_cases(config))
        elif interface_type == INTERFACE_GEMINI:
            cases.extend(build_gemini_cases(config))
        elif interface_type == INTERFACE_EMBEDDING:
            cases.extend(build_embedding_cases(config))
    return cases


# ---------------------------------------------------------------------------
# 过滤 / 汇总 / 报告
# ---------------------------------------------------------------------------

def filter_cases(cases: List[Case], only: str) -> Optional[List[Case]]:
    """--only 过滤: 支持 tag（F-01a / F-03 前缀组）/ 用例名子串。

    匹配不到任何用例时打印可用清单并返回 None（调用方退出 2）。
    """
    wanted = [w.strip() for w in only.split(",") if w.strip()]
    selected: set = set()
    unknown: List[str] = []
    for w in wanted:
        hits = [
            idx for idx, c in enumerate(cases)
            if w in c.tags
            or any(t == w or t.startswith(w + "_") for t in c.tags)
            or w.lower() in c.name.lower()
        ]
        if hits:
            selected.update(hits)
        else:
            unknown.append(w)
    if unknown:
        print(f"未知用例: {unknown}\n"
              f"可用 tag (--list 查看): {sorted({t for c in cases for t in c.tags})}", file=sys.stderr)
        return None
    return [c for idx, c in enumerate(cases) if idx in selected]


_SEVERITY = {STATUS_PASS: 0, STATUS_SKIP: 1, STATUS_WARN: 2, STATUS_FAIL: 3}


def severity(status: str) -> int:
    """判定严重程度排序（FAIL > WARN > SKIP > PASS）"""
    return _SEVERITY.get(status, 0)


def status_counts(results: Sequence[ApiCaseResult]) -> Dict[str, int]:
    counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0, STATUS_SKIP: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def format_counts(results: Sequence[ApiCaseResult]) -> str:
    counts = status_counts(results)
    return (
        f"pass={counts.get(STATUS_PASS, 0)}, warn={counts.get(STATUS_WARN, 0)}, "
        f"fail={counts.get(STATUS_FAIL, 0)}, skip={counts.get(STATUS_SKIP, 0)}"
    )


def write_report(config: Config, results: Sequence[ApiCaseResult], path: Optional[Path] = None) -> Path:
    """写 markdown 报告: Summary + Cases 表 + Failures 段"""
    report_path = Path(path or config.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    counts = status_counts(results)
    lines = [
        "# API End-to-End Test Report",
        "",
        "## Summary",
        "",
        f"- Generated at: {dt.datetime.now().isoformat(timespec='seconds')}",
        f"- Base URL: `{config.base_url}`",
        f"- Model: `{config.model}`",
        f"- Interface types: `{','.join(config.interface_types)}`",
        f"- API versions: openai=`{config.openai_api_version}`, messages=`{config.messages_api_version}`, "
        f"gemini=`{config.gemini_api_version}`, embedding=`{config.embedding_api_version}`",
        f"- Capability repeat: {config.capability_repeat}",
        f"- Results: PASS={counts.get(STATUS_PASS, 0)}, WARN={counts.get(STATUS_WARN, 0)}, "
        f"FAIL={counts.get(STATUS_FAIL, 0)}, SKIP={counts.get(STATUS_SKIP, 0)}",
        "",
        "## Cases",
        "",
        "| category | case | status | HTTP | latency_ms | endpoint | detail | issue |",
        "|---|---|---:|---:|---:|---|---|---|",
    ]
    for result in results:
        name = result.name
        if result.run_total > 1:
            name = f"{name} (run {result.run_index}/{result.run_total})"
        lines.append(
            "| "
            + " | ".join(
                [
                    result.category,
                    name,
                    result.status,
                    "" if result.http_status is None else str(result.http_status),
                    "" if result.latency_ms is None else str(result.latency_ms),
                    f"`{result.endpoint}`",
                    result.detail.replace("|", "\\|"),
                    result.issue.replace("\n", " ").replace("|", "\\|"),
                ]
            )
            + " |"
        )
    failures = [result for result in results if result.status == STATUS_FAIL]
    if failures:
        lines.extend(["", "## Failures", ""])
        for result in failures:
            name = result.name
            if result.run_total > 1:
                name = f"{name} (run {result.run_index}/{result.run_total})"
            lines.append(f"- `{result.category}/{name}` HTTP={result.http_status}: {result.issue}")
    warnings = [result for result in results if result.status == STATUS_WARN]
    if warnings:
        lines.extend(["", "## Warnings", ""])
        for result in warnings:
            lines.append(f"- `{result.category}/{result.name}` HTTP={result.http_status}: {result.issue}")
    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


# ---------------------------------------------------------------------------
# 配置装配（config.yaml 为主, CLI/环境变量覆盖）
# ---------------------------------------------------------------------------

def load_kit_config(path: str) -> dict:
    """读 config.yaml; 文件不存在或缺 pyyaml 时返回空 dict（纯 CLI 用法）"""
    if yaml is None or not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, ValueError) as exc:
        print(f"警告: 读取 {path} 失败: {exc}", file=sys.stderr)
        return {}


def headers_from_kit_config(cfg: dict) -> Dict[str, str]:
    """kit config 的请求头 -> dict（Host 开关 / extra_headers / api_key）"""
    headers: Dict[str, str] = {}
    req = cfg.get("request") or {}
    svc = cfg.get("service") or {}
    if req.get("add_host_header"):
        host = req.get("host_header") or ""
        if not host:
            host = urllib.parse.urlsplit(svc.get("url") or "").hostname or ""
        if host:
            headers["Host"] = host
    for item in req.get("extra_headers") or []:
        text = str(item)
        # config.yaml 文档格式是 "Key=Value", 所以 "=" 优先（值里含 ":" 也不会被切错）;
        # 没写 "=" 时才退回 ":" 分隔
        if "=" in text:
            key, _, raw = text.partition("=")
        else:
            key, _, raw = text.partition(":")
        if key.strip():
            headers[key.strip()] = raw.strip()
    return headers


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run standalone Simaas API E2E tests.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help="kit 配置文件（service/request/functional 段）; 缺失时纯用 CLI/环境变量")
    parser.add_argument("--base-url", default="", help="Service base URL. Falls back to BASE_URL or OPENAI_BASE_URL.")
    parser.add_argument("--api-key", default="", help="Bearer token. Falls back to API_KEY or OPENAI_API_KEY.")
    parser.add_argument("--model", default="", help="Model name used by the API tests.")
    parser.add_argument(
        "--interface-types",
        action="append",
        default=[],
        help="Comma-separated or repeatable interface types: openai,messages,gemini,embedding.",
    )
    parser.add_argument(
        "--api-version",
        default="",
        help="Override all API path versions. Defaults: openai/messages/embedding=v1, gemini=v1beta.",
    )
    parser.add_argument(
        "--gemini-interactions-api-revision",
        default="2025-04-21",
        help="Api-Revision header for Gemini interactions. Use empty string to omit.",
    )
    parser.add_argument("--header", action="append", type=parse_header, default=[], help="Extra header, repeatable.")
    parser.add_argument("--timeout", type=int, default=0, help="0 = 用 config functional.timeout_s / 默认 300")
    parser.add_argument("--report", default="", help="Markdown report path.")
    parser.add_argument("--print-logs", action="store_true", help="Print request payloads and responses.")
    parser.add_argument("--log-max-chars", type=int, default=12000)
    parser.add_argument(
        "--insecure",
        "--disable-ssl-verify",
        dest="insecure",
        action="store_true",
        help="Disable HTTPS certificate verification. Use only for test endpoints with self-signed or IP-mismatched certificates.",
    )
    parser.add_argument("--skip-long-context-api", action="store_true", help="Skip OpenAI long-context recall.")
    parser.add_argument("--long-context-api", action="store_true",
                        help="强制启用长上下文召回（覆盖 config functional.skip_long_context_api）")
    parser.add_argument("--long-context-api-tokens", type=int, default=0,
                        help="0 = 用 config functional.long_context_api_tokens / 默认 8192")
    parser.add_argument("--skip-bench", action="store_true",
                        help="本脚本只跑 API 用例, 该参数仅作兼容（跳过 bench 段）")
    parser.add_argument("--capability-repeat", type=int, default=0,
                        help="capability 类用例重复轮数（抖动检测）; 0 = 用 config functional.capability_repeat / 默认 1")
    parser.add_argument("--list", action="store_true", help="列出全部用例后退出")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划, 不发任何请求")
    parser.add_argument("--only", default=None, help="只跑指定用例（逗号分隔, 支持 tag 或前缀组）, 如 F-03,openai")
    # 兼容位: run.sh 用 -f/--foreground 决定前台/nohup 分支后原样透传, 此处仅接收不生效
    parser.add_argument("-f", "--foreground", action="store_true",
                        help="前台运行（实际由 run.sh 处理, 本脚本侧仅为兼容接收）")
    return parser.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    kit = load_kit_config(args.config)
    svc = kit.get("service") or {}
    req = kit.get("request") or {}
    fcfg = kit.get("functional") or {}

    stamp = now_stamp()
    output_dir = Path(fcfg.get("output_dir") or os.path.join("results", "functional"))
    if not output_dir.is_absolute():
        output_dir = Path(KIT_ROOT) / output_dir

    # base_url: CLI > 环境变量 > config service.url 推导
    # 优先级: --base-url > 环境变量 > config service.url
    # 三者都过 base_url_of 归一化: 传完整 chat 路径也不会拼出 /v1/v1/chat/completions
    raw_base = (args.base_url or os.getenv("BASE_URL") or os.getenv("OPENAI_BASE_URL")
                or svc.get("url") or "")
    base_url = normalize_base_url(base_url_of(raw_base))

    model = args.model or svc.get("model") or ""
    if not model:
        raise ValueError("model is required. Use --model or config service.model.")

    api_key = args.api_key or os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY") or req.get("api_key") or ""

    headers = headers_from_kit_config(kit)
    headers.update(dict(args.header))

    interface_types = parse_interface_types(args.interface_types) if args.interface_types else (
        parse_interface_types(fcfg.get("interface_types") or []) if fcfg.get("interface_types")
        else [INTERFACE_OPENAI])

    timeout = args.timeout or int(fcfg.get("timeout_s") or 0) or DEFAULT_TIMEOUT_SECONDS
    report_path = Path(args.report) if args.report else output_dir / f"e2e_{stamp}" / "report.md"

    long_ctx_tokens = args.long_context_api_tokens or int(fcfg.get("long_context_api_tokens") or 0) or 8192
    if args.skip_long_context_api:
        skip_long_ctx = True
    elif args.long_context_api:
        skip_long_ctx = False
    else:
        skip_long_ctx = bool(fcfg.get("skip_long_context_api", True))

    capability_repeat = (args.capability_repeat or int(fcfg.get("capability_repeat") or 0) or 1)
    max_tokens = int(fcfg.get("max_tokens") or 0) or 2048
    efforts = fcfg.get("efforts") or ["low", "high", "xhigh", "max"]

    api_version_explicit = bool(args.api_version.strip())
    if api_version_explicit:
        api_version = normalize_api_version(args.api_version)
        openai_api_version = api_version
        messages_api_version = api_version
        gemini_api_version = api_version
        embedding_api_version = api_version
    else:
        openai_api_version = DEFAULT_API_VERSION
        messages_api_version = DEFAULT_API_VERSION
        gemini_api_version = DEFAULT_GEMINI_API_VERSION
        embedding_api_version = DEFAULT_API_VERSION

    return Config(
        base_url=base_url,
        api_key=api_key,
        model=model,
        interface_types=interface_types,
        openai_api_version=openai_api_version,
        messages_api_version=messages_api_version,
        gemini_api_version=gemini_api_version,
        embedding_api_version=embedding_api_version,
        gemini_interactions_api_revision=args.gemini_interactions_api_revision.strip(),
        headers=headers,
        timeout_seconds=max(1, timeout),
        report_path=report_path,
        print_logs=args.print_logs,
        log_max_chars=max(0, args.log_max_chars),
        verify_ssl=not args.insecure,
        skip_long_context_api=skip_long_ctx,
        long_context_api_tokens=max(512, long_ctx_tokens),
        skip_cases=[str(s).strip() for s in (fcfg.get("skip") or []) if str(s).strip()],
        efforts=[str(e).strip() for e in efforts if str(e).strip()],
        max_tokens=max(1, max_tokens),
        capability_repeat=max(1, capability_repeat),
        skip_bench=bool(args.skip_bench),
        output_dir=output_dir,
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def is_skipped(case: Case, config: Config) -> str:
    """返回跳过原因; 空串 = 不跳过"""
    if getattr(case, "skip_always", False):
        return getattr(case, "skip_reason", "") or "not applicable"
    for want in config.skip_cases:
        if want in case.tags or any(t == want or t.startswith(want + "_") for t in case.tags) \
                or want.lower() in case.name.lower():
            return "config functional.skip"
    return ""


def repeat_count(case: Case, config: Config) -> int:
    """capability 类用例重复 config.capability_repeat 轮"""
    if case.category.endswith(CAPABILITY_SUFFIX):
        return config.capability_repeat
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    try:
        config = build_config(args)
    except Exception as exc:
        print(f"argument error: {exc}", file=sys.stderr)
        return 2

    cases = build_cases(config)
    if args.only:
        filtered = filter_cases(cases, args.only)
        if filtered is None:
            return 2
        cases = filtered

    if args.list:
        print(f"base_url={config.base_url}  model={config.model}  "
              f"interfaces={','.join(config.interface_types)}")
        for case in cases:
            skip = is_skipped(case, config)
            mark = "[skip] " if skip else "       "
            times = repeat_count(case, config)
            repeat_mark = f"  x{times}" if times > 1 else ""
            print(f"  {mark}{case.category:<24} {case.name}{repeat_mark}"
                  + (f"  ({skip})" if skip else ""))
        print(f"\n共 {len(cases)} 条用例")
        return 0

    if args.dry_run:
        print(f"[api-e2e] base_url={config.base_url}")
        print(f"[api-e2e] model={config.model}")
        print(f"[api-e2e] interface_types={','.join(config.interface_types)}")
        print(f"[api-e2e] report={config.report_path}")
        print("[api-e2e] 检查计划（dry-run 不发请求）:")
        for case in cases:
            skip = is_skipped(case, config)
            times = repeat_count(case, config)
            print(f"  {'[skip]' if skip else '     '} {case.category}/{case.name}"
                  f"  {case.method} {case.endpoint}"
                  + (f"  x{times}" if times > 1 else "")
                  + (f"  ({skip})" if skip else ""))
        return 0

    print(f"[api-e2e] base_url={config.base_url}")
    print(f"[api-e2e] model={config.model}")
    print(f"[api-e2e] interface_types={','.join(config.interface_types)}")
    print(f"[api-e2e] report={config.report_path}")
    if config.skip_bench:
        print("[api-e2e] --skip-bench: 本脚本只跑 API 用例, 已跳过 bench 段")
    if config.capability_repeat > 1:
        print(f"[api-e2e] capability 用例重复 {config.capability_repeat} 轮（抖动检测）")

    results: List[ApiCaseResult] = []
    t0 = time.time()
    out_dir = config.report_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    total_runs = sum(repeat_count(c, config) for c in cases)
    done_runs = 0
    for case in cases:
        skip = is_skipped(case, config)
        if skip:
            results.append(ApiCaseResult(case.category, case.name, case.endpoint,
                                         STATUS_SKIP, None, None, skip, ""))
            print(f"[SKIP] {case.category}/{case.name}  ({skip})")
            continue
        times = repeat_count(case, config)
        for i in range(1, times + 1):
            done_runs += 1
            print(f"\n>>> [{done_runs}/{total_runs}] {case.category}/{case.name}"
                  + (f" (run {i}/{times})" if times > 1 else ""))
            slug = case.slug or case.name.lower().replace(" ", "_")
            save_path = str(out_dir / (f"{slug}.r{i}.json" if times > 1 else f"{slug}.json"))
            try:
                if case.runner is not None:
                    result = case.runner(config, case, save_path)
                    result.run_index, result.run_total = i, times
                elif case.stream:
                    result = run_stream_case(config, case, run_index=i, run_total=times,
                                             save_path=save_path)
                else:
                    result = run_case(config, case, run_index=i, run_total=times,
                                      save_path=save_path)
            except Exception as exc:  # 单条异常不中断整轮, 记 FAIL
                result = ApiCaseResult(case.category, case.name, case.endpoint, STATUS_FAIL,
                                       None, None, f"case execution error: {exc}", "",
                                       i, times)
            results.append(result)
            print(f"[{result.status}] {case.category}/{case.name} "
                  f"HTTP={result.http_status} {result.latency_ms}ms"
                  + (f"  {result.issue}" if result.issue else "")
                  + (f"  ({result.detail})" if result.detail else ""))

    report = write_report(config, results)
    print(f"\n[api-e2e] {format_counts(results)}")
    print(f"[api-e2e] wrote report: {report}")
    print(f"[api-e2e] 耗时 {time.time() - t0:.1f} 秒")
    counts = status_counts(results)
    if counts.get(STATUS_FAIL):
        print(f"[api-e2e] 排查: {report.parent}/ 下的 report.md 与各用例响应原文")
    return 1 if counts.get(STATUS_FAIL) else 0


if __name__ == "__main__":
    raise SystemExit(main())
