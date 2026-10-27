#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""功能测试入口（兼容壳）——实现已迁移到 scripts/run_api_e2e.py。

历史沿革:
    functional_runner.py  最早的 F-01~F-05 检查 runner（ChkBook 判定 + summary.md）
    scripts/run_api_e2e.py  统一到 Simaas 风格 API E2E 框架（PASS/FAIL/WARN/SKIP,
                           四种接口协议族 openai/messages/gemini/embedding,
                           markdown 报告 + 每用例响应原文落盘）

本文件只做转发, 不再保留任何判定逻辑, 避免两套实现漂移:
    python3 functional_runner.py --list / --dry-run / --only F-03 / （直接跑）
    等价于
    python3 scripts/run_api_e2e.py --list / --dry-run / --only F-03 / （直接跑）

新用法推荐直接调 scripts/run_api_e2e.py（支持 --base-url/--model/--header/
--interface-types/--capability-repeat/--insecure 等完整参数）。
"""
import os
import sys

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(KIT_ROOT, "scripts", "run_api_e2e.py")

sys.path.insert(0, os.path.join(KIT_ROOT, "scripts"))

if __name__ == "__main__":
    if not os.path.isfile(TARGET):
        print(f"找不到功能测试主脚本: {TARGET}", file=sys.stderr)
        sys.exit(1)
    import run_api_e2e
    sys.exit(run_api_e2e.main(sys.argv[1:]))
