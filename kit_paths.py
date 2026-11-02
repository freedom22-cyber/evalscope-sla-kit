"""kit_paths.py —— 统一解析 agentx / bfcl / swe 段的机器相关路径。

设计原则（优先级从高到低）:
  1. 环境变量（AGENTX_BIN / BFCL_VENV / SWE_WORK_DIR …）—— 部署时最灵活
  2. config.yaml 对应段显式写的值 —— 前端配置页写入 web_state override
  3. kit 相对默认（.cache/agentx-venv, .cache/bfcl-venv, results/swe_pro …）
     —— setup.sh 在 kit 本地创建，开箱即用、不依赖外部 NFS

这样 config.yaml 不含任何机器绝对路径，换机器只改环境变量或前端填写框。
"""

import os

KIT_ROOT = os.path.dirname(os.path.abspath(__file__))

# ---- agentx ----
def resolve_agentx_bin(cfg: dict) -> str:
    """agentx 可执行文件: AGENTX_BIN > config agentx.bin > PATH 上的 agentx。"""
    explicit = os.environ.get("AGENTX_BIN") or (cfg.get("agentx") or {}).get("bin")
    return explicit or "agentx"


def resolve_agentx_env(cfg: dict) -> dict:
    """agentx 子进程环境变量: config agentx.env 覆盖 os.environ。

    HF_HOME / HF_HUB_CACHE / AIPERF_* 未显式配置时默认落到 kit 本地 .cache/，
    首次跑会下载 GB 级语料——放 kit 下不依赖外部 NFS 挂载。
    """
    env = os.environ.copy()
    cache_dir = os.path.join(KIT_ROOT, ".cache")
    defaults = {
        "HF_ENDPOINT": "https://hf-mirror.com",
        "HF_HUB_DISABLE_XET": "1",
        "HF_HOME": os.path.join(cache_dir, "hf"),
        "HF_HUB_CACHE": os.path.join(cache_dir, "hf", "hub"),
        "AIPERF_DATASET_MMAP_CACHE_DIR": os.path.join(cache_dir, "aiperf"),
        "AIPERF_DATASET_MMAP_BASE_PATH": os.path.join(cache_dir, "mmap"),
    }
    for k, v in ((cfg.get("agentx") or {}).get("env") or {}).items():
        env[k] = str(v)
    # 只补未由 config/env 设置的键
    for k, v in defaults.items():
        if k not in env or not env[k]:
            env[k] = v
    return env


# ---- bfcl ----
def resolve_bfcl_venv_python(cfg: dict) -> str:
    """BFCL 评测 venv 解释器: BFCL_VENV_PY > config bfcl.venv_python > kit .cache/bfcl-venv/bin/python。"""
    explicit = os.environ.get("BFCL_VENV_PY") or (cfg.get("bfcl") or {}).get("venv_python")
    return explicit or os.path.join(KIT_ROOT, ".cache", "bfcl-venv", "bin", "python")


def resolve_bfcl_container(cfg: dict) -> str:
    """BFCL 评测容器: BFCL_CONTAINER > config bfcl.container > 空串(容器内直跑)。"""
    return (os.environ.get("BFCL_CONTAINER")
            or (cfg.get("bfcl") or {}).get("container")
            or "")


# ---- swe ----
def resolve_swe_work_dir(cfg: dict) -> str:
    """SWE live 工作区: SWE_WORK_DIR > config swe.work_dir > kit results/swe_pro。"""
    return (os.environ.get("SWE_WORK_DIR")
            or (cfg.get("swe") or {}).get("work_dir")
            or os.path.join(KIT_ROOT, "results", "swe_pro"))


def resolve_swe_repo_path(cfg: dict) -> str:
    """SWE-bench_Pro-os 仓库: SWE_REPO_PATH > config swe.repo_path > kit .cache/swe_bench_pro_os/repo。"""
    return (os.environ.get("SWE_REPO_PATH")
            or (cfg.get("swe") or {}).get("repo_path")
            or os.path.join(KIT_ROOT, ".cache", "swe_bench_pro_os", "repo"))


def resolve_swe_venv(cfg: dict) -> str:
    """SWE 容器内 evalscope venv: SWE_VENV > config swe.venv > kit .cache/swe-venv。"""
    return (os.environ.get("SWE_VENV")
            or (cfg.get("swe") or {}).get("venv")
            or os.path.join(KIT_ROOT, ".cache", "swe-venv"))


def resolve_swe_container(cfg: dict) -> str:
    """SWE 评测容器: SWE_CONTAINER > config swe.container > 空串(容器内直跑)。"""
    return (os.environ.get("SWE_CONTAINER")
            or (cfg.get("swe") or {}).get("container")
            or "")


def resolve_swe_relay_socket(cfg: dict) -> str:
    """docker unix relay socket: SWE_RELAY_SOCKET > config swe.relay_socket > /var/run/docker.sock。"""
    return (os.environ.get("SWE_RELAY_SOCKET")
            or (cfg.get("swe") or {}).get("relay_socket")
            or "/var/run/docker.sock")


def resolve_swe_disk_mount(cfg: dict) -> str:
    """docker 根盘: SWE_DISK_MOUNT > config swe.disk_mount > /var/lib/docker。"""
    return (os.environ.get("SWE_DISK_MOUNT")
            or (cfg.get("swe") or {}).get("disk_mount")
            or "/var/lib/docker")
