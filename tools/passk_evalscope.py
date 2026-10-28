#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""evalscope CLI wrapper: 让 --dataset-args 支持 primary_metric(JSON dict)。

用法（与原生 evalscope CLI 完全一致，只是入口换成本文件）:
    python tools/passk_evalscope.py eval --datasets ... --dataset-args '{...}'
    # 或直接 ./tools/passk_evalscope.py eval ...

⚠️ 必须用**装了 evalscope 的那个解释器**跑: accuracy_runner.py 在 k>1 时会自动
   用 evalscope 同环境的 python 调本文件（resolve_python_exe），别手写错环境。

为什么需要 wrapper（evalscope 1.14.x 实测 + 源码核对）:
  pass@k 口径(aggregation=mean_and_pass_at_k)会产出 k+1 个非诊断指标
  (mean + pass@1..k), 报告生成器 report/generator.py::_resolve_semantics
  要求 BenchmarkMeta.primary_metric 声明唯一主指标, 否则:
      ValueError: Found N non-diagnostic metric identities; no unique primary metric.
  而 primary_metric 只能经 BenchmarkMeta._update(dataset_args) 覆盖, 且:
    - 传 str  -> _normalize_primary_metric 归一化成 MetricSelector(name=...)
                 (aggregation 缺省=通配) -> k+1 个指标全匹配 -> 依然 AMBIGUOUS
    - 传 dict -> dataclass 裸 setattr 保留 dict -> _validate_primary_metric
                 访问 .name 直接 AttributeError
  官方用法(benchmarks/humanevalplus)是 MetricSelector 对象:
      MetricSelector(name='accuracy', aggregation='pass_at_k', dimensions={'k': 4})
  本 wrapper 在 _update 前把 dict 转成 MetricSelector, 其余行为与原 CLI 完全一致。
"""
import sys

from evalscope.api.benchmark.meta import BenchmarkMeta
from evalscope.api.metric.semantics import MetricSelector

_orig_update = BenchmarkMeta._update


def _patched_update(self, args):
    """dataset_args 里的 primary_metric 若是 dict, 先转 MetricSelector 再走原逻辑。"""
    pm = args.get('primary_metric') if isinstance(args, dict) else None
    if isinstance(pm, dict):
        args = dict(args)
        args['primary_metric'] = MetricSelector(**pm)
    return _orig_update(self, args)


BenchmarkMeta._update = _patched_update

from evalscope.cli.cli import run_cmd  # noqa: E402  (patch 必须在 CLI import 前)

if __name__ == '__main__':
    sys.exit(run_cmd())
