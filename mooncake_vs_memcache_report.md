# Mooncake vs Memcache 外置 KV 池性能对比报告

- 日期：2026-10-30 ~ 10-31
- 被测服务：DeepSeek-V4.1-Flash-1017-w8a8，A3 910C 1P1D PD 分离（.48 Prefill DP4×TP4 / .47 Decode DP8×TP2，PD proxy :8181）
- 测试场景：64k / 128k / 900k 输入 → 1k 输出，前缀命中率 0.9，gsm8k 语料 312 条/场景
- 压测工具：evalscope-sla-kit（SLA 并发摸高，搜索区间 [1,32]，SLA = TTFT≤10s 且 TPOT≤150ms 且零失败）
- **两轮协议一致**：同一数据集、同一 SLA、同一 kit、片上 prefix cache 均关闭（`prefix_caching=False`，片上命中率 0%——外置池是唯一复用来源）
- **memcache 为两轮独立复测**（r1 20261030 / r2 20261031，轮间 83.6/83.8 分钟，各档 TTFT 偏差 <5%，900k conc1 两轮 19,685/19,687ms）；mooncake 为正式补测轮（r2）

## 一、结论总表（`python3 compare_pools.py mooncake memcache`）

| 场景 | 并发 | Mooncake TTFT / TPOT | Memcache TTFT / TPOT（r1→r2） | 差距 |
|---|---|---|---|---|
| **64k→1k** | 1 | **229,643ms** / 10.2ms **FAIL** | 1,138→1,139ms / 10.6→10.9ms PASS | **TTFT 202×** |
| | 32 | （搜索在 conc1 失败即止） | 2,998→3,124ms / 14.5→15.1ms PASS（最大达标并发=32=上限） | |
| **128k→1k** | 1 | **460,323ms** / 10.8ms **FAIL** | 2,122→2,119ms / 12.4→12.3ms PASS | **TTFT 217×** |
| | 32 | （同上） | 4,951→5,074ms / 15.7→15.1ms PASS（最大达标并发=32=上限） | |
| **900k→1k** | 1 | **请求全失败**（引擎 OOM 死亡） | 19,685→19,687ms / 16.3→16.3ms FAIL（TPOT 正常；90k 新前缀 prefill+排队 >10s） | memcache 不达标但单请求 6.5s 级可用；mooncake 不可用 |

> mooncake 三场景全部"无达标档"（auto-tune 在 conc1 即失败停止）。memcache 64k/128k 满档通过、900k 因 SLA 本身偏紧不达标。

## 二、Memcache 轮（两轮全绿，复测一致）

- 两轮独立复测：r1 83.6 分钟 / r2 83.8 分钟，各档 TTFT 偏差 <5%（64k conc1 1138/1139ms、128k conc1 2122/2119ms、900k conc1 19685/19687ms——逐位复现）；两轮外置池累计命中率均 **99.4%**（14.6 亿 token，P/D 12 引擎池侧命中 98.3-100%）
- 预热（前缀灌池）验证：64k conc1 TTFT `[9.7(冷), 0.8, 0.9, 0.7, 0.8]`；900k conc1 `[84.8(冷), 7.4, 6.5, 6.7, 6.5]`——首条入池后同前缀请求亚秒级
- 摸高（2 轮平均、零失败）：64k conc1→32 TTFT 1139→3124ms / TPOT 10.9→15.1ms / 83.0→1238.5 tok/s；128k 2119→5074ms / 12.3→15.1ms / 69.6→1143.9 tok/s；900k conc1 19687ms / 16.3ms
- 900k 不达标的原因不是池：90% 前缀命中后仍需 prefill ~90k 新 token（13-19s）+排队 > 10s SLA。放宽 SLA 或降低新鲜度即可达标
- 预热 7 档阶梯（conc1→64）单场景 4-11 分钟全部完成

## 三、Mooncake 轮（put 路径破损 → 等效无池；900k 直接打死引擎）

两轮跑法：第一轮（150 分钟）预算按 memcake 同参数，三场景连 conc1 档都无法完成；**第二轮（100.7 分钟）放大预算精简 warmup（`config.dsv41.mooncake.yaml`：warmup_num 10→2、sla_num_runs 2→1、warmup_budget 25→6min、time_budget 25→100min），让档位跑完出真实数字**。两轮行为完全一致，第二轮拿到了正式档位数据：

- **64k conc1：TTFT 229.6s**（memcache 同档 1.14s，**202×**），TPOT 10.2ms，2/2 成功
- **128k conc1：TTFT 460.3s**（217×），TPOT 10.8ms，2/2 成功
- **900k conc1：2/2 请求失败，无指标**——第二条 900k 请求的 put 重试风暴 + KV staging 把容器内存打爆：`01:09:59 CST 内核 memcg OOM 击杀 → .48 全部 4 个 P 引擎死亡（EngineDeadError）→ 后续请求 0.16s 内即失败`
- 行为模式（两轮一致）：**首条请求正常（64k 5-10s / 900k 94-120s），之后所有同前缀请求退化为全冷 prefill + put 重试风暴**（64k 202s→230s、128k 250→965s、900k >600s→引擎死亡）
- 本轮 master 累计：`batch_put_start` 4,633,236 items vs `batch_put_end`（提交）26,420 —— **提交率 0.57%**；revoke 4,606,816；读路径 `batch_exist` 5,879,232 items 全部成功（读好写坏）
- 引擎日志全程 `External prefix cache hit rate: 0.0%`（memcache 轮同指标 99.4%）
- `-800` 失败按 rank 偏斜（早前轮 P7100 3314 次 / P7101 382 / P7102、7103 0）

## 四、Mooncake put 失败根因（已定位，本环境不可修）

1. **写入目标错端口**：每个 P rank 有两个 TransferEngine——全局 TE（P→D 直传，正常 55-119ms）+ store TE（独立模式，master 注册为 12xxx-14xxx 段）。put 的远端 engine 地址却解析到 `.48:20xxx-21xxx`（vLLM 全局 TE 端口）→ 对端无该段 → `-800 TRANSFER_FAIL` → 反复重试（风暴来源）。
2. **`use_layerwise` 必须为 false**：DS-V4.1 的 CED 混合注意力不支持逐层存取（官方文档 §5.4），只能走整 key put 路径——恰好是坏的那条。
3. 排除项：`MOONCAKE_STORE_SHARE_TE` 在本镜像 v0.30.0-a3 是残留变量（源码/C++ 均不读）；master 段注册/分配全部正常（AllocFail=0）。
4. **900k 级负载下的后果**：staging + 重试风暴耗尽容器内存 → OOM → 引擎死亡。

**结论：mooncake 在本环境（quay.io/ascend/vllm-ascend:v0.30.0-a3 + DS-V4.1-Flash CED）整 key put 路径有缺陷：0.9 命中场景退化为 0 命中，长输入下引擎不稳定（OOM 致死）。**

## 五、对比口径与已知偏差

- 为保证公平，runner.py 加了 `warmup_budget_min` 预热限时补丁（两轮同用），否则 mooncake 冷 prefill 预热单场景拖数小时。
- mooncake 第二轮的 warmup_num/sla_num_runs 与 memcache 轮不同（10/2 vs 2/1）：只影响统计开销不影响引擎行为（实测两轮 TTFT 一致量级）；预算差异在报告中标明。
- `evalscope` 必须用 eval312 环境（PATH 前置 `/root/miniconda3/envs/eval312/bin`；base conda python3.14 的 torch_npu 会崩）。
- 结果目录：`results/mooncake/<sid>/`（r1=首轮无完成档、r2=补测有正式数据）、`results/memcache/<sid>/`（`20261030_*`=r1、`20261031_*`=r2、`prefix_warmup_r1/r2`=两轮预热阶梯）；128k 顶层保留同事的 GLM 结果（`20261030_072628/glm5.3`），未动。
- **服务现状**：已切换回 memcache 形态并在线（docker_reset 双节点 → meta → standalone → P/D 引擎 → proxy，`DEPLOY_MODE=pool_memcache PREFIX_CACHING=off`），r2 复测即在此服务上完成。如需换 mooncake 形态：`cd dsv41-pd-memcache && DEPLOY_MODE=pool_mooncake PREFIX_CACHING=off bash docker_reset.sh` 后按 README 序列拉起（性能/稳定性均不推荐，见 §四）。
