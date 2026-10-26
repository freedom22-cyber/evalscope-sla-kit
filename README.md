# evalscope 摸高测试工程（SLA Peak Search Kit）

对 OpenAI 兼容推理服务（vLLM / MindIE / 路由网关等）做 **evalscope 摸高测试**：
`--sla-auto-tune` 二分搜索满足 SLA 判据（TTFT / TPOT）的**最大并发数**。

## 目录结构

```
evalscope_sla_kit/
├── setup.sh          # 一键安装环境
├── run.sh            # 摸高入口（后台/前台/dry-run/单场景）
├── config.yaml       # 全部配置：服务地址、模型名、Host 头开关、场景清单
├── runner.py         # 场景解析 + 数据集生成 + evalscope 命令构建执行
├── report.py         # 结果汇总（并发-指标表，可折算单卡吞吐）
├── tools/            # gsm8k 数据集生成工具（aisbench）+ jsonl→prompts 转换
├── datasets/         # 生成的数据集落盘处（按文件名复用）
├── results/          # evalscope 输出: results/<场景ID>/<时间戳>/<model>/
└── logs/             # run.sh 后台运行日志
```

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
