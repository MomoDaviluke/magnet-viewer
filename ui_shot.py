"""UI 截图工具（离屏，人工验收用）：python ui_shot.py [输出目录]

产物：01-empty / 02-files / 03-preview / 04-downloads 四张 PNG，
用于"改造前 vs 改造后"对照。默认写到 .hermes/shots/（已 gitignore）。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication          # noqa: E402
from ui.main_window import MainWindow               # noqa: E402
from ui.theme import apply_theme                    # noqa: E402


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(".hermes", "shots")
    os.makedirs(out, exist_ok=True)
    app = QApplication([])
    apply_theme(app)
    w = MainWindow()
    w.resize(1040, 700)
    w.show()
    app.processEvents()
    for idx, name in enumerate(("01-empty", "02-files", "03-preview",
                               "04-downloads"), start=1):
        w.tabs.setCurrentIndex(min(idx - 1, w.tabs.count() - 1))
        app.processEvents()
        w.grab().save(os.path.join(out, f"{name}.png"))
        print("saved", os.path.join(out, f"{name}.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
