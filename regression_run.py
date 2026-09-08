"""9 套测试回归运行器：一键全量回归，汇总退出码。

依据 README.md 退出码约定与 t4_acceptance_plan.md 回归契约（D4）：
下载管理模块改造后必须保证 7 套旧测试全绿（0=通过 / 1=失败 / 2=SKIP）。

套件构成：第 1 套 `contract_check` 为对外契约自检（秒级，不启会话；
拆分 `core/fetcher.py` 期间用它守住 25 个公开接口与协作模块签名）；
其后 7 套为旧测试兼容契约（解析/流媒体/GUI）；
末套 `download_mgr_test` 为下载管理模块验收（约 2 分钟，含本机做种闭环）。

用法：
    python regression_run.py            # 全量 9 套
    python regression_run.py smoke      # 单套（按名字前缀匹配）

退出码：任一测试 FAIL(1) → 本脚本退出 1；全部通过(0) → 0；
       SKIP(2) 不计失败（显式跳过，报告时区分标注）。
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 首套 = 对外契约自检（秒级，fetcher 重构期间的安全网，须最先跑）；
# 其后 7 套 = 旧测试兼容契约；末套 = 下载管理模块验收
SUITES = [
    "contract_check",
    "persist_test",       # 阶段 1 持久化专项（假依赖，秒级，先跑最便宜的失败信号）
    "session_test",       # 阶段 2 会话核心专项（假依赖，秒级）
    "registry_test",      # 阶段 3 注册表与锁归属专项（假句柄，秒级）
    "taskops_test",       # 阶段 4 任务 CRUD 专项（假句柄+真注册表，秒级）
    "smoke_test",
    "local_magnet_test",
    "local_torrent_test",
    "single_file_test",
    "gui_feature_test",
    "moov_stream_test",
    "qt_stream_open_test",
    "download_mgr_test",
]

NAME = {
    0: "PASS",
    1: "FAIL",
    2: "SKIP",
}


def run_one(py: str, suite: str, logdir: str) -> int:
    log = os.path.join(logdir, f"reg_{suite}.log")
    t0 = time.time()
    with open(log, "wb") as f:
        rc = subprocess.call([py, f"{suite}.py"], stdout=f, stderr=subprocess.STDOUT)
    return rc, time.time() - t0, log


def main() -> int:
    py = sys.executable
    filt = [s for s in SUITES if not sys.argv[1:] or s.startswith(sys.argv[1])]
    if not filt:
        print(f"未匹配到测试：{sys.argv[1]!r}（可选：{' '.join(SUITES)}）")
        return 1
    print(f"=== 回归运行（{len(filt)}/{len(SUITES)} 套）===")
    results: list[tuple[str, int, float]] = []
    for suite in filt:
        print(f"\n--- {suite} ...", flush=True)
        rc, dt, log = run_one(py, suite, os.path.dirname(os.path.abspath(__file__)))
        print(f"    {suite}: {NAME.get(rc, rc)}（{dt:.1f}s），日志 {os.path.basename(log)}")
        results.append((suite, rc, dt))
    print("\n=== 汇总 ===")
    failed = [s for s, rc, _ in results if rc == 1]
    skipped = [s for s, rc, _ in results if rc == 2]
    for s, rc, dt in results:
        print(f"  {NAME.get(rc, rc):4s}  {s:24s} {dt:6.1f}s")
    if failed:
        print(f"\nX 回归失败：{failed}（修复后重跑本脚本）")
        return 1
    if skipped:
        print(f"\n- 依赖缺失显式跳过：{skipped}（不视为失败）")
    print("\n=== 回归全绿（契约未破）===")
    return 0


if __name__ == "__main__":
    sys.exit(main())