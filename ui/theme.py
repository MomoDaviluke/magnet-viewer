"""全局主题：暗色 QSS + 语义色常量（视频播放场景，低饱和蓝强调）。

各 UI 模块的内联样式一律引用本模块的常量，禁止再写魔法色值——
切换/调整主题只改这一个文件。QSS 由 main.py 的 apply_theme() 挂到
QApplication 上，widget 级 inline stylesheet 仍可局部覆盖（优先级更高）。
"""
from __future__ import annotations

# ---- 语义色 ----
BG = "#1b1d21"          # 窗口背景
BG_PANEL = "#232529"    # 面板/卡片
BG_INPUT = "#2a2d33"    # 输入框/下拉
BG_HOVER = "#2f3339"    # 悬停
BORDER = "#33363d"      # 边框
TEXT = "#d7dae0"        # 主文字
TEXT_MUTED = "#8b909a"  # 次级文字
TEXT_DIM = "#6b7078"    # 更暗一档（暂停/停止态）
ACCENT = "#4f8cff"      # 强调蓝（主按钮/选中/handle）
ACCENT_HOVER = "#6b9dff"
OK = "#57ab5a"          # 下载完成
WARN = "#c69026"        # 警告/限速
DANGER = "#e5534b"      # 失败/错误
SLIDER_SEGMENT = (255, 255, 255, 42)   # 缓冲分段（半透明白，暗背景可辨）

QSS = f"""
* {{ outline: none; }}
QWidget {{ background: {BG}; color: {TEXT}; font-size: 13px; }}
QVideoWidget {{ background: #000000; }}
QMainWindow, QDialog {{ background: {BG}; }}

/* ---- 标签页 ---- */
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px;
    background: {BG_PANEL}; top: -1px; }}
QTabBar::tab {{
    background: transparent; color: {TEXT_MUTED}; padding: 7px 18px;
    margin-right: 2px; border: 1px solid transparent; border-bottom: none;
    border-top-left-radius: 6px; border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{ color: {TEXT}; background: {BG_PANEL};
    border-color: {BORDER}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; background: {BG_HOVER}; }}

/* ---- 输入 ---- */
QLineEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background: {BG_INPUT}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 5px 8px; selection-background-color: {ACCENT};
}}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ width: 0; height: 0;
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 6px solid {TEXT_MUTED}; margin-right: 8px; }}
QComboBox QAbstractItemView {{ background: {BG_PANEL};
    border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}

/* ---- 按钮 ---- */
QPushButton {{
    background: {BG_INPUT}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: 6px; padding: 6px 14px;
}}
QPushButton:hover {{ background: {BG_HOVER}; border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BG_INPUT}; }}
QPushButton:disabled {{ color: {TEXT_MUTED}; border-color: {BORDER};
    background: {BG_PANEL}; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT};
    color: #ffffff; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background: {ACCENT}; }}
QPushButton#primary:disabled {{ background: {BG_INPUT}; }}

/* ---- 滑块（含 BufferedSlider，分段由 paintEvent 叠加绘制） ---- */
QSlider {{ background: transparent; min-height: 22px; }}
QSlider::groove:horizontal {{ border: none; height: 5px; border-radius: 2px;
    background: {BG_INPUT}; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 14px; margin: -5px 0;
    border-radius: 7px; background: {ACCENT_HOVER}; }}
QSlider::handle:horizontal:hover {{ background: #ffffff; }}
QSlider::groove:vertical, QSlider::sub-page:vertical {{ width: 5px; }}

/* ---- 进度条（缓冲细条） ---- */
QProgressBar {{ background: {BG_INPUT}; border: none; border-radius: 2px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}

/* ---- 树 / 表（文件树、下载列表） ---- */
QTreeWidget, QTreeView, QListWidget, QTableWidget {{
    background: {BG_PANEL}; alternate-background-color: {BG_INPUT};
    border: 1px solid {BORDER}; border-radius: 6px; padding: 2px; }}
QTreeWidget::item, QTreeView::item, QListWidget::item {{ height: 26px;
    border-radius: 4px; }}
QTreeWidget::item:selected, QTreeView::item:selected,
QListWidget::item:selected {{ background: {ACCENT}; color: #ffffff; }}
QTreeWidget::item:hover, QTreeView::item:hover,
QListWidget::item:hover {{ background: {BG_HOVER}; }}
QHeaderView::section {{
    background: {BG_INPUT}; color: {TEXT_MUTED}; border: none;
    border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER};
    padding: 5px 8px; }}

/* ---- 滚动条（细窄） ---- */
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px;
    min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_MUTED}; }}
QScrollBar:horizontal {{ background: transparent; height: 9px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 4px;
    min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_MUTED}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---- 其他 ---- */
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 6px; margin-top: 10px;
    padding-top: 6px; background: {BG_PANEL}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
    color: {TEXT_MUTED}; }}
QCheckBox, QRadioButton {{ spacing: 7px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QToolTip {{ background: {BG_INPUT}; color: {TEXT}; border: 1px solid {BORDER};
    padding: 4px 8px; }}
QMessageBox {{ background: {BG_PANEL}; }}
QMessageBox QLabel {{ background: transparent; }}
QLabel {{ background: transparent; }}
"""


def apply_theme(app) -> None:
    """把全局 QSS 挂到 QApplication（main.py 启动时调用一次）。"""
    app.setStyleSheet(QSS)
