# evalscope 测试控制台（SLA Peak Search Kit）

对 OpenAI 兼容推理服务（vLLM / MindIE / 路由网关等）做 **evalscope 摸高测试**：
`--sla-auto-tune` 二分搜索满足 SLA 判据（TTFT / TPOT）的**最大并发数**。

六条测试线，共用一份 `config.yaml`，统一通过 Web 控制台或命令行驱动：

| 测试线 | 入口 | 产出 |
|---|---|---|
| 摸高（性能） | `bash run.sh` | `results/<场景ID>/…/sla_summary.json` + `report.py` 汇总表 |
| 精度（pass@k） | `bash run.sh --accuracy` | `results/accuracy/<tag>_pass@k<k>_r<N>/…` + `scores.md` |
| 功能（API E2E） | `bash run.sh --functional` | `results/functional/e2e_<时间戳>/report.md` + 各用例响应原文 |
| 智能体回放（AgentX） | `bash run.sh --agentx` | `results/agentx/<tag>_<时间戳>/…`（summary/metrics json + 关键指标打印） |
| 函数调用（BFCL v4） | `bash run.sh --bfcl` | `results/bfcl/<tag>[_limit<N>]/reports/…/bfcl_v4.json` + `results/bfcl/scores.md` |
| 智能体代码（SWE-bench_Pro） | `bash run.sh --swe status` | live 产物在 `swe_pro` 工作区（`swe_runner.py` 只读监控/聚合/补跑，见 SWE 章节） |

## 目录结构

```
evalscope_sla_kit/
├── setup.sh            # 一键安装环境（含 evalscope custom 插件 offset 补丁）
├── run.sh              # 入口（后台/前台/dry-run/单场景；--accuracy / --functional 分发）
├── config.yaml         # 全部配置：服务地址、模型名、Host 头、场景清单、精度数据集组、功能参数
├── runner.py           # 摸高: 场景解析 + 数据集生成 + evalscope 命令构建执行
├── accuracy_runner.py  # 精度: 数据集组 x 轮的 evalscope eval + pass@k/mean 分数解析汇总
├── agentx_runner.py    # AgentX: 智能体回放（cc-traces 公共语料 / local:<目录> 本地裁剪语料）
├── bfcl_runner.py      # BFCL: 函数调用评测（22 子集, evalscope bfcl_v4 适配器, 独立 bfcl-venv）
├── swe_runner.py       # SWE-bench_Pro: 只读监控/聚合/补跑（评测本体是 swe_pro 工作区的分批驱动）
├── scripts/run_api_e2e.py  # 功能测试: API E2E 框架（50 条用例 C01-C61 单一编号, 全非流式, PASS/WARN/FAIL/SKIP）
├── scripts/swe/        # swe_pro 驱动脚本的归档副本（只存档不执行, live 跑的是原件）
├── report.py           # 摸高结果汇总（并发-指标表，可折算单卡吞吐）
├── webapp.py           # Web 控制台后端（FastAPI，端口 8300）
├── web/index.html      # Web 控制台前端（八页签单页应用，深色玻璃拟态）
├── tools/              # gsm8k 数据集生成 / jsonl→prompts / weka 裁剪 / 守护脚本 / BFCL 入口 / SWE 工具副本
├── datasets/           # 生成的数据集（按文件名复用；weka 本地语料也放这里）
├── results/            # 摸高 + 精度 + 功能 + AgentX + BFCL 输出
└── logs/               # 运行日志
```

## Web 控制台

```bash
python3 webapp.py --port 8300     # 浏览器打开 http://<主机IP>:8300
```

八个页签（深色玻璃拟态 UI，Fira Sans/Code 字体，Lucide SVG 图标）：

### 1. 配置项
- 服务 URL / 模型名 / Tokenizer 路径 / API Key
- 请求头 Host 开关与值、额外请求头
- 连接测试按钮（GET /v1/models 验证可达）
- 保存到 `web_state.json`，不覆盖 `config.yaml` 注释

### 2. 测试用例
- 用例列表（增删启停），数据集条数在添加时固化
- gsm8k 用例：逐用例生成数据集（按文件名去重复用），状态实时刷新
- **摸高参数**（并发下界/上界、每档轮数、请求数倍数、warmup、worker 数）
- 添加用例表单（数据集/输入/输出/命中率/前缀/SLA/条数）

### 3. 测试工程
- 新建工程时勾选要执行的用例
- 工程卡片：进度条 + 已完成/进行中/未完成统计
- 工程详情表：**只显示最优结果**（最优并发 / 最优 TTFT/TPOT / 峰值输出吞吐），点「查看详情」看完整曲线 + SVG 柱状图 + SLA pass/fail 行高亮 + CSV 导出
- **打包下载**：一键打包工程所有日志 + 结果为 zip
- 启动/终止任务（终止真正杀掉子进程树，`killpg` 整组杀）
- 运行中用例显示实时耗时，5 秒自动刷新

### 4. 功能测试
- 一键运行 / 停止（`killpg` 杀进程树）
- **用例清单表**：22 条用例，每条带**复制 curl**按钮（HTTP 下用 `execCommand` fallback）
- 运行状态卡（实时耗时、日志查看）
- 测试报告：渲染 `report.md`（Summary + Cases 表 + Failures），PASS/WARN/FAIL/SKIP 彩色标注
- 用例清单弹窗（`--list` 输出）

### 5. 精度测试
- **数据集多选**：checkbox 网格（gpqa_diamond / aime25 / aime26 / gsm8k / mmlu），全选/全不选
- 运行参数：`--limit`（冒烟）/ pass@k / 轮数 / effort / max_tokens / batch_size
- 两种运行入口：顶部按钮（默认参数）/ 参数卡按钮（自定义参数）
- 运行 / 停止（`killpg` 杀进程树）
- 精度汇总：渲染 `scores.md`（pass@k 表格）

### 6. AgentX 回放
- 变体选择（cc-traces 公共语料 / `local:<目录>` 本地裁剪语料）+ 运行参数卡
- 运行 / 停止（`killpg` 杀进程树），结果卡渲染 summary / 关键指标

### 7. BFCL 函数调用
- 子集勾选（按 6 大类分组，web_search / memory 默认不勾）+ 运行参数
- 运行 / 停止（`killpg` 杀进程树），报告渲染 `results/bfcl/scores.md`（acc 表 + Overall）

### 8. SWE-bench_Pro
- **只读监控**（60s 轮询）：状态卡（当前批 / 进度条+ETA / 崩溃标记 / 进程活性 / binfmt / 磁盘）
- 聚合表（repo × resolved/total/acc）、批次时间线（batches.log 尾部）、补跑队列卡
- **没有运行/停止按钮**：启动与补跑一律走 CLI（`start-rerun` 有双驱动硬护栏），防止 web 误启动撞上正在跑的评测

## 快速开始

```bash
# 1. 一键安装（自动给 evalscope custom 插件打 dataset_offset 补丁）
bash setup.sh

# 2. 改配置（必改: url / model / tokenizer_path）
vi config.yaml

# 3. 预检
bash run.sh --dry-run

# 4. 开跑（后台 nohup）
bash run.sh
tail -f logs/run_*.log

# 5. 汇总
python3 report.py --npu 8   # 8 卡时输出单卡吞吐列

# 或用 Web 控制台
python3 webapp.py --port 8300
```

## 精度测试（accuracy_runner.py）

对被测服务跑 evalscope eval 数据集组（gsm8k / mmlu / gpqa_diamond / aime25+aime26 …），
逐组逐轮顺序执行并解析精度分数。**宿主机直跑**（不走 docker）。

精度口径 = **pass@k**（k 可配）：每题重复采样 k 次，evalscope 用
`aggregation=mean_and_pass_at_k` 聚合，结果同时给出 **mean** 与 **pass@1..pass@k**。

```bash
bash run.sh --accuracy                      # 后台跑所有启用数据集组
bash run.sh --accuracy -f                   # 前台跑
bash run.sh --accuracy --resume             # 后台断点续跑（复用已有 predictions）
python3 accuracy_runner.py --list           # 列出启用的数据集组
python3 accuracy_runner.py --only all5      # 只跑指定组
python3 accuracy_runner.py --only all5 --resume   # 断点续跑：复用已有 predictions 只补缺样本
python3 accuracy_runner.py --k 4            # 覆盖 pass@k 的 k
python3 accuracy_runner.py --limit 10       # 冒烟：每组只评 10 条
python3 accuracy_runner.py --report         # 重打已有结果汇总表
```

### config.yaml accuracy 段

```
"tag, evalscope_datasets, rounds, effort, max_tokens, batch_size [, k]"
```

| 字段 | 说明 |
|---|---|
| `tag` | 数据集组名 |
| `evalscope_datasets` | 传给 `--datasets` 的值（空格分隔多个） |
| `rounds` | 重复轮数 |
| `effort` | `reasoning_effort`（`medium` 在部分部署上必 400，避开） |
| `max_tokens` | 生成上限 |
| `batch_size` | `--eval-batch-size` |
| `k` |（可选）pass@k 的采样次数，默认 `1` = 单次 mean 口径；`k>1` 时请求量 × k |

其他键：`evalscope_bin`（evalscope 可执行文件，可被环境变量 `EVALSCOPE_BIN`
覆盖）；`proxy`（非空时给子进程导出 `http_proxy`/`https_proxy`——evalscope 首次
下载评测集需要外网；被测服务主机名自动加入 `no_proxy` 防劫持，不污染父进程环境）。

### pass@k 口径怎么落地

k>1 时 runner 对 evalscope 命令做三处改动（其余参数与单次口径一致）：

1. CLI 从 `evalscope` 切换到 **`tools/passk_evalscope.py`**（wrapper，行为与原
   CLI 完全一致，只多一步 dataset-args 预处理）；
2. 追加 `--repeats <k>`（每题采样 k 次，请求量 × k）；
3. 追加 `--dataset-args`（每个数据集都要给，主指标取本组的 pass@k，即 k）：

```
--dataset-args '{"<ds>": {"aggregation": "mean_and_pass_at_k",
                          "primary_metric": {"name": "accuracy", "aggregation": "pass_at_k",
                                             "dimensions": {"k": <k>}}}}'
```

`--repeats` 让每题被复制 k 份各采样一次（evalscope 内部按 `group_size=k`
重新分组），`mean_and_pass_at_k` 聚合器据此算出 mean 与 pass@1..pass@k。
`primary_metric` 必须显式声明：该聚合产出 k+1 个非诊断指标，evalscope 1.14.x
的报告生成器要求唯一主指标，缺了必崩（`ValueError: Found 5 non-diagnostic
metric identities; no unique primary metric`）；而 JSON dict 形式的
primary_metric 只有转成 `MetricSelector` 对象才有效（这正是 wrapper 存在的
原因，机制细节见 `tools/passk_evalscope.py` 头注释）。
结果目录与 k=1 的单次口径分开（`<tag>_pass@k<k>_r<N>`），不同 k 也各占一个目录，
两种口径/各 k 的结果不会互相覆盖。

> **冒烟（`--limit N`）另存目录**：`<tag>_pass@k<k>_limit<N>_r<N>`。limit 会
> 截断样本数，若与全量共用 work-dir，冒烟结果会覆盖全量报告、并把少量预测混进
> 全量 predictions 缓存（后续 `--resume` 会静默复用这批脏缓存）。所以 limit
> 单独隔离目录。注意 `--limit` 是**每个子集 N 条**：mmlu 有 57 个子集，
> `--limit 5` 实际会评 5×57=285 题。

### 断点续跑（--resume）

精度一轮动辄数小时（all5 全量 + pass@k 的 ×4 请求量 ≈ 20h），中途崩掉或被打断后
**不要删 work-dir 重跑**——`predictions/` 里是已完成的推理成果。`--resume` 让
evalscope 复用它们，只补缺的样本、重算判分与报告：

```bash
python3 accuracy_runner.py --only all5 --k 4 --resume   # 自动指向 results/accuracy/all5_pass@k4_r1
bash run.sh --accuracy --resume                          # 等价（参数透传给 runner）
```

底层是给 evalscope 追加两个参数（runner 自动带上，无需手填）：

- `--use-cache <work-dir>`：work-dir 指向已有目录，按样本 id 复用 predictions、
  缺哪补哪。实测 gpqa 792 条预测（96MB）复用 + 重出报告只花 1.7s，对比原路 4h。
- `--rerun-review`：**必带**。evalscope 会校验"缓存身份哈希"（命令 + 数据集参数
  的指纹），命令一变（如中途补了 `primary_metric`）就对不上并拒跑：
  `ValueError: Cached evaluation identity does not match`。`--rerun-review`
  表示明确放弃哈希核对、复用 predictions、重算 review/聚合/报告（规则判分，秒级）。

注意：

- 身份校验被 `--rerun-review` 绕过后，evalscope **不再帮你核对**缓存 predictions
  是否与本次命令同口径——改了 k / 换数据集组合 / 换采样参数还继续 `--resume`，
  旧预测会被静默混进新报告。换口径请换 tag（新 work-dir）。
- `tools/wait_service_passk.sh` 检测到 work-dir 已有 predictions 时自动加
  `--resume`：服务闪断 → 守护等待恢复 → 断点续跑，全程不需要人工记命令。

### reasoning_effort 已知缺陷

effort 通过 generation-config 的 `extra_body` 顶层展开进请求体（evalscope →
OpenAI SDK，已抓包验证）。服务端接受 `low/high/xhigh/max/none` 及整数 1~100；
**`"medium"` 在部分部署上必 HTTP 400**（服务端 chat template 校验拒绝，见
sunzhi 仓 F-03 / docs/lessons_api208.md §4），配置时避开。

### 结果与汇总

- 每组每轮跑完即打印 `  [tag_r1] gsm8k: score=0.94 (n=1319)`；
  pass@k 口径再追加一行 `  [tag_r1] gsm8k: pass@1=0.87  pass@2=0.94  ...`；
  多轮同组结束后打印均值（mean 与各 pass@k 分别平均）。
- 分数来源：`results/accuracy/<tag>_r<N>/reports/<model>/<ds>.json` 的 `metrics`：
  `identity.aggregation=="mean"` 的 `score`/`num`（mean 口径），
  `identity.aggregation=="pass_at_k"` 且 `identity.dimensions.k==n` 的 `score`
  （pass@n）。文件缺失/解析失败按缺失提示，不中断。
- 每跑完一轮就追加写 `results/accuracy/scores.md`（时间戳头 + tag/轮/数据集/mean/n +
  pass@1..pass@k 列），中途被打断也保留已完成轮次；`--report` 可随时重打汇总表。
- 分组 key 带 k（`all5_pass@k4`），所以 k=1 与 k=4 的同 tag 结果**不会**被当成
  两轮混在一起算均值，实时表与 `--report` 表的 tag 也一致。
- 退出码：任一组失败 → 1；**evalscope 退出码 0 但没解析到分数也按假通过记 1**
  （报告缺失 / 指标为空 / 报告结构变化都会落到这里）；`--only` 含未知 tag → 2。

## 功能测试（scripts/run_api_e2e.py）

Simaas 风格 API E2E 框架：**50 条用例，单一 C 编号**（用户矩阵 C01-C41 除流式空洞
外全覆盖；C42-C50 的检查并入对应编号；kit 专有检查续编 C51-C61），
覆盖兼容性/参数探测/能力/SDK/结构化输出/思考泄漏/上下文/用量/负向等，
**全部非流式**，判定 **PASS / WARN / FAIL / SKIP**。纯标准库 urllib 发请求
（C16/C18/C19 用 openai SDK，lazy import，未安装时 SKIP）。

```bash
bash run.sh --functional                        # 后台跑全部用例
bash run.sh --functional -f                     # 前台跑
python3 scripts/run_api_e2e.py --list           # 列出全部用例（五列, 按编号排序）
python3 scripts/run_api_e2e.py --only C30       # 只跑指定用例（C 编号 / gate tag / 名字子串）
python3 scripts/run_api_e2e.py --dry-run        # 打印计划不发请求
```

### 用户矩阵用例（C01-C41）

> **C17/C20/C46 流式用例已按需求移除**（编号保留空洞不重排），流式能力由性能线覆盖。
> C42-C50 是同一批检查的原始描述，已并入对应编号：
> C42→C01、C43→C02、C44→C03、C45→C04、C47→C22、C48→C35、C49→C39、C50→C37。

| 编号 | category | 检查点（判定要点） |
|---|---|---|
| C01 | openai/compatibility | GET /v1/models 列模型（吸收 C42「无 body」） |
| C02 | openai/compatibility | chat 冒烟，正文非空（吸收 C43） |
| C03 | openai/compatibility | completions 冒烟（吸收 C44） |
| C04 | openai/compatibility | responses 冒烟；4xx→WARN（吸收 C45） |
| C05 | openai/param-probe | 逐参数隔离探测 temperature/top_p/top_k/max_tokens/max_completion_tokens/frequency_penalty+组合；2xx=PASS，4xx=WARN（不支持），5xx=FAIL |
| C06 | openai/param-probe | responses 同款参数探测（max_output_tokens 替代 max_tokens） |
| C07 | openai/capability | tools 触发 tool_calls |
| C08 | openai/capability | Responses API function tool；断言 function_call item + arguments 合法 JSON |
| C09 | openai/capability | Responses API web_search tool；**仅 gpt 系模型**，否则 SKIP |
| C10 | openai/capability | chat_template_kwargs.thinking on/off；方向不符→WARN |
| C11 | openai/capability | responses reasoning.effort=low，正文非空 |
| C12 | openai/capability | completions logprobs+echo；缺 logprobs 字段→WARN |
| C13 | openai/capability | chat KV cache：同前缀两连发，第二发 usage.prompt_tokens_details.cached_tokens>0；缺失/0→WARN |
| C14 | openai/capability | responses KV cache（input_tokens_details.cached_tokens，口径同 C13） |
| C15 | openai/capability | 视频理解（自包含 mp4 探测载荷）；端点拒绝→WARN |
| C16 | openai/sdk | openai SDK responses.create 严格解析（4xx→WARN=端点未实现） |
| C18 | openai/sdk | openai SDK responses function tool（tool_choice 强制） |
| C19 | openai/sdk | openai SDK responses web_search；**仅 gpt 系模型**，否则 SKIP |
| C21 | openai/structured-output | 仅靠 prompt 的 best-effort JSON 输出 |
| C22 | openai/structured-output | response_format=json_object（吸收 C47） |
| C23 | openai/structured-output | response_format=json_schema strict；required 字段校验 |
| C24 | openai/structured-output | responses text.format json_schema |
| C25 | openai/structured-output | GLM 风格：system 给 schema + json_object |
| C26 | openai/structured-output | json_object × 思考关闭/effort 变体，仍须产出合法 JSON |
| C27 | openai/thinking-leak | chat 带 tools 不带思考参数：正文无 think 标签且非空（tool_call 轮除外），思考走 reasoning_content；截断→WARN |
| C28 | openai/thinking-leak | responses 带 tools 不带 reasoning：正文无 think 标签，思考为独立 reasoning item |
| C29 | openai/thinking-leak | responses 不带 tools 不带 reasoning，断言同 C28 |
| C30 | openai/thinking-leak | 缺省+顶层 reasoning_effort 7 档 × {无/有 tool}=16 请求（全非流式）；正文/泄漏/方向/结构四类断言，4xx→WARN |
| C31 | openai/thinking-leak | enable_thinking / thinking.type / ctk.thinking 三种开关 × on/off（带 tool）=6 请求 |
| C32 | openai/thinking-leak | responses reasoning.effort 4 档 × {无/有 tool}=10 请求（尊重 --skip-responses-api） |
| C33 | openai/thinking-depth | 思考深度探针（**opt-in**）：ctk {thinking,reasoning_effort} low/high/max，每档 3 次取中位，断言单调递增，不达标→WARN |
| C34 | openai/context | system prompt 回显（marker 断言） |
| C35 | openai/context | multi-turn 召回 secret token（吸收 C48） |
| C36 | openai/context | 末尾追加运行时 system 消息，正文非空 |
| C37 | openai/context | 长上下文 needle 召回（吸收 C50；受 skip_long_context_api 门控，默认关） |
| C38 | openai/context | 超长输入=service.max_context_length×1.1 填充；4xx=正确拒绝 PASS，2xx→WARN（原表 Endpoint 笔误写 completions，按分组语义以 chat completions 实现） |
| C39 | openai/finops | chat usage 一致性（吸收 C49；非流式请求断言 usage 字段齐且 prompt+completion=total） |
| C40 | openai/finops | completions usage 一致性 |
| C41 | openai/robustness | 负向矩阵：invalid model / messages 类型 / 数值参数 / tool schema 四变体，全部期望 4xx；2xx 静默接受→FAIL |

### 专有检查（C51-C61）

F 矩阵未覆盖、kit 自带的专有检查，续编在用户矩阵之后；effort 档位展开自
`functional.efforts`（默认 `[low, high, xhigh, max]` 时编号如下，档位数变化时
medium/会话顺延）：

| 编号 | category | 检查点（判定要点） |
|---|---|---|
| C51 | openai/robustness | completions max_tokens=8 截断：usage.completion_tokens<=8 |
| C52 | openai/robustness | completions stop 参数：命中 stop 词后截断 |
| C53 | openai/robustness | completions 确定性：temperature=0 连发两次文本一致 |
| C54 | openai/capability | thinking 默认开：响应含 reasoning 内容 |
| C55 | openai/capability | reasoning_effort=none：reasoning 内容为空 |
| C56 | openai/capability | reasoning_effort=low：2xx 且含 reasoning 内容 |
| C57 | openai/capability | reasoning_effort=high（档位展开自 functional.efforts） |
| C58 | openai/capability | reasoning_effort=xhigh |
| C59 | openai/capability | reasoning_effort=max |
| C60 | openai/capability | reasoning_effort=medium 已知缺陷：400→WARN（缺陷仍在），2xx→WARN（静默接受），服务异常→FAIL |
| C61 | openai/session | claude code 会话（恒 SKIP，真会话跑 `tools/f05_claude_code.sh`） |

### 新增开关

| 开关（CLI / config） | 默认 | 作用 |
|---|---|---|
| `--skip-responses-api` / `functional.skip_responses_api` | false | 跳过全部 /v1/responses 用例（C04/C06/C08/C09/C11/C14/C16/C18/C19/C24/C28/C29/C32 共 13 条） |
| `--thinking-depth-check` / `functional.thinking_depth_check` | false | 开启 C33 思考深度探针（3 档 ×3 次=9 请求） |
| `--long-context-api` / `functional.skip_long_context_api: false` | true（跳过） | 开启 C37/C38；C38 规模由 `service.max_context_length` 决定（未设置则 SKIP） |
| `--only C05,C41` / `functional.skip: [C30]` | — | C 编号 / gate tag / 用例名子串均可作为过滤/跳过键 |

### 判定口径

| 判定 | 含义 | 计入退出码 |
|---|---|---|
| PASS | 正常 | 否 |
| FAIL | 失败 | **是** |
| WARN | 已知缺陷 / 可选能力未实现（如端点 404、参数不支持、KV cache 未开、SDK 4xx） | 否 |
| SKIP | 主动跳过（config skip / 门控 / 非 gpt 模型 / SDK 未安装） | 否 |

矩阵类用例（C05/C06/C10/C26/C30-C32/C41）聚合口径：任一变体 FAIL→FAIL，
有 WARN 无 FAIL→WARN，全 PASS→PASS；变体明细写在报告 detail 列。

## AgentX 智能体回放（agentx_runner.py）

用 agentx-harness 回放 SemiAnalysis 公开的 Claude Code 真实会话 trace
（393 个智能体编码会话：多轮、子智能体、前缀复用 / KV cache 命中模式），
测**真实智能体工作负载**下的性能（TTFT / 吞吐 / 缓存命中），不是精度。
**宿主机直跑**（setup.sh 已装 agentx-harness）。

```bash
bash run.sh --agentx                      # 后台跑所有启用变体（日志 logs/agentx_*.log）
bash run.sh --agentx -f                   # 前台跑
bash run.sh --agentx -f --only weka8      # 前台只跑指定变体
python3 agentx_runner.py --list           # 列出启用变体
python3 agentx_runner.py --dry-run        # 只打印 agentx 命令不执行
python3 agentx_runner.py --concurrency 80 # 覆盖并发会话数（覆盖 config）
```

### config.yaml agentx 段

```
"tag, public_dataset | local:<目录>, concurrency [, tokenizer]"
```

| 字段 | 说明 |
|---|---|
| `tag` | 变体名（`--only` / 结果目录名用） |
| `public_dataset` | HuggingFace 公共语料（agentx 自动下载），或 `local:<目录>` 本地裁剪语料 |
| `concurrency` | 并发会话数（= 回放车道数） |
| `tokenizer` |（可选）行内 tokenizer（HF 名，自动带 `--tokenizer-trust-remote-code`）；留空 = `service.tokenizer_path` |

公共语料变体（HuggingFace `semianalysisai/cc-traces-weka-*`）：
`full`（1M ctx，135k 请求）/ `256k`（256k ctx，68k 请求，兼容小上下文，推荐）。

### 本地裁剪语料（cc-traces-weka-062126）

公共语料是全量回放（数十万请求）；要测特定画像（如 80 会话 × 8 轮 × 平均
输入 65k × 缓存命中 80%），先用裁剪工具从 HF 快照生成 `local:<目录>` 语料：

```bash
# 快照路径: hf_cache 里 datasets--semianalysisai--cc-traces-weka-062126/snapshots/*/traces.jsonl
python3 tools/curate_weka_traces.py \
    --src <HF快照>/traces.jsonl --out-dir datasets/weka8 \
    --main-turns 8 --sessions 80 --target-isl 65000 --target-hit 0.80 \
    --max-context 204800 --idle-cap 300
# 输出: datasets/weka8/<会话id>.json（每会话一个文件）
#       datasets/weka8_stats.json（汇总; total_requests = 回放 request-count 上限）
```

然后取消 config.yaml agentx 段的注释并开跑：

```yaml
- "weka8, local:datasets/weka8, 80"
```

```bash
bash run.sh --agentx -f --only weka8
```

裁剪算法（确定性、可复现）：每会话只保留前 N 个主轮次（窗口内派生的子
智能体会话整体保留），按「平均输入长度 / 理论前缀缓存命中率」最接近目标
值打分选出 M 个会话，超上下文上限的先剔除。glm5.3（204.8k ctx）实测：

- 810 请求全部成功（640 主轮 + 170 子智能体），0 失败
- ISL p50 65.4k（= 目标 65k）；理论前缀缓存命中 77.75%（hash_ids 差集推得）
- 结果目录 `results/agentx/weka8_<时间戳>/`

### local: 变体的固定回放参数（为什么和公共语料不同）

`local:` 语料会被 runner 固定成一组和公共语料不同的 agentx 参数（写死在
`build_cmd`，不能从 config 覆盖）—— 都是实测踩出来的坑：

| 参数 | 原因 |
|---|---|
| `--scenario inferencex-agentx-mvp` | 默认 `agentx` 场景注入 `warmup_requests_per_lane=10` 加速预热，但预热**真实消耗回放轮次**：8 轮短语料直接被吃光；且 80 并发预热 = ~80 条零间隔大 prefill 同时打出，排队网关（如 ModelArts）会杀掉排队超时请求，单条失败即整体中止 |
| `--unsafe-override` | 本地目录不是场景锁校验的 pinned 公共语料，必须显式 override（聚合结果标记 `submission_valid=false`，不影响指标） |
| `--request-count <stats 的 total_requests>` | 车道跑完短会话会**回收重放**（循环消费语料），把裁剪窗口外的轮次挤进来；用裁剪总量封顶 |
| `--trajectory-start-min/max-ratio 0` | 预设 0.25~0.75 随机起点会跳过短语料的大部分轮次，固定从第 0 轮回放整个裁剪窗口 |
| `--concurrency-ramp-duration 120` | 不传时全部车道 t=0 同时派发第 0 轮（~N 条全新 65k prefill 同时打到排队网关，同预热坑）；120s 斜坡错开起步 |
| `--benchmark-duration 3600` | 回放时长上限；weka8 实测 ~11 分钟（最长会话 987s），3600 留慢端点余量（mvp 场景不传时自动填 1800） |
| `--trace-idle-gap-cap-seconds 300` | 与裁剪 `--idle-cap` 对齐：轮间超长空档（人走了）压缩到 300s，否则回放时长被空闲时间主导 |
| `--max-context-length` | 取 `service.max_context_length`（可选）：超长 trace 在客户端先过滤，避免打服务端 400 |

`--random-seed 42` / `--failed-request-threshold 0.10` / `--use-server-token-count`
与原 `agentx` 场景预设一致，mvp 场景不注入，显式补齐。

> 中途终止用 Ctrl-C（SIGINT）：aiperf 收到 SIGINT 后优雅取消并落盘已完成的
> 请求，指标仍可解析。

## BFCL 函数调用评测（bfcl_runner.py）

用 evalscope 的 bfcl_v4 适配器跑 Berkeley Function-Calling Leaderboard v4
（22 个子集 / 6 大类），测**函数调用精度**（AST 匹配 / 多轮状态机 / 幻觉拒绝），
不是性能。官方文档:
<https://evalscope.readthedocs.io/zh-cn/latest/third_party/bfcl_v4.html>

依赖装在**独立 venv**（不碰 /tmp/bench-venv，不动 SWE 评测）:
`/mnt/sfs_turbo/c00803992/bfcl-venv`（NFS 上，容器重建不丢）。
评测命令统一经 `docker exec ais-bench` 派发到容器内用该 venv 执行
（webapp 在容器内时自动直跑，不加 docker 前缀）。

```bash
bash run.sh --bfcl                                   # 后台跑启用子集（日志 logs/bfcl_*.log）
bash run.sh --bfcl -f                                # 前台跑
bash run.sh --bfcl -f --only simple_python,irrelevance --limit 10   # 冒烟（20 请求）
python3 bfcl_runner.py --list                        # 列出 22 个子集与所属类别
python3 bfcl_runner.py --dry-run                     # 只打印 docker exec 命令不执行
bash run.sh --bfcl -f --resume                       # 断点续跑（复用 work-dir 已有推理）
python3 bfcl_runner.py --report                      # 汇总 results/bfcl/ 各次结果
```

结果: `results/bfcl/<tag>[_limit<N>]/reports/<模型>/bfcl_v4.json`（tag 默认=模型名），
每次运行追加写 `results/bfcl/scores.md`（子集 acc 表 + 类别汇总 + OVERALL）。

### config.yaml bfcl 段

```yaml
bfcl:
  venv_python: /mnt/sfs_turbo/c00803992/bfcl-venv/bin/python   # 容器内可见的独立 venv
  container: ais-bench                 # 派发目标容器
  url: http://192.168.0.9:18020/v1     # 缺省回退 service.url
  model: dsv4                          # 缺省回退 service.model
  api_key: "EMPTY"                     # 缺省回退 request.api_key
  enabled_subsets: [...]               # 默认 17 个离线子集（见 --list）
  enable_web_search: false             # 追加 web_search 2 个子集（要 SERPAPI_API_KEY）
  enable_memory: false                 # 追加 memory 3 个子集（要联网拉 HF 嵌入模型）
  serpapi_api_key: ""
  limit: 0                             # >0 时每个子集只取前 N 条
  eval_batch_size: 10
  temperature: 0
  is_fc_model: true                    # 端点原生函数调用; 冒烟 acc=0 时改 false 走提示词解析模式
  underscore_to_dot: true              # 模型名下划线→点号（BFCL handler 约定）
```

### Overall 计分公式（与 evalscope 官方聚合逐项对齐）

```
Overall = 40%·Agentic + 30%·MultiTurn + 10%·Live + 10%·NonLive + 10%·Hallucination
Agentic       = mean( mean(web_search×2), mean(memory×3) )   # 两块均值再取均值
MultiTurn     = mean( multi_turn×4 )                          # 不加权
Live          = Σ(score×num) / Σnum                           # 唯一按样本数加权的类别
NonLive       = mean( simple_ast, multiple, parallel, parallel_multiple )
                其中 simple_ast = mean(simple_python, simple_java, simple_javascript)
Hallucination = mean( live_irrelevance, irrelevance )
```

- `live_relevance` 会跑但**不计分**（官方口径）。
- 只跑部分子集时，缺失类别按 0 分计权（权重**不**重归一化）——部分运行的
  OVERALL 与全量表没有可比性，看分项表即可。runner 会打印缺项类别提醒。
- runner 本地重算 OVERALL 并与 evalscope 报告值交叉核对（差 >1e-3 时告警）。

### venv 重建（bfcl-venv 装法）

`setup.sh` 的 [3.5/4] 步骤已内置下述安装（幂等, 可选线装失败仅告警）;
以下命令用于手动重建:

```bash
docker exec ais-bench /usr/local/python3.12.13/bin/python3.12 -m venv \
  /mnt/sfs_turbo/c00803992/bfcl-venv
docker exec ais-bench /mnt/sfs_turbo/c00803992/bfcl-venv/bin/pip install \
  -i https://pypi.tuna.tsinghua.edu.cn/simple \
  "evalscope==1.14.1" "bfcl-eval==2025.10.27.1" "pyarrow==21.0.0" soundfile
```

两个**必须显式补**的依赖（都是上游声明缺失踩出来的坑）：

| 包 | 原因 |
|---|---|
| `pyarrow==21.0.0` | bfcl-eval 钉 `numpy==1.26.4`，但解析器默认装最新 pyarrow 26 要求 numpy>=2 → `import evalscope` 直接崩；pyarrow 21 是最后一个兼容 numpy 1.x 且满足 datasets>=21 门槛的版本 |
| `soundfile` | bfcl-eval → qwen-agent 的 `utils.py` 顶层 `import soundfile`，但 qwen-agent 没声明该依赖 → 打分时 `eval_runner` import 崩（报错是 `cannot import name '_evaluate_single_agentic_entry'`，有误导性） |

> venv 里 torch 是 2.15.0+cu132（装 CPU 版时 pytorch 源读超时，后续被 tuna 的
> CUDA 版顶替）。import 正常，离线子集不用 GPU，可不动；要换 CPU 版：
> `pip install torch --index-url https://download.pytorch.org/whl/cpu`。

### 已知风险 / 边界

- `memory_*` 3 子集要拉 HuggingFace 嵌入模型（sentence-transformers）：容器内
  直连 huggingface.co 不通，需 `HF_ENDPOINT=https://hf-mirror.com` 且磁盘够。
  默认 `enable_memory: false` 不受影响。
- `web_search_*` 2 子集要真实 Google 搜索，需 `serpapi_api_key`。默认关。
- dsv4 冒烟（limit=10）：`simple_python` acc=1.0、`irrelevance` acc=0.9，
  说明端点**原生函数调用可用**，`is_fc_model: true` 保持即可。
- 多轮/智能体子集（multi_turn、web_search、memory）会真实执行模型给出的
  工具调用（含 shell 命令），全量跑前确认容器环境可承受。

## SWE-bench_Pro 智能体代码评测（swe_runner.py）

用 evalscope 的 `swe_bench_pro` 适配器跑 ModelScope **ScaleAI/SWE-bench_Pro**
（数据集实际 **642 实例**，官方文档写 731）：模型对真实仓库 issue 生成补丁，
每个实例在**独立 amd64 沙箱镜像**里跑金标测试，产出 resolved 率。测的是
智能体式代码能力（长上下文 + 补丁 + 跑测试），不是 API SLA。

与其他线最大的不同：**评测本体不在本仓库**。分批驱动、看守脚本住在 live
工作区 `/mnt/sfs_turbo/c00803992/swe_pro`，因为 642 实例 × 每实例独立镜像
（jefzda/sweap-images，无共享层，全量约 1.3TB 磁盘）必须 32 一批滚动
"拉镜像 → 评 → prune"，由常驻宿主侧驱动负责。`swe_runner.py` 是只读
监控 + 带双驱动护栏的补跑入口——**防的是 web/CLI 一键启动撞上正在跑的
评测**：两个 evalscope 同时写 review 文件会互相覆盖。web 启停按钮经
宿主桥接守护（`tools/swe_web_bridge.py`）驱动 live 原件，护栏同源
（见「Web 启停与参数」）。

> **live 评测产物（results / logs / reviews / 驱动脚本本体）都在
> `/mnt/sfs_turbo/c00803992/swe_pro`，勿移动/修改/删除**——宿主侧常驻
> 驱动与看守进程正在读它们。仓库里 `scripts/swe/`、`tools/` 下的同名
> 脚本是归档副本（`scripts/swe/` 头部有标注），swe_runner 与全量驱动
> 一律用 live 原件。

### 架构（aarch64 宿主上跑 amd64 评测）

```
宿主（aarch64, 320 核）
├── run_all_batches.sh            # 分批驱动（常驻）: 每批 32 实例循环
│     └── run_swe_bench_pro.sh    # 单批: evalscope eval + 评分 + review 快照 + eval_rc 记录
│           └── evalscope eval（跑在 ais-bench 容器里, /tmp/bench-venv）
│                 └── 每实例独立 amd64 沙箱容器（~2GB/个, 评完 prune）
│                       └── qemu-x86_64（宿主 binfmt_misc 注册, 容器内不可见）
├── docker_unix_relay.py          # 容器 → 宿主 docker API 中继
│     （unix socket 经 NFS 共享: /mnt/sfs_turbo/c00803992/dockersock/… ）
├── staged_watch.sh               # 逐实例日志分段看守（进度行的来源）
└── review_guard.sh               # review 文件实时备份看守（防事务性丢失）
```

- 容器里没有 `/var/run/docker.sock`：docker API 走 unix socket 中继。
  **必须 unix relay**——TCP 代理会弄丢 docker-py hijack 之后的响应字节。
- amd64 镜像在 aarch64 上靠 binfmt + qemu 跑；binfmt flags 必须是 `OCF`
  （`POCF` 会让 docker 拉镜像阶段直接失败）。宿主注册，容器内
  `/proc/sys/fs/binfmt_misc` 是空的，所以容器口径的 status 看不到它。
- 容器内 `/tmp/bench-venv` 的 evalscope 1.14.1 有**两个必须打的适配器补丁**
  （**容器重建即失效**，用仓库副本重打：`bash tools/prepare_swe_bench_env.sh`）：

  | 补丁脚本 | 修什么 | 文件内 marker |
  |---|---|---|
  | `tools/patch_swe_bench_pro_tests.py` | 金标 test_patch 不进沙箱 → 测试空过、全 resolved | `# swe-bench-pro-testpatch` |
  | `tools/patch_evalscope_instance_filter.py` | 适配器忽略 INSTANCE_FILE → 补跑跑成全量 | `# evalscope-instance-filter` |

  `swe_runner.py env-check` 会 grep 这两个 marker 验证补丁在不在。
- 补丁脚本对 venv 内目标文件用"临时文件 + `os.replace`"原子写，中断不留半个文件。
- 评测进程在容器、驱动在宿主 → PID namespace 互相隔离。`status` 是**双口径**：
  宿主机跑看全量；webapp（容器内）自动切容器口径，宿主侧进程标"不可见"。

### 用法

```bash
bash run.sh --swe status              # 状态: 当前批/进度行/ETA/进程活性/磁盘/崩溃标记
bash run.sh --swe status --json       # 同上 JSON（web 端点即此输出）
bash run.sh --swe aggregate           # 已评分聚合: repo × resolved/total/acc
bash run.sh --swe rerun-list          # 补跑队列: 条数 + 分组原因 + 样例
bash run.sh --swe env-check           # 环境自检 7 项（relay/binfmt/venv 补丁/驱动脚本）
bash run.sh --swe start-rerun         # 补跑 dry-run: 只打印 docker exec 命令
bash run.sh --swe start-rerun --yes   # 真正启动（容器内在跑 evalscope → 拒绝, exit 3）
```

补跑原理：`EVALSCOPE_INSTANCE_FILE`（实例清单）+ `EVALSCOPE_DATASET_OFFSET=0`
+ `--limit` 大于清单条数；聚合器按 instance_id **去重取最后一条**，所以补跑
只覆盖清单内实例、不碰已 resolved 的历史批。清单自动均分串行执行（`;` 连接，
绝不起两个并行 evalscope）。

### config.yaml swe 段

```yaml
swe:
  work_dir: /mnt/sfs_turbo/c00803992/swe_pro    # live 工作区（驱动/reviews/logs 都在这）
  results_dir: <work_dir>/results/accuracy/swe_bench_pro_dsv4   # 缺省由 work_dir 推导
  rerun_ids:   <work_dir>/rerun_ids.txt
  repo_path: /mnt/sfs_turbo/c00803992/evalscope-sla-kit        # 驱动调用仓库内工具用
  api_url: http://192.168.0.9:18020/v1/chat/completions
  model: dsv4
  venv: /tmp/bench-venv              # 容器内 evalscope venv（补丁所在）
  container: ais-bench
  relay_socket: /mnt/sfs_turbo/c00803992/dockersock/docker.sock
  disk_mount: /mnt/docker            # 磁盘余量看这个挂载点
  batch_window: 32                   # 每批实例数（受磁盘滚动 prune 约束）
  total_instances: 642
  eval_batch_size: 16
```

`api_url` / `model` **故意不回退 `service` 段**：live 评测打的是 dsv4 端点，
而 `service` 段当前是 glm5.3，回退会把补跑指向错误端点。换被测模型时
两处都要显式改。

### 已知坑（qemu / review 事务性 / 长尾）

1. **qemu CPU 直通 OOM**：沙箱里看到宿主全部 320 核 → 每实例独立跑
   ansible（pytest-xdist `-n auto`）/ jest（`--maxWorkers` 默认=核数）/
   go build 全部 OOM。三个补丁钳死并发：
   `patch_swe_bench_pro_workers.py`（xdist `-n 4`）、
   `patch_swe_bench_pro_jest.py`（`--maxWorkers=2 --testTimeout=30000`）、
   `patch_swe_bench_pro_go.py`（`GOMAXPROCS=2`）。
2. **go 模块拉取**：默认代理在容器网络里不通，必须
   `GOPROXY=https://goproxy.cn,direct`（已打进 go 补丁）。
3. **长尾**：单批 32 实例里 1-2 个实例 20+ 分钟是常态（大仓库编译+全量
   测试），进度条 ~600s/it、整批 4 小时量级，别误判为卡死。
4. **review 事务性丢失**：evalscope 正常结束才 `os.replace(temp, base)`——
   正常结束会**覆盖上一批**的 review；异常退出则 temp 连同当批全部评分被丢弃。
   防护三件套：`run_swe_bench_pro.sh` 评后快照 + `eval_rc.log` 崩溃记录 +
   `review_guard.sh` 实时备份；全丢了还能
   `tools/recover_reviews_from_workspaces.py` 从 workspace 重建。
5. **eval_rc.log 缺失≠异常**：崩溃记录是后加进驱动的，老批次跑完时文件
   还不存在属正常（status 会注明）。
6. **进度行两种形态**：原始 tqdm `\r` 刷新行（两个 `|`）和 INFO 日志行
   （一个 `|`、带时间戳），解析必须两种都吃（见
   `swe_runner.parse_progress_line`，单测里两个形态都有 fixture）。

### Web 页签

「SWE-bench_Pro」页签：状态卡 / 聚合表 / 批次时间线 / 补跑队列 +
**运行控制卡**（启动/停止，见下节）。聚合和状态在容器内以 subprocess 派发
`swe_runner.py`，与 CLI 同一份代码；运行中的页签 20s 轮询，静止 60s。

### Web 启停与参数

webapp 跑在 ais-bench 容器里（无 docker CLI、看不到宿主进程），启停指令
走**命令文件 + 宿主桥接守护**落地：

```
web 前端 → webapp 容器内 API          宿主机（tools/swe_web_bridge.py，常驻轮询 1s）
  POST /api/swe/run  → 写 swe_pro/webctl/run.json ─→ 校验护栏 → 拉起 run_all_batches.sh
  POST /api/swe/stop → 写 swe_pro/webctl/stop      ─→ 全停序列（见下）
  GET  /api/swe/status ← 读 webctl/state.json      ← 桥接每 3s 刷新（driver/eval 活性、
                                                       当前批、最近动作）
```

`/mnt/sfs_turbo/c00803992` 是 NFS 共享盘，容器与宿主双向立即可见；桥接
由 `bash scripts/start_web.sh` 幂等拉起（`setsid nohup`，日志
`logs/swe_web_bridge.log`），也可手动 `python3 tools/swe_web_bridge.py`。

- **桥接在线判定**：state.json 的 `updated_at` 距今 < 5s。离线时 run/stop
  接口返回 503 并提示启动命令，前端按钮置灰 + 红字提示。
- **双驱动护栏**：宿主侧 run_all_batches 在跑、或容器内 evalscope eval 在
  跑 → run 命令被拒绝（错误写回 state.json，前端 toast 展示）。pgrep
  模式串都用 `[x]` 括号技巧防自身误匹配。
- **启动模式**：`full` = 断点续跑全量（detach 派发 run_all_batches.sh，
  env 带 START_OFFSET/WINDOW/CONCURRENCY/KEEP_IMAGES）；`rerun` = 补跑
  队列（`swe_runner.py start-rerun --yes`）。两种模式都会把
  staged_watch / review_guard 看守幂等拉起。
- **启动表单默认 start_offset**：后端 `GET /api/swe/config` 算
  `default_start_offset`——batches.log 里最后**无 finished 配对**的批 →
  续跑该批；全部配对 → 下一偏移；`all batches done` → 封顶 642。
- **参数**：mode / start_offset（0-641）/ window（1-64，默认 32）/
  concurrency（1-32，默认 16）/ keep_images / limit；`POST /api/swe/config`
  只存 `web_state.json`（同 accuracy 线口径），**不改 config.yaml**。
- **停止语义**（点击后确认框明示）：宿主驱动 + 容器内 evalscope/看守
  一律 `kill -9`——**SIGTERM 会触发 evalscope 的 discard_review_reruns
  把已评分行丢掉**；清掉 `sandbox-*` 沙箱容器（白名单 dsv41-mmc /
  aiserver-agent / ais-bench 永不动）；再做数据抢救（recover from
  workspaces + 比对 reviews 已评 instance_id + datasets arrow 找出
  中断批未评分集，去重追加进 `rerun_ids.txt`）。已评分数据保留并自动
  入补跑队列；无事在跑时停止是纯空操作。
- 命令文件消费后即删（uuid 防串扰），所有写盘走"临时文件 + os.replace"
  原子替换；桥接单实例自锁（发现第二个自身进程即退出）。

**离线单测**（`python3 tests/test_swe.py`，不真启评测、mock 进程）覆盖：
run 参数校验、命令文件写入、state.json 解析、断点默认值计算、sandbox
容器过滤白名单、补跑清单去重追加。

## config.yaml 说明

### service / request 段

| 键 | 说明 |
|---|---|
| `service.url` | 被测端点，完整 chat completions 路径 |
| `service.model` | 服务端 served model 名 |
| `service.tokenizer_path` | 模型权重路径（tokenizer）：**必须本机存在**，不存在时 modelscope 会退化成远程下载 → 404 |
| `service.max_context_length` |（可选）服务上下文上限；AgentX 回放转成 `--max-context-length` 在客户端过滤超长 trace，不设置则不传 |
| `request.add_host_header` | true = 请求头加 Host（LB/网关按域名路由时用） |
| `request.host_header` | Host 值，留空自动取 url 主机名 |
| `request.api_key` | 设置后自动加 `Authorization: Bearer ***` |

### benchmark 段

| 键 | 默认 | 说明 |
|---|---|---|
| `sla_lower_bound` / `sla_upper_bound` | 1 / 512 | 并发搜索区间 |
| `sla_num_runs` | 2 | 每个候选并发跑几轮取平均 |
| `number_multiplier` | 2 | 每档请求数 = 并发 × 此值 |
| `warmup_num` | 10 | warmup 请求数 |
| `extra_args` | ignore_eos 等 | 透传给 evalscope `--extra-args` |

### scenarios 段

每行一个场景，7-8 个逗号分隔字段：

```
"dataset, input_len, output_len, cache_hit_rate, prefix_num, ttft_sla_ms, tpot_sla_ms [, dataset_num]"
```

- `dataset`: `random`（在线生成，0% 命中）或 `gsm8k`（本地定长数据集）
- 长度支持 `k` 简写：`1k`=1024
- `cache_hit_rate`: 0 / 0.3 / 0.9
- 第 8 字段 `dataset_num`：gsm8k 数据集条数（不填 = 全局默认 1000）
- **注释行 = 跳过；取消注释 = 启用**

## setup.sh 的 custom 插件补丁

安装 evalscope 后自动给 `perf/plugin/datasets/custom.py` 的 `build_messages` 打
`dataset_offset` 补丁（官方 random/multi_turn 插件支持 `--dataset-offset`，custom 不支持）。
幂等：已打过则跳过；版本不匹配时警告不破坏。

## 场景 ID 与单场景重跑

```bash
bash run.sh --list                    # 查看所有启用场景 ID
bash run.sh --only gsm8k_64k_1k_hit30pct   # 只重跑一个场景
bash run.sh --gen-datasets            # 批量预生成所有 gsm8k 数据集
```

## 结果解读

- 每场景：`results/<场景ID>/<时间戳>/<model>/sla_summary.json`
- `python3 report.py` 输出每场景各并发的 TTFT/TPOT/吞吐表 + SLA 判定列
- 摸高结论 = 最大达标并发 + 该并发下的 avg TTFT / avg TPOT / 输出吞吐

## 预算看门狗与 SLA 判定

### 单场景时间预算 `time_budget_min`

- `>0`（分钟）时该场景压测到限即被终止，并按**已完成的探测档**算 partial 结论，
  写入 `results/<场景ID>/partial_summary.json`（含 `status` / `selected` / `probes` 明细）；
  `0` = 不限制。
- **partial 语义**：搜索被预算切断时结论不完整，`selected`（最大达标并发）是
  真实上限的**下界**，真实上限落在 `(selected, 首个违反档)` 区间内。

### partial 判定规则（失败请求零容忍）

- 逐档解析 `results/<场景ID>/<时间戳>/<model>/sla_tuning/sla_parallel_N_run_M/benchmark_summary.json`：
  TTFT / TPOT 按 run 取算术平均，Success / Failed / Total 按 run 求和；
  缺 `benchmark_summary.json` 的档（被切断）直接跳过。
- 该档 **Failed 之和必须为 0**，且 avg TTFT / avg TPOT 双达标才算 PASS；
  selected = 达标档中的最大并发。

### 健康探活 `health_check_interval_s`

- `>0`（秒）时压测运行期间每隔该秒数 GET 一次服务 `/v1/models`（连通性检查，
  任意 HTTP 状态码都算存活）；连续 ≥3 次失败打告警（**只告警不杀进程**），
  恢复后计数清零；`0` = 关闭。

### report.py 的 SLA 判定

- 每场景并发表多一列 `判定`：Failed==0 且 TTFT / TPOT 双达标 → `PASS`，否则 `FAIL`
  （判据取自 config.yaml 对应场景行；场景不在 config 中则跳过该列）。
- 每表末尾给结论：`SLA 判定: 最大达标并发 = N` 或
  `SLA 判定: 无达标档（Failed at lower bound）`。

## 工具脚本（tools/）

| 脚本 | 用途 |
|---|---|
| `tools/gen_dataset_only.py` / `tools/generate_dataset.py` | gsm8k 定长数据集生成（aisbench） |
| `tools/jsonl_to_prompts.py` | jsonl → 纯文本 prompts.txt |
| `tools/curate_weka_traces.py` | cc-traces-weka 本地裁剪语料生成（目标画像: 会话数 / 主轮数 / 平均输入 / 缓存命中；输出每会话一个 JSON + `<目录>_stats.json`，AgentX `local:<目录>` 变体回放） |
| `tools/data_picker.py` | 数据集挑选 |
| `tools/f05_claude_code.sh` | claude code CLI 会话（问答 + 工具调用）自动化；功能线 C61 只做存在性检查（恒 SKIP），真会话跑这个脚本 |
| `tools/passk_evalscope.py` | evalscope CLI wrapper：把 `--dataset-args` 里 JSON dict 形式的 `primary_metric` 转成 `MetricSelector` 对象（pass@k 报告期必需，否则 no-unique-primary-metric 必崩；k>1 时 runner 自动切到它） |
| `tools/wait_service_passk.sh` | 守护：等 ModelArts 服务恢复健康后自动启动 pass@k 评测（健康判定走 `/v1/models` + 极小流式对话，**curl 一律 `--noproxy '*'`**）；work-dir 已有 predictions 时自动带 `--resume` 断点续跑 |
| `tools/queue_passk.sh` | 守护：等前序精度评测退出后自动启动 pass@k 评测（等"没有精度进程在跑"，不是等写死的 PID） |

两个守护脚本都用锁目录防双跑，地址/模型从 `config.yaml` 的 `service` 段读，
不写死在脚本里。

```bash
setsid nohup bash tools/wait_service_passk.sh > logs/acc_passk_queue.log 2>&1 &
```

## 已知坑（实测教训）

1. **tokenizer_path 必须本机存在**：不存在时 modelscope 退化成远程下载 → 404。runner 有预检。
2. **`Dataset is empty!`**：gsm8k jsonl 没转纯文本 prompts.txt，或
   `--max-prompt-length` 小于 数据集长度+模板开销。runner 已自动处理
   （阈值 = input_len + 2000）。
3. **131k 数据集加 chat template 后实测 ~131160 token**：阈值必须留余量，
   否则全部请求被过滤。
4. **摸高耗时数小时**（长输入场景单轮数十分钟 × 二分点数 × 轮数）：
   必须 nohup 后台 + 定期 tail；服务报错要能快速发现。
5. **`Failed at lower bound (1)` 多为请求失败而非指标不达标**：先看
   Success/Total（0 成功 = 服务 400/端口不通），curl 单发一条请求确认。
6. **gsm8k 数据集生成需 torch + transformers**（NPU/GPU 容器内一般已有）；
   random 场景不需要。
7. **数据集按文件名复用**：改 seed/prefix_num/dp 后需 `force_regenerate: true`
   或手动删 `datasets/` 下对应文件。
8. **picked_ids.txt 累积**：数据集生成报"请清空 picked ids"时删除
   `tools/picked_ids.txt` 重跑。
9. **thinking 模型**必须 `chat_template_kwargs.thinking=false`（见上）。
10. **`ignore_eos: true`** 让输出固定到 max_tokens，TPOT/吞吐才可比（默认已开）。
11. **MaaS 网关有 60s 首字节超时**：非流式请求的首字节 = 整个响应，思考型模型的
    长回答必然踩雷（网关返 504，看起来像服务坏了）。功能线已按需求**全非流式**
    （`functional.timeout_s: 300` 整体兜底，流式能力由性能线覆盖）；精度评测的
    generation-config 仍必须带 `stream: true`。
12. **思考型模型（GLM-5.3 强制思考）的输出预算**：预算太小会被思考链吃光、正文
    为空（`content: null` + `finish_reason: length`）。功能用例给
    `functional.max_tokens: 2048`；旧代码用子串匹配 `"content"` 判过（值为
    `null` 也算 PASS），现在改成"正文非空"才算过。
13. **环境里的 `http_proxy` 会劫持内网请求**：功能测试与健康探测都要绕开它——
    curl 加 `--noproxy '*'`，python 用 `build_opener(ProxyHandler({}))`。不绕的
    话健康探测会一直拿不到 200（守护脚本就会空等）。精度评测是另一回事：
    `accuracy.proxy` 非空时是**故意**给子进程导出代理给数据集首次下载用，
    同时把被测服务主机名塞进 `no_proxy`。
14. **退出码 0 不等于通过**：精度评测 evalscope 退出 0 但没解析到分数（报告缺失 /
    指标为空 / 报告结构变化）同样是假通过，runner 按 exit 1 处理。
15. **显式指定的路径找不到就照实报错**：不要用"本机已知路径"兜底覆盖显式配置，
    否则两台机器会静默跑不同版本的 evalscope，分数不可比。
16. **pass@k 报告期必崩 "no unique primary metric"**：`mean_and_pass_at_k`
    产出 k+1 个非诊断指标，evalscope 1.14.0 报告生成器要求唯一主指标；且
    `primary_metric` 只认 `MetricSelector` 对象——JSON dict 直接 AttributeError，
    字符串会被归一化成通配、依旧歧义。只能经 `tools/passk_evalscope.py`
    wrapper 从 `--dataset-args` 注入（runner 在 k>1 时已自动切换）。
17. **evalscope 缓存身份哈希会拒跑**：`--use-cache` 续跑时 evalscope 校验命令
    指纹，命令一变（补参数、换口径）即 `ValueError: Cached evaluation identity
    does not match`。断点续跑必须带 `--rerun-review`（复用 predictions、重算
    判分与报告，秒级；runner 的 `--resume` 已自动带上）。代价是身份校验被绕过，
    换口径续跑会把旧预测静默混进新报告——换口径请换 tag。
18. **停止任务需杀进程树**：`killpg(SIGKILL)` 整组杀，`proc.kill()` 只杀直接
    子进程不够（evalscope 还会派生 worker）。
