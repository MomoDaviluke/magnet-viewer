"""20 套测试回归运行器：一键全量回归，汇总退出码。

依据 README.md 退出码约定与 t4_acceptance_plan.md 回归契约（D4）：
- 下载管理模块改造后必须保证 8 套旧测试全绿（0=通过 / 1=失败 / 2=SKIP）。

末位 `close_lag_test`（plan 阶段 A + 阶段 A 审查整改）守关闭路径：首次
close() 必须 <200ms 返回、遮罩可见、后台收尾各动作恰好一次（C1 快照取自
shutdown 之前）、硬超时**自动**触发关窗、停机窗口内 22 个入口/桥回调全部
守卫早退、遮罩绘制/跟动、closeEvent 逆常回退、重复关闭幂等。

套件构成：第 1 套 `contract_check` 为对外契约自检（秒级，不启会话；
拆分 `core/fetcher.py` 期间用它守住 23 个公开接口与协作模块签名）；
其后 8 套为旧测试兼容契约（解析/流媒体/GUI，含混合 v2 入口矩阵）；
末套 `download_mgr_test` 为下载管理模块验收（约 2 分钟，含本机做种闭环）。

SKIP 判定（REVIEW-2026-09 P0-3）：部分套件因依赖缺失显式跳过（退出码 2）
不视为失败，但 **全部跳过 = 失败**——那意味着环境崩坏或导入段被误吞，
绝不能打印「回归全绿」。

用法：
    python regression_run.py            # 全量 19 套
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
    "theme_check",          # 主题门禁：色值/内联样式只许在 ui/theme.py（plan B4）
    "persist_test",       # 阶段 1 持久化专项（假依赖，秒级，先跑最便宜的失败信号）
    "session_test",       # 阶段 2 会话核心专项（假依赖，秒级）
    "registry_test",      # 阶段 3 注册表与锁归属专项（假句柄，秒级）
    "taskops_test",       # 阶段 4 任务 CRUD 专项（假句柄+真注册表，秒级）
    "resolver_test",      # 阶段 5 解析与元数据编排专项（假会话，秒级）
    "preview_test",       # 阶段 5 预览桥与状态专项（假句柄，秒级）
    "playback_window_test",  # plan/07 阶段 1 播放窗口按字节+deadline 递增（假句柄，秒级）
    "smoke_test",
    "local_magnet_test",
    "local_torrent_test",
    "single_file_test",
    "hybrid_v2_test",     # 入口矩阵补齐：混合 v2 × 本地种子/磁力链两入口
    "gui_feature_test",
    "moov_stream_test",
    "qt_stream_open_test",
    "download_mgr_test",
    "cache_mode_e2e_test",  # plan/06 缓存模式真链路：转正/续传/清理保护（真 libtorrent）
    "close_lag_test",       # plan 阶段 A：关窗异步化（遮罩/后台收尾/硬超时兜底）
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
        if len(skipped) == len(results):
            # 全 SKIP = 环境崩坏或导入段被误吞，绝不能算绿灯
            # （REVIEW-2026-09 P0-3：旧逻辑全 SKIP 也打印「回归全绿」）
            print(f"\nX 全部 {len(skipped)} 套被跳过——按失败处理"
                  f"（全跳过 = 环境或代码出了系统性问题）")
            return 1
        print(f"\n- 依赖缺失显式跳过：{skipped}（部分跳过不视为失败）")
    print("\n=== 回归全绿（契约未破）===")
    return 0


if __name__ == "__main__":
    sys.exit(main())