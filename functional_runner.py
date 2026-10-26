#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalscope 功能测试 runner（迁移自 sunzhi_sla_eval run208.sh cmd_functional, 通用化）。

对 OpenAI 兼容推理服务跑一组功能检查（chat/completions/流式/截断/stop/确定性/
thinking 开关/effort 矩阵/tool call/错误处理），纯标准库 urllib 发请求（不依赖
curl/requests），判定口径沿用 sunzhi: PASS / FAIL / KNOWN-DEFECT（已知缺陷,
不计 FAIL）/ SKIP。

用法:
    python3 functional_runner.py --list             # 列出全部检查 tag 与说明
    python3 functional_runner.py --dry-run          # 只打印检查清单，不发任何请求
    python3 functional_runner.py --only F-01a,F-03  # 只跑指定检查（tag 或前缀组, 见 --list）
    python3 functional_runner.py                    # 顺序执行全部检查

执行流程:
    1. 复用 config.yaml 的 service/request 段: 请求头经 runner.build_headers()
       （Host 开关/extra_headers/api_key 全部生效）; runner 导入失败时降级为仅用
       config 的 api_key 拼 Authorization（保证本脚本在残缺环境下仍可运行）
    2. 被测目标 = service.url（chat completions 完整路径）; completions 端点 =
       url 去掉尾部 /chat/completions 换 /completions
    3. 每项检查的响应原文落盘 results/functional/oneclick_<时间戳>/（f01a.json /
       f01b.sse / f03_medium.json …, 命名仿 sunzhi）, 最后写 summary.md 汇总表
    4. 已知缺陷: reasoning_effort="medium" 在部分服务上必 400（sunzhi F-03 沉淀,
       docs/lessons_api208.md）——F-03_medium 无论结果如何都记 KNOWN-DEFECT:
       400=缺陷仍在; 200=可能已修复（提示人工确认）
    5. 退出码: 任一 FAIL -> 1（KNOWN-DEFECT/SKIP 不算失败）; 否则 0;
       --only 含未知 tag -> 2

判定口径备注（对照 sunzhi 逐条移植）:
    sunzhi 原脚本部分检查用 `[ ... ] && ok=200 || ok=$code` 写法, 在 "HTTP 200
    但缺期望内容" 时会因 ok=$code=200 误判 PASS; 本实现统一收紧: 200 但缺期望
    内容记 FAIL（实际值显示 000）, 见 _ok_or_code() 与各检查函数注释。
"""
import argparse
import json
import os
import re
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, KIT_ROOT)

try:
    import yaml
except ImportError:
    print("缺少 pyyaml，请先运行: pip3 install pyyaml", file=sys.stderr)
    sys.exit(1)

DEFAULT_CONFIG = os.path.join(KIT_ROOT, "config.yaml")
RESULTS_DIR = os.path.join(KIT_ROOT, "results")
FUNC_DIR = os.path.join(RESULTS_DIR, "functional")

# functional 段可缺省, 缺省键取这里默认（config 显式配置优先）
FUNC_DEFAULTS = {
    "timeout_s": 60,                              # 单请求超时（秒）
    "efforts": ["low", "high", "xhigh", "max"],   # F-03 effort 矩阵（medium 由 F-03_medium 专项检查）
    "skip": [],                                   # 跳过的检查 tag（记 SKIP, 不发请求）
}

# 流式响应累计读取上限（防异常服务无限输出拖死检查）
STREAM_MAX_BYTES = 1 << 20

# 直连 opener（等价 sunzhi 的 curl --noproxy '*'）: 被测服务是内网地址,
# 不走 http_proxy 环境变量, 避免功能请求被代理劫持
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ---------------------------------------------------------------------------
# 配置 / 端点 / 请求头
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def functional_cfg(cfg: dict) -> dict:
    """functional 段（可缺省）与默认值合并; efforts/skip 显式为空时回落默认"""
    fcfg = dict(FUNC_DEFAULTS)
    fcfg.update(cfg.get("functional") or {})
    if not fcfg.get("efforts"):
        fcfg["efforts"] = list(FUNC_DEFAULTS["efforts"])
    if not fcfg.get("skip"):
        fcfg["skip"] = []
    return fcfg


def completions_url_of(chat_url: str) -> str:
    """chat completions 完整路径 -> completions 路径（去掉尾部 /chat/completions 换 /completions）"""
    if chat_url.endswith("/chat/completions"):
        return chat_url[: -len("/chat/completions")] + "/completions"
    return chat_url.rstrip("/") + "/completions"


def resolve_header_dict(cfg: dict) -> dict:
    """请求头 dict: 复用 runner.build_headers()（Host 开关/extra_headers/api_key 都生效）,
    其 "Key=Value" 列表按第一个 = 切成 dict, 再补 Content-Type。

    runner 导入失败时降级（except 覆盖 ImportError 与 runner 缺 pyyaml 时自身的
    SystemExit）: 仅用 config 的 api_key 拼 Authorization 头, 没有则无自定义头。
    """
    pairs = None
    try:
        import runner
        safe = dict(cfg)
        safe.setdefault("service", {})
        safe.setdefault("request", {})
        pairs = runner.build_headers(safe)
    except (ImportError, SystemExit):
        pairs = None
    if pairs is None:
        api_key = (cfg.get("request") or {}).get("api_key") or ""
        pairs = [f"Authorization=Bearer {api_key}"] if api_key else []
    hd = {"Content-Type": "application/json"}
    for h in pairs or []:
        k, _, v = str(h).partition("=")
        if k.strip():
            hd[k.strip()] = v
    return hd


# ---------------------------------------------------------------------------
# HTTP（urllib 原生, 仿 sunzhi 的 curl 用法）
# ---------------------------------------------------------------------------

def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)


def _json_loads(body: bytes):
    """响应体 bytes -> dict; 空体/非 JSON 返回 None（绝不抛异常）"""
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None


def http_post(url: str, headers: dict, payload: dict, timeout: float,
              save_path: str = None) -> tuple:
    """POST JSON, 返回 (code_str, body_bytes)。

    code_str 为字符串状态码; "000" = 网络层失败（超时/连接拒绝, 仿 curl -w %{http_code}）。
    响应原文（含 HTTP 错误响应体）落盘 save_path（排查用）。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with _NO_PROXY_OPENER.open(req, timeout=timeout) as resp:
            code, body = str(resp.status), resp.read()
    except urllib.error.HTTPError as e:  # HTTPError 是 URLError 子类, 必须先接
        code = str(e.code)
        try:
            body = e.read()
        except Exception:
            body = b""
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        print(f"  [网络] {url} 请求失败: {e}")
        return "000", b""
    if save_path:
        _write_bytes(save_path, body)
    return code, body


def http_post_stream(url: str, headers: dict, payload: dict, timeout: float,
                     save_path: str = None) -> tuple:
    """POST JSON 并读 SSE 流, 返回 (body_bytes, err)。

    逐块收取, 累计 STREAM_MAX_BYTES 或读到 [DONE] 提前结束; 流中途中断不抛出,
    已收到的部分照常落盘并由调用方按内容判定（缺 data:/[DONE] 即 FAIL）。
    超时语义: 每次阻塞读最多等 timeout 秒（与 curl -m 的整段限时略有差异, 但对
    "流卡死"同样能检出——卡住的那次 read 抛 socket.timeout）。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        resp = _NO_PROXY_OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        if save_path:
            _write_bytes(save_path, body)
        return body, None
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        return b"", str(e)
    buf = bytearray()
    try:
        with resp:
            while len(buf) < STREAM_MAX_BYTES:
                block = resp.read(8192)
                if not block:
                    break
                buf += block
                if b"[DONE]" in buf:
                    break
    except (socket.timeout, OSError) as e:
        return bytes(buf), str(e)
    body = bytes(buf)
    if save_path:
        _write_bytes(save_path, body)
    return body, None


def _ok_or_code(code: str, cond: bool) -> str:
    """期望内容判定辅助: cond 成立 -> "200"; 否则 HTTP 码非 200 时原样返回,
    200 但缺期望内容时返回 "000"（可与 PASS 值区分开, 见模块 docstring 判定口径备注）"""
    if cond:
        return "200"
    return code if code != "200" else "000"


# ---------------------------------------------------------------------------
# 判定（仿 sunzhi run208.sh chk()）
# ---------------------------------------------------------------------------

class ChkBook:
    """sunzhi run208.sh chk() 的 Python 版: 判定 + 四类计数 + summary 行收集。

    exp 为 "SKIP"/"KNOWN-DEFECT" 时直接记对应判定（不看实际值）;
    否则 exp == act -> PASS, 不等 -> FAIL。
    """

    def __init__(self):
        self.rows = []  # (tag, 实际, 判定, 说明)
        self.pass_n = self.fail_n = self.known_n = self.skip_n = 0

    def chk(self, tag: str, exp, act, note: str) -> str:
        exp, act = str(exp), str(act)
        if exp == "SKIP":
            verdict = "SKIP"
            self.skip_n += 1
        elif exp == "KNOWN-DEFECT":
            verdict = "KNOWN-DEFECT"
            self.known_n += 1
        elif exp == act:
            verdict = "PASS"
            self.pass_n += 1
        else:
            verdict = "FAIL"
            self.fail_n += 1
        self.rows.append((tag, act, verdict, note))
        print(f"  {tag:<18}实际={act:<7} {verdict:<13} {note}")
        return verdict


# ---------------------------------------------------------------------------
# 检查实现（判定口径逐条对照 sunzhi run208.sh cmd_functional）
# 每个函数签名 fn(ctx, book) -> None, 内部自行 book.chk(...) 记录结果;
# ctx 为 dict: cfg/chat_url/comp_url/model/headers/timeout/wd
# ---------------------------------------------------------------------------

def chk_f01a(ctx: dict, book: ChkBook):
    """F-01a chat 非流式 200+content"""
    code, body = http_post(
        ctx["chat_url"], ctx["headers"],
        {"model": ctx["model"],
         "messages": [{"role": "user", "content": "你好，用一句话介绍自己"}],
         "max_tokens": 128},
        ctx["timeout"], os.path.join(ctx["wd"], "f01a.json"))
    ok = _ok_or_code(code, code == "200" and b'"content"' in body)
    book.chk("F-01a_chat", "200", ok, "chat 非流式 200+content")


def chk_f01b(ctx: dict, book: ChkBook):
    """F-01b chat 流式 SSE 含 data: 与 [DONE]"""
    body, err = http_post_stream(
        ctx["chat_url"], ctx["headers"],
        {"model": ctx["model"],
         "messages": [{"role": "user", "content": "数到5"}],
         "max_tokens": 64, "stream": True},
        ctx["timeout"], os.path.join(ctx["wd"], "f01b.sse"))
    # sunzhi 口径: SSE 文本里同时含 data: 行与 [DONE] 结束标记才算 PASS
    ok = "200" if b"data:" in body and b"[DONE]" in body else "000"
    note = "SSE 流式含 data: 与 [DONE]"
    if err:
        note += f"（流读取中断: {err}）"
    book.chk("F-01b_stream", "200", ok, note)


def chk_f01c(ctx: dict, book: ChkBook):
    """F-01c completions 非流式基础"""
    code, _ = http_post(
        ctx["comp_url"], ctx["headers"],
        {"model": ctx["model"], "prompt": "介绍一下你自己",
         "max_tokens": 128, "temperature": 0.3, "top_p": 0.95},
        ctx["timeout"], os.path.join(ctx["wd"], "f01c.json"))
    book.chk("F-01c_completions", "200", code, "completions 基础")


def chk_f01d(ctx: dict, book: ChkBook):
    """F-01d max_tokens=8 截断看 usage.completion_tokens"""
    code, body = http_post(
        ctx["comp_url"], ctx["headers"],
        {"model": ctx["model"], "prompt": "从1数到100", "max_tokens": 8},
        ctx["timeout"], os.path.join(ctx["wd"], "f01d.json"))
    ct = -1
    j = _json_loads(body)
    if isinstance(j, dict):
        try:
            ct = int((j.get("usage") or {}).get("completion_tokens", -1))
        except (TypeError, ValueError):
            ct = -1
    # sunzhi 原口径只查 ct<=8（解析失败 ct=-1 也会过）, 这里收紧为 0<=ct<=8
    ok = _ok_or_code(code, code == "200" and 0 <= ct <= 8)
    book.chk("F-01d_maxtok", "200", ok, f"max_tokens=8 截断(completion_tokens={ct})")


def chk_f01e(ctx: dict, book: ChkBook):
    """F-01e stop 参数不报错"""
    code, _ = http_post(
        ctx["comp_url"], ctx["headers"],
        {"model": ctx["model"],
         "prompt": "把单词 banana 重复输出 20 遍，用空格分隔",
         "max_tokens": 128, "stop": ["banana"]},
        ctx["timeout"], os.path.join(ctx["wd"], "f01e.json"))
    book.chk("F-01e_stop", "200", code, "stop 参数不报错(内容见 f01e.json)")


def chk_f01f(ctx: dict, book: ChkBook):
    """F-01f temperature=0 两次一致（参考项）"""
    payload = {"model": ctx["model"], "prompt": "1+1等于几",
               "max_tokens": 16, "temperature": 0}
    _, b1 = http_post(ctx["comp_url"], ctx["headers"], payload, ctx["timeout"],
                      os.path.join(ctx["wd"], "f01f1.json"))
    _, b2 = http_post(ctx["comp_url"], ctx["headers"], payload, ctx["timeout"],
                      os.path.join(ctx["wd"], "f01f2.json"))
    try:
        same = (_json_loads(b1)["choices"][0]["text"]
                == _json_loads(b2)["choices"][0]["text"])
    except (KeyError, IndexError, TypeError):
        same = False
    book.chk("F-01f_determ", "200", "200" if same else "000",
             "temperature=0 两次一致(参考项)")


def chk_f02a(ctx: dict, book: ChkBook):
    """F-02a 默认 thinking 开（响应含 reasoning 字段）"""
    code, body = http_post(
        ctx["chat_url"], ctx["headers"],
        {"model": ctx["model"],
         "messages": [{"role": "user", "content": "12*17=?"}],
         "max_tokens": 512},
        ctx["timeout"], os.path.join(ctx["wd"], "f02a.json"))
    ok = _ok_or_code(code, code == "200"
                     and re.search(rb'"reasoning(_content)?"', body) is not None)
    book.chk("F-02a_think_on", "200", ok, "默认 thinking 开(含 reasoning 字段)")


def chk_f02b(ctx: dict, book: ChkBook):
    """F-02b effort=none 关 thinking（reasoning 内容为空）"""
    code, body = http_post(
        ctx["chat_url"], ctx["headers"],
        {"model": ctx["model"],
         "messages": [{"role": "user", "content": "12*17=?"}],
         "max_tokens": 256, "reasoning_effort": "none"},
        ctx["timeout"], os.path.join(ctx["wd"], "f02b.json"))
    r = None
    j = _json_loads(body)
    if isinstance(j, dict):
        try:
            msg = j["choices"][0]["message"]
            r = str(msg.get("reasoning") or msg.get("reasoning_content") or "").strip()
        except (KeyError, IndexError, TypeError):
            r = None
    ok = _ok_or_code(code, code == "200" and r == "")
    book.chk("F-02b_think_off", "200", ok, "effort=none 关 thinking")


def _make_f03(effort: str):
    """F-03 effort 矩阵单项: reasoning_effort=<effort> 请求应 200（矩阵项由 config functional.efforts 展开）"""
    def fn(ctx: dict, book: ChkBook):
        code, _ = http_post(
            ctx["chat_url"], ctx["headers"],
            {"model": ctx["model"],
             "messages": [{"role": "user", "content": "1+1=?"}],
             "max_tokens": 128, "reasoning_effort": effort},
            ctx["timeout"], os.path.join(ctx["wd"], f"f03_{effort}.json"))
        book.chk(f"F-03_{effort}", "200", code, f"reasoning_effort={effort}")
    return fn


def chk_f03_medium(ctx: dict, book: ChkBook):
    """F-03 medium 专项: 已知缺陷, 任何结果都记 KNOWN-DEFECT（不计 FAIL, 仿 sunzhi）"""
    code, _ = http_post(
        ctx["chat_url"], ctx["headers"],
        {"model": ctx["model"],
         "messages": [{"role": "user", "content": "1+1=?"}],
         "max_tokens": 128, "reasoning_effort": "medium"},
        ctx["timeout"], os.path.join(ctx["wd"], "f03_medium.json"))
    if code == "400":
        note = "medium 400=已知缺陷(如为200说明已修复,需更新结论)"
    else:
        note = f"medium 应400(已知缺陷), 实际={code} 请人工确认"
    book.chk("F-03_medium", "KNOWN-DEFECT", code, note)


def chk_f04(ctx: dict, book: ChkBook):
    """F-04 tools 触发 tool_calls"""
    code, body = http_post(
        ctx["chat_url"], ctx["headers"],
        {"model": ctx["model"],
         "messages": [{"role": "user", "content": "北京今天天气如何?"}],
         "max_tokens": 256,
         "tools": [{"type": "function", "function": {
             "name": "get_weather", "description": "查询城市天气",
             "parameters": {"type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"]}}}],
         "tool_choice": "auto"},
        ctx["timeout"], os.path.join(ctx["wd"], "f04.json"))
    ok = _ok_or_code(code, code == "200" and b'"tool_calls"' in body)
    book.chk("F-04_toolcall", "200", ok, "tools 触发 tool_calls")


def chk_ferr(ctx: dict, book: ChkBook):
    """错误模型名返回 4xx。

    sunzhi 原实现把 4xx 归一成 200 再比对（服务对未知模型也回 200 时会误判 PASS）;
    这里按口径收紧: 仅 4xx 记 PASS, 2xx/5xx/网络失败都记 FAIL。
    """
    code, _ = http_post(
        ctx["chat_url"], ctx["headers"],
        {"model": "not-exist-model",
         "messages": [{"role": "user", "content": "hi"}],
         "max_tokens": 16},
        ctx["timeout"], os.path.join(ctx["wd"], "ferr.json"))
    ok = _ok_or_code(code, bool(re.match(r"^4\d\d$", code)))
    book.chk("F_err_4xx", "200", ok, "错误模型名返回 4xx")


def chk_f05(ctx: dict, book: ChkBook):
    """F-05 claude code 会话: 需人工/CLI 交互, 有无 claude CLI 都记 SKIP（文案区分, 仿 sunzhi）"""
    if shutil.which("claude"):
        book.chk("F-05_claude", "SKIP", "SKIP",
                 "检测到 claude CLI，请按 docs 手动跑问答+工具会话")
    else:
        book.chk("F-05_claude", "SKIP", "SKIP", "宿主机无 claude CLI，跳过")


# ---------------------------------------------------------------------------
# 检查注册表 / 过滤
# ---------------------------------------------------------------------------

def build_checks(cfg: dict) -> list:
    """检查注册表: [(tag, 一行说明, 执行函数)]; F-03 矩阵按 functional.efforts 展开"""
    fcfg = functional_cfg(cfg)
    checks = [
        ("F-01a_chat", "chat 非流式 200 且响应含 content", chk_f01a),
        ("F-01b_stream", "chat 流式 SSE 含 data: 与 [DONE]", chk_f01b),
        ("F-01c_completions", "completions 非流式基础", chk_f01c),
        ("F-01d_maxtok", "completions max_tokens=8 截断(completion_tokens<=8)", chk_f01d),
        ("F-01e_stop", "completions stop 参数不报错", chk_f01e),
        ("F-01f_determ", "temperature=0 两次结果一致(参考项)", chk_f01f),
        ("F-02a_think_on", "默认 thinking 开(响应含 reasoning 字段)", chk_f02a),
        ("F-02b_think_off", "reasoning_effort=none 关 thinking(内容为空)", chk_f02b),
    ]
    for e in fcfg["efforts"]:
        checks.append((f"F-03_{e}", f"reasoning_effort={e} 返回 200", _make_f03(e)))
    checks.append(("F-03_medium",
                   "medium 已知缺陷专项(400=KNOWN-DEFECT, 200 提示人工确认)",
                   chk_f03_medium))
    checks += [
        ("F-04_toolcall", "tools 触发 tool_calls", chk_f04),
        ("F_err_4xx", "错误模型名返回 4xx", chk_ferr),
        ("F-05_claude", "claude CLI 检查(有无均记 SKIP, 会话需人工)", chk_f05),
    ]
    return checks


def filter_checks(checks: list, only: str):
    """--only 过滤: 支持精确 tag 或前缀组（如 F-03 匹配 F-03_low/F-03_medium…）。

    任一 wanted 匹配不到任何 tag 时打印错误并返回 None（调用方退出 2）;
    正常返回按注册表顺序去重后的子集。
    """
    wanted = [w.strip() for w in only.split(",") if w.strip()]
    selected, unknown = set(), []
    for w in wanted:
        m = [c[0] for c in checks if c[0] == w or c[0].startswith(w + "_")]
        if m:
            selected.update(m)
        else:
            unknown.append(w)
    if unknown:
        print(f"未知检查 tag: {unknown}\n"
              f"可用 tag (--list 查看): {[c[0] for c in checks]}", file=sys.stderr)
        return None
    return [c for c in checks if c[0] in selected]


def write_summary(wd: str, url: str, ts_h: str, book: ChkBook) -> str:
    """summary.md: sunzhi 同款格式（markdown 表 + 四类计数行）"""
    lines = [
        f"# 功能测试汇总 {ts_h}  {url}",
        "",
        "| 检查 | 实际 | 判定 | 说明 |",
        "|---|---|---|---|",
    ]
    lines += [f"| {tag} | {act} | {verdict} | {note} |"
              for tag, act, verdict, note in book.rows]
    lines += ["", f"PASS={book.pass_n} FAIL={book.fail_n} "
                  f"KNOWN-DEFECT={book.known_n} SKIP={book.skip_n}"]
    path = os.path.join(wd, "summary.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="evalscope 功能测试 runner（F-01~F-05 检查）")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="配置文件路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印检查清单，不发请求")
    ap.add_argument("--list", action="store_true", help="列出检查 tag 与说明后退出")
    ap.add_argument("--only", default=None,
                    help="只跑指定检查（逗号分隔, 支持精确 tag 或前缀组）, 如 F-01a,F-03")
    ap.add_argument("-f", "--foreground", action="store_true",
                    help="（兼容 run.sh 前台透传, 无实际作用——直接执行本脚本即为前台）")
    args = ap.parse_args()

    cfg = load_config(args.config)
    fcfg = functional_cfg(cfg)
    checks = build_checks(cfg)

    if args.list:
        for tag, desc, _fn in checks:
            print(f"{tag:<20} {desc}")
        print(f"\n共 {len(checks)} 项功能检查")
        return 0

    if args.only:
        checks = filter_checks(checks, args.only)
        if checks is None:
            return 2
    if not checks:
        print("没有可执行的检查")
        return 0

    for key in ("url", "model"):
        if not (cfg.get("service") or {}).get(key):
            raise ValueError(f"config 缺少 service.{key}（功能测试必填）")

    timeout_s = float(fcfg["timeout_s"])
    skip_set = {str(s).strip() for s in fcfg["skip"] if str(s).strip()}

    if args.dry_run:
        print(f"目标服务: {cfg['service']['url']}  模型: {cfg['service']['model']}")
        print(f"检查清单（{len(checks)} 项, dry-run 不发请求）:")
        for tag, desc, _fn in checks:
            mark = "[skip] " if tag in skip_set else "       "
            print(f"  {mark}{tag:<20} {desc}")
        if skip_set:
            print(f"config functional.skip 跳过: {sorted(skip_set)}")
        return 0

    ts_h = time.strftime("%Y-%m-%d %H:%M:%S")
    wd = os.path.join(FUNC_DIR, f"oneclick_{time.strftime('%Y%m%d_%H%M%S')}")
    os.makedirs(wd, exist_ok=True)
    ctx = {
        "cfg": cfg,
        "chat_url": cfg["service"]["url"],
        "comp_url": completions_url_of(cfg["service"]["url"]),
        "model": cfg["service"]["model"],
        "headers": resolve_header_dict(cfg),
        "timeout": timeout_s,
        "wd": wd,
    }

    print(f"== 功能测试 {ts_h}  目标: {ctx['chat_url']}  模型: {ctx['model']}")
    print(f"completions 端点: {ctx['comp_url']}")
    print(f"请求头: {sorted(ctx['headers'])}")
    print(f"输出目录: {wd}")

    book = ChkBook()
    t0 = time.time()
    for i, (tag, _desc, fn) in enumerate(checks, 1):
        print(f"\n>>> [{i}/{len(checks)}] {tag}")
        if tag in skip_set:
            book.chk(tag, "SKIP", "SKIP", "config functional.skip 跳过")
            continue
        try:
            fn(ctx, book)
        except Exception as e:  # 单项异常不中断整轮, 记 FAIL
            book.chk(tag, "200", "EXC", f"检查执行异常: {e}")

    summary_path = write_summary(wd, ctx["chat_url"], ts_h, book)
    print(f"\n{'=' * 70}")
    print(f"功能测试完成，耗时 {time.time() - t0:.1f} 秒")
    if book.fail_n:
        print(f"排查: {wd}/ 下 summary.md 与各响应原文")
    print(f"PASS={book.pass_n} FAIL={book.fail_n} "
          f"KNOWN-DEFECT={book.known_n} SKIP={book.skip_n} -> {summary_path}")
    return 1 if book.fail_n else 0


if __name__ == "__main__":
    sys.exit(main())
