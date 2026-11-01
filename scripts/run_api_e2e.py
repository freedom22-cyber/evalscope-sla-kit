#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simaas 风格 API E2E 功能测试（evalscope-sla-kit 统一框架版）。

对被测服务跑一组 API 端到端检查，覆盖四种接口协议族:

    openai     OpenAI 兼容  (/v1/models, /v1/chat/completions, /v1/completions,
                             /v1/responses, 结构化输出, 多轮/长上下文召回, usage)
    messages   Anthropic Messages 风格 (/v1/messages, tool use, JSON 输出)
    gemini     Gemini 风格 (:generateContent, function calling, JSON 输出)
    embedding  /v1/embeddings

**全部非流式**（2026-10-31 按需求移除流式: C17/C20/C46 三个流式用例删除,
编号保留空洞不重排; 流式/SSE 能力由性能线覆盖, 功能线不再发任何
stream:true 请求, 也不解析 SSE）。

判定口径统一为 PASS / FAIL / WARN / SKIP:

    PASS  正常
    FAIL  失败（计入退出码）
    WARN  已知缺陷 / 可接受异常（如 reasoning_effort=medium 的 400 已知缺陷、
          可选端点未实现）——不计失败
    SKIP  主动跳过（config functional.skip / 该协议族未启用）

用例编号统一为单一 C 编号（用户给定 C01-C50 矩阵为权威基准）:

    - C01-C41 与矩阵一一对应; C17/C20/C46 为流式用例空洞（已移除, 不重排）。
    - C42-C50 是同一批检查的原始描述, 检查逻辑并入对应编号实现
      （C42→C01, C43→C02, C44→C03, C45→C04, C47→C22, C48→C35,
       C49→C39, C50→C37; 不再保留旧编号别名 tag）。
    - 旧 Simaas F 编号全部退役: F-01a/b/c（流式冒烟）随 C20 一并删除, F-04 即
      C07, F_err 并入 C41 负向矩阵; F 别名与兼容 skip 已移除。F 矩阵未覆盖的
      专有检查（max_tokens 截断 / stop / 确定性 / thinking 开关 / effort 矩阵 /
      claude code 会话）续编在矩阵之后: C51-C61（C56 起为 effort 矩阵,
      档位来自 functional.efforts, medium 紧随其后, 会话最后）。

用法:
    python3 scripts/run_api_e2e.py --list             # 列出全部用例（五列, 按编号排序）
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

# capability 类用例（category 以 /capability 结尾）受 --capability-repeat 影响:
# 重复跑 N 轮用于抖动检测——单次能过不代表稳定。
CAPABILITY_SUFFIX = "/capability"

# C27-C32 思考泄漏判定: 可见正文里出现 think 标签即视为思考泄漏到 content
THINK_TAG_RE = re.compile(r"<think>|</think>|<thinking>|</thinking>|<!DOCTYPE think|^\s*think[:：]",
                          re.IGNORECASE)

# C30 矩阵的顶层 reasoning_effort 档位（7 档 + 缺省 = 8 态, x2 无/有 tool = 16 请求）
THINKING_EFFORTS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
# C32 矩阵的 responses reasoning.effort 档位（4 档 + 缺省 = 5 态, x2 = 10 请求）
RESPONSES_EFFORTS = ["minimal", "low", "medium", "high"]
# C33 思考深度探针档位（每档 3 次取中位, 断言深度单调递增）
DEPTH_EFFORTS = ["low", "high", "max"]
# C09/C19 web_search 工具仅适用于 gpt 系模型（其它模型名直接 SKIP）
WEB_SEARCH_MODEL_MARK = "gpt"

# responses 协议族用例的统一 tag（--skip-responses-api / config 门控按它整族跳过）
RESPONSES_API_TAG = "responses_api"
# C33 思考深度探针的 gate tag（默认关, --thinking-depth-check / config 开启）
THINKING_DEPTH_TAG = "thinking_depth"

# 工具定义（C07/C27/C30/C31 等 chat 族工具用例共用, Responses API 用 responses_tool()）
WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "查询指定城市的当前天气",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名"}},
            "required": ["city"],
        },
    },
}


def responses_tool() -> Dict[str, Any]:
    """Responses API 的 function 工具（扁平结构, 与 chat 的嵌套结构不同）。"""
    return {
        "type": "function",
        "name": "get_weather",
        "description": "查询指定城市的当前天气",
        "parameters": WEATHER_TOOL["function"]["parameters"],
    }


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
    # ---- C 矩阵扩展（全部带默认值, 兼容 webapp.py 等外部按旧字段表构造 Config） ----
    # /v1/responses 协议族用例整族开关（C04/C06/C08/C09/C11/C14/C16/C18/C19/C24/C28/C29/C32, 共 13 条）
    skip_responses_api: bool = False
    # C33 思考深度探针（3 档 x3 次共 9 请求, 默认关）
    thinking_depth_check: bool = False
    # service.max_context_length（C38 超长输入按它的 1.1 倍构造; 0 = 未配置 -> C38 跳过）
    max_context_length: int = 0


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

    number 是唯一 C 编号（C01-C61, 流式空洞 C17/C20/C46 不占用）;
    family / group / name / endpoint 与用户给定编号矩阵逐字段对齐,
    --list 据此打印五列; optional=True 的用例在端点未实现（404/405）时记
    WARN 而非 FAIL; runner 非空时由自定义执行函数接管（如需要连发两次的
    确定性检查）。tags 只登记 number 与门控 tag（responses_api /
    long_context / thinking_depth）, 不再有旧编号别名。
    stream 字段仅为兼容 webapp.py /api/functional/cases 的旧字段读取,
    功能线已全部非流式, 恒为 False。
    """
    category: str
    name: str
    endpoint: str
    method: str = "POST"
    payload: Optional[Dict[str, Any]] = None
    validate: Optional[Callable[[HttpResponse], Tuple[str, str, str]]] = None
    extra_headers: Optional[Dict[str, str]] = None
    stream: bool = False
    number: str = ""
    family: str = ""
    group: str = ""
    tags: List[str] = field(default_factory=list)
    optional: bool = False
    always_validate: bool = False
    slug: str = ""
    skip_always: bool = False
    skip_reason: str = ""
    runner: Optional[Callable[["Config", "Case", Optional[str]], ApiCaseResult]] = None


def case_sort_key(case: Case) -> Tuple[int, str]:
    """--list / 展示排序键: C 编号数值升序（无编号的兜底排最后）。"""
    if case.number and case.number[1:].isdigit():
        return int(case.number[1:]), case.name
    return 9999, case.name


def _display_width(text: str) -> int:
    """CJK 字符按 2 列计的显示宽度（--list 列对齐用）。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(1, width - _display_width(text))


def _case_family(case: Case) -> str:
    """--list 接口族列: 优先 Case.family, 未填的从 category 前缀兜底。"""
    return case.family or case.category.split("/")[0]


def _case_group(case: Case) -> str:
    """--list 分组列: 优先 Case.group, 未填的从 category 后缀兜底。"""
    if case.group:
        return case.group
    _, _, tail = case.category.partition("/")
    return tail


def _case_endpoint_label(case: Case) -> str:
    """--list Endpoint 列: '方法 路径'（去掉 base 域名, 与矩阵表一致）。"""
    endpoint = case.endpoint or "-"
    if endpoint.startswith(("http://", "https://")):
        path = urllib.parse.urlparse(endpoint).path
        return f"{case.method} {path}"
    if case.method in ("", "-", "GET") and "/" not in endpoint:
        return endpoint          # 非常规端点（如 "(manual)"）只显示原样
    return f"{case.method} {endpoint}".strip()


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


def request_json(
    config: Config,
    endpoint: str,
    payload: Dict[str, Any],
) -> Tuple[HttpResponse, Dict[str, Any]]:
    """非流式 JSON 请求 -> (resp, 解析后的响应 dict; 非 2xx / 解析失败时为 {})。

    矩阵类 runner（C05/C06/C10/C13/C14/C26/C30-C33 等）共用的统一入口,
    功能线全部非流式, 不再解析 SSE。
    """
    resp = make_request(config, "POST", endpoint, payload)
    if not (200 <= resp.status < 300):
        return resp, {}
    try:
        data = resp.json()
    except Exception:
        return resp, {}
    return resp, data if isinstance(data, dict) else {}


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
            # reasoning item 的 text 是思考链, 不是可见正文——拼进来会污染 JSON 校验
            # （2026-10-31 实锤: dsv41 responses 的 C24, reasoning + 正文拼接后不可解析）。
            if item.get("type") == "reasoning":
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


# ---- 专有检查校验器（旧 Simaas F 检查迁移, 现挂 C51-C61 / C41）---------------

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
    """C51: max_tokens=8 截断后 usage.completion_tokens <= 8（解析不到记 FAIL）"""
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
    """C54: 默认 thinking 开 -> 响应含 reasoning 内容"""
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
    """C55: reasoning_effort=none -> reasoning 内容为空"""
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
    """C07: tools 触发后响应含 tool_calls"""
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
    """C41 负向矩阵之一: 错误模型名必须 4xx（2xx/5xx/网络失败都算 FAIL）"""
    if 400 <= resp.status < 500:
        return STATUS_PASS, "", f"http={resp.status}"
    return STATUS_FAIL, f"expected 4xx for unknown model, got http={resp.status}.", summarize_body(resp)


def validate_effort_level(resp: HttpResponse) -> Tuple[str, str, str]:
    """C56 起档位用例: 2xx 且有 reasoning 内容。

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
    """C60 medium: 已知缺陷, 记 WARN（不计失败）——但服务异常必须 FAIL。

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


# ---------------------------------------------------------------------------
# C 矩阵判定函数（纯逻辑, 供单测覆盖; 不发请求）
# ---------------------------------------------------------------------------

def judge_param_probe(status: int) -> Tuple[str, str]:
    """C05/C06 参数探测: 2xx=PASS; 4xx=WARN(该参数不被支持, 不计失败); 5xx/传输层=FAIL。"""
    if 200 <= status < 300:
        return STATUS_PASS, ""
    if 400 <= status < 500:
        return STATUS_WARN, f"parameter rejected (http={status})."
    return STATUS_FAIL, f"server error (http={status})."


def _cached_tokens_of(usage: Optional[Dict[str, Any]]) -> Optional[int]:
    """usage -> KV cache 命中 token 数（chat: prompt_tokens_details.cached_tokens;
    responses: input_tokens_details.cached_tokens; 兼容 cached_tokens 顶层字段）。"""
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(key)
        if isinstance(details, dict) and isinstance(details.get("cached_tokens"), int):
            return details["cached_tokens"]
    value = usage.get("cached_tokens")
    return value if isinstance(value, int) else None


def judge_kv_cache_second(usage: Optional[Dict[str, Any]]) -> Tuple[str, str, str]:
    """C13/C14: 第二发同前缀请求的 usage 应报告 cached_tokens>0。

    前缀缓存未开 / 网关多副本路由导致未命中都记 WARN（可选能力, 不计失败）。
    """
    cached = _cached_tokens_of(usage)
    if cached is None:
        return STATUS_WARN, "usage has no cached_tokens field (KV cache report not implemented).", "usage=no cached_tokens"
    if cached > 0:
        return STATUS_PASS, "", f"cached_tokens={cached}"
    return STATUS_WARN, "cached_tokens=0 (prefix cache missed or disabled).", "cached_tokens=0"


def judge_negative_status(status: int) -> Tuple[str, str, str]:
    """C41 负向: 非法输入必须 4xx; 2xx（静默接受）与 5xx 都记 FAIL。"""
    if 400 <= status < 500:
        return STATUS_PASS, "", f"http={status}"
    if 200 <= status < 300:
        return STATUS_FAIL, "invalid request was accepted with 2xx.", f"http={status}"
    return STATUS_FAIL, f"expected 4xx, got http={status}.", ""


def judge_over_context_status(status: int) -> Tuple[str, str, str]:
    """C38 超长输入: 4xx=正确拒绝; 2xx=未拒绝(服务端截断或支持更长上下文, WARN)。"""
    if 400 <= status < 500:
        return STATUS_PASS, "over-context input correctly rejected.", f"http={status}"
    if 200 <= status < 300:
        return (STATUS_WARN,
                "over-context input was accepted (server may truncate silently or support a longer context).",
                f"http={status}")
    return STATUS_FAIL, f"unexpected status {status} for over-context input.", ""


def check_tool_call_structure(tool_calls: Any, finish_reason: Optional[str] = None) -> Tuple[str, str, str]:
    """tool_call 结构自校（C27/C30/C31）: id/name 非空、arguments 是合法 JSON。

    流式截断（finish_reason=length）时 arguments 可能不完整 -> 降 WARN。
    """
    if not isinstance(tool_calls, list) or not tool_calls:
        return STATUS_FAIL, "tool_calls is empty.", ""
    truncated = finish_reason == "length"
    for call in tool_calls:
        if not isinstance(call, dict):
            return STATUS_FAIL, "tool_call entry is not an object.", ""
        fn = call.get("function") or {}
        if not str(call.get("id") or "").strip():
            return STATUS_FAIL, "tool_call.id is missing.", compact_json(call)
        if not str(fn.get("name") or "").strip():
            return STATUS_FAIL, "tool_call.function.name is missing.", compact_json(call)
        args = fn.get("arguments")
        if isinstance(args, dict):
            continue
        try:
            json.loads(str(args or ""))
        except Exception:
            if truncated:
                return STATUS_WARN, "tool_call.arguments is not complete JSON (stream truncated by length).", ""
            return STATUS_FAIL, "tool_call.arguments is not valid JSON.", str(args)
    return STATUS_PASS, "", f"tool_calls={len(tool_calls)}"


def judge_thinking_leak_chat(message: Dict[str, Any], finish_reason: Optional[str] = None) -> Tuple[str, str, str]:
    """C27/C30/C31 思考泄漏判定（chat 口径）。

    - 可见 message.content 含 think 标签 -> FAIL（思考泄漏到正文）
    - tool_call 轮 content 可空（结构自校）
    - 非 tool_call 轮 content 空: 截断/思考吃光预算 -> WARN; 否则 FAIL
    - 思考本身应走 reasoning / reasoning_content 字段（detail 里报告长度）
    """
    content = str(message.get("content") or "")
    reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "")
    tool_calls = message.get("tool_calls") or []
    if THINK_TAG_RE.search(content):
        return STATUS_FAIL, "think tag leaked into visible message content.", truncate_text(content, 200)
    if tool_calls:
        return check_tool_call_structure(tool_calls, finish_reason)
    if content.strip():
        detail = f"text_len={len(content)}"
        if reasoning.strip():
            detail += f", reasoning_len={len(reasoning)}"
        return STATUS_PASS, "", detail
    if finish_reason == "length" or reasoning.strip():
        return (STATUS_WARN,
                "answer text is empty: truncated or the reasoning chain consumed the whole "
                "max_tokens budget (thinking should go to reasoning_content).",
                f"finish_reason={finish_reason}, reasoning_len={len(reasoning)}")
    return STATUS_FAIL, "no visible content and no reasoning (non tool-call turn).", ""


def reasoning_direction_issue(effort: Optional[str], has_reasoning: bool) -> str:
    """C30 方向校验: effort=none 期望无思考, 其它档期望有思考; 方向不符返回 WARN 理由。

    缺省（effort=None）不判。返回空串 = 无问题（或仅降 WARN 提示, 不升 FAIL）。
    """
    if effort is None:
        return ""
    if effort == "none" and has_reasoning:
        return "effort=none but reasoning content present (level may be ignored)."
    if effort != "none" and not has_reasoning:
        return f"effort={effort} produced no reasoning (level may be silently downgraded)."
    return ""


def responses_output_items(data: Any) -> List[Dict[str, Any]]:
    """responses 响应 -> output item 列表（非流式完整对象或聚合产物）。"""
    if not isinstance(data, dict):
        return []
    items = data.get("output")
    if isinstance(items, list):
        return [i for i in items if isinstance(i, dict)]
    return []


def responses_text_of(data: Any) -> str:
    """responses 可见正文: output_text 优先, 否则拼 message item 的 content[].text。"""
    if not isinstance(data, dict):
        return ""
    text = data.get("output_text")
    if isinstance(text, str) and text.strip():
        return text
    parts: List[str] = []
    for item in responses_output_items(data):
        if item.get("type") in ("message", "text"):
            for content in item.get("content") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    parts.append(content["text"])
    return "".join(parts)


def responses_reasoning_len(data: Any) -> int:
    """responses 思考量: 独立 reasoning item 的 summary/text/content 总长（思考应在独立 item 里）。"""
    total = 0
    for item in responses_output_items(data):
        if item.get("type") == "reasoning":
            for content in item.get("summary") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    total += len(content["text"])
            # vLLM 系服务（如 dsv41）把思考放 content[].text(type=reasoning_text), summary 为空
            for content in item.get("content") or []:
                if isinstance(content, dict) and isinstance(content.get("text"), str):
                    total += len(content["text"])
            text = item.get("text")
            if isinstance(text, str):
                total += len(text)
    return total


def responses_function_calls(data: Any) -> List[Dict[str, Any]]:
    return [i for i in responses_output_items(data) if i.get("type") == "function_call"]


def judge_thinking_leak_responses(data: Any) -> Tuple[str, str, str]:
    """C28/C29/C32 思考泄漏判定（responses 口径）。

    - 可见 text 含 think 标签 -> FAIL
    - function_call 轮 text 可空（结构自校: name + arguments 合法 JSON）
    - 非 function_call 轮 text 空: 有独立 reasoning item / incomplete -> WARN; 否则 FAIL
    """
    text = responses_text_of(data)
    reasoning_len = responses_reasoning_len(data)
    calls = responses_function_calls(data)
    incomplete = bool(isinstance(data, dict) and (data.get("incomplete_details") or data.get("status") == "incomplete"))
    if THINK_TAG_RE.search(text):
        return STATUS_FAIL, "think tag leaked into visible response text.", truncate_text(text, 200)
    if calls:
        for call in calls:
            if not str(call.get("name") or "").strip():
                return STATUS_FAIL, "function_call.name is missing.", compact_json(call)
            try:
                json.loads(str(call.get("arguments") or ""))
            except Exception:
                if incomplete:
                    return STATUS_WARN, "function_call.arguments incomplete (response incomplete).", ""
                return STATUS_FAIL, "function_call.arguments is not valid JSON.", str(call.get("arguments"))
        return STATUS_PASS, "", f"function_calls={len(calls)}"
    if text.strip():
        detail = f"text_len={len(text)}"
        if reasoning_len:
            detail += f", reasoning_len={reasoning_len}"
        return STATUS_PASS, "", detail
    if incomplete or reasoning_len:
        return (STATUS_WARN,
                "visible text is empty: response incomplete or thinking consumed the whole "
                "token budget (thinking should be a separate reasoning output item).",
                f"reasoning_len={reasoning_len}, incomplete={incomplete}")
    return STATUS_FAIL, "no visible text and no reasoning output item.", ""


def validate_thinking_leak_chat(resp: HttpResponse) -> Tuple[str, str, str]:
    """C27 用例校验器（流式聚合后的 chat 响应）。"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    choices = data.get("choices") or [{}]
    message = (choices[0] or {}).get("message") or {}
    finish = (choices[0] or {}).get("finish_reason")
    return judge_thinking_leak_chat(message, finish)


def validate_thinking_leak_responses(resp: HttpResponse) -> Tuple[str, str, str]:
    """C28/C29 用例校验器（responses 聚合响应）。"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    return judge_thinking_leak_responses(data)


def run_determinism_case(config: Config, case: Case, save_path: Optional[str] = None) -> ApiCaseResult:
    """C53: temperature=0 连发两次, 响应文本应一致（参考项, 不一致记 FAIL）"""
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


# ---------------------------------------------------------------------------
# C 矩阵请求变体构造（纯逻辑, 供单测覆盖）
# ---------------------------------------------------------------------------

def build_chat_param_probe_variants() -> List[Tuple[str, Dict[str, Any]]]:
    """C05 逐参数隔离探测变体: (label, 追加到 base 的参数)。

    max_completion_tokens 与 max_tokens 在 OpenAI 协议里互斥, 所以该项自身不带
    max_tokens（隔离探测: 每个变体只引入一个待测参数, base 提供其余公共字段）。
    """
    return [
        ("temperature", {"temperature": 0.7}),
        ("top_p", {"top_p": 0.9}),
        ("top_k", {"top_k": 20}),
        ("max_tokens", {"max_tokens": 32}),
        ("max_completion_tokens", {"max_completion_tokens": 32, "max_tokens": None}),
        ("frequency_penalty", {"frequency_penalty": 1.5}),
        ("combined", {"temperature": 0.7, "top_p": 0.9, "frequency_penalty": 0.5}),
    ]


def build_responses_param_probe_variants() -> List[Tuple[str, Dict[str, Any]]]:
    """C06 responses 逐参数隔离探测变体。"""
    return [
        ("temperature", {"temperature": 0.7}),
        ("top_p", {"top_p": 0.9}),
        ("top_k", {"top_k": 20}),
        ("max_output_tokens", {"max_output_tokens": 16}),
        ("frequency_penalty", {"frequency_penalty": 1.5}),
        ("combined", {"temperature": 0.7, "top_p": 0.9, "frequency_penalty": 0.5}),
    ]


def build_thinking_effort_matrix() -> List[Tuple[str, Optional[str], bool]]:
    """C30: 缺省 + 顶层 reasoning_effort 7 档 x {无 tool, 有 tool} = 16 变体。

    返回 (label, effort, with_tools)。
    """
    variants: List[Tuple[str, Optional[str], bool]] = []
    for with_tools in (False, True):
        suffix = "+tools" if with_tools else ""
        variants.append((f"default{suffix}", None, with_tools))
        for effort in THINKING_EFFORTS:
            variants.append((f"effort={effort}{suffix}", effort, with_tools))
    return variants


def build_thinking_toggle_variants() -> List[Tuple[str, Dict[str, Any]]]:
    """C31: 三种思考开关形式 x on/off, 仅带 tool（无 tool 侧由 C26 结构化+思考覆盖）。

    返回 (label, 顶层追加参数)。
    """
    return [
        ("enable_thinking=true", {"enable_thinking": True}),
        ("enable_thinking=false", {"enable_thinking": False}),
        ("thinking.type=enabled", {"thinking": {"type": "enabled"}}),
        ("thinking.type=disabled", {"thinking": {"type": "disabled"}}),
        ("ctk.thinking=true", {"chat_template_kwargs": {"thinking": True}}),
        ("ctk.thinking=false", {"chat_template_kwargs": {"thinking": False}}),
    ]


def build_responses_effort_matrix() -> List[Tuple[str, Optional[str], bool]]:
    """C32: 缺省 + reasoning.effort 4 档 x {无 tool, 有 tool} = 10 变体。"""
    variants: List[Tuple[str, Optional[str], bool]] = []
    for with_tools in (False, True):
        suffix = "+tools" if with_tools else ""
        variants.append((f"default{suffix}", None, with_tools))
        for effort in RESPONSES_EFFORTS:
            variants.append((f"effort={effort}{suffix}", effort, with_tools))
    return variants


def build_structured_thinking_variants() -> List[Tuple[str, Dict[str, Any]]]:
    """C26: json_object 结构化输出 x 思考关闭/effort 变体（无 tool 场景）。"""
    return [
        ("json_object+ctk_thinking_off", {"chat_template_kwargs": {"thinking": False}}),
        ("json_object+effort_none", {"reasoning_effort": "none"}),
        ("json_object+effort_low", {"reasoning_effort": "low"}),
    ]


def build_negative_variants(model: str) -> List[Tuple[str, Dict[str, Any]]]:
    """C41 负向矩阵: invalid model / messages 类型错 / 数值参数类型错 / 畸形 tool schema。

    返回 (label, 完整 payload)（含 C41 的 invalid model 负向变体）。
    """
    messages = [{"role": "user", "content": "hi"}]
    return [
        ("invalid_model",
         {"model": "not-exist-model", "messages": messages, "max_tokens": 16}),
        ("invalid_messages_type",
         {"model": model, "messages": "not-a-list", "max_tokens": 16}),
        ("invalid_numeric_param",
         {"model": model, "messages": messages, "temperature": "not-a-number", "max_tokens": 16}),
        ("malformed_tool_schema",
         {"model": model, "messages": messages, "max_tokens": 128,
          "tools": [{"type": "function", "function": {
              "name": "broken_tool", "parameters": {"type": "invalid-type"}}}]}),
    ]


def build_minimal_mp4() -> bytes:
    """构造自包含的最小 mp4 探测载荷（确定性, 无外部依赖）。

    C15 是"端点是否接受 video content"的能力探测, 不是解码测试: 载荷只需是
    结构合法的 mp4（ftyp/free/mdat 盒子）即可。真支持视频解码的服务可能因无
    可解码帧而拒绝 -> 同样记 WARN（该服务上视频能力不可用）, 与类型不支持同判。
    """
    def box(kind: bytes, payload: bytes) -> bytes:
        size = 8 + len(payload)
        return size.to_bytes(4, "big") + kind + payload

    ftyp = box(b"ftyp", b"isom" + (0).to_bytes(4, "big") + b"isomiso2mp41")
    free = box(b"free", b"evalscope-sla-kit c15 probe")
    # mdat: 确定性填充（无随机性, 结果可复现）
    filler = bytes(range(256)) * 1
    mdat = box(b"mdat", filler)
    return ftyp + free + mdat


def build_video_content_part() -> Dict[str, Any]:
    """C15 视频消息的 content part（data URL 内嵌, 自包含可复现）。"""
    import base64
    b64 = base64.b64encode(build_minimal_mp4()).decode("ascii")
    return {
        "type": "video_url",
        "video_url": {"url": f"data:video/mp4;base64,{b64}"},
    }


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


def run_usage_case(config: Config, case: Case, save_path: Optional[str] = None) -> ApiCaseResult:
    """chat usage: 非流式请求, 断言 usage 字段存在且 prompt+completion=total。"""
    resp = make_request(config, case.method, case.endpoint, dict(case.payload or {}))
    save_body(save_path, resp.body)
    if not (200 <= resp.status < 300):
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_FAIL,
                             resp.status, resp.latency_ms, summarize_body(resp), "")
    status, issue, detail = validate_usage_response(resp)
    return ApiCaseResult(case.category, case.name, case.endpoint, status, resp.status,
                         resp.latency_ms, issue, detail)


# ---------------------------------------------------------------------------
# C 矩阵 runner：多请求变体 / SDK / KV cache / 负向矩阵 等
# ---------------------------------------------------------------------------

# C23/C24/C25 共用的结构化输出 schema（city/temperature_c 两字段）
WEATHER_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "city": {"type": "string"},
        "temperature_c": {"type": "number"},
    },
    "required": ["city", "temperature_c"],
    "additionalProperties": False,
}


def validate_json_schema_output(resp: HttpResponse) -> Tuple[str, str, str]:
    """C23/C25: 结构化输出应产出含 required 字段的 JSON 对象。"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    text = extract_response_text(data)
    parsed, issue = parse_json_from_text(text)
    if parsed is None:
        return STATUS_FAIL, f"model output is not parseable JSON: {issue}", truncate_text(text, 300)
    if isinstance(parsed, dict) and "city" in parsed and "temperature_c" in parsed:
        return STATUS_PASS, "", f"json_keys={','.join(sorted(parsed.keys()))}"
    return STATUS_FAIL, "JSON object is missing required fields (city/temperature_c).", truncate_text(text, 200)


def validate_responses_answer(resp: HttpResponse) -> Tuple[str, str, str]:
    """C11/C24: responses 正文非空（思考吃光预算 -> WARN, 同 chat 口径）。"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    text = responses_text_of(data)
    if text.strip():
        return STATUS_PASS, "", f"text_len={len(text)}"
    if responses_reasoning_len(data) or responses_function_calls(data):
        return STATUS_WARN, "visible text is empty (reasoning/tool call consumed the budget).", ""
    return STATUS_FAIL, "response has no visible text.", summarize_body(resp)


def validate_responses_function_call(resp: HttpResponse) -> Tuple[str, str, str]:
    """C08: responses tool calling 应产出 function_call output item 且 arguments 合法。"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    calls = responses_function_calls(data)
    if not calls:
        return STATUS_FAIL, "no function_call output item in response.", summarize_body(resp, 300)
    for call in calls:
        try:
            json.loads(str(call.get("arguments") or ""))
        except Exception:
            return STATUS_FAIL, "function_call.arguments is not valid JSON.", str(call.get("arguments"))
    return STATUS_PASS, "", f"function_calls={len(calls)}"


def validate_logprobs_echo(resp: HttpResponse) -> Tuple[str, str, str]:
    """C12: completions logprobs+echo, 响应应含 logprobs 字段（缺失=可选能力未实现, WARN）。"""
    try:
        data = resp.json()
    except Exception as exc:
        return STATUS_FAIL, f"response is not JSON: {exc}", summarize_body(resp)
    choices = data.get("choices")
    first = (choices or [{}])[0] if isinstance(choices, list) and choices else {}
    logprobs = first.get("logprobs") if isinstance(first, dict) else None
    if logprobs:
        return STATUS_PASS, "", "logprobs present"
    return STATUS_WARN, "response has no logprobs field (optional capability not implemented).", ""


def validate_over_context(resp: HttpResponse) -> Tuple[str, str, str]:
    """C38: 超长输入的判定（always_validate, 4xx=正确拒绝）。"""
    return judge_over_context_status(resp.status)


def run_responses_case(config: Config, case: Case, save_path: Optional[str] = None,
                       validator: Optional[Callable[[HttpResponse], Tuple[str, str, str]]] = None
                       ) -> ApiCaseResult:
    """responses 用例的统一执行路径（C08/C11/C24/C28/C29 共用, 非流式）。

    4xx 一律 WARN（optional 语义: 网关未实现 /v1/responses 或不支持该参数）;
    校验器直接吃原始 JSON 响应（output/usage 形状与非流式响应一致）。
    """
    resp = make_request(config, "POST", case.endpoint, dict(case.payload or {}))
    save_body(save_path, resp.body)
    if not (200 <= resp.status < 300):
        if 400 <= resp.status < 500:
            return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_WARN,
                                 resp.status, resp.latency_ms,
                                 f"not supported by this service (http={resp.status}).", "")
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_FAIL,
                             resp.status, resp.latency_ms, summarize_body(resp), "")
    if validator is None:
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_PASS,
                             resp.status, resp.latency_ms, "", "")
    try:
        status, issue, detail = validator(resp)
    except Exception as exc:
        status, issue, detail = STATUS_FAIL, f"validator error: {exc}", ""
    return ApiCaseResult(case.category, case.name, case.endpoint, status, resp.status,
                         resp.latency_ms, issue, detail)


@dataclass
class VariantOutcome:
    """一个请求变体的判定结果（矩阵类用例的组成单元）。"""
    label: str
    status: str
    http: Optional[int] = None
    note: str = ""

    def show(self) -> str:
        text = f"{self.label}={self.status}"
        if self.http is not None:
            text += f"(http={self.http})"
        if self.note:
            text += f":{truncate_text(self.note, 120)}"
        return text


def summarize_variant_outcomes(outcomes: List[VariantOutcome]) -> Tuple[str, str, str]:
    """矩阵聚合口径: 任一 FAIL -> FAIL; 有 WARN 无 FAIL -> WARN; 全 PASS -> PASS。"""
    fails = [o for o in outcomes if o.status == STATUS_FAIL]
    warns = [o for o in outcomes if o.status == STATUS_WARN]
    detail = "; ".join(o.show() for o in outcomes)
    if fails:
        return STATUS_FAIL, f"{len(fails)}/{len(outcomes)} variants failed.", detail
    if warns:
        return STATUS_WARN, f"{len(warns)}/{len(outcomes)} variants warned.", detail
    return STATUS_PASS, "", detail


def variant_result(case: Case, outcomes: List[VariantOutcome], latency_ms: int = 0,
                   http_status: Optional[int] = None) -> ApiCaseResult:
    status, issue, detail = summarize_variant_outcomes(outcomes)
    return ApiCaseResult(case.category, case.name, case.endpoint, status, http_status,
                         latency_ms, issue, detail)


def _first_chat_message(aggregated: Dict[str, Any]) -> Tuple[Dict[str, Any], Optional[str]]:
    """聚合 chat 响应 -> (message, finish_reason)。"""
    choices = aggregated.get("choices") or [{}]
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    return message, first.get("finish_reason")


def _has_reasoning_chat(message: Dict[str, Any]) -> bool:
    return bool(str(message.get("reasoning") or message.get("reasoning_content") or "").strip())


def run_param_probe_case(config: Config, case: Case, save_path: Optional[str] = None,
                         responses: bool = False) -> ApiCaseResult:
    """C05/C06 参数探测: 逐参数隔离发请求, 200=PASS / 4xx=WARN(不支持) / 5xx=FAIL。"""
    t0 = time.time()
    outcomes: List[VariantOutcome] = []
    if responses:
        base = {"model": config.model, "input": "Reply with api-ok",
                "max_output_tokens": 64, "temperature": 0}
        variants = build_responses_param_probe_variants()
    else:
        base = {"model": config.model,
                "messages": [{"role": "user", "content": "Reply with api-ok"}],
                "max_tokens": 128, "temperature": 0}
        variants = build_chat_param_probe_variants()
    for label, extra in variants:
        payload = dict(base)
        for key, value in extra.items():
            if value is None:
                payload.pop(key, None)   # 隔离探测: max_completion_tokens 项去掉 max_tokens
            else:
                payload[key] = value
        resp, _ = request_json(config, case.endpoint, payload)
        status, issue = judge_param_probe(resp.status)
        outcomes.append(VariantOutcome(label, status, resp.status, issue))
    return variant_result(case, outcomes, int((time.time() - t0) * 1000))


def _kv_cache_probe_payload(config: Config) -> Dict[str, Any]:
    """KV cache 两连发的公共载荷: 较长共享前缀提高前缀缓存命中概率。"""
    prefix = " ".join(["shared report header line for cache probe"] * 96)
    return chat_payload(config, [
        {"role": "system", "content": f"You are a report assistant. {prefix}"},
        {"role": "user", "content": "Summarize the report header in one short sentence."},
    ], max_tokens=512)


def run_kv_cache_case(config: Config, case: Case, save_path: Optional[str] = None,
                      responses: bool = False) -> ApiCaseResult:
    """C13/C14: 同前缀两连发, 第二发的 usage 应报 cached_tokens>0（非流式）。"""
    t0 = time.time()
    if responses:
        prefix = " ".join(["shared report header line for cache probe"] * 96)
        payload = {"model": config.model, "max_output_tokens": 256,
                   "input": f"{prefix}\nSummarize the above header in one short sentence.",
                   "temperature": 0}
    else:
        payload = _kv_cache_probe_payload(config)
    first, _ = request_json(config, case.endpoint, payload)
    if not (200 <= first.status < 300):
        # 4xx = 能力未实现（如网关无 /v1/responses → 404），与 responses 族 runner
        # 的口径一致记 WARN；5xx 才是真实服务错误记 FAIL。
        if 400 <= first.status < 500:
            return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_WARN,
                                 first.status, first.latency_ms,
                                 f"not supported by this service (http={first.status}).", "")
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_FAIL,
                             first.status, first.latency_ms, f"first request failed: {summarize_body(first)}", "")
    second, data = request_json(config, case.endpoint, payload)
    save_body(save_path, second.body)
    status, issue, detail = judge_kv_cache_second(extract_usage(data))
    return ApiCaseResult(case.category, case.name, case.endpoint, status, second.status,
                         int((time.time() - t0) * 1000), issue, detail)


def run_negative_case(config: Config, case: Case, save_path: Optional[str] = None) -> ApiCaseResult:
    """C41 负向矩阵: invalid model / messages 类型 / 数值参数 / tool schema, 全部期望 4xx。"""
    t0 = time.time()
    outcomes: List[VariantOutcome] = []
    for label, payload in build_negative_variants(config.model):
        resp = make_request(config, "POST", case.endpoint, payload)
        if label == "invalid_model":
            save_body(save_path, resp.body)
        status, issue, _detail = judge_negative_status(resp.status)
        outcomes.append(VariantOutcome(label, status, resp.status, issue))
    return variant_result(case, outcomes, int((time.time() - t0) * 1000))


def run_thinking_toggle_case(config: Config, case: Case, save_path: Optional[str] = None) -> ApiCaseResult:
    """C10: chat_template_kwargs.thinking on/off（vLLM 风格）, 断言思考方向与正文不泄漏。"""
    t0 = time.time()
    outcomes: List[VariantOutcome] = []
    for enabled in (True, False):
        label = f"ctk.thinking={str(enabled).lower()}"
        payload = chat_payload(config, [{"role": "user", "content": "12*17=?"}],
                               chat_template_kwargs={"thinking": enabled})
        resp, data = request_json(config, case.endpoint, payload)
        if not (200 <= resp.status < 300):
            status, issue = judge_param_probe(resp.status)
        else:
            message, finish = _first_chat_message(data)
            content = str(message.get("content") or "")
            reasoning = _has_reasoning_chat(message)
            if THINK_TAG_RE.search(content):
                status, issue = STATUS_FAIL, "think tag leaked into content."
            elif enabled and not reasoning:
                status, issue = STATUS_WARN, "thinking on but no reasoning content (switch ignored?)."
            elif not enabled and reasoning:
                status, issue = STATUS_WARN, "thinking off but reasoning content present (switch ignored?)."
            else:
                status, issue = STATUS_PASS, ""
        outcomes.append(VariantOutcome(label, status, resp.status, issue))
    return variant_result(case, outcomes, int((time.time() - t0) * 1000))


def run_structured_thinking_case(config: Config, case: Case, save_path: Optional[str] = None) -> ApiCaseResult:
    """C26: json_object x 思考关闭/effort 变体, 断言各变体仍产出合法 JSON。"""
    t0 = time.time()
    base = chat_payload(config, [
        {"role": "system", "content": "Return only valid JSON."},
        {"role": "user", "content": 'Return {"city":"Shanghai","temperature_c":23,"condition":"clear"}'},
    ], response_format={"type": "json_object"})
    outcomes: List[VariantOutcome] = []
    for label, extra in build_structured_thinking_variants():
        payload = dict(base)
        payload.update(extra)
        resp, data = request_json(config, case.endpoint, payload)
        if not (200 <= resp.status < 300):
            status, issue = judge_param_probe(resp.status)
        else:
            message, _finish = _first_chat_message(data)
            text = str(message.get("content") or "")
            parsed, parse_issue = parse_json_from_text(text)
            if parsed is None:
                status, issue = STATUS_FAIL, f"output is not JSON: {parse_issue}"
            else:
                status, issue = STATUS_PASS, ""
        outcomes.append(VariantOutcome(label, status, resp.status, issue))
    return variant_result(case, outcomes, int((time.time() - t0) * 1000))


def thinking_matrix_payload(config: Config, effort: Optional[str], with_tools: bool) -> Dict[str, Any]:
    """C30 变体 -> chat payload（缺省/顶层 effort x 无/有 tool; 供 runner 与单测共用）。"""
    messages = [{"role": "user", "content": "北京今天天气如何?" if with_tools else "1+1=?"}]
    extra: Dict[str, Any] = {}
    if effort is not None:
        extra["reasoning_effort"] = effort
    if with_tools:
        extra["tools"] = [WEATHER_TOOL]
        extra["tool_choice"] = "auto"
    return chat_payload(config, messages, **extra)


def responses_matrix_payload(config: Config, effort: Optional[str], with_tools: bool) -> Dict[str, Any]:
    """C32 变体 -> responses payload（缺省/reasoning.effort x 无/有 tool）。"""
    payload: Dict[str, Any] = {
        "model": config.model, "max_output_tokens": 512, "temperature": 0,
        "input": "北京今天天气如何?" if with_tools else "1+1=?",
    }
    if effort is not None:
        payload["reasoning"] = {"effort": effort}
    if with_tools:
        payload["tools"] = [responses_tool()]
        payload["tool_choice"] = "auto"
    return payload


def run_thinking_effort_matrix_case(config: Config, case: Case,
                                    save_path: Optional[str] = None) -> ApiCaseResult:
    """C30: 缺省 + 顶层 reasoning_effort 7 档 x {无/有 tool} = 16 请求（非流式）。

    断言: 正文无 think 标签且非空（tool_call 轮除外）、方向不符降 WARN、
    tool_call 结构自校; 4xx（如 medium 已知缺陷）降 WARN 不升 FAIL。
    """
    t0 = time.time()
    outcomes: List[VariantOutcome] = []
    for label, effort, with_tools in build_thinking_effort_matrix():
        payload = thinking_matrix_payload(config, effort, with_tools)
        resp, data = request_json(config, case.endpoint, payload)
        if not (200 <= resp.status < 300):
            status, issue = judge_param_probe(resp.status)
        else:
            message, finish = _first_chat_message(data)
            status, issue, _detail = judge_thinking_leak_chat(message, finish)
            direction = reasoning_direction_issue(effort, _has_reasoning_chat(message))
            if status == STATUS_PASS and direction:
                status, issue = STATUS_WARN, direction
        outcomes.append(VariantOutcome(label, status, resp.status, issue))
    return variant_result(case, outcomes, int((time.time() - t0) * 1000))


def run_thinking_toggle_tool_case(config: Config, case: Case,
                                  save_path: Optional[str] = None) -> ApiCaseResult:
    """C31: enable_thinking / thinking.type / ctk.thinking 三种开关 x on/off, 仅带 tool。"""
    t0 = time.time()
    outcomes: List[VariantOutcome] = []
    for label, extra in build_thinking_toggle_variants():
        payload = chat_payload(config, [{"role": "user", "content": "北京今天天气如何?"}],
                               tools=[WEATHER_TOOL], tool_choice="auto")
        payload.update(extra)
        resp, data = request_json(config, case.endpoint, payload)
        if not (200 <= resp.status < 300):
            status, issue = judge_param_probe(resp.status)
        else:
            message, finish = _first_chat_message(data)
            status, issue, _detail = judge_thinking_leak_chat(message, finish)
        outcomes.append(VariantOutcome(label, status, resp.status, issue))
    return variant_result(case, outcomes, int((time.time() - t0) * 1000))


def run_responses_effort_matrix_case(config: Config, case: Case,
                                     save_path: Optional[str] = None) -> ApiCaseResult:
    """C32: 缺省 + reasoning.effort 4 档 x {无/有 tool} = 10 请求（responses 口径, 非流式）。"""
    t0 = time.time()
    outcomes: List[VariantOutcome] = []
    for label, effort, with_tools in build_responses_effort_matrix():
        payload = responses_matrix_payload(config, effort, with_tools)
        resp, data = request_json(config, case.endpoint, payload)
        if not (200 <= resp.status < 300):
            status, issue = judge_param_probe(resp.status)
        else:
            status, issue, _detail = judge_thinking_leak_responses(data)
            if status == STATUS_PASS and effort is not None and responses_reasoning_len(data) == 0:
                status, issue = STATUS_WARN, f"effort={effort} produced no reasoning output item."
        outcomes.append(VariantOutcome(label, status, resp.status, issue))
    return variant_result(case, outcomes, int((time.time() - t0) * 1000))


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def run_thinking_depth_case(config: Config, case: Case, save_path: Optional[str] = None) -> ApiCaseResult:
    """C33 思考深度探针（opt-in）: ctk {thinking:true, reasoning_effort:v} x3 次取中位,
    断言 reasoning 量 low < high < max; 不达标降 WARN（不 FAIL）。"""
    t0 = time.time()
    medians: Dict[str, float] = {}
    failures: List[str] = []
    for effort in DEPTH_EFFORTS:
        lens: List[int] = []
        for _ in range(3):
            payload = chat_payload(config, [{"role": "user", "content": "1+1=?"}],
                                   temperature=0,
                                   chat_template_kwargs={"thinking": True, "reasoning_effort": effort})
            resp, data = request_json(config, case.endpoint, payload)
            if not (200 <= resp.status < 300):
                failures.append(f"{effort}: http={resp.status}")
                break
            message, _finish = _first_chat_message(data)
            lens.append(len(str(message.get("reasoning") or message.get("reasoning_content") or "")))
        if lens:
            medians[effort] = _median(lens)
    if failures:
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_FAIL, None,
                             int((time.time() - t0) * 1000), "; ".join(failures), "")
    detail = ",".join(f"{k}={int(v)}" for k, v in medians.items())
    ordered = [medians.get(e) for e in DEPTH_EFFORTS]
    if all(v is not None for v in ordered) and ordered[0] < ordered[1] < ordered[2]:
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_PASS, None,
                             int((time.time() - t0) * 1000), "", f"median_reasoning_len: {detail}")
    return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_WARN, None,
                         int((time.time() - t0) * 1000),
                         "reasoning depth is not strictly increasing low < high < max.", detail)


# ---- C16-C19 openai SDK 用例（lazy import; ImportError -> SKIP） ----------------

def import_openai_sdk():
    try:
        import openai
    except ImportError:
        return None
    return openai


def build_sdk_client(openai_mod, config: Config):
    """SDK client: base_url 指向 /{api_version}（SDK 会在其后拼 /responses 等）。"""
    base = f"{config.base_url.rstrip('/')}/{config.openai_api_version}"
    kwargs: Dict[str, Any] = {
        "base_url": base,
        "api_key": config.api_key or "EMPTY",
        "timeout": config.timeout_seconds,
        "max_retries": 0,
    }
    if not config.verify_ssl:
        try:
            import httpx
            kwargs["http_client"] = httpx.Client(verify=False)
        except ImportError:
            pass
    return openai_mod.OpenAI(**kwargs)


def judge_sdk_exception(exc: Exception, openai_mod) -> Tuple[str, str]:
    """SDK 异常判定: 4xx APIStatusError=端点/参数不支持(WARN); 其余(解析/网络/5xx)=FAIL。

    注: SDK 结构解析异常（严格校验失败）一律 FAIL; 只有"服务端明确回 4xx"才视为
    该部署未实现该能力, 降 WARN（与 REST 用例 optional 口径对齐）。
    """
    api_status_error = getattr(openai_mod, "APIStatusError", ())
    status_code = getattr(exc, "status_code", None)
    if api_status_error and isinstance(exc, api_status_error) \
            and status_code is not None and 400 <= status_code < 500:
        return STATUS_WARN, f"endpoint or parameter not supported (http={status_code})."
    return STATUS_FAIL, f"SDK error: {exc}"


def _sdk_output_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str):
        return text
    return ""


def _sdk_output_items(response: Any) -> List[Any]:
    output = getattr(response, "output", None)
    return list(output) if isinstance(output, (list, tuple)) else []


def run_sdk_responses_case(config: Config, case: Case, save_path: Optional[str] = None,
                           kind: str = "create") -> ApiCaseResult:
    """C16/C18/C19: openai SDK responses 严格解析（非流式 create 口径）。

    SDK 异常即 FAIL（4xx APIStatusError 降 WARN = 端点未实现, 见 judge_sdk_exception）;
    SDK 未安装 -> SKIP。
    """
    openai_mod = import_openai_sdk()
    if openai_mod is None:
        return ApiCaseResult(case.category, case.name, case.endpoint, STATUS_SKIP, None, None,
                             "openai SDK is not installed (lazy import failed).", "")
    t0 = time.time()
    try:
        client = build_sdk_client(openai_mod, config)
        if kind == "create":
            resp = client.responses.create(
                model=config.model, input="Reply with api-ok",
                max_output_tokens=256, temperature=0)
            text = _sdk_output_text(resp)
            item_types = [getattr(i, "type", "?") for i in _sdk_output_items(resp)]
            if text.strip():
                status, issue, detail = STATUS_PASS, "", f"text_len={len(text)}"
            elif "reasoning" in item_types:
                status, issue = STATUS_WARN, "output_text empty (reasoning consumed the token budget)."
                detail = f"output_types={item_types}"
            else:
                status, issue, detail = STATUS_FAIL, "SDK parsed the response but output_text is empty.", \
                    f"output_types={item_types}"
        elif kind == "function":
            resp = client.responses.create(
                model=config.model, input="北京今天天气如何?",
                max_output_tokens=512, temperature=0,
                tools=[responses_tool()], tool_choice={"type": "function", "name": "get_weather"})
            calls = [i for i in _sdk_output_items(resp) if getattr(i, "type", "") == "function_call"]
            if not calls:
                status, issue, detail = STATUS_FAIL, "no function_call output item (tool_choice forced).", ""
            else:
                bad = []
                for call in calls:
                    args = getattr(call, "arguments", None)
                    try:
                        json.loads(str(args or ""))
                    except Exception:
                        bad.append(str(args))
                if bad:
                    status, issue, detail = STATUS_FAIL, "function_call.arguments is not valid JSON.", ""
                else:
                    status, issue, detail = STATUS_PASS, "", f"function_calls={len(calls)}"
        else:  # web_search
            resp = client.responses.create(
                model=config.model, input="Search the web: what is the capital of France?",
                tools=[{"type": "web_search"}])
            text = _sdk_output_text(resp)
            status, issue = (STATUS_PASS, "") if text.strip() else (STATUS_FAIL, "output_text is empty.")
            detail = f"text_len={len(text)}"
        latency = int((time.time() - t0) * 1000)
        return ApiCaseResult(case.category, case.name, case.endpoint, status, None, latency,
                             issue, detail)
    except Exception as exc:
        status, issue = judge_sdk_exception(exc, openai_mod)
        return ApiCaseResult(case.category, case.name, case.endpoint, status, None,
                             int((time.time() - t0) * 1000), issue, "")


def build_openai_compat_cases(config: Config) -> List[Case]:
    """兼容性（C01-C04; C42-C45 的原始检查并入对应编号实现）。"""
    chat_ep = openai_endpoint(config, "chat/completions")
    comp_ep = openai_endpoint(config, "completions")
    return [
        Case(
            category="openai/compatibility", number="C01", family="models", group="兼容性",
            name="C01 models 列模型",
            endpoint=openai_endpoint(config, "models"),
            method="GET",
            validate=lambda resp: (STATUS_PASS, "", f"bytes={len(resp.body)}"),
            tags=["C01"],
            slug="c01_models_list",
        ),
        Case(
            category="openai/compatibility", number="C02", family="chat", group="兼容性",
            name="C02 chat 冒烟",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user", "content": "Reply with exactly: api-ok"}]),
            validate=validate_answer_text,
            tags=["C02"],
            slug="c02_chat_smoke",
        ),
        Case(
            category="openai/compatibility", number="C03", family="completions", group="兼容性",
            name="C03 completions 冒烟",
            endpoint=comp_ep,
            payload={"model": config.model, "prompt": "Reply with api-ok",
                     "max_tokens": 128, "temperature": 0},
            validate=validate_completion_response,
            tags=["C03"],
            slug="c03_completions_smoke",
        ),
        Case(
            category="openai/compatibility", number="C04", family="responses", group="兼容性",
            name="C04 responses 冒烟",
            endpoint=openai_endpoint(config, "responses"),
            payload={"model": config.model, "input": "Reply with api-ok",
                     "max_output_tokens": 64, "temperature": 0},
            # responses 响应是 output[] 结构（无 choices），必须用 responses 口径校验
            # （2026-10-31 实锤: dsv41 实现 /v1/responses, 误挂 chat 校验器 -> 假 FAIL）。
            validate=validate_responses_answer,
            optional=True,  # 能力探测: 4xx 记 WARN（该服务未实现）, 不计失败
            tags=["C04", RESPONSES_API_TAG],
            slug="c04_responses_smoke",
        ),
    ]


def build_openai_param_probe_cases(config: Config) -> List[Case]:
    """参数探测（C05 chat / C06 responses, 逐参数隔离）。"""
    return [
        Case(
            category="openai/param-probe", number="C05", family="chat", group="参数探测",
            name="C05 chat 参数探测",
            endpoint=openai_endpoint(config, "chat/completions"),
            runner=lambda cfg, case, save: run_param_probe_case(cfg, case, save, responses=False),
            tags=["C05"],
            slug="c05_param_probe_chat",
        ),
        Case(
            category="openai/param-probe", number="C06", family="responses", group="参数探测",
            name="C06 responses 参数探测",
            endpoint=openai_endpoint(config, "responses"),
            runner=lambda cfg, case, save: run_param_probe_case(cfg, case, save, responses=True),
            tags=["C06", RESPONSES_API_TAG],
            slug="c06_param_probe_responses",
        ),
    ]


def build_openai_capability_cases(config: Config) -> List[Case]:
    """能力类（C07-C15 + 专有检查 C54/C55、effort 矩阵 C56 起; 受 --capability-repeat 影响）。"""
    chat_ep = openai_endpoint(config, "chat/completions")
    comp_ep = openai_endpoint(config, "completions")
    responses_ep = openai_endpoint(config, "responses")
    is_gpt = WEB_SEARCH_MODEL_MARK in config.model.lower()
    cases: List[Case] = [
        Case(
            category="openai/capability", number="C07", family="chat", group="能力",
            name="C07 chat function call 数据集",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user", "content": "北京今天天气如何?"}],
                tools=[WEATHER_TOOL], tool_choice="auto"),
            validate=validate_tool_calls,
            tags=["C07"],
            slug="c07_tool_call",
        ),
        Case(
            category="openai/capability", number="C54", family="chat", group="能力",
            name="C54 chat thinking 开（reasoning）",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user", "content": "12*17=?"}]),
            validate=validate_thinking_on,
            tags=["C54"],
            slug="c54_thinking_on",
        ),
        Case(
            category="openai/capability", number="C55", family="chat", group="能力",
            name="C55 chat thinking 关（effort=none）",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user", "content": "12*17=?"}], reasoning_effort="none"),
            validate=validate_thinking_off,
            tags=["C55"],
            slug="c55_thinking_off",
        ),
        Case(
            category="openai/capability", number="C08", family="responses", group="能力",
            name="C08 responses tool calling",
            endpoint=responses_ep,
            payload={"model": config.model, "input": "北京今天天气如何?",
                     "max_output_tokens": 512, "temperature": 0,
                     "tools": [responses_tool()], "tool_choice": "auto"},
            runner=lambda cfg, case, save: run_responses_case(
                cfg, case, save, validator=validate_responses_function_call),
            tags=["C08", RESPONSES_API_TAG],
            slug="c08_responses_tool",
        ),
        Case(
            category="openai/capability", number="C09", family="responses", group="能力",
            name="C09 responses web_search tool",
            endpoint=responses_ep,
            payload={"model": config.model, "input": "Search the web: capital of France?",
                     "tools": [{"type": "web_search"}]},
            runner=lambda cfg, case, save: run_responses_case(cfg, case, save),
            skip_always=not is_gpt,
            skip_reason=(f"web_search tool 仅适用于 gpt 系模型 (current model: {config.model})"
                         if not is_gpt else ""),
            tags=["C09", RESPONSES_API_TAG],
            slug="c09_responses_web_search",
        ),
        Case(
            category="openai/capability", number="C10", family="chat", group="能力",
            name="C10 chat thinking enable_thinking",
            endpoint=chat_ep,
            runner=run_thinking_toggle_case,
            tags=["C10"],
            slug="c10_thinking_toggle",
        ),
        Case(
            category="openai/capability", number="C11", family="responses", group="能力",
            name="C11 responses reasoning.effort=low",
            endpoint=responses_ep,
            payload={"model": config.model, "input": "1+1=?", "max_output_tokens": 512,
                     "temperature": 0, "reasoning": {"effort": "low"}},
            runner=lambda cfg, case, save: run_responses_case(
                cfg, case, save, validator=validate_responses_answer),
            tags=["C11", RESPONSES_API_TAG],
            slug="c11_responses_effort_low",
        ),
        Case(
            category="openai/capability", number="C12", family="completions", group="能力",
            name="C12 completions logprobs and echo",
            endpoint=comp_ep,
            payload={"model": config.model, "prompt": "The capital of France is",
                     "max_tokens": 8, "temperature": 0, "logprobs": 5, "echo": True},
            validate=validate_logprobs_echo,
            optional=True,  # logprobs/echo 是可选能力: 4xx 或缺字段记 WARN
            tags=["C12"],
            slug="c12_logprobs_echo",
        ),
        Case(
            category="openai/capability", number="C13", family="chat", group="能力",
            name="C13 chat KV cache 报告",
            endpoint=chat_ep,
            runner=lambda cfg, case, save: run_kv_cache_case(cfg, case, save, responses=False),
            tags=["C13"],
            slug="c13_chat_kv_cache",
        ),
        Case(
            category="openai/capability", number="C14", family="responses", group="能力",
            name="C14 responses KV cache 报告",
            endpoint=responses_ep,
            runner=lambda cfg, case, save: run_kv_cache_case(cfg, case, save, responses=True),
            tags=["C14", RESPONSES_API_TAG],
            slug="c14_responses_kv_cache",
        ),
        Case(
            category="openai/capability", number="C15", family="chat", group="能力",
            name="C15 chat video visual",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user", "content": [
                    {"type": "text", "text": "Describe this video in one sentence."},
                    build_video_content_part(),
                ]}]),
            validate=validate_answer_text,
            optional=True,  # 视觉是可选能力: 端点拒绝该 content 类型记 WARN, 不 FAIL
            tags=["C15"],
            slug="c15_video",
        ),
    ]
    # effort 矩阵（C56 起连续编号, config functional.efforts 展开）
    for i, effort in enumerate(config.efforts):
        number = f"C{56 + i}"
        cases.append(Case(
            category="openai/capability", number=number, family="chat", group="能力",
            name=f"{number} chat reasoning_effort={effort}",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user", "content": "1+1=?"}], reasoning_effort=effort),
            validate=validate_effort_level,
            tags=[number],
            slug=f"{number.lower()}_effort_{effort}",
        ))
    # medium 已知缺陷（400=缺陷仍在 / 200=被静默接受, 都记 WARN; 服务异常记 FAIL）
    med_number = f"C{56 + len(config.efforts)}"
    cases.append(Case(
        category="openai/capability", number=med_number, family="chat", group="能力",
        name=f"{med_number} chat reasoning_effort=medium（已知缺陷）",
        endpoint=chat_ep,
        payload=chat_payload(config, [
            {"role": "user", "content": "1+1=?"}], reasoning_effort="medium"),
        validate=validate_effort_known_defect,
        always_validate=True,
        tags=[med_number],
        slug=f"{med_number.lower()}_effort_medium",
    ))
    return cases


def build_openai_sdk_cases(config: Config) -> List[Case]:
    """SDK 严格解析（C16/C18/C19, lazy import openai; 未安装 -> SKIP; C17 流式已移除）。"""
    responses_ep = openai_endpoint(config, "responses")
    is_gpt = WEB_SEARCH_MODEL_MARK in config.model.lower()
    return [
        Case(
            category="openai/sdk", number="C16", family="responses", group="SDK",
            name="C16 responses SDK non-stream",
            endpoint=responses_ep,
            runner=lambda cfg, case, save: run_sdk_responses_case(cfg, case, save, kind="create"),
            tags=["C16", RESPONSES_API_TAG],
            slug="c16_sdk_create",
        ),
        Case(
            category="openai/sdk", number="C18", family="responses", group="SDK",
            name="C18 responses SDK function tool",
            endpoint=responses_ep,
            runner=lambda cfg, case, save: run_sdk_responses_case(cfg, case, save, kind="function"),
            tags=["C18", RESPONSES_API_TAG],
            slug="c18_sdk_function",
        ),
        Case(
            category="openai/sdk", number="C19", family="responses", group="SDK",
            name="C19 responses SDK web_search",
            endpoint=responses_ep,
            runner=lambda cfg, case, save: run_sdk_responses_case(cfg, case, save, kind="web_search"),
            skip_always=not is_gpt,
            skip_reason=(f"web_search tool 仅适用于 gpt 系模型 (current model: {config.model})"
                         if not is_gpt else ""),
            tags=["C19", RESPONSES_API_TAG],
            slug="c19_sdk_web_search",
        ),
    ]


def build_openai_structured_cases(config: Config) -> List[Case]:
    """结构化输出（C21-C26; C47 的 response_format json_object 检查并入 C22）。"""
    chat_ep = openai_endpoint(config, "chat/completions")
    responses_ep = openai_endpoint(config, "responses")
    return [
        Case(
            category="openai/structured-output", number="C21", family="chat", group="结构化输出",
            name="C21 chat json best-effort",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user",
                 "content": 'Return {"city":"Shanghai","temperature_c":23,"condition":"clear"}'},
            ]),
            validate=validate_json_output(),
            slug="c21_json_best_effort",
            tags=["C21"],
        ),
        Case(
            category="openai/structured-output", number="C22", family="chat", group="结构化输出",
            name="C22 chat json_object",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "system", "content": "Return only valid JSON."},
                {"role": "user",
                 "content": 'Return {"city":"Shanghai","temperature_c":23,"condition":"clear"}'},
            ], response_format={"type": "json_object"}),
            validate=validate_json_output(),
            optional=True,      # 能力探测: 网关不支持 response_format 时记 WARN
            tags=["C22"],
            slug="c22_json_object",
        ),
        Case(
            category="openai/structured-output", number="C23", family="chat", group="结构化输出",
            name="C23 chat json_schema strict",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "system", "content": "Return only the JSON object described by the schema."},
                {"role": "user",
                 "content": 'Return {"city":"Shanghai","temperature_c":23,"condition":"clear"}'},
            ], response_format={"type": "json_schema",
                                "json_schema": {"name": "weather_report", "strict": True,
                                                "schema": WEATHER_JSON_SCHEMA}}),
            validate=validate_json_schema_output,
            optional=True,      # json_schema 是新参数: 网关不支持时记 WARN
            tags=["C23"],
            slug="c23_json_schema",
        ),
        Case(
            category="openai/structured-output", number="C24", family="responses", group="结构化输出",
            name="C24 responses json_schema",
            endpoint=responses_ep,
            payload={"model": config.model, "max_output_tokens": 256, "temperature": 0,
                     "input": 'Return a JSON object: {"city":"Shanghai","temperature_c":23}',
                     "text": {"format": {"type": "json_schema", "name": "weather_report",
                                         "strict": True, "schema": WEATHER_JSON_SCHEMA}}},
            runner=lambda cfg, case, save: run_responses_case(
                cfg, case, save, validator=validate_json_schema_output),
            tags=["C24", RESPONSES_API_TAG],
            slug="c24_responses_json_schema",
        ),
        Case(
            category="openai/structured-output", number="C25", family="chat", group="结构化输出",
            name="C25 chat glm json_object system schema",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "system",
                 "content": '你是 JSON 输出器。只输出一个 JSON 对象, 字段: '
                            'city(字符串)、temperature_c(数字)、condition(字符串)。'},
                {"role": "user", "content": "上海今天晴, 气温 23 度。"},
            ], response_format={"type": "json_object"}),
            validate=validate_json_schema_output,
            optional=True,
            tags=["C25"],
            slug="c25_glm_json_schema",
        ),
        Case(
            category="openai/structured-output", number="C26", family="chat", group="结构化输出",
            name="C26 chat 结构化+思考变体",
            endpoint=chat_ep,
            runner=run_structured_thinking_case,
            tags=["C26"],
            slug="c26_structured_thinking",
        ),
    ]


def build_openai_thinking_leak_cases(config: Config) -> List[Case]:
    """思考泄漏（C27-C32: tools/思考参数交叉, 断言思考不泄漏进可见正文）。"""
    chat_ep = openai_endpoint(config, "chat/completions")
    responses_ep = openai_endpoint(config, "responses")
    return [
        Case(
            category="openai/thinking-leak", number="C27", family="chat", group="思考泄漏",
            name="C27 chat 思考泄漏 tools+no thinking",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user", "content": "北京今天天气如何?"}],
                tools=[WEATHER_TOOL], tool_choice="auto"),
            validate=validate_thinking_leak_chat,
            tags=["C27"],
            slug="c27_tools_no_thinking",
        ),
        Case(
            category="openai/thinking-leak", number="C28", family="responses", group="思考泄漏",
            name="C28 responses 思考泄漏 tools+no reasoning",
            endpoint=responses_ep,
            payload={"model": config.model, "input": "北京今天天气如何?",
                     "max_output_tokens": 512, "temperature": 0,
                     "tools": [responses_tool()], "tool_choice": "auto"},
            runner=lambda cfg, case, save: run_responses_case(
                cfg, case, save, validator=validate_thinking_leak_responses),
            tags=["C28", RESPONSES_API_TAG],
            slug="c28_responses_tools",
        ),
        Case(
            category="openai/thinking-leak", number="C29", family="responses", group="思考泄漏",
            name="C29 responses 思考泄漏 no tools+no reasoning",
            endpoint=responses_ep,
            payload={"model": config.model, "input": "Reply with a short sentence.",
                     "max_output_tokens": 512, "temperature": 0},
            runner=lambda cfg, case, save: run_responses_case(
                cfg, case, save, validator=validate_thinking_leak_responses),
            tags=["C29", RESPONSES_API_TAG],
            slug="c29_responses_plain",
        ),
        Case(
            category="openai/thinking-leak", number="C30", family="chat", group="思考泄漏",
            name="C30 chat thinking×tool 16 请求",
            endpoint=chat_ep,
            runner=run_thinking_effort_matrix_case,
            tags=["C30"],
            slug="c30_thinking_tool_matrix",
        ),
        Case(
            category="openai/thinking-leak", number="C31", family="chat", group="思考泄漏",
            name="C31 chat thinking×tool 开关形式 6 请求",
            endpoint=chat_ep,
            runner=run_thinking_toggle_tool_case,
            tags=["C31"],
            slug="c31_toggles_tool",
        ),
        Case(
            category="openai/thinking-leak", number="C32", family="responses", group="思考泄漏",
            name="C32 responses thinking×tool 10 请求",
            endpoint=responses_ep,
            runner=run_responses_effort_matrix_case,
            tags=["C32", RESPONSES_API_TAG],
            slug="c32_responses_effort_matrix",
        ),
    ]


def build_openai_context_cases(config: Config) -> List[Case]:
    """上下文（C34-C38; C48 的 4-messages 召回并入 C35, C50 的长上下文并入 C37）。"""
    chat_ep = openai_endpoint(config, "chat/completions")
    cases: List[Case] = [
        Case(
            category="openai/context", number="C34", family="chat", group="上下文",
            name="C34 chat system prompt",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "system",
                 "content": "You must include the marker MARKER_SYS_ECHO_8817 in every reply."},
                {"role": "user", "content": "Say hello and include the marker exactly as instructed."},
            ]),
            validate=validate_contains_token("MARKER_SYS_ECHO_8817"),
            tags=["C34"],
            slug="c34_system_echo",
        ),
        Case(
            category="openai/context", number="C35", family="chat", group="上下文",
            name="C35 chat multi-turn recall",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "system", "content": "You recall exact secret tokens."},
                {"role": "user", "content": "Remember this secret token: SIMAAS_OPENAI_SECRET"},
                {"role": "assistant", "content": "I will remember it."},
                {"role": "user", "content": "Return only the secret token."},
            ]),
            validate=validate_contains_token("SIMAAS_OPENAI_SECRET"),
            tags=["C35"],
            slug="c35_multi_turn_recall",
        ),
        Case(
            category="openai/context", number="C36", family="chat", group="上下文",
            name="C36 chat trailing system visible reply",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "What is 6 times 7?"},
                {"role": "system", "content": "Runtime state: session=func-c36, locale=zh-CN."},
            ]),
            validate=validate_answer_text,
            tags=["C36"],
            slug="c36_trailing_system",
        ),
    ]
    # C37 长上下文召回（默认按 config 决定是否跑）
    if config.skip_long_context_api:
        cases.append(Case(
            category="openai/context", number="C37", family="chat", group="上下文",
            name="C37 chat long context needle",
            endpoint=chat_ep,
            method="GET",
            tags=["C37", "long_context"],
            slug="c37_long_context_recall",
            skip_always=True,
            skip_reason="disabled by --skip-long-context-api / config functional.skip_long_context_api",
        ))
    else:
        needle = "SIMAAS_LONG_CONTEXT_NEEDLE"
        filler = " ".join(["context filler"] * max(1, config.long_context_api_tokens // 2))
        cases.append(Case(
            category="openai/context", number="C37", family="chat", group="上下文",
            name="C37 chat long context needle",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user",
                 "content": f"{filler}\nNeedle: {needle}\n{filler}\nReturn only the needle token."}]),
            validate=validate_contains_token(needle),
            tags=["C37", "long_context"],
            slug="c37_long_context_recall",
        ))
    # C38 超长输入（同样受长上下文开关控制; 按 service.max_context_length 的 1.1 倍构造）
    if config.skip_long_context_api or config.max_context_length <= 0:
        reason = ("disabled by --skip-long-context-api / config functional.skip_long_context_api"
                  if config.skip_long_context_api
                  else "config service.max_context_length 未设置, 无法界定超长输入规模")
        cases.append(Case(
            category="openai/context", number="C38", family="chat", group="上下文",
            name="C38 chat over-context input",
            endpoint=chat_ep,
            method="GET",
            tags=["C38", "long_context"],
            slug="c38_over_context",
            skip_always=True,
            skip_reason=reason,
        ))
    else:
        # 1 个英文单词 ≈ 1 token, 按 1.1 倍上下文上限铺词, 有界且可复现
        words = " ".join(["filler"] * int(config.max_context_length * 1.1))
        cases.append(Case(
            category="openai/context", number="C38", family="chat", group="上下文",
            name="C38 chat over-context input",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user", "content": f"{words}\nReply with exactly: api-ok"}],
                max_tokens=64),
            validate=validate_over_context,
            always_validate=True,   # 4xx = 正确拒绝, 必须在非 2xx 时也走判定
            tags=["C38", "long_context"],
            slug="c38_over_context",
        ))
    return cases


def build_openai_finops_cases(config: Config) -> List[Case]:
    """用量（C39 chat usage; C40 completions usage）。"""
    chat_ep = openai_endpoint(config, "chat/completions")
    comp_ep = openai_endpoint(config, "completions")
    return [
        Case(
            category="openai/finops", number="C39", family="chat", group="用量",
            name="C39 chat usage accounting",
            endpoint=chat_ep,
            payload=chat_payload(config, [
                {"role": "user", "content": "Say hello in three words."}]),
            runner=run_usage_case,
            tags=["C39"],
            slug="c39_chat_usage",
        ),
        Case(
            category="openai/finops", number="C40", family="completions", group="用量",
            name="C40 completions usage accounting",
            endpoint=comp_ep,
            payload={"model": config.model, "prompt": "Say hello in three words.",
                     "max_tokens": 128, "temperature": 0},
            validate=validate_usage_response,
            tags=["C40"],
            slug="c40_completions_usage",
        ),
    ]


def build_openai_robustness_cases(config: Config) -> List[Case]:
    """健壮性/负向（C41 负向矩阵吸收旧 F_err; C51-C53 为 kit 专有检查续编）。"""
    comp_ep = openai_endpoint(config, "completions")
    chat_ep = openai_endpoint(config, "chat/completions")
    return [
        Case(
            category="openai/robustness", number="C51", family="completions", group="健壮性",
            name="C51 completions max_tokens 截断",
            endpoint=comp_ep,
            payload={"model": config.model, "prompt": "从1数到100", "max_tokens": 8},
            validate=validate_completion_tokens_capped,
            tags=["C51"],
            slug="c51_max_tokens_truncation",
        ),
        Case(
            category="openai/robustness", number="C52", family="completions", group="健壮性",
            name="C52 completions stop 参数",
            endpoint=comp_ep,
            payload={"model": config.model,
                     "prompt": "把单词 banana 重复输出 20 遍，用空格分隔",
                     "max_tokens": 128, "stop": ["banana"]},
            validate=validate_completion_response,
            tags=["C52"],
            slug="c52_stop_parameter",
        ),
        Case(
            category="openai/robustness", number="C53", family="completions", group="健壮性",
            name="C53 completions determinism",
            endpoint=comp_ep,
            payload={"model": config.model, "prompt": "1+1等于几", "max_tokens": 16, "temperature": 0},
            tags=["C53"],
            slug="c53_determinism",
            runner=run_determinism_case,
        ),
        Case(
            category="openai/robustness", number="C41", family="chat", group="负向",
            name="C41 chat 负向矩阵",
            endpoint=chat_ep,
            runner=run_negative_case,
            tags=["C41"],
            slug="c41_negative",
        ),
    ]


def build_openai_session_cases(config: Config) -> List[Case]:
    """会话 / 思考深度（C61 claude code 会话恒 SKIP; C33 默认关, --thinking-depth-check 开启）。"""
    chat_ep = openai_endpoint(config, "chat/completions")
    session_number = f"C{56 + len(config.efforts) + 1}"
    return [
        Case(
            category="openai/session", number=session_number, family="chat", group="会话",
            name=f"{session_number} chat claude code 会话",
            endpoint="(manual)",
            method="-",
            payload=None,
            validate=None,
            tags=[session_number],
            slug="c61_claude_session",
            skip_always=True,
            skip_reason=f"需人工/CLI 会话, 见 {config.claude_session_hint}",
        ),
        Case(
            category="openai/thinking-depth", number="C33", family="chat", group="思考深度",
            name="C33 chat 思考深度探针",
            endpoint=chat_ep,
            runner=run_thinking_depth_case,
            tags=["C33", THINKING_DEPTH_TAG],
            skip_always=not config.thinking_depth_check,
            skip_reason=("" if config.thinking_depth_check
                         else "opt-in: --thinking-depth-check / config functional.thinking_depth_check"),
            slug="c33_thinking_depth",
        ),
    ]


def build_openai_cases(config: Config) -> List[Case]:
    """OpenAI 兼容用例（C 矩阵 + C51 起的专有检查, 全部非流式）。

    chat/completions 类用例统一走非流式（功能线已按需求移除流式; 网关若有
    首字节超时, 由功能线超时 functional.timeout_s 兜底, 流式能力归性能线覆盖）。
    """
    cases: List[Case] = []
    cases.extend(build_openai_compat_cases(config))
    cases.extend(build_openai_param_probe_cases(config))
    cases.extend(build_openai_capability_cases(config))
    cases.extend(build_openai_sdk_cases(config))
    cases.extend(build_openai_structured_cases(config))
    cases.extend(build_openai_thinking_leak_cases(config))
    cases.extend(build_openai_context_cases(config))
    cases.extend(build_openai_finops_cases(config))
    cases.extend(build_openai_robustness_cases(config))
    cases.extend(build_openai_session_cases(config))
    cases.sort(key=case_sort_key)   # 注册顺序与编号解耦: 展示/执行都按 C 编号排序
    return cases


def build_messages_cases(config: Config) -> List[Case]:
    endpoint = messages_endpoint(config)
    base_payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": "Reply with exactly: messages-ok"}],
        "max_tokens": 64,
        "temperature": 0,
    }
    return [   # 功能线全部非流式（流式能力由性能线覆盖）
        Case(
            category="messages/compatibility",
            name="messages",
            endpoint=endpoint,
            payload=base_payload,
            validate=lambda resp: validate_non_empty_text(resp, extract_messages_text),
            slug="messages",
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
    """--only 过滤: 支持 C 编号 / gate tag（responses_api 等）/ 用例名子串。

    匹配不到任何用例时打印可用清单并返回 None（调用方退出 2）。
    """
    wanted = [w.strip() for w in only.split(",") if w.strip()]
    selected: set = set()
    unknown: List[str] = []
    for w in wanted:
        hits = [
            idx for idx, c in enumerate(cases)
            if w in c.tags or w.lower() in c.name.lower()
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
    parser.add_argument("--skip-responses-api", action="store_true",
                        help="跳过 /v1/responses 协议族用例（C04/C06/C08/C09/C11/C14/C16/"
                             "C18/C19/C24/C28/C29/C32, 共 13 条; C17 流式已移除）; "
                             "网关不支持 responses 时可整体跳过")
    parser.add_argument("--thinking-depth-check", action="store_true",
                        help="开启 C33 思考深度探针（3 档 effort x3 次共 9 请求, 默认关）")
    parser.add_argument("--skip-bench", action="store_true",
                        help="本脚本只跑 API 用例, 该参数仅作兼容（跳过 bench 段）")
    parser.add_argument("--capability-repeat", type=int, default=0,
                        help="capability 类用例重复轮数（抖动检测）; 0 = 用 config functional.capability_repeat / 默认 1")
    parser.add_argument("--list", action="store_true", help="列出全部用例后退出")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划, 不发任何请求")
    parser.add_argument("--only", default=None, help="只跑指定用例（逗号分隔, 支持 C 编号 / tag / "
                                                     "用例名子串）, 如 C03,C30,json_object")
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

    # C 矩阵门控: responses 族整族开关 / C33 思考深度探针 / C38 超长输入规模
    skip_responses_api = bool(args.skip_responses_api or fcfg.get("skip_responses_api", False))
    thinking_depth_check = bool(args.thinking_depth_check or fcfg.get("thinking_depth_check", False))
    max_context_length = int(svc.get("max_context_length") or 0)

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
        skip_responses_api=skip_responses_api,
        thinking_depth_check=thinking_depth_check,
        max_context_length=max(0, max_context_length),
    )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def is_skipped(case: Case, config: Config) -> str:
    """返回跳过原因; 空串 = 不跳过"""
    if getattr(case, "skip_always", False):
        return getattr(case, "skip_reason", "") or "not applicable"
    if config.skip_responses_api and RESPONSES_API_TAG in (case.tags or []):
        return "disabled by --skip-responses-api / config functional.skip_responses_api"
    for want in config.skip_cases:
        if want in case.tags or want.lower() in case.name.lower():
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
        print(f"        {_pad('编号', 6)}{_pad('接口族', 12)}{_pad('分组', 18)}"
              f"{_pad('用例', 46)}Endpoint")
        for case in cases:
            skip = is_skipped(case, config)
            times = repeat_count(case, config)
            number = case.number or "-"
            mark = "[skip]" if skip else "      "
            repeat_mark = f" x{times}" if times > 1 else ""
            print(f"  {mark} {_pad(number, 6)}{_pad(_case_family(case), 12)}"
                  f"{_pad(_case_group(case), 18)}{_pad(case.name + repeat_mark, 46)}"
                  f"{_case_endpoint_label(case)}"
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
