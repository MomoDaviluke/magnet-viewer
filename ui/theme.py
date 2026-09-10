"""全局设计系统：语义色 / 间距 / 圆角 / 字号 + 全局 QSS。

规则（机器门禁 theme_check.py 强制）：
- **本文件之外任何 ui/*.py 不得出现 hex 色值，也不得调 setStyleSheet**；
  需要局部样式时一律换成 setObjectName + 本文件 QSS 规则（动态属性配
  `QLabel#playerTitle[error="true"]` 这类属性选择器，改属性后需调用
  `style().unpolish(w)` + `style().polish(w)` 才生效）。
- 视觉规格：8px 网格（SP_*）、圆角 8/12、正文 13px、次级 12px、标题 15px。
- 暗色为唯一主题（YAGNI：不做主题切换）。
"""
from __future__ import annotations

# ---- 语义色（暗色，对比度 ≥ 4.5:1 正文）----
BG = "#14161a"            # 窗口底
BG_PANEL = "#1b1e24"      # 面板/卡片
BG_INPUT = "#22262e"      # 输入/下拉
BG_HOVER = "#2a2f38"      # 悬停
BG_SELECTED = "#243043"   # 选中行（比 accent 淡，避免大面积高饱和）
BORDER = "#2f353f"        # 常规边框
BORDER_STRONG = "#3d4450"  # 分组/聚焦前边框
TEXT = "#e6e9ef"          # 主文字
TEXT_MUTED = "#99a1ae"    # 次级
TEXT_DIM = "#6d7683"      # 三级（禁用/时间戳）
ACCENT = "#5b9dff"        # 强调（主按钮/选中/进度）
ACCENT_HOVER = "#7cb2ff"
ACCENT_PRESSED = "#4a86e0"
OK = "#4ec27a"            # 完成/做种
WARN = "#e0a83c"          # 警告/限速
DANGER = "#ef6b62"        # 失败
SLIDER_SEGMENT = (255, 255, 255, 46)   # 缓冲分段（半透明白）

# ---- 间距（8px 网格）----
SP_XS, SP_SM, SP_MD, SP_LG, SP_XL = 4, 8, 12, 16, 24
R_SM, R_MD, R_LG = 6, 8, 12
FS_CAPTION, FS_BODY, FS_TITLE, FS_DISPLAY = 12, 13, 15, 20

QSS = f"""
* {{ outline: none; }}
QWidget {{ background: {BG}; color: {TEXT}; font-size: {FS_BODY}px; }}
QMainWindow, QDialog {{ background: {BG}; }}
QToolTip {{ background: {BG_INPUT}; color: {TEXT};
    border: 1px solid {BORDER_STRONG}; padding: {SP_XS}px {SP_SM}px;
    border-radius: {R_SM}px; }}

/* ---------- 卡片 / 分区 ---------- */
QFrame#card, QWidget#card {{ background: {BG_PANEL};
    border: 1px solid {BORDER}; border-radius: {R_LG}px; }}
QLabel#sectionTitle {{ font-size: {FS_TITLE}px; font-weight: 600;
    color: {TEXT}; padding: 0; }}
QLabel#fieldNote, QLabel#hint, QLabel#metaLabel {{
    color: {TEXT_MUTED}; font-size: {FS_CAPTION}px; }}
QLabel#emptyHint {{ color: {TEXT_DIM}; font-size: {FS_BODY}px; }}

/* ---------- 顶栏 ---------- */
QWidget#topBar {{ background: {BG_PANEL};
    border-bottom: 1px solid {BORDER}; }}
QLineEdit#urlInput {{ background: {BG_INPUT}; border: 1px solid {BORDER};
    border-radius: {R_MD}px; padding: {SP_SM}px {SP_MD}px; min-height: 20px; }}
QLineEdit#urlInput:focus {{ border: 1px solid {ACCENT}; }}

/* ---------- 按钮 ---------- */
QPushButton {{ background: {BG_INPUT}; color: {TEXT};
    border: 1px solid {BORDER}; border-radius: {R_MD}px;
    padding: {SP_SM}px {SP_LG}px; min-height: 20px; }}
QPushButton:hover {{ background: {BG_HOVER}; border-color: {BORDER_STRONG}; }}
QPushButton:pressed {{ background: {BG_INPUT}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; background: {BG_PANEL};
    border-color: {BORDER}; }}
QPushButton#primary {{ background: {ACCENT}; border-color: {ACCENT};
    color: #ffffff; font-weight: 600; }}
QPushButton#primary:hover {{ background: {ACCENT_HOVER}; }}
QPushButton#primary:pressed {{ background: {ACCENT_PRESSED}; }}
QPushButton#primary:disabled {{ background: {BG_INPUT}; color: {TEXT_DIM}; }}
QPushButton#ghost {{ background: transparent; border-color: transparent;
    color: {TEXT_MUTED}; }}
QPushButton#ghost:hover {{ background: {BG_HOVER}; color: {TEXT}; }}
QPushButton#playButton {{ background: {ACCENT}; border: none; color: #ffffff;
    border-radius: 18px; font-weight: 600; padding: 0; }}

/* ---------- 标签页 ---------- */
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: {R_LG}px;
    background: {BG_PANEL}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {TEXT_MUTED};
    padding: {SP_SM}px {SP_LG}px; margin-right: {SP_XS}px;
    border: 1px solid transparent; border-bottom: none;
    border-top-left-radius: {R_MD}px; border-top-right-radius: {R_MD}px;
    min-width: 72px; }}
QTabBar::tab:selected {{ color: {TEXT}; background: {BG_PANEL};
    border-color: {BORDER}; border-bottom: 2px solid {ACCENT}; }}
QTabBar::tab:hover:!selected {{ color: {TEXT}; background: {BG_HOVER}; }}

/* ---------- 输入类 ---------- */
QLineEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background: {BG_INPUT}; color: {TEXT}; border: 1px solid {BORDER};
    border-radius: {R_MD}px; padding: {SP_XS}px {SP_SM}px;
    selection-background-color: {ACCENT}; }}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {ACCENT}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ width: 0; height: 0;
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 6px solid {TEXT_MUTED}; margin-right: {SP_SM}px; }}
QComboBox QAbstractItemView {{ background: {BG_PANEL};
    border: 1px solid {BORDER_STRONG};
    selection-background-color: {BG_SELECTED}; color: {TEXT}; }}

/* ---------- 滑块 / 进度 ---------- */
QSlider {{ background: transparent; min-height: 22px; }}
QSlider::groove:horizontal {{ border: none; height: 4px; border-radius: 2px;
    background: {BG_INPUT}; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -5px 0;
    border-radius: 6px; background: #ffffff; }}
QSlider::handle:horizontal:hover {{ background: {ACCENT_HOVER}; }}
QProgressBar {{ background: {BG_INPUT}; border: none; border-radius: 2px; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}
QProgressBar#shutdownBar::chunk {{ background: {ACCENT}; }}

/* ---------- 树 / 列表 / 表 ---------- */
QTreeView, QTreeWidget, QListView, QListWidget, QTableView, QTableWidget {{
    background: {BG_PANEL}; alternate-background-color: #1f232a;
    border: 1px solid {BORDER}; border-radius: {R_LG}px; padding: {SP_XS}px;
    show-decoration-selected: 1; }}
QTreeView::item, QListView::item {{ min-height: 28px; border-radius: {R_SM}px;
    padding: 2px {SP_XS}px; }}
QTreeView::item:selected, QListView::item:selected {{
    background: {BG_SELECTED}; color: {TEXT}; }}
QTreeView::item:hover, QListView::item:hover {{ background: {BG_HOVER}; }}
QHeaderView::section {{ background: {BG_INPUT}; color: {TEXT_MUTED};
    border: none; border-bottom: 1px solid {BORDER};
    padding: {SP_SM}px {SP_SM}px; font-size: {FS_CAPTION}px; }}
QListView#thumbList {{ background: {BG_PANEL}; border: 1px solid {BORDER};
    border-radius: {R_LG}px; padding: {SP_SM}px; }}
QListView#thumbList::item {{ border-radius: {R_SM}px; padding: {SP_XS}px; }}
QTreeView#fileTree, QTreeView#taskList {{ border: none; background: transparent;
    padding: 0; }}
QLabel#imageViewer {{ background: #101216; color: {TEXT_DIM};
    border: 1px dashed {BORDER_STRONG}; border-radius: {R_LG}px; }}

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER_STRONG};
    border-radius: 5px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {TEXT_DIM}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER_STRONG};
    border-radius: 5px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: {TEXT_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- 命名控件（B3 逐面板打磨） ---------- */
QLabel#playerTitle {{ color: {TEXT_MUTED}; padding: {SP_XS}px 2px;
    font-weight: 500; }}
QLabel#playerTitle[error="true"] {{ color: {DANGER}; font-weight: 600; }}
QWidget#statusBar {{ background: {BG_PANEL};
    border-top: 1px solid {BORDER}; }}
QLabel#statusState {{ color: {TEXT}; }}

/* ---------- 其他 ---------- */
QGroupBox {{ border: 1px solid {BORDER}; border-radius: {R_LG}px;
    margin-top: {SP_MD}px; padding-top: {SP_SM}px; background: {BG_PANEL}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: {SP_MD}px;
    padding: 0 {SP_XS}px; color: {TEXT_MUTED}; }}
QCheckBox, QRadioButton {{ spacing: {SP_SM}px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 15px; height: 15px; }}
QVideoWidget {{ background: #000000; }}
QMessageBox {{ background: {BG_PANEL}; }}
QMessageBox QLabel {{ background: transparent; }}
QLabel {{ background: transparent; }}

/* ---------- 关闭遮罩 ---------- */
QWidget#shutdownOverlay {{ background: rgba(10, 12, 15, 200); }}
QLabel#shutdownLabel {{ color: {TEXT}; font-size: {FS_TITLE}px; }}
"""


def apply_theme(app) -> None:
    """把全局 QSS 挂到 QApplication（main.py 启动时调用一次）。"""
    app.setStyleSheet(QSS)
