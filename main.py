"""磁力链实时解析查看器 —— 入口。

用法：python main.py
"""
import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from core.config import AppConfig
from ui.main_window import MainWindow
from ui.theme import apply_theme


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Magnet Viewer")
    app.setApplicationDisplayName("磁力链实时解析查看器")
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)
    # 按配置挂主题（默认浅色；dark=深色；system=跟随系统深浅）
    apply_theme(app, str(AppConfig().get("ui_theme") or "light"))
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
