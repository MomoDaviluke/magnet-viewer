"""全局设计系统：双主题色板（浅色默认 / 深色 / 跟随系统）+ 间距 / 圆角 / 字号 + QSS。

规则（机器门禁 theme_check.py 强制）：
- **本文件之外任何 ui/*.py 不得出现 hex 色值，也不得调 setStyleSheet**；
  需要局部样式时一律换成 setObjectName + 本文件 QSS 规则（动态属性配
  `QLabel#playerTitle[error="true"]` 这类属性选择器，改属性后需调用
  `style().unpolish(w)` + `style().polish(w)` 才生效）。
- 视觉规格：8px 网格（SP_*）、控件圆角 8、卡片/分组框/容器圆角 10、
  正文 14px、次级 13px、标题 16px（旧版 13/12/15 偏小偏密）。
- 主题：``LIGHT``（默认）/ ``DARK`` 两套调色板，运行时 ``apply_theme(app, mode)``
  热切换（mode ∈ light/dark/system）。模块级常量（BG/BG_PANEL/...）**始终等于
  当前激活色板**的取值——既有 `from ui.theme import X` 的取值随切换更新。
- 自绘控件（BufferedSlider / 下载进度委托等）必须**在绘制时**读模块属性
  （``import ui.theme as theme`` 后 ``theme.SLIDER_SEGMENT``），不得
  `from ui.theme import X` 固化导入期快照，否则切主题后颜色不跟随。
"""
from __future__ import annotations

# ---- 调色板（键固定：两套键集完全一致）----
# 固定 17 键：bg / bg_panel / bg_input / bg_hover / bg_selected / border /
#            border_strong / text / text_muted / text_dim / accent /
#            accent_hover / accent_pressed / ok / warn / danger / segment
# 渲染扩展 2 键：alt_row（交替行底色）/ image_bg（图片位图占位底色）
LIGHT: dict = {
    "bg": "#f4f5f7",            # 窗口底
    "bg_panel": "#ffffff",      # 面板 / 卡片
    "bg_input": "#ffffff",      # 输入 / 下拉
    "bg_hover": "#eef0f3",      # 悬停
    "bg_selected": "#e7effd",   # 选中行（比 accent 淡，避免大面积高饱和）
    "border": "#e1e4e9",        # 常规边框
    "border_strong": "#c9cfd8",  # 分组 / 聚焦前边框
    "text": "#1f2328",          # 主文字
    "text_muted": "#5b6472",    # 次级
    "text_dim": "#8a929f",      # 三级（禁用 / 时间戳）
    "accent": "#2563eb",        # 强调（主按钮 / 选中 / 进度）
    "accent_hover": "#3b78f0",
    "accent_pressed": "#1d4fd7",
    "ok": "#1a7f47",            # 完成 / 做种
    "warn": "#9a6700",          # 警告 / 限速
    "danger": "#c0392b",        # 失败
    "segment": (0, 0, 0, 40),   # 缓冲分段（半透明黑）
    "alt_row": "#fafbfc",       # 交替行（须与面板底可辨）
    "image_bg": "#eceef1",      # 图片查看器占位底（浅版）
}
DARK: dict = {
    "bg": "#14161a",
    "bg_panel": "#1b1e24",
    "bg_input": "#22262e",
    "bg_hover": "#2a2f38",
    "bg_selected": "#243043",
    "border": "#2f353f",
    "border_strong": "#3d4450",
    "text": "#e6e9ef",
    "text_muted": "#99a1ae",
    "text_dim": "#6d7683",
    "accent": "#5b9dff",
    "accent_hover": "#7cb2ff",
    "accent_pressed": "#4a86e0",
    "ok": "#4ec27a",
    "warn": "#e0a83c",
    "danger": "#ef6b62",
    "segment": (255, 255, 255, 46),  # 缓冲分段（半透明白）
    "alt_row": "#1f232a",
    "image_bg": "#101216",           # 图片查看器占位底（深版，保持原值）
}

PALETTES: dict = {"light": LIGHT, "dark": DARK}
THEME_MODES: tuple = ("light", "dark", "system")   # apply_theme 的合法值域
DEFAULT_MODE = "light"

# 调色板键 → 模块级常量名（apply_theme 据此同步，两套键集一致）
_CONST_NAMES: dict = {
    "bg": "BG", "bg_panel": "BG_PANEL", "bg_input": "BG_INPUT",
    "bg_hover": "BG_HOVER", "bg_selected": "BG_SELECTED",
    "border": "BORDER", "border_strong": "BORDER_STRONG",
    "text": "TEXT", "text_muted": "TEXT_MUTED", "text_dim": "TEXT_DIM",
    "accent": "ACCENT", "accent_hover": "ACCENT_HOVER",
    "accent_pressed": "ACCENT_PRESSED", "ok": "OK", "warn": "WARN",
    "danger": "DANGER", "segment": "SLIDER_SEGMENT",
}

# ---- 与主题无关的固定值 ----
ON_ACCENT = "#ffffff"           # 强调色上的前景（两套主题均白字）
OVERLAY = "rgba(10, 12, 15, 200)"   # 关窗遮罩（两套主题共用，深浅下都可遮）
VIDEO_BG = "#000000"            # 视频区恒纯黑（不随主题）

# ---- 间距（8px 网格）----
SP_XS, SP_SM, SP_MD, SP_LG, SP_XL = 4, 8, 12, 16, 24
# ---- 圆角：控件 8（R_MD）/ 卡片·分组框·树容器 10（R_LG）----
R_SM, R_MD, R_LG = 6, 8, 10
# ---- 字号：正文 14 / 次级 13 / 标题 16 / 展示 20 ----
FS_CAPTION, FS_BODY, FS_TITLE, FS_DISPLAY = 13, 14, 16, 20
# 数字观感：时长 / 体积用等宽字体，中文回退雅黑
FONT_NUMERIC = '"Consolas", "Microsoft YaHei UI"'


def qss(palette: dict) -> str:
    """按传入色板生成全局 QSS（纯函数；``QSS`` = qss(当前激活色板)）。"""
    p = palette
    return f"""
* {{ outline: none; }}
QWidget {{ background: {p['bg']}; color: {p['text']}; font-size: {FS_BODY}px; }}
QMainWindow, QDialog {{ background: {p['bg']}; }}
QToolTip {{ background: {p['bg_input']}; color: {p['text']};
    border: 1px solid {p['border_strong']}; padding: {SP_XS}px {SP_SM}px;
    border-radius: {R_SM}px; }}

/* ---------- 卡片 / 分区 ---------- */
QFrame#card, QWidget#card {{ background: {p['bg_panel']};
    border: 1px solid {p['border']}; border-radius: {R_LG}px; }}
QLabel#sectionTitle {{ font-size: {FS_TITLE}px; font-weight: 600;
    color: {p['text']}; padding: 0; }}
QLabel#fieldNote, QLabel#hint, QLabel#metaLabel {{
    color: {p['text_muted']}; font-size: {FS_CAPTION}px; }}
/* 时长 / 体积数字：等宽字体对齐观感更整齐 */
QLabel#timeLabel, QLabel#metaLabel {{ font-family: {FONT_NUMERIC}; }}
QLabel#emptyHint {{ color: {p['text_dim']}; font-size: {FS_BODY}px; }}

/* ---------- 顶栏 ---------- */
QWidget#topBar {{ background: {p['bg_panel']};
    border-bottom: 1px solid {p['border']}; }}
QLineEdit#urlInput {{ background: {p['bg_input']}; border: 1px solid {p['border']};
    border-radius: {R_MD}px; padding: 7px 10px; min-height: 30px; }}
QLineEdit#urlInput:focus {{ border: 1px solid {p['accent']}; }}

/* ---------- 按钮（现代化尺寸：28px 内容高 + 9×18 内边距） ---------- */
QPushButton {{ background: {p['bg_input']}; color: {p['text']};
    border: 1px solid {p['border']}; border-radius: {R_MD}px;
    padding: 9px 18px; min-height: 28px; }}
QPushButton:hover {{ background: {p['bg_hover']}; border-color: {p['border_strong']}; }}
QPushButton:pressed {{ background: {p['bg_input']}; }}
QPushButton:disabled {{ color: {p['text_dim']}; background: {p['bg_panel']};
    border-color: {p['border']}; }}
QPushButton#primary {{ background: {p['accent']}; border-color: {p['accent']};
    color: {ON_ACCENT}; font-weight: 600; }}
QPushButton#primary:hover {{ background: {p['accent_hover']}; }}
QPushButton#primary:pressed {{ background: {p['accent_pressed']}; }}
QPushButton#primary:disabled {{ background: {p['bg_input']}; color: {p['text_dim']}; }}
QPushButton#ghost {{ background: transparent; border-color: transparent;
    color: {p['text_muted']}; }}
QPushButton#ghost:hover {{ background: {p['bg_hover']}; color: {p['text']}; }}
QPushButton#playButton {{ background: {p['accent']}; border: none; color: {ON_ACCENT};
    border-radius: 18px; font-weight: 600; padding: 0; min-height: 0; }}

/* ---------- 标签页 ---------- */
QTabWidget::pane {{ border: 1px solid {p['border']}; border-radius: {R_LG}px;
    background: {p['bg_panel']}; top: -1px; padding: {SP_MD}px; }}
QTabBar::tab {{ background: transparent; color: {p['text_muted']};
    padding: 10px 22px; font-size: {FS_BODY}px; margin-right: {SP_XS}px;
    border: 1px solid transparent; border-bottom: none;
    border-top-left-radius: {R_MD}px; border-top-right-radius: {R_MD}px;
    min-width: 72px; }}
QTabBar::tab:selected {{ color: {p['text']}; background: {p['bg_panel']};
    border-color: {p['border']}; border-bottom: 3px solid {p['accent']}; }}
QTabBar::tab:hover:!selected {{ color: {p['text']}; background: {p['bg_hover']}; }}

/* ---------- 输入类 ---------- */
QLineEdit, QPlainTextEdit, QSpinBox, QComboBox {{
    background: {p['bg_input']}; color: {p['text']}; border: 1px solid {p['border']};
    border-radius: {R_MD}px; padding: 7px 10px; min-height: 30px;
    selection-background-color: {p['accent']}; }}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {p['accent']}; }}
QComboBox::drop-down {{ border: none; width: 28px; }}
QComboBox::down-arrow {{ width: 0; height: 0;
    border-left: 5px solid transparent; border-right: 5px solid transparent;
    border-top: 6px solid {p['text_muted']}; margin-right: {SP_SM}px; }}
QComboBox QAbstractItemView {{ background: {p['bg_panel']};
    border: 1px solid {p['border_strong']}; padding: {SP_XS}px;
    selection-background-color: {p['bg_selected']}; color: {p['text']}; }}
QComboBox QAbstractItemView::item {{ min-height: 28px; }}

/* ---------- 滑块 / 进度 ---------- */
QSlider {{ background: transparent; min-height: 22px; }}
QSlider::groove:horizontal {{ border: none; height: 4px; border-radius: 2px;
    background: {p['bg_input']}; }}
QSlider::sub-page:horizontal {{ background: {p['accent']}; border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; height: 12px; margin: -5px 0;
    border-radius: 6px; background: {ON_ACCENT}; }}
QSlider::handle:horizontal:hover {{ background: {p['accent_hover']}; }}
QProgressBar {{ background: {p['bg_input']}; border: none; border-radius: 2px; }}
QProgressBar::chunk {{ background: {p['accent']}; border-radius: 2px; }}
QProgressBar#shutdownBar::chunk {{ background: {p['accent']}; }}

/* ---------- 树 / 列表 / 表 ---------- */
QTreeView, QTreeWidget, QListView, QListWidget, QTableView, QTableWidget {{
    background: {p['bg_panel']}; alternate-background-color: {p['alt_row']};
    border: 1px solid {p['border']}; border-radius: {R_LG}px; padding: {SP_XS}px;
    show-decoration-selected: 1; }}
QTreeView::item, QListView::item {{ min-height: 32px; border-radius: {R_SM}px;
    padding: 2px {SP_XS}px; }}
QTreeView::item:selected, QListView::item:selected {{
    background: {p['bg_selected']}; color: {p['text']}; }}
QTreeView::item:hover, QListView::item:hover {{ background: {p['bg_hover']}; }}
QHeaderView::section {{ background: {p['bg_input']}; color: {p['text_muted']};
    border: none; border-bottom: 1px solid {p['border']};
    padding: 8px 10px; font-size: {FS_CAPTION}px; font-weight: 600; }}
QListView#thumbList {{ background: {p['bg_panel']}; border: 1px solid {p['border']};
    border-radius: {R_LG}px; padding: {SP_SM}px; }}
QListView#thumbList::item {{ border-radius: {R_SM}px; padding: {SP_XS}px; }}
QTreeView#fileTree, QTreeView#taskList {{ border: none; background: transparent;
    padding: 0; }}
QLabel#imageViewer {{ background: {p['image_bg']}; color: {p['text_dim']};
    border: 1px dashed {p['border_strong']}; border-radius: {R_LG}px; }}

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {p['border_strong']};
    border-radius: 5px; min-height: 32px; }}
QScrollBar::handle:vertical:hover {{ background: {p['text_dim']}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {p['border_strong']};
    border-radius: 5px; min-width: 32px; }}
QScrollBar::handle:horizontal:hover {{ background: {p['text_dim']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- 命名控件（B3 逐面板打磨） ---------- */
QLabel#playerTitle {{ color: {p['text_muted']}; padding: {SP_SM}px 2px;
    font-weight: 500; }}
QLabel#playerTitle[error="true"] {{ color: {p['danger']}; font-weight: 600; }}
QWidget#statusBar {{ background: {p['bg_panel']};
    border-top: 1px solid {p['border']}; }}
QLabel#statusState {{ color: {p['text']}; font-size: {FS_CAPTION}px; }}

/* ---------- 其他 ---------- */
QGroupBox {{ border: 1px solid {p['border']}; border-radius: {R_LG}px;
    margin-top: 12px; padding-top: 10px; background: {p['bg_panel']}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: {SP_MD}px;
    padding: 0 {SP_XS}px; color: {p['text_muted']}; }}
QCheckBox, QRadioButton {{ spacing: {SP_SM}px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; }}
QVideoWidget {{ background: {VIDEO_BG}; }}
QMessageBox {{ background: {p['bg_panel']}; }}
QMessageBox QLabel {{ background: transparent; }}
QLabel {{ background: transparent; }}

/* ---------- 关闭遮罩 ---------- */
QWidget#shutdownOverlay {{ background: {OVERLAY}; }}
QLabel#shutdownLabel {{ color: {p['text']}; font-size: {FS_TITLE}px; }}
"""


# ---- 当前激活色板（模块级常量 = 既有导入面；由 apply_theme 同步）----
BG = LIGHT["bg"]
BG_PANEL = LIGHT["bg_panel"]
BG_INPUT = LIGHT["bg_input"]
BG_HOVER = LIGHT["bg_hover"]
BG_SELECTED = LIGHT["bg_selected"]
BORDER = LIGHT["border"]
BORDER_STRONG = LIGHT["border_strong"]
TEXT = LIGHT["text"]
TEXT_MUTED = LIGHT["text_muted"]
TEXT_DIM = LIGHT["text_dim"]
ACCENT = LIGHT["accent"]
ACCENT_HOVER = LIGHT["accent_hover"]
ACCENT_PRESSED = LIGHT["accent_pressed"]
OK = LIGHT["ok"]
WARN = LIGHT["warn"]
DANGER = LIGHT["danger"]
SLIDER_SEGMENT = LIGHT["segment"]

_ACTIVE_MODE = DEFAULT_MODE
QSS = qss(LIGHT)


def system_mode() -> str:
    """跟随系统：Qt6 ``styleHints().colorScheme()`` 判深浅；拿不到回退 light。"""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        hints = QGuiApplication.styleHints()
        scheme = hints.colorScheme() if hints is not None else None
        dark = getattr(Qt.ColorScheme, "Dark", None)
        light = getattr(Qt.ColorScheme, "Light", None)
        if dark is not None and scheme == dark:
            return "dark"
        if light is not None and scheme == light:
            return "light"
    except Exception:      # noqa: BLE001 —— 平台不支持/无 GUI：显式回退
        pass
    return "light"


def resolve_mode(mode: str) -> str:
    """把 "light"/"dark"/"system"（含存量非法值）解析为实际生效的 light/dark。"""
    m = str(mode or DEFAULT_MODE).strip().lower()
    if m not in THEME_MODES:
        m = DEFAULT_MODE      # 存量非法值/空值：回退默认浅色，绝不抛错
    return system_mode() if m == "system" else m


def active_mode() -> str:
    """当前生效主题名（light / dark；system 已解析）。"""
    return _ACTIVE_MODE


def current_palette() -> dict:
    """当前激活色板（apply_theme 同步过的那一套）。"""
    return PALETTES[_ACTIVE_MODE]


def _repaint_open_widgets(app) -> None:
    """QSS 重建后让**已存在**部件重画。

    自绘控件（BufferedSlider 的分段、下载进度委托）在 paintEvent 里读
    ``ui.theme`` 模块属性：QSS 重挂会自动 re-polish，但这些部件需要一次
    update() 才会用新颜色重绘。遍历顶层窗口及其子部件（数量级为百，切换
    是低频人工操作，代价可忽略）；异常一律吞掉——重绘失败不能影响切主题。
    """
    try:
        from PySide6.QtWidgets import QWidget
        for top in app.topLevelWidgets():
            top.update()
            for w in top.findChildren(QWidget):
                w.update()
    except Exception:      # noqa: BLE001
        pass


def apply_theme(app, mode: str = DEFAULT_MODE) -> str:
    """设置界面主题（启动时一次 / 设置保存时热切换）。

    ``mode`` ∈ ``light`` / ``dark`` / ``system``（非法值回退 light）。
    效果：① 重建 QSS 并挂到 QApplication（Qt 自动 re-polish 全部部件）；
    ② 同步本模块的常量（``BG/ACCENT/...``），使绘制期读模块属性的自绘控件
    跟随；③ 对已存在部件发一次 update()（自绘颜色快照刷新）。
    返回实际生效的主题名（``system`` 已解析为 light/dark）。
    """
    global QSS, _ACTIVE_MODE
    name = resolve_mode(mode)
    pal = PALETTES[name]
    _ACTIVE_MODE = name
    g = globals()
    for key, const in _CONST_NAMES.items():
        g[const] = pal[key]
    QSS = qss(pal)
    if app is not None:
        app.setStyleSheet(QSS)
        _repaint_open_widgets(app)
    return name
