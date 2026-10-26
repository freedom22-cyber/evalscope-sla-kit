# evalscope 摸高测试工程（SLA Peak Search Kit）

对 OpenAI 兼容推理服务（vLLM / MindIE / 路由网关等）做 **evalscope 摸高测试**：
`--sla-auto-tune` 二分搜索满足 SLA 判据（TTFT / TPOT）的**最大并发数**。

## 目录结构

```
evalscope_sla_kit/
├── setup.sh            # 一键安装环境
├── run.sh              # 入口（后台/前台/dry-run/单场景；--accuracy 分发精度、--functional 分发功能）
├── config.yaml         # 全部配置：服务地址、模型名、Host 头开关、场景清单、精度数据集组、功能检查参数
├── runner.py           # 摸高: 场景解析 + 数据集生成 + evalscope 命令构建执行
├── accuracy_runner.py  # 精度: 数据集组 x 轮的 evalscope eval + accuracy 分数解析汇总
├── functional_runner.py # 功能: F-01~F-05 检查（urllib 直发, PASS/FAIL/KNOWN-DEFECT/SKIP 四类判定）
├── report.py           # 摸高结果汇总（并发-指标表，可折算单卡吞吐）
├── webapp.py           # Web 控制台后端（FastAPI）
├── web/index.html      # Web 控制台前端（三视图单页）
├── tools/              # gsm8k 数据集生成工具（aisbench）+ jsonl→prompts 转换
├── datasets/           # 生成的数据集落盘处（按文件名复用）
├── results/            # 摸高输出: results/<场景ID>/<时间戳>/<model>/
│   ├── accuracy/       # 精度输出: accuracy/<tag>_r<N>/reports/<model>/<ds>.json + scores.md
│   └── functional/     # 功能输出: functional/oneclick_<时间戳>/（各检查响应原文 + summary.md）
└── logs/               # 运行日志（摸高 run_*.log / 精度 acc_*.log / 功能 func_*.log / Web 日志）
```

## Web 控制台（三视图）

```bash
python3 webapp.py --port 8300     # 启动后浏览器打开 http://<主机IP>:8300
```

左侧三个视图：

1. **⚙️ 配置项** —— 前端配置服务 URL / 模型名 / tokenizer、**Host 头开关**、
   摸高参数、以及测试场景的启停/增删（保存到 `web_state.json`，不覆盖
   `config.yaml` 的注释）
2. **📊 测试详情** —— 每个场景的状态（已完成 / 进行中 / 排队 / 未开始），
   一键运行已启用/未完成场景；点“查看结果”可打开该场景的**性能结果表**
   （各并发 TTFT/TPOT/吞吐/成功率）和**日志路径**（运行日志 + 分并发日志）
3. **🗂️ 数据集详情** —— 按场景列出 gsm8k 数据集（相同「输入长度 + 命中率」
   **自动复用同一文件**），可逐数据集配置**生成条数**，支持**一键生成全部
   数据集**与强制重新生成

命令行入口（`run.sh` / `report.py`）与 Web 共用 `config.yaml`，两者可混用。

## 快速开始（目标主机上）

```bash
# 1. 一键安装
bash setup.sh

# 2. 改配置（必改: url / model / tokenizer_path）
vi config.yaml

# 3. 预检：打印每个场景将执行的命令，不真跑
bash run.sh --dry-run

# 4. 开跑（后台 nohup，全部场景顺序执行）
bash run.sh
tail -f logs/run_*.log      # 跟踪进度

# 5. 汇总结果
python3 report.py --npu 8   # 8 卡时输出单卡吞吐列
```

## 精度测试（accuracy_runner.py）

迁移自 sunzhi_sla_eval `run208.sh cmd_accuracy` 的通用化版本：对被测服务跑
evalscope eval 数据集组（gsm8k / mmlu / gpqa_diamond / aime25+aime26 …），
逐组逐轮顺序执行并解析 accuracy 分数。**宿主机直跑**（不走 docker，setup.sh 已装
evalscope），只依赖 evalscope + pyyaml。

```bash
bash run.sh --accuracy                      # 后台跑所有启用数据集组（日志 logs/acc_*.log）
bash run.sh --accuracy -f                   # 前台跑（看实时输出）
python3 accuracy_runner.py --list           # 列出启用的数据集组
python3 accuracy_runner.py --dry-run        # 只打印命令与 work-dir，不执行
python3 accuracy_runner.py --only gsm8k,aime  # 只跑指定组
python3 accuracy_runner.py --report         # 不跑测试，重打已有结果汇总表
```

### config.yaml accuracy 段

数据集行格式（逗号分隔，顺序固定）：

```
"tag, evalscope_datasets, rounds, effort, max_tokens, batch_size"
```

| 字段 | 说明 |
|---|---|
| `tag` | 数据集组名，结果目录 `results/accuracy/<tag>_r<N>/` |
| `evalscope_datasets` | 传给 `--datasets` 的值（空格分隔多个，如 `aime25 aime26`） |
| `rounds` | 重复轮数，每轮独立 work-dir，便于算均值/方差 |
| `effort` | `reasoning_effort`（见下方已知缺陷提醒） |
| `max_tokens` | 生成上限 |
| `batch_size` | `--eval-batch-size` |

其他键：`evalscope_bin`（evalscope 可执行文件，可被环境变量 `EVALSCOPE_BIN`
覆盖）；`proxy`（非空时给子进程导出 `http_proxy`/`https_proxy`——evalscope 首次
下载评测集需要外网；被测服务主机名自动加入 `no_proxy` 防劫持，不污染父进程环境）。

### reasoning_effort 已知缺陷

effort 通过 generation-config 的 `extra_body` 顶层展开进请求体（evalscope →
OpenAI SDK，已抓包验证）。服务端接受 `low/high/xhigh/max/none` 及整数 1~100；
**`"medium"` 在部分部署上必 HTTP 400**（服务端 chat template 校验拒绝，见
sunzhi 仓 F-03 / docs/lessons_api208.md §4），配置时避开。

### 结果与汇总

- 每组每轮跑完即打印 `  [tag_r1] gsm8k: score=0.94 (n=1319)`；
  多轮同组结束后打印均值。
- 分数来源：`results/accuracy/<tag>_r<N>/reports/<model>/<ds>.json`，取 `metrics`
  里 `identity.name=="accuracy"` 的 `score` / `num`（缺失文件按缺失提示，不中断）。
- 每次跑完追加写 `results/accuracy/scores.md`（时间戳头 + tag/轮/数据集/score/n 表）；
  `--report` 可随时重打汇总表。

## 功能测试（functional_runner.py）

迁移自 sunzhi_sla_eval `run208.sh cmd_functional` 的通用化版本：对被测服务跑一组
OpenAI 协议功能检查（chat/completions/流式/截断/stop/确定性/thinking 开关/effort
矩阵/tool call/错误处理）。**纯标准库 urllib 发请求**（不依赖 curl/requests），
请求头复用 `runner.build_headers()`（Host 开关 / extra_headers / api_key 全部生效），
直连不走 http_proxy（等价 `curl --noproxy '*'`）。

```bash
bash run.sh --functional                        # 后台跑全部检查（日志 logs/func_*.log）
bash run.sh --functional -f                     # 前台跑（功能检查耗时短，一般直接前台）
python3 functional_runner.py --list             # 列出全部检查 tag 与说明
python3 functional_runner.py --dry-run          # 只打印检查清单，不发任何请求
python3 functional_runner.py --only F-01a,F-03  # 只跑指定检查（精确 tag 或前缀组）
```

### 检查清单与判定口径

| tag | 内容 | 判定 |
|---|---|---|
| `F-01a_chat` | chat 非流式 | 200 且响应含 `content` → PASS |
| `F-01b_stream` | chat 流式 SSE | 文本含 `data:` 与 `[DONE]` → PASS |
| `F-01c_completions` | `/v1/completions` 非流式基础 | 200 → PASS |
| `F-01d_maxtok` | max_tokens=8 截断 | 200 且 `usage.completion_tokens<=8` → PASS |
| `F-01e_stop` | `stop` 参数 | 不报错（200）→ PASS |
| `F-01f_determ` | temperature=0 两次请求 | 文本一致 → PASS（参考项） |
| `F-02a_think_on` | 默认 thinking | 响应含 `reasoning(_content)?` 字段 → PASS |
| `F-02b_think_off` | `reasoning_effort=none` | reasoning 内容为空 → PASS |
| `F-03_low/high/xhigh/max` | effort 矩阵（`functional.efforts` 可配） | 200 → PASS |
| `F-03_medium` | effort=medium 专项 | **恒记 KNOWN-DEFECT**（见下） |
| `F-04_toolcall` | tools 触发 | 响应含 `tool_calls` → PASS |
| `F_err_4xx` | 错误模型名 | 返回 4xx → PASS（2xx/5xx/网络失败 → FAIL） |
| `F-05_claude` | claude code 会话 | 有/无 claude CLI 均记 SKIP（会话需人工按 docs 跑） |

### KNOWN-DEFECT 语义与 medium 缺陷

判定分四类：**PASS / FAIL / KNOWN-DEFECT / SKIP**，只有 FAIL 计入退出码。
`F-03_medium`（`reasoning_effort="medium"`）在部分部署上**必 HTTP 400**（服务端
chat template 校验拒绝，sunzhi 仓 F-03 确定性复现 4 次，等效替代为整数分档
`chat_template_kwargs.reasoning_effort: 50`）：

- 返回 400 = 已知缺陷仍在，记 KNOWN-DEFECT，不计 FAIL；
- 返回 200 = 可能已修复，仍记 KNOWN-DEFECT，说明列提示人工确认。

### config.yaml functional 段

```yaml
functional:
  timeout_s: 60                    # 单请求超时（秒）
  efforts: [low, high, xhigh, max] # F-03 effort 矩阵（medium 由 F-03_medium 专项检查）
  skip: []                         # 跳过的检查 tag, 如 [F-05_claude]
```

段可整体缺省（取以上默认值）。`skip` 中的 tag 不发请求、summary 记 SKIP；
`--only` 不覆盖 skip，需强制执行请先从 skip 移除。

### 结果文件与退出码

- 响应原文逐项落盘 `results/functional/oneclick_<时间戳>/`（f01a.json、f01b.sse、
  f03_medium.json …，命名仿 sunzhi），汇总表 `summary.md` 为
  `| 检查 | 实际 | 判定 | 说明 |` 表 + `PASS=n FAIL=n KNOWN-DEFECT=n SKIP=n` 计数行。
- 退出码：**任一 FAIL → 1**（KNOWN-DEFECT / SKIP 不算失败）；全部通过 → 0；
  `--only` 含未知 tag → 2。
- 单项检查抛异常不中断整轮，该项记 FAIL（实际值 `EXC`）。

## config.yaml 说明

### service / request 段

| 键 | 说明 |
|---|---|
| `service.url` | 被测端点，完整 chat completions 路径 |
| `service.model` | 服务端 served model 名，必须与服务配置一致 |
| `service.tokenizer_path` | 模型权重路径（tokenizer）：random 生成、长度过滤、gsm8k 数据集生成都要用 |
| `request.add_host_header` | **true = 请求头加 Host**（LB/网关按域名路由时用）；false = 不加 |
| `request.host_header` | Host 值，留空自动取 url 的主机名 |
| `request.extra_headers` | 其他自定义头，`"Key=Value"` 列表 |
| `request.api_key` | 设置后自动加 `Authorization: Bearer <key>` |

### benchmark 段

| 键 | 默认 | 说明 |
|---|---|---|
| `sla_lower_bound` / `sla_upper_bound` | 1 / 512 | 并发搜索区间 |
| `sla_num_runs` | 2 | 每个候选并发跑几轮取平均 |
| `number_multiplier` | 2 | 每档请求数 = 并发 × 此值 |
| `warmup_num` | 10 | 正式测量前 warmup 请求数 |
| `time_budget_min` | 0 | 单场景时间预算（分钟）：>0 到限终止该场景并按已完成探测档算 partial 结论（0 = 不限制） |
| `health_check_interval_s` | 60 | 压测运行中健康探活间隔（秒）：定期 GET `/v1/models`，连续失败打告警（0 = 关闭） |
| `extra_args` | ignore_eos 等 | 原样透传给 evalscope `--extra-args` |

**thinking 模型必须保留 `chat_template_kwargs.thinking: false`**，否则思考 token
拉高 TPOT 使结果失真；非 thinking 模型删掉该小节即可。

### scenarios 段（场景开关）

每行一个场景，7 个逗号分隔字段：

```
"dataset, input_len, output_len, cache_hit_rate, prefix_num, ttft_sla_ms, tpot_sla_ms"
```

- `dataset`: `random`（evalscope 在线生成，天然 0% 缓存命中）或 `gsm8k`（本地生成定长数据集）
- 长度支持 `k` 简写：`1k`=1024、`1000k`=1024000
- `cache_hit_rate`: gsm8k 支持 0 / 0.3 / 0.9（对应数据集前缀重复率，即 KV cache 命中比例）
- **注释行 = 跳过该场景；取消注释 = 启用**（YAML 列表项，行首加 `#` 即可）

内置固定场景清单（默认全部启用）：
random 1k-1k / 1k-8k / 8k-1k（TTFT≤10s）、1k-32k / 1k-64k（TTFT≤3s）；
gsm8k 16k~1000k @ 0% 命中（TTFT≤60s）、64k~1000k @ 30%（TTFT≤30s）、
64k~1000k @ 90%（TTFT≤10s）；全部 TPOT≤50ms，输出 1k。

## 场景 ID 与单场景重跑

场景 ID 格式：`<dataset>_<输入>_<输出>_hit<命中率>`，如 `random_1k_8k`、
`gsm8k_64k_1k_hit30pct`（0% 命中时后缀为 `hit0`）。

```bash
bash run.sh --list                    # 查看所有启用场景 ID
bash run.sh --only gsm8k_64k_1k_hit30pct   # 只重跑一个场景
```

## 结果解读

- 每场景结果：`results/<场景ID>/<时间戳>/<model>/sla_summary.json`
  键 `parallel_N` → 该并发的 metrics/percentiles
- evalscope 日志末尾的 **Selections 表** = 该场景 SLA 下的选定最大并发
- `python3 report.py` 输出每场景各并发的 TTFT/TPOT/吞吐表
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

## 已知坑（实测教训）

1. **`Dataset is empty!`**：gsm8k jsonl 没转纯文本 prompts.txt，或
   `--max-prompt-length` 小于 数据集长度+模板开销。runner 已自动处理
   （阈值 = input_len + 2000）。
2. **131k 数据集加 chat template 后实测 ~131160 token**：阈值必须留余量，
   否则全部请求被过滤。
3. **摸高耗时数小时**（长输入场景单轮数十分钟 × 二分点数 × 轮数）：
   必须 nohup 后台 + 定期 tail；服务报错要能快速发现。
4. **`Failed at lower bound (1)` 多为请求失败而非指标不达标**：先看
   Success/Total（0 成功 = 服务 400/端口不通），curl 单发一条请求确认。
5. **gsm8k 数据集生成需 torch + transformers**（NPU/GPU 容器内一般已有）；
   random 场景不需要。
6. **数据集按文件名复用**：改 seed/prefix_num/dp 后需 `force_regenerate: true`
   或手动删 `datasets/` 下对应文件。
7. **picked_ids.txt 累积**：数据集生成报"请清空 picked ids"时删除
   `tools/picked_ids.txt` 重跑。
8. **thinking 模型**必须 `chat_template_kwargs.thinking=false`（见上）。
9. **`ignore_eos: true`** 让输出固定到 max_tokens，TPOT/吞吐才可比（默认已开）。
