#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""功能测试用例矩阵单测: C 编号统一 + 全非流式 + 判定函数 + 请求构造。

全部纯逻辑, 不发真实请求。直接运行: python3 tests/test_func_cases.py
pytest 运行: python3 -m pytest tests/test_func_cases.py -q
"""
import io
import json
import os
import re
import sys
import contextlib
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import run_api_e2e as m  # noqa: E402

FAILS = []
UNDER_PYTEST = "pytest" in sys.modules


def check(name, cond, detail=""):
    if cond:
        print(f"  ok  {name}")
        return True
    print(f"  FAIL {name}  {detail}")
    FAILS.append(name)
    if UNDER_PYTEST:
        # pytest 下必须真实失败, 不能只打印
        raise AssertionError(f"{name}  {detail}")
    return False


def make_config(**overrides):
    """构造最小 Config（与 webapp.py /api/functional/cases 的老字段表一致,
    验证新增字段全部带默认值, 外部按旧签名构造不破坏）。"""
    kwargs = dict(
        base_url="http://svc.internal", api_key="K", model="glm5.3",
        interface_types=["openai"], openai_api_version="v1",
        messages_api_version="v1", gemini_api_version="v1beta",
        embedding_api_version="v1", gemini_interactions_api_revision="",
        headers={}, timeout_seconds=5, report_path=None, print_logs=False,
        log_max_chars=100, verify_ssl=True, skip_long_context_api=True,
        long_context_api_tokens=8192,
    )
    kwargs.update(overrides)
    return m.Config(**kwargs)


NUMBER_RE = re.compile(r"^C\d{2}$")


def openai_cases(**cfg_overrides):
    return m.build_cases(make_config(**cfg_overrides))


# ---------------------------------------------------------------- 编号矩阵 --
def test_case_numbering():
    print("[numbering] 50 条 / 单一 C 编号 / 流式空洞不重排")
    cases = openai_cases()
    check("用例总数 = 50（C17/C20/C46 流式已移除）", len(cases) == 50,
          f"got {len(cases)}")
    numbers = [c.number for c in cases]
    check("全部用例都有 C 编号且格式 ^C\\d{2}$",
          all(n and NUMBER_RE.match(n) for n in numbers),
          [c.name for c in cases if not (c.number and NUMBER_RE.match(c.number))][:5])
    cnt: Counter = Counter(numbers)
    duplicated = {n: k for n, k in cnt.items() if k > 1}
    check("C 编号唯一（无别名双编号）", not duplicated, f"duplicated={duplicated}")
    holes = [n for n in ("C17", "C20", "C46") if n in cnt]
    check("流式编号 C17/C20/C46 已删除", not holes, f"holes={holes}")
    # 用户矩阵 C01-C41 除空洞外必须恰好各出现一次;
    # C42-C50 是被并入对应编号的检查描述, 不再占用独立编号
    matrix = [f"C{i:02d}" for i in range(1, 42)
              if f"C{i:02d}" not in ("C17", "C20")]
    missing = [n for n in matrix if cnt.get(n, 0) == 0]
    check("矩阵编号 C01-C41（除流式空洞）全覆盖", not missing, f"missing={missing}")
    absorbed = [n for n in numbers if 42 <= int(n[1:]) <= 50]
    check("C42-C50 不占独立编号（已并入对应检查）", not absorbed, f"found={absorbed}")
    proprietary = [n for n in numbers if int(n[1:]) > 50]
    check("专有检查续编在 C51-C61 区间", proprietary
          and all(51 <= int(n[1:]) <= 61 for n in proprietary),
          f"got={sorted(proprietary)}")
    # 编号 tag 与 number 字段一致; 用例名以编号开头
    bad_tag, bad_name, bad_start = [], [], []
    for c in cases:
        ctags = [t for t in c.tags if NUMBER_RE.match(t)]
        if ctags != [c.number]:
            bad_tag.append((c.name, c.tags, c.number))
        if not c.name.startswith(c.number + " "):
            bad_start.append(c.name)
        for part in c.name.replace("[", " ").replace("]", " ").split():
            if NUMBER_RE.match(part) and part not in c.tags:
                bad_name.append((c.name, part))
    check("编号 tag 与 Case.number 一致（单编号机制）", not bad_tag, f"bad={bad_tag[:3]}")
    check("用例名都以 C 编号开头", not bad_start, f"bad={bad_start[:3]}")
    check("名字里的 C 编号都登记在 tags", not bad_name, f"bad={bad_name[:3]}")
    # slug 全局唯一（响应落盘文件名不互相覆盖）
    slugs = [c.slug or c.name for c in cases]
    check("slug 全局唯一", len(set(slugs)) == len(slugs))


def test_no_f_residue():
    print("[alias] 旧 F 编号机制完全退役（无别名 / 无前缀组 / 无兼容 skip）")
    cases = openai_cases()
    f_tags = [t for c in cases for t in (c.tags or []) if t.startswith("F")]
    check("无任何 F 前缀 tag", not f_tags, f"found={f_tags}")
    f_names = [c.name for c in cases if "F-0" in c.name or "F-1" in c.name]
    check("无用例名含 F 编号", not f_names, f"found={f_names}")
    check("F-03 前缀组已不可过滤（未知用例）",
          m.filter_cases(cases, "F-03") is None)
    check("F-04 别名已不可过滤", m.filter_cases(cases, "F-04") is None)
    cfg = make_config()
    cfg.skip_cases = ["F-04"]
    c07 = [c for c in cases if c.number == "C07"][0]
    check("config functional.skip 用 F 名不再生效（须用 C 编号）",
          not m.is_skipped(c07, cfg))


def test_filter_cases():
    print("[filter] --only 按 C 编号 / gate tag / 用例名子串过滤")
    cases = openai_cases()
    only_c05 = m.filter_cases(cases, "C05")
    check("--only C05 恰好 1 条", only_c05 is not None and len(only_c05) == 1
          and only_c05[0].number == "C05")
    only_two = m.filter_cases(cases, "C02,C03")
    check("--only C02,C03 2 条", only_two is not None and len(only_two) == 2
          and {c.number for c in only_two} == {"C02", "C03"})
    by_gate = m.filter_cases(cases, "responses_api")
    check("--only responses_api 命中整族（13 条, C17 已移除）",
          by_gate is not None and len(by_gate) == 13,
          f"got={len(by_gate) if by_gate else None}")
    by_name = m.filter_cases(cases, "json_object")
    check("--only json_object 按名字命中 C22/C25", by_name is not None
          and {c.number for c in by_name} == {"C22", "C25"})
    check("--only 未知用例返回 None", m.filter_cases(cases, "NO_SUCH_CASE") is None)
    check("--only C17/C20/C46（已删除的流式号）返回 None",
          m.filter_cases(cases, "C17") is None
          and m.filter_cases(cases, "C20") is None
          and m.filter_cases(cases, "C46") is None)


def test_list_output():
    print("[list] --list 五列输出 / 按编号排序 / 无 F")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = m.main(["--list"])
    out = buf.getvalue()
    check("--list 退出码 0", rc == 0)
    header = out.splitlines()[1] if len(out.splitlines()) > 1 else ""
    for col in ("编号", "接口族", "分组", "用例", "Endpoint"):
        check(f"表头含 {col} 列", col in header, header)
    body = [ln for ln in out.splitlines()[2:] if ln.strip() and not ln.startswith("共")]
    numbered = [ln for ln in body if re.match(r"^\s*(\[skip\]\s*)?C\d{2}\s", ln)]
    check("50 条 C 编号行", len(body) == 50 and len(numbered) == 50,
          f"body={len(body)} numbered={len(numbered)}")
    nums = [re.match(r"^\s*(?:\[skip\]\s*)?(C\d{2})\s", ln).group(1) for ln in numbered]
    check("按 C 编号数值升序", nums == sorted(nums, key=lambda n: int(n[1:])))
    check("--list 无 F 编号", "F-0" not in out and "F-1" not in out)
    for gone in ("C17", "C20", "C46"):
        check(f"--list 无 {gone}", not re.search(rf"^\s*\[skip\]\s*{gone}\s", out, re.M))


def test_gates():
    print("[gate] responses / thinking-depth / long-context 门控")
    base_cases = openai_cases()
    resp_tagged = [c for c in base_cases if m.RESPONSES_API_TAG in c.tags]
    check(f"responses 族 {len(resp_tagged)} 条（C17 已移除）全部被整族跳过",
          len(resp_tagged) == 13 and all(
              m.is_skipped(c, make_config(skip_responses_api=True))
              for c in resp_tagged))
    check("responses gate 不影响非 responses 用例", all(
        not m.is_skipped(c, make_config(skip_responses_api=True))
        for c in base_cases
        if m.RESPONSES_API_TAG not in c.tags and not c.skip_always))
    # C33 思考深度: 默认跳过, 开启后可跑（skip_always 在构造期按 config 固化）
    c33 = [c for c in base_cases if c.number == "C33"][0]
    check("C33 默认 SKIP（opt-in）", m.is_skipped(c33, make_config()).startswith("opt-in"))
    c33_open = [c for c in openai_cases(thinking_depth_check=True)
                if c.number == "C33"][0]
    check("C33 --thinking-depth-check 后不跳过",
          not m.is_skipped(c33_open, make_config(thinking_depth_check=True)))
    # C37/C38 长上下文开关
    c37 = [c for c in base_cases if c.number == "C37"][0]
    c38 = [c for c in base_cases if c.number == "C38"][0]
    check("C37/C38 默认 SKIP", m.is_skipped(c37, make_config())
          and m.is_skipped(c38, make_config()))
    open_cfg = make_config(skip_long_context_api=False, max_context_length=204800)
    open_cases = m.build_cases(open_cfg)
    c37_open = [c for c in open_cases if c.number == "C37"][0]
    check("C37 --long-context-api 后有真实 payload", c37_open.payload is not None)
    c38_open = [c for c in open_cases if c.number == "C38"][0]
    check("C38 开启后构造超长 payload（1.1 倍上下文上限）",
          c38_open.payload is not None
          and c38_open.payload["messages"][0]["content"].count("filler") >= 225280)
    c38_nolimit = [c for c in m.build_cases(make_config(
        skip_long_context_api=False, max_context_length=0)) if c.number == "C38"][0]
    check("C38 未配置 max_context_length 时 SKIP", m.is_skipped(c38_nolimit, make_config(
        skip_long_context_api=False, max_context_length=0)).startswith("config service"))
    # C09/C19 web_search 门控: 模型名不含 gpt -> SKIP
    for number in ("C09", "C19"):
        case = [c for c in base_cases if c.number == number][0]
        check(f"{number} 非 gpt 模型 SKIP", "gpt" in m.is_skipped(case, make_config()))
        gpt_case = [c for c in openai_cases(model="gpt-5o") if c.number == number][0]
        check(f"{number} gpt 模型不跳过", not m.is_skipped(gpt_case, make_config(model="gpt-5o")))
    # config functional.skip 用 C 编号生效
    cfg_skip = make_config()
    cfg_skip.skip_cases = ["C07"]
    c07 = [c for c in base_cases if c.number == "C07"][0]
    check("config functional.skip 用 C 编号跳过 C07", m.is_skipped(c07, cfg_skip))


# ---------------------------------------------------------------- 全非流式 --
def test_no_stream_anywhere():
    print("[stream] 全部用例非流式（payload 无 stream 键 / Case.stream 恒 False）")

    def walk(value, path):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in ("stream", "stream_options"):
                    yield path + [k], v
                yield from walk(v, path + [k])
        elif isinstance(value, list):
            for i, v in enumerate(value):
                yield from walk(v, path + [str(i)])

    problems = []
    for case in openai_cases():
        if case.stream:
            problems.append((case.number, "case.stream=True"))
        for key_path, _v in walk(case.payload or {}, []):
            problems.append((case.number, ".".join(key_path)))
        # runner 类用例的请求体由各 runner 构造: 逐个矩阵/变体构造函数兜底校验
    check("Case.stream 全部 False（webapp 兼容字段）",
          not [p for p in problems if p[1] == "case.stream=True"], problems[:5])
    check("用例 payload 无 stream/stream_options 键",
          not [p for p in problems if p[1] != "case.stream=True"], problems[:5])
    config = make_config()
    runner_payloads = (
        [("chat_payload", m.chat_payload(config, [{"role": "user", "content": "x"}])),
         ("thinking_matrix_payload", m.thinking_matrix_payload(config, "high", True)),
         ("thinking_matrix_payload-default", m.thinking_matrix_payload(config, None, False)),
         ("responses_matrix_payload", m.responses_matrix_payload(config, "medium", True)),
         ("kv probe", m._kv_cache_probe_payload(config))]
        + [(f"effort={e}", m.chat_payload(config, [{"role": "user", "content": "x"}],
                                          reasoning_effort=e)) for e in ("none", "low", "high")]
    )
    bad = [name for name, payload in runner_payloads
           if "stream" in payload or "stream_options" in payload]
    check("runner 请求构造函数全部无 stream 键", not bad, f"bad={bad}")


# ---------------------------------------------------------------- 判定函数 --
def test_judge_param_probe():
    print("[judge] C05/C06 参数探测判定")
    check("200 -> PASS", m.judge_param_probe(200) == (m.STATUS_PASS, ""))
    check("404/400 -> WARN", m.judge_param_probe(400)[0] == m.STATUS_WARN
          and m.judge_param_probe(404)[0] == m.STATUS_WARN)
    check("500 -> FAIL", m.judge_param_probe(500)[0] == m.STATUS_FAIL)
    check("传输层 0 -> FAIL", m.judge_param_probe(0)[0] == m.STATUS_FAIL)


def test_judge_kv_cache():
    print("[judge] C13/C14 KV cache 判定")
    check("cached_tokens>0 -> PASS", m.judge_kv_cache_second(
        {"prompt_tokens_details": {"cached_tokens": 128}})[0] == m.STATUS_PASS)
    check("responses input_tokens_details 也识别", m.judge_kv_cache_second(
        {"input_tokens_details": {"cached_tokens": 8}})[0] == m.STATUS_PASS)
    check("cached_tokens=0 -> WARN", m.judge_kv_cache_second(
        {"prompt_tokens_details": {"cached_tokens": 0}})[0] == m.STATUS_WARN)
    check("字段缺失 -> WARN", m.judge_kv_cache_second(
        {"prompt_tokens": 10})[0] == m.STATUS_WARN)
    check("usage 缺失 -> WARN", m.judge_kv_cache_second(None)[0] == m.STATUS_WARN)


def test_judge_negative():
    print("[judge] C41 负向判定")
    check("400/404/422 -> PASS", all(m.judge_negative_status(s)[0] == m.STATUS_PASS
                                     for s in (400, 404, 422)))
    check("200/2xx -> FAIL（非法输入被静默接受）",
          m.judge_negative_status(200)[0] == m.STATUS_FAIL)
    check("500/503 -> FAIL", m.judge_negative_status(503)[0] == m.STATUS_FAIL)


def test_judge_over_context():
    print("[judge] C38 超长输入判定")
    check("4xx -> PASS（正确拒绝）",
          m.judge_over_context_status(400)[0] == m.STATUS_PASS)
    check("2xx -> WARN（静默接受）",
          m.judge_over_context_status(200)[0] == m.STATUS_WARN)
    check("5xx -> FAIL", m.judge_over_context_status(504)[0] == m.STATUS_FAIL)


def test_think_tag_detection():
    print("[judge] think 标签检测（C27-C32 泄漏断言）")
    marker = m.THINK_TAG_RE.pattern.split("|")[0]  # 第一个 alternative = 主 think 标记
    for text in (f"答案{marker}推理",
                 "<thinking>abc</thinking>", "THINK: 第一步"):
        check(f"识别泄漏: {text[:12]!r}", bool(m.THINK_TAG_RE.search(text)))
    for text in ("Paris is the capital.", "I think 6*7=42.",
                 "thinking about it later", "思考中", "让我想一想"):
        check(f"不误报: {text[:20]!r}", not m.THINK_TAG_RE.search(text))


def test_judge_thinking_leak_chat():
    print("[judge] C27 chat 思考泄漏判定")
    marker = m.THINK_TAG_RE.pattern.split("|")[0]
    check("正文含 think 标签 -> FAIL", m.judge_thinking_leak_chat(
        {"content": f"答案{marker}推理"})[0] == m.STATUS_FAIL)
    check("tool_call 轮 + 结构完整 -> PASS", m.judge_thinking_leak_chat(
        {"content": "", "tool_calls": [{"id": "call_1", "type": "function",
                                        "function": {"name": "get_weather",
                                                     "arguments": '{"city":"北京"}'}}]})[0] == m.STATUS_PASS)
    check("tool_call arguments 烂 JSON + length 截断 -> WARN", m.judge_thinking_leak_chat(
        {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {"name": "f", "arguments": '{"ci'}}]},
        "length")[0] == m.STATUS_WARN)
    check("tool_call arguments 烂 JSON 未截断 -> FAIL", m.judge_thinking_leak_chat(
        {"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                        "function": {"name": "f", "arguments": "not json"}}]})[0] == m.STATUS_FAIL)
    check("正文非空 -> PASS", m.judge_thinking_leak_chat(
        {"content": "api-ok"})[0] == m.STATUS_PASS)
    check("正文空 + reasoning 非空 -> WARN（预算被思考吃光）", m.judge_thinking_leak_chat(
        {"content": "", "reasoning": "推理中..."}, "length")[0] == m.STATUS_WARN)
    check("正文空 + 无思考 + 非工具轮 -> FAIL", m.judge_thinking_leak_chat(
        {"content": ""}, "stop")[0] == m.STATUS_FAIL)


def test_judge_thinking_leak_responses():
    print("[judge] C28/C29 responses 思考泄漏判定")
    def resp(output=None, text=None, incomplete=False):
        data = {"output": output or []}
        if text is not None:
            data["output_text"] = text
        if incomplete:
            data["status"] = "incomplete"
            data["incomplete_details"] = "max_output_tokens"
        return data
    marker = m.THINK_TAG_RE.pattern.split("|")[0]
    check("text 含 think 标签 -> FAIL", m.judge_thinking_leak_responses(
        resp(text=f"答{marker}x"))[0] == m.STATUS_FAIL)
    check("function_call 轮 -> PASS", m.judge_thinking_leak_responses(
        resp(output=[{"type": "function_call", "name": "get_weather",
                      "arguments": '{"city":"北京"}'}]))[0] == m.STATUS_PASS)
    check("function_call arguments 烂 + incomplete -> WARN", m.judge_thinking_leak_responses(
        resp(output=[{"type": "function_call", "name": "f", "arguments": "{"}],
             incomplete=True))[0] == m.STATUS_WARN)
    check("text 非空 -> PASS", m.judge_thinking_leak_responses(
        resp(output=[{"type": "message",
                      "content": [{"type": "output_text", "text": "api-ok"}]}]))[0] == m.STATUS_PASS)
    check("text 空 + 有独立 reasoning item -> WARN", m.judge_thinking_leak_responses(
        resp(output=[{"type": "reasoning",
                      "summary": [{"type": "summary_text", "text": "思考"}]}]))[0] == m.STATUS_WARN)
    check("text 空 + 无任何输出 -> FAIL",
          m.judge_thinking_leak_responses(resp())[0] == m.STATUS_FAIL)


def test_reasoning_direction():
    print("[judge] C30 方向校验（不符降 WARN）")
    check("缺省不判", m.reasoning_direction_issue(None, True) == ""
          and m.reasoning_direction_issue(None, False) == "")
    check("effort=none 有思考 -> WARN 提示", "reasoning content present" in
          m.reasoning_direction_issue("none", True))
    check("effort=high 无思考 -> WARN 提示", "silently downgraded" in
          m.reasoning_direction_issue("high", False))
    check("方向一致 -> 空串", m.reasoning_direction_issue("none", False) == ""
          and m.reasoning_direction_issue("low", True) == "")


def test_responses_extraction_vllm_reasoning():
    print("[judge] responses 抽取: reasoning item（vLLM content 格式）不污染可见正文")
    def body(msg_text):
        # dsv41（vLLM 系）真实形状: 思考放 reasoning item 的 content[].text, summary 为空
        return {"output": [
            {"type": "reasoning", "summary": [],
             "content": [{"type": "reasoning_text", "text": 'We need answer {"city": "坏'}]},
            {"type": "message", "status": "completed",
             "content": [{"type": "output_text", "text": msg_text}]},
        ]}
    b = body('{"city":"Shanghai","temperature_c":23}')
    check("extract_response_text 跳过 reasoning item",
          m.extract_response_text(b) == '{"city":"Shanghai","temperature_c":23}')
    parsed, issue = m.parse_json_from_text(m.extract_response_text(b))
    check("结构化 JSON 判定不被思考链污染", parsed is not None and "city" in parsed, issue)
    check("responses_reasoning_len 统计 content[].text",
          m.responses_reasoning_len(b) == len('We need answer {"city": "坏'))
    check("responses_text_of 只取 message 正文",
          m.responses_text_of(body("api-ok")) == "api-ok")
    cases = {c.number: c for c in openai_cases()}
    check("C04 校验器为 responses 口径（不再误挂 chat 校验器）",
          cases["C04"].validate is m.validate_responses_answer)


def test_check_tool_call_structure():
    print("[judge] tool_call 结构自校")
    ok_call = [{"id": "call_1", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":"北京"}'}}]
    check("结构完整 -> PASS", m.check_tool_call_structure(ok_call)[0] == m.STATUS_PASS)
    check("dict 型 arguments 也过", m.check_tool_call_structure(
        [{"id": "c", "function": {"name": "f", "arguments": {"a": 1}}}])[0] == m.STATUS_PASS)
    check("缺 id -> FAIL", m.check_tool_call_structure(
        [{"function": {"name": "f", "arguments": "{}"}}])[0] == m.STATUS_FAIL)
    check("空列表 -> FAIL", m.check_tool_call_structure([])[0] == m.STATUS_FAIL)


# ---------------------------------------------------------------- 请求构造 --
def test_param_probe_variants():
    print("[build] C05/C06 参数探测变体")
    chat = m.build_chat_param_probe_variants()
    check("chat 7 个变体（6 参数 + 组合）", len(chat) == 7, f"got {len(chat)}")
    labels = [v[0] for v in chat]
    check("含 temperature/top_p/top_k/max_tokens/max_completion_tokens/frequency_penalty",
          set("temperature top_p top_k max_tokens max_completion_tokens frequency_penalty".split())
          <= set(labels))
    mct = dict(chat)["max_completion_tokens"]
    check("max_completion_tokens 变体隔离（清掉 max_tokens 防互斥 400）",
          "max_tokens" in mct and mct["max_tokens"] is None)
    resp = m.build_responses_param_probe_variants()
    check("responses 6 个变体（5 参数 + 组合）", len(resp) == 6)
    check("responses 用 max_output_tokens 不用 max_tokens",
          "max_output_tokens" in dict(resp) and "max_tokens" not in dict(resp))


def test_matrix_builders():
    print("[build] C30/C31/C32/C33/C26/C41 矩阵构造")
    matrix = m.build_thinking_effort_matrix()
    check("C30 = 16 变体（8 态 x 2）", len(matrix) == 16, f"got {len(matrix)}")
    check("C30 effort 档位齐全",
          [e for _l, e, t in matrix if e is not None] == m.THINKING_EFFORTS * 2)
    check("C30 含缺省态 x2", sum(1 for _l, e, _t in matrix if e is None) == 2)
    check("C30 tool 侧 8 条", sum(1 for _l, _e, t in matrix if t) == 8)
    toggles = m.build_thinking_toggle_variants()
    check("C31 = 6 变体（3 开关形式 x on/off）", len(toggles) == 6)
    check("C31 覆盖 enable_thinking/thinking.type/ctk.thinking",
          {"enable_thinking=true", "thinking.type=enabled", "ctk.thinking=true"}
          <= {l for l, _e in toggles})
    rmatrix = m.build_responses_effort_matrix()
    check("C32 = 10 变体（5 态 x 2）", len(rmatrix) == 10, f"got {len(rmatrix)}")
    check("C32 effort 档位",
          [e for _l, e, t in rmatrix if e is not None] == m.RESPONSES_EFFORTS * 2)
    depth = m.build_structured_thinking_variants()
    check("C26 = 3 个结构化+思考变体", len(depth) == 3)
    check("C33 深度档位 low<high<max", m.DEPTH_EFFORTS == ["low", "high", "max"])
    neg = m.build_negative_variants("glm5.3")
    check("C41 = 4 个负向变体", len(neg) == 4)
    check("C41 invalid model（吸收旧 F_err）",
          dict(neg)["invalid_model"]["model"] == "not-exist-model")
    check("C41 messages 类型错",
          dict(neg)["invalid_messages_type"]["messages"] == "not-a-list")
    check("C41 数值参数类型错",
          dict(neg)["invalid_numeric_param"]["temperature"] == "not-a-number")
    check("C41 畸形 tool schema", dict(neg)["malformed_tool_schema"]["tools"][0]["function"]
          ["parameters"]["type"] == "invalid-type")


def test_matrix_payloads():
    print("[build] C30/C32 交叉矩阵 payload 构造（全非流式）")
    config = make_config()
    p_default = m.thinking_matrix_payload(config, None, False)
    check("缺省无 tool: 无 reasoning_effort/tools 键",
          "reasoning_effort" not in p_default and "tools" not in p_default)
    p_effort = m.thinking_matrix_payload(config, "high", False)
    check("effort 态: 顶层 reasoning_effort=high, 无 tools",
          p_effort.get("reasoning_effort") == "high" and "tools" not in p_effort)
    p_tool = m.thinking_matrix_payload(config, "low", True)
    check("effort+tool 态: tools+tool_choice 齐全",
          p_tool.get("reasoning_effort") == "low" and p_tool.get("tool_choice") == "auto"
          and p_tool["tools"][0]["function"]["name"] == "get_weather")
    check("chat payload 全非流式（功能线已去流式）",
          "stream" not in p_tool and p_tool.get("stream") is None)
    r_default = m.responses_matrix_payload(config, None, False)
    check("responses 缺省: 无 reasoning 键", "reasoning" not in r_default)
    r_effort = m.responses_matrix_payload(config, "medium", True)
    check("responses effort+tool: reasoning.effort + 扁平 tools",
          r_effort.get("reasoning") == {"effort": "medium"}
          and r_effort["tools"][0]["type"] == "function"
          and r_effort["tools"][0]["name"] == "get_weather")


def test_video_payload():
    print("[build] C15 视频载荷（自包含/可复现）")
    part = m.build_video_content_part()
    check("content part 类型 video_url", part["type"] == "video_url")
    url = part["video_url"]["url"]
    check("data URL 前缀", url.startswith("data:video/mp4;base64,"))
    import base64
    raw = base64.b64decode(url.split(",", 1)[1])
    check("mp4 盒子结构（ftyp 开头）", raw[4:8] == b"ftyp")
    check("确定性（两次生成一致）", m.build_minimal_mp4() == m.build_minimal_mp4())
    check("尺寸有界（< 1KB）", len(raw) < 1024)


def test_variant_outcome_summary():
    print("[aggregate] 变体结果聚合口径")
    oks = [m.VariantOutcome("a", m.STATUS_PASS, 200), m.VariantOutcome("b", m.STATUS_PASS, 200)]
    warns = oks + [m.VariantOutcome("c", m.STATUS_WARN, 400, "rejected")]
    fails = warns + [m.VariantOutcome("d", m.STATUS_FAIL, 500, "server error")]
    check("全 PASS -> PASS", m.summarize_variant_outcomes(oks)[0] == m.STATUS_PASS)
    check("有 WARN 无 FAIL -> WARN", m.summarize_variant_outcomes(warns)[0] == m.STATUS_WARN)
    check("任一 FAIL -> FAIL", m.summarize_variant_outcomes(fails)[0] == m.STATUS_FAIL)
    check("detail 含各变体状态", "a=PASS" in m.summarize_variant_outcomes(oks)[2])


def test_median():
    print("[math] C33 中位数")
    check("奇数个", m._median([3, 1, 2]) == 2)
    check("偶数个", m._median([4, 1, 3, 2]) == 2.5)


def _fake_status_error(mod):
    """构造一个带 status_code 的 APIStatusError 实例（不发真实请求, 绕过父类构造）。"""
    class _FakeStatusError(mod.APIStatusError):
        status_code = 404

        def __init__(self):  # 绕过父类 __init__（父类会读 response 对象）
            pass

    return _FakeStatusError()


def test_sdk_import_shape():
    print("[sdk] openai SDK lazy import 判定")
    mod = m.import_openai_sdk()
    if mod is None:
        check("未安装时返回 None（SKIP 路径）", True)
    else:
        check("已安装返回模块", hasattr(mod, "OpenAI"))
        check("4xx APIStatusError -> WARN", m.judge_sdk_exception(
            _fake_status_error(mod), mod)[0] == m.STATUS_WARN)
        check("普通异常 -> FAIL", m.judge_sdk_exception(ValueError("boom"), mod)[0] == m.STATUS_FAIL)


def test_numbering_after_effort_changes():
    print("[numbering] efforts 配置变化时专有检查编号仍连续且唯一")
    cases = openai_cases(efforts=["low", "high"])
    numbers = [c.number for c in cases]
    check("efforts=2 档时 48 条（50 - 2 档）", len(cases) == 48, f"got={len(cases)}")
    check("编号仍唯一", len(set(numbers)) == len(numbers))
    check("medium=C58 / session=C59 顺延",
          "C58" in numbers and "C59" in numbers and "C60" not in numbers)
    by_num = {c.number: c for c in cases}
    check("medium 档位用例名标注已知缺陷", "已知缺陷" in by_num["C58"].name)
    cases_full = openai_cases(efforts=["low", "high", "xhigh", "max", "ultra"])
    numbers_full = [c.number for c in cases_full]
    check("efforts=5 档时 51 条, medium=C61 / session=C62",
          len(cases_full) == 51 and "C61" in numbers_full and "C62" in numbers_full)


def main():
    test_case_numbering()
    test_no_f_residue()
    test_filter_cases()
    test_list_output()
    test_gates()
    test_no_stream_anywhere()
    test_judge_param_probe()
    test_judge_kv_cache()
    test_judge_negative()
    test_judge_over_context()
    test_think_tag_detection()
    test_judge_thinking_leak_chat()
    test_judge_thinking_leak_responses()
    test_reasoning_direction()
    test_check_tool_call_structure()
    test_param_probe_variants()
    test_matrix_builders()
    test_matrix_payloads()
    test_video_payload()
    test_variant_outcome_summary()
    test_median()
    test_sdk_import_shape()
    test_numbering_after_effort_changes()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项: {FAILS}")
        return 1
    print("功能测试用例矩阵单测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
