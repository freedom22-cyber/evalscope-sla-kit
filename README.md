# evalscope 测试控制台（SLA Peak Search Kit）

对 OpenAI 兼容推理服务（vLLM / MindIE / 路由网关等）做 **evalscope 摸高测试**：
`--sla-auto-tune` 二分搜索满足 SLA 判据（TTFT / TPOT）的**最大并发数**。

三条测试线，共用一份 `config.yaml`，统一通过 Web 控制台或命令行驱动：

| 测试线 | 入口 | 产出 |
|---|---|---|
| 摸高（性能） | `bash run.sh` | `results/<场景ID>/…/sla_summary.json` + `report.py` 汇总表 |
| 精度（pass@k） | `bash run.sh --accuracy` | `results/accuracy/<tag>_pass@k<k>_r<N>/…` + `scores.md` |
| 功能（API E2E） | `bash run.sh --functional` | `results/functional/e2e_<时间戳>/report.md` + 各用例响应原文 |

## 目录结构

```
evalscope_sla_kit/
├── setup.sh            # 一键安装环境（含 evalscope custom 插件 offset 补丁）
├── run.sh              # 入口（后台/前台/dry-run/单场景；--accuracy / --functional 分发）
├── config.yaml         # 全部配置：服务地址、模型名、Host 头、场景清单、精度数据集组、功能参数
├── runner.py           # 摸高: 场景解析 + 数据集生成 + evalscope 命令构建执行
├── accuracy_runner.py  # 精度: 数据集组 x 轮的 evalscope eval + pass@k/mean 分数解析汇总
├── scripts/run_api_e2e.py  # 功能测试: API E2E 框架（22 条用例，PASS/WARN/FAIL/SKIP）
├── report.py           # 摸高结果汇总（并发-指标表，可折算单卡吞吐）
├── webapp.py           # Web 控制台后端（FastAPI，端口 8300）
├── web/index.html      # Web 控制台前端（五页签单页应用，深色玻璃拟态）
├── tools/              # gsm8k 数据集生成 / jsonl→prompts / 守护脚本
├── datasets/           # 生成的数据集（按文件名复用）
├── results/            # 摸高 + 精度 + 功能 输出
└── logs/               # 运行日志
```

## Web 控制台

```bash
python3 webapp.py --port 8300     # 浏览器打开 http://<主机IP>:8300
```

五个页签（深色玻璃拟态 UI，Fira Sans/Code 字体，Lucide SVG 图标）：

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
- `tools/supervise_passk.sh` 把"拉起"也自动化：评测崩了（503 闪断 / 进程被杀）
  就等服务恢复自动 `--resume` 续跑，直到报告全出才退出，适合深夜长跑。

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

Simaas 风格 API E2E 框架：22 条用例，覆盖 OpenAI 兼容性/流式/结构化输出/多轮/工具调用等，
判定 **PASS / WARN / FAIL / SKIP**。纯标准库 urllib 发请求。

```bash
bash run.sh --functional                        # 后台跑全部用例
bash run.sh --functional -f                     # 前台跑
python3 scripts/run_api_e2e.py --list           # 列出全部用例
python3 scripts/run_api_e2e.py --only F-03      # 只跑指定用例
```

### 用例清单（摘要）

| category | 用例 | 检查点 |
|---|---|---|
| openai/compatibility | models list / chat / completions / responses | 端点可用、响应非空 |
| openai/streaming | chat streaming | SSE 含 `data:` 与 `[DONE]` |
| openai/structured-output | json object | 输出可解析为 JSON |
| openai/context | multi-turn recall / long-context recall | 多轮与长上下文召回 |
| openai/finops | chat usage | usage 字段齐全 |
| openai/robustness | max_tokens / stop / determinism / unknown model | 鲁棒性 |
| openai/capability | thinking on/off / effort 矩阵 / tool call | 能力探测 |

### 判定口径

| 判定 | 含义 | 计入退出码 |
|---|---|---|
| PASS | 正常 | 否 |
| FAIL | 失败 | **是** |
| WARN | 已知缺陷 / 可接受异常 | 否 |
| SKIP | 主动跳过 | 否 |

## config.yaml 说明

### service / request 段

| 键 | 说明 |
|---|---|
| `service.url` | 被测端点，完整 chat completions 路径 |
| `service.model` | 服务端 served model 名 |
| `service.tokenizer_path` | 模型权重路径（tokenizer）：**必须本机存在**，不存在时 modelscope 会退化成远程下载 → 404 |
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
| `tools/data_picker.py` | 数据集挑选 |
| `tools/f05_claude_code.sh` | F-05 claude code CLI 会话（问答 + 工具调用）自动化；`functional_runner.py` 里的 F-05 只做存在性检查（恒 SKIP），真会话跑这个脚本 |
| `tools/passk_evalscope.py` | evalscope CLI wrapper：把 `--dataset-args` 里 JSON dict 形式的 `primary_metric` 转成 `MetricSelector` 对象（pass@k 报告期必需，否则 no-unique-primary-metric 必崩；k>1 时 runner 自动切到它） |
| `tools/wait_service_passk.sh` | 守护：等 ModelArts 服务恢复健康后自动启动 pass@k 评测（健康判定走 `/v1/models` + 极小流式对话，**curl 一律 `--noproxy '*'`**）；work-dir 已有 predictions 时自动带 `--resume` 断点续跑 |
| `tools/queue_passk.sh` | 守护：等前序精度评测退出后自动启动 pass@k 评测（等"没有精度进程在跑"，不是等写死的 PID） |
| `tools/supervise_passk.sh` | 监督守护：跑起来之后**全程盯**——服务闪断 503 / 进程被杀（OOM）后自动等恢复再 `--resume` 续跑，直到全部数据集报告生成才退出（完成判定查报告文件，能逮住 runner 退出码 0 但报告缺失的假通过） |

三个守护脚本都用锁目录防双跑，地址/模型从 `config.yaml` 的 `service` 段读，
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
    长回答必然踩雷（网关返 504，看起来像服务坏了）。功能用例的 chat 一律走流式，
    精度评测的 generation-config 也必须带 `stream: true`。
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
19. **服务闪断 503 会让评测"猝死"**：与压测共享同一 ModelArts 服务时后端可能
    临时 `backend is not available`（503），evalscope 只快速重试 5 次（秒级）就
    抛异常退出，没人拉起就损失墙钟时间。解法 = `tools/supervise_passk.sh`
    监督守护：崩了等恢复自动 `--resume`，直到报告全出。本机还有 OOM 风险
    （7.2G 内存无 swap 时 evalscope 峰值 ~2.5G 会被内核杀，-9 退出码即此；
    已加 8G /swapfile 兜底）。
