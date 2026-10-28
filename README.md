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
python3 accuracy_runner.py --list           # 列出启用的数据集组
python3 accuracy_runner.py --only all5      # 只跑指定组
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
| `k` |（可选）pass@k 采样次数，默认 `1` |

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

## 已知坑

1. **tokenizer_path 必须本机存在**：不存在时 modelscope 退化成远程下载 → 404。runner 有预检。
2. **gsm8k 数据集生成需 torch + transformers**；random 场景不需要。
3. **thinking 模型**必须 `chat_template_kwargs.thinking=false`，否则思考 token 拉高 TPOT。
4. **`ignore_eos: true`** 让输出固定到 max_tokens，TPOT/吞吐才可比。
5. **MaaS 网关 60s 首字节超时**：功能用例 chat 一律走流式，精度 generation-config 带 `stream: true`。
6. **环境 `http_proxy` 劫持内网请求**：功能测试用 `build_opener(ProxyHandler({}))` 绕开。
7. **退出码 0 不等于通过**：精度评测 evalscope 退出 0 但没解析到分数也是假通过。
8. **停止任务需杀进程树**：`killpg(SIGKILL)` 整组杀，`proc.kill()` 只杀直接子进程不够。
