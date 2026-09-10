"""主题门禁：色值/内联样式必须只出现在 ui/theme.py。

背景
----
阶段 B 把界面升级为「单一来源设计系统」：**只有 ui/theme.py 能写 hex 色值、
只有它能调 setStyleSheet**，其余 ui 模块需要局部样式时一律走
`setObjectName` + theme.py 里的 QSS 规则（动态属性配属性选择器）。
这条约定靠人眼 review 守不住——本脚本把它变成机器门禁。

规则（plan B1/B2）
------------------
  R1  ui/*.py（除 theme.py）不得出现 hex 色值（#rgb / #rrggbb / #rrggbbaa）
  R2  ui/*.py（除 theme.py）不得调用 setStyleSheet
      （仅注释里提到该名字 → 警告不失败，避免文档误伤）
  R3  ui/theme.py 必须导出约定 token：BG/BG_PANEL/BG_INPUT/BG_HOVER/BORDER/
      TEXT/TEXT_MUTED/TEXT_DIM/ACCENT/OK/WARN/DANGER 与 SP_XS..SP_XL /
      R_SM..R_LG

用法
----
    .\\.venv\\Scripts\\python.exe theme_check.py

退出码：`0` 通过 · `1` 违规 · `2` 环境/依赖异常。
不启 libtorrent 会话、不联网，秒级完成（可放进 regression_run.py）。
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(ROOT, "ui")
THEME = "theme.py"

# 白名单：确有正当理由出现 hex 字面量的文件（当前为空——新增必须在此写
# 明理由，否则门禁应保持红）
ALLOW: set[str] = set()

# R1：#rgb / #rrggbb / #rrggbbaa（词边界，避免误吃 #topBar 这类 objectName）
HEX_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
# R2：真正的调用形态（注释里提名字不算违规）
CALL_RE = re.compile(r"\.setStyleSheet\s*\(")
MENTION_RE = re.compile(r"setStyleSheet")

# R3：约定 token（token 名 = 对外词汇，改名前必须同步本清单与 plan）
REQUIRED_TOKENS = [
    "BG", "BG_PANEL", "BG_INPUT", "BG_HOVER", "BORDER",
    "TEXT", "TEXT_MUTED", "TEXT_DIM", "ACCENT", "OK", "WARN", "DANGER",
    "SP_XS", "SP_SM", "SP_MD", "SP_LG", "SP_XL",
    "R_SM", "R_MD", "R_LG",
]

OK: list[str] = []
FAIL: list[str] = []
WARN: list[str] = []


def check(cond: bool, msg: str) -> None:
    (OK if cond else FAIL).append(msg)
    print(("  [OK] " if cond else "  [FAIL] ") + msg)


def scan_sources() -> list[tuple[str, str]]:
    """返回 [(相对路径, 源码)]，排除 ui/theme.py（唯一允许写色值的文件）。"""
    out: list[tuple[str, str]] = []
    for name in sorted(os.listdir(UI_DIR)):
        if not name.endswith(".py") or name == THEME:
            continue
        rel = f"ui/{name}"
        if rel in ALLOW:
            continue
        with open(os.path.join(UI_DIR, name), "r", encoding="utf-8") as fh:
            out.append((rel, fh.read()))
    return out


def rule1(sources: list[tuple[str, str]]) -> None:
    """R1：ui/*.py（除 theme.py）零 hex 色值。"""
    hits: list[str] = []
    for rel, src in sources:
        for no, line in enumerate(src.splitlines(), start=1):
            for m in HEX_RE.finditer(line):
                hits.append(f"{rel}:{no} {m.group(0)}  {line.strip()}")
    check(not hits, "R1 ui/*.py（除 theme.py）零 hex 色值")
    for h in hits:
        print(f"      · {h}")


def rule2(sources: list[tuple[str, str]]) -> None:
    """R2：ui/*.py（除 theme.py）不调 setStyleSheet。"""
    calls: list[str] = []
    for rel, src in sources:
        for no, line in enumerate(src.splitlines(), start=1):
            if CALL_RE.search(line):
                calls.append(f"{rel}:{no} {line.strip()}")
            elif MENTION_RE.search(line):
                WARN.append(f"{rel}:{no} 注释/文案提到 setStyleSheet")
    check(not calls, "R2 ui/*.py（除 theme.py）零 setStyleSheet 调用")
    for c in calls:
        print(f"      · {c}")


def rule3() -> None:
    """R3：theme.py 必须导出约定 token（并已被 import 成功）。"""
    try:
        sys.path.insert(0, ROOT)
        import ui.theme as theme
    except Exception as e:      # 依赖缺失：显式退出码 2，绝不假装通过
        print(f"依赖缺失，无法执行主题门禁：{e}")
        sys.exit(2)
    missing = [t for t in REQUIRED_TOKENS if not hasattr(theme, t)]
    check(not missing, f"R3 ui/theme.py 导出约定 token（{len(REQUIRED_TOKENS)} 项）")
    if missing:
        print(f"      · 缺失：{', '.join(missing)}")
    check(isinstance(getattr(theme, "QSS", None), str)
          and len(getattr(theme, "QSS", "")) > 0, "R3 ui/theme.py 导出非空 QSS")
    check(callable(getattr(theme, "apply_theme", None)),
          "R3 ui/theme.py 导出 apply_theme(app)")


def main() -> int:
    print("=== 主题门禁（色值/内联样式只允许在 ui/theme.py）===")
    if not os.path.isdir(UI_DIR):
        print(f"依赖缺失：找不到 {UI_DIR}")
        return 2
    sources = scan_sources()
    print(f"- 扫描 {len(sources)} 个 ui/*.py（已排除 {THEME}"
          + (f"，白名单 {sorted(ALLOW)}" if ALLOW else "") + "）")
    rule1(sources)
    rule2(sources)
    rule3()
    for w in WARN:
        print(f"  [WARN] {w}")
    print(f"\n=== 主题门禁：OK {len(OK)} / FAIL {len(FAIL)} ===")
    if FAIL:
        for m in FAIL:
            print(f"  - {m}")
        print("\nX 主题门禁未过：色值/内联样式必须收敛到 ui/theme.py"
              "（改 objectName + QSS 规则，不要写局部色值）")
        return 1
    print("=== 主题门禁：OK ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
