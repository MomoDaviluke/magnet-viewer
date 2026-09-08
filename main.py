"""磁力链实时解析查看器 —— 入口。

用法：python main.py
"""
import sys

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from ui.main_window import MainWindow
from ui.theme import apply_theme


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Magnet Viewer")
    app.setApplicationDisplayName("磁力链实时解析查看器")
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)
    apply_theme(app)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
