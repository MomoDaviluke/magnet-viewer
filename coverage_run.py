"""覆盖率报告（REVIEW.md §四建议 3 的落地；只报告不设门禁）。

用法：
    python coverage_run.py            # 快速集（假依赖专项 + 秒级套件）
    python coverage_run.py full       # 全量 16 套（约 3~4 分钟，含端到端）

实现要点：测试经 `python -m coverage run` 逐个拉起（run_one 本就是
subprocess，进程内钩子方案会漏采——coverage run 直接当入口最稳）；
各进程写 .coverage.<host>.<pid> 分片，最后 combine + 终端报告。

门禁策略（对齐 G4「实测≥目标才上调」原则）：先观察基线不设阈值；
把「未被任何测试触达的 core/ 代码」显性化即是本脚本的全部目的。

退出码：0=成功；1=任一测试 FAIL；2=SKIP（coverage 未安装等依赖缺失）。
"""
from __future__ import annotations

import glob
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# 快速集：6 套假依赖专项 + 契约 + 冒烟（秒级，日常开发循环用）
FAST = ["contract_check", "persist_test", "session_test", "registry_test",
        "taskops_test", "resolver_test", "preview_test", "smoke_test"]


def main() -> int:
    full = len(sys.argv) > 1 and sys.argv[1] == "full"
    if full:
        from regression_run import SUITES
        suites = SUITES
    else:
        suites = FAST
    try:
        import coverage  # noqa: F401
    except Exception:
        print("coverage 未安装：pip install coverage 后重试")
        return 2
    py = sys.executable
    for f in glob.glob(os.path.join(HERE, ".coverage*")):
        if not f.endswith(".coveragerc") and os.path.basename(f) != ".coverage":
            os.remove(f)
    old = os.path.join(HERE, ".coverage")
    if os.path.exists(old):
        os.remove(old)
    rc_all = 0
    for s in suites:
        r = subprocess.call(
            [py, "-m", "coverage", "run", "--parallel-mode",
             "--rcfile", os.path.join(HERE, ".coveragerc"),
             f"{s}.py"], cwd=HERE,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        print(f"  {s}: {r}")
        rc_all = rc_all or (r == 1)
    subprocess.call([py, "-m", "coverage", "combine"], cwd=HERE)
    subprocess.call([py, "-m", "coverage", "report"], cwd=HERE)
    print("\n（只报告不设门禁；明细：python -m coverage html → htmlcov/）")
    return 1 if rc_all else 0


if __name__ == "__main__":
    sys.exit(main())
