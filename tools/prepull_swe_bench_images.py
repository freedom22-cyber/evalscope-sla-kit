#!/usr/bin/env python3
"""Pre-pull SWE-bench_Pro sandbox images in parallel.

Why: ms_enclave pulls the per-instance image one at a time, and a single
stream from the only reachable registry mirror (docker.1panel.live) runs at
~0.25 MB/s -> ~30 h per 32-instance window.  The host's egress is fine
(~30 MB/s aggregate); it is the per-connection latency that hurts, so
pulling many images concurrently multiplies the throughput.

Usage (inside the ais-bench container, which has docker-py + modelscope):
    DOCKER_HOST=unix://<host dockersock>/docker.sock \
        python3 prepull_swe_bench_images.py <offset> <count> [threads]

DOCKER_HOST is taken from the environment when set (run_all_batches.sh
passes the relay path explicitly); the fallback below only fits the
original NFS deployment box.
"""
import concurrent.futures
import os
import sys
import time

# DOCKER_HOST: 环境变量优先（run_all_batches.sh 会显式传 relay 路径）;
# 兜底 SWE_RELAY_SOCK, 再兜底原始 NFS 部署机的绝对路径（仅本机布局成立）。
_RELAY = os.environ.get('SWE_RELAY_SOCK',
                        '/mnt/sfs_turbo/c00803992/dockersock/docker.sock')
os.environ.setdefault('DOCKER_HOST', 'unix://' + _RELAY.removeprefix('unix://'))

import docker  # noqa: E402
from modelscope.msdatasets import MsDataset  # noqa: E402

DATASET = 'ScaleAI/SWE-bench_Pro'
SUBSET = 'default'
SPLIT = 'test'
REPO = 'jefzda/sweap-images'


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    offset = int(sys.argv[1])
    count = int(sys.argv[2])
    threads = int(sys.argv[3]) if len(sys.argv) > 3 else 16

    rows = list(MsDataset.load(DATASET, subset_name=SUBSET, split=SPLIT))
    window = rows[offset:offset + count]
    tags = [r['dockerhub_tag'] for r in window if r.get('dockerhub_tag')]
    print(f'pulling {len(tags)} images (offset={offset}, threads={threads})', flush=True)

    client = docker.from_env()
    done = [0]
    started = time.time()

    def pull(tag):
        image = f'{REPO}:{tag}'
        try:
            client.images.pull(image, decode=True)
            return ('ok', tag)
        except Exception as exc:  # noqa: BLE001
            return ('err', tag, str(exc)[:200])

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        for result in pool.map(pull, tags):
            done[0] += 1
            elapsed = time.time() - started
            print(f'[{done[0]}/{len(tags)}] {result[0]} {result[1]} '
                  f'({elapsed / 60:.1f} min elapsed)', flush=True)
            if result[0] == 'err':
                print(f'    error: {result[2]}', flush=True)

    return 0


if __name__ == '__main__':
    sys.exit(main())
