"""磁力链实时解析查看器 —— 入口。

用法：python main.py
"""
import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from core.config import AppConfig
from ui.main_window import MainWindow
from ui.style import install as install_chevron_style
from ui.theme import apply_theme


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Magnet Viewer")
    app.setApplicationDisplayName("磁力链实时解析查看器")
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)
    # 箭头绘制：必须在 apply_theme（挂 QSS）**之前**装 ChevronStyle——之后 QSS 会
    # 包成 QStyleSheetStyle 代理，本样式才是它转发箭头绘制时的基样式。
    install_chevron_style(app)
    # 按配置挂主题（默认浅色；dark=深色；system=跟随系统深浅）
    apply_theme(app, str(AppConfig().get("ui_theme") or "light"))
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
