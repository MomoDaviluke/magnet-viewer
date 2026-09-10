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
- 下拉框/微调框右侧「分区 + 箭头」**全部交给 QSS 子控件**（``::drop-down`` /
  ``::up-button`` / ``::down-button`` + ``image: url(<SVG>)``）：几何由样式引擎
  计算，不会像自绘那样算错位。箭头资源在 ``ui/assets/``，路径由
  :func:`asset_path` 解析（兼容 PyInstaller 的 ``sys._MEIPASS``）。
"""
from __future__ import annotations

import os
import sys

from core.logutil import log_warning

# ---- 调色板（键固定：两套键集完全一致）----
# 固定 17 键：bg / bg_panel / bg_input / bg_hover / bg_selected / border /
#            border_strong / text / text_muted / text_dim / accent /
#            accent_hover / accent_pressed / ok / warn / danger / segment
# 渲染扩展 4 键：alt_row（交替行底色）/ image_bg（图片位图占位底色）/
#            bg_pressed（按钮按下底：比 hover 深一档，做出"按下去"的层次）/
#            bg_compartment（下拉框·微调框**右侧分区**底：浅色 = hover 档、
#            深色 = 比面板亮一档；由 ui/theme.py 的 QSS 子控件规则直接引用，
#            见 `QComboBox::drop-down` / `QSpinBox::up-button`）
LIGHT: dict = {
    "bg": "#f4f5f7",            # 窗口底
    "bg_panel": "#ffffff",      # 面板 / 卡片
    "bg_input": "#ffffff",      # 输入 / 下拉
    "bg_hover": "#eef0f3",      # 悬停
    "bg_pressed": "#e6e9ee",    # 按下（比 hover 深一档）
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
    "alt_row": "#f3f5f8",       # 交替行（须与面板底可辨；旧 #fafbfc 对比过弱）
    "image_bg": "#eceef1",      # 图片查看器占位底（浅版）
    "bg_compartment": "#eef0f3",   # 下拉/微调右侧分区底（浅：与 hover 同档）
}
DARK: dict = {
    "bg": "#14161a",
    "bg_panel": "#1b1e24",
    "bg_input": "#22262e",
    "bg_hover": "#2a2f38",
    "bg_pressed": "#262b33",
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
    "alt_row": "#232833",            # 交替行（旧 #1f232a 与面板底几乎同色）
    "image_bg": "#101216",           # 图片查看器占位底（深版，保持原值）
    "bg_compartment": "#2b313b",     # 右侧分区底（深：比面板 #1b1e24 亮一档）
}

PALETTES: dict = {"light": LIGHT, "dark": DARK}
THEME_MODES: tuple = ("light", "dark", "system")   # apply_theme 的合法值域
DEFAULT_MODE = "light"

# 调色板键 → 模块级常量名（apply_theme 据此同步，两套键集一致）
_CONST_NAMES: dict = {
    "bg": "BG", "bg_panel": "BG_PANEL", "bg_input": "BG_INPUT",
    "bg_hover": "BG_HOVER", "bg_pressed": "BG_PRESSED",
    "bg_selected": "BG_SELECTED",
    "border": "BORDER", "border_strong": "BORDER_STRONG",
    "text": "TEXT", "text_muted": "TEXT_MUTED", "text_dim": "TEXT_DIM",
    "accent": "ACCENT", "accent_hover": "ACCENT_HOVER",
    "accent_pressed": "ACCENT_PRESSED", "ok": "OK", "warn": "WARN",
    "danger": "DANGER", "segment": "SLIDER_SEGMENT",
    "bg_compartment": "BG_COMPARTMENT",
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


# ---- 输入类控件的**总高**：下拉分区的显式高度必须等于它 ----
# Qt 样式表**不支持百分比长度**（实测 `height: 100%` 被解析成 **100px**、
# `height: 50%` → 50px —— `%` 被当 px 吃掉），所以分区高度只能写绝对像素，
# 而绝对像素必须等于「控件总高」才铺得满。为让这个常量恒成立，把控件的
# **内容高**用 min/max-height 钉死，总高随即确定（= 内容高 + 上下 padding +
# 上下 border）。实测（离屏 Fusion / windows11 / windowsvista 三套样式同值，
# 见 gui_feature_test 的「声明高度 == 实测高度」断言）：
#     独立控件（padding 7、border 1）：内容 30 → 总高 46
#     分组框内（padding 5、border 1）：内容 26 → 总高 38
# 微调框与下拉框用**同一组内容高**（min == max 钉死）：总高因此完全相同
# （独立 46 / 分组框 38），且都是**偶数** —— 基样式把微调的 up/down 按控件高
# 对半分，总高为奇数时中间会空出 1px（露出输入底色）→ 分隔线下方多一条浅线。
# 实测（min == max 时总高 = 内容高 + 上下 padding + 上下 border，算术精确）：
#     独立微调：内容 30 → 总高 46（偶，无缝）；29 → 45（奇，缝在 y=22）
#     分组框微调：内容 26 → 总高 38（偶，无缝）；25 → 37（奇，缝在 y=18）
# ⇒ 内容高必须取**偶数**。
FIELD_CONTENT_H = 30        # 独立输入类内容高（min == max，微调/下拉同高 46）
FIELD_PAD_V = 7             # 独立输入类上下 padding
FIELD_H = FIELD_CONTENT_H + 2 * FIELD_PAD_V + 2           # 46（控件总高 = 分区高）
FIELD_CONTENT_H_GB = 26     # QGroupBox 内内容高（更紧凑，总高 38）
FIELD_PAD_V_GB = 5
FIELD_H_GB = FIELD_CONTENT_H_GB + 2 * FIELD_PAD_V_GB + 2  # 38（控件总高 = 分区高）

# ---- 箭头资源（ui/assets/*.svg；12×12 视窗雪佛龙，描边色 = 对应主题 text_muted）----
# 浅色描边 #5b6472 = LIGHT["text_muted"]，深色 #99a1ae = DARK["text_muted"]。
ASSET_CHEVRON_DOWN = "chevron-down-{tag}.svg"
ASSET_CHEVRON_UP = "chevron-up-{tag}.svg"
# 打包自检用：spec 的 datas 必须包含这 4 个（两份 spec 同步）
ASSET_FILES: tuple = ("chevron-down-light.svg", "chevron-up-light.svg",
                      "chevron-down-dark.svg", "chevron-up-dark.svg")


def asset_path(name: str) -> str:
    """解析 ``ui/assets/<name>`` 的绝对路径（兼容 PyInstaller 冻结包）。

    - 冻结（``sys._MEIPASS`` 存在）：``<_MEIPASS>/ui/assets/<name>``
      （onefile 解包目录 / onedir 的 ``_internal``）；
    - 源码运行：本文件同级的 ``assets/<name>``。

    文件不存在时 ``log_warning``——**打包后自检靠它**：资源漏进 datas 会
    在这里留下 WARNING，而不是静默画不出箭头（静默失败是历史教训，
    见 core/logutil.py 的模块说明）。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        path = os.path.join(meipass, "ui", "assets", name)
    else:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "assets", name)
    if not os.path.isfile(path):
        log_warning("ui.theme.asset_path", f"箭头资源缺失：{name} -> {path}")
    return path


def asset_url(name: str) -> str:
    """``image: url()`` 用的路径串：正斜杠 + 双引号包裹（QSS 不接受反斜杠）。"""
    return 'url("%s")' % asset_path(name).replace("\\", "/")


def _asset_tag(palette: dict) -> str:
    """色板 → 资源后缀（light / dark）。qss() 只会拿到 LIGHT/DARK 本体。"""
    return "dark" if palette is DARK else "light"


def qss(palette: dict) -> str:
    """按传入色板生成全局 QSS（纯函数；``QSS`` = qss(当前激活色板)）。"""
    p = palette
    tag = _asset_tag(palette)
    chev_down = asset_url(ASSET_CHEVRON_DOWN.format(tag=tag))
    chev_up = asset_url(ASSET_CHEVRON_UP.format(tag=tag))
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

/* ---------- 按钮（平滑：圆角 8 + 柔和边框 + hover/pressed 色阶，无渐变） ---------- */
QPushButton {{ background: {p['bg_input']}; color: {p['text']};
    border: 1px solid {p['border']}; border-radius: {R_MD}px;
    padding: 9px 18px; min-height: 28px; }}
QPushButton:hover {{ background: {p['bg_hover']}; border-color: {p['border_strong']}; }}
QPushButton:pressed {{ background: {p['bg_pressed']}; border-color: {p['border_strong']}; }}
QPushButton:focus {{ border: 1px solid {p['accent']}; }}
QPushButton:disabled {{ color: {p['text_dim']}; background: {p['bg_panel']};
    border-color: {p['border']}; }}
QPushButton#primary {{ background: {p['accent']}; border: none;
    color: {ON_ACCENT}; font-weight: 600; border-radius: {R_MD}px; }}
QPushButton#primary:hover {{ background: {p['accent_hover']}; }}
QPushButton#primary:pressed {{ background: {p['accent_pressed']}; }}
QPushButton#primary:disabled {{ background: {p['bg_hover']}; color: {p['text_dim']}; }}
QPushButton#ghost {{ background: transparent; border: none;
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
    border-radius: {R_MD}px; padding: {FIELD_PAD_V}px 10px; min-height: {FIELD_CONTENT_H}px;
    selection-background-color: {p['accent']}; }}
/* 把输入类**总高钉死**（min == max）：下拉分区的显式高度是绝对像素
   （= 控件总高，见文件上方 FIELD_* 常量），控件高度必须可预测；内容高取
   **偶数**，总高才是偶数 —— 微调的 up/down 由基样式按控件高对半分，奇数
   总高会在中间留 1px 接缝。微调与下拉同高（46），观感统一。 */
QComboBox {{ max-height: {FIELD_CONTENT_H}px; }}
QSpinBox {{ min-height: {FIELD_CONTENT_H}px; max-height: {FIELD_CONTENT_H}px; }}
QLineEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 1px solid {p['accent']}; }}
QComboBox QAbstractItemView {{ background: {p['bg_panel']};
    border: 1px solid {p['border_strong']}; padding: {SP_XS}px;
    selection-background-color: {p['bg_selected']}; color: {p['text']}; }}
QComboBox QAbstractItemView::item {{ min-height: 28px; }}

/* ---------- 下拉 / 微调右侧「按钮」区：QSS 子控件 + SVG 箭头 ----------
   上一轮用 QProxyStyle 自算几何画分区/箭头，实测算错位（分区只盖住右上角一角、
   箭头不垂直居中、微调上下半之间的分隔线跑到框外）。本轮**废弃自绘几何**，
   改回 Qt 标准做法：分区（`::drop-down` / `::up-button` / `::down-button`）
   与箭头（`::down-arrow` / `::up-arrow`）全部由 QSS 声明，几何由样式引擎按
   控件矩形计算 → 不会错位；箭头用 `image:` 指向 ui/assets 的 SVG 雪佛龙
   （Qt 支持 image，不支持 CSS 三角 border hack——那才是更早那版变灰方块的根因）。

   规格统一（下拉与微调**同宽 / 同底 / 同圆角 / 同箭头**）：
     · 分区宽 26px、subcontrol-origin: border、贴右上（微调下半贴右下）
     · **下拉分区的 height 必须显式写死 = 控件总高**（46 / 分组框内 38）：
       上一版只写 width 不写 height，把分区高度交给 QStyleSheetStyle 的默认
       子控件矩形——那是「锚在右上角的小块」观感的来源（高度没铺满、箭头随之
       偏上）。Qt 不认百分比，所以高度写成绝对像素，并用 min == max 把控件
       总高钉死（见文件上方 FIELD_* 常量），杜绝漂移。
     · 底色 bg_compartment；hover 转 bg_hover
     · 分隔线（微调的 up 底边 / 下拉的左边）1px 实色 border
     · 圆角与输入框同档（R_MD=8）：下拉右侧两角、微调上半右上角/下半右下角
     · 箭头**两处同为 12×12**（一致性：两张 PNG 截图里下拉与微调的箭头包围盒
       必须逐像素等大；上一轮 12/10 混用本身就"两套样子不一致"）
   **微调上下按钮不写百分比高度**：Qt 样式表不认百分比长度（实测「百分比 50%」
   被当成 50px → up 高 51px、down 从 y=-1 起，分隔线被推出控件外——正是要
   消灭的那类错位）。省掉高度交给基样式按控件矩形对半分，上下严丝合缝。 */
QComboBox::drop-down {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 26px; height: {FIELD_H}px; background: {p['bg_compartment']};
    border-left: 1px solid {p['border']};
    border-top-right-radius: {R_MD}px; border-bottom-right-radius: {R_MD}px; }}
/* 分组框内输入类更矮（总高 38）：分区高度同步，否则分区会比控件矮 */
QGroupBox QComboBox::drop-down {{ height: {FIELD_H_GB}px; }}
QComboBox::drop-down:hover {{ background: {p['bg_hover']}; }}
QComboBox::down-arrow {{ image: {chev_down}; width: 12px; height: 12px; }}
QSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
    width: 26px; background: {p['bg_compartment']};
    border-left: 1px solid {p['border']}; border-bottom: 1px solid {p['border']};
    border-top-right-radius: {R_MD}px; }}
QSpinBox::up-button:hover {{ background: {p['bg_hover']}; }}
QSpinBox::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right;
    width: 26px; background: {p['bg_compartment']};
    border-left: 1px solid {p['border']};
    border-bottom-right-radius: {R_MD}px; }}
QSpinBox::down-button:hover {{ background: {p['bg_hover']}; }}
QSpinBox::up-arrow {{ image: {chev_up}; width: 12px; height: 12px; }}
QSpinBox::down-arrow {{ image: {chev_down}; width: 12px; height: 12px; }}

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
    margin-top: 10px; padding-top: 8px; background: {p['bg_panel']}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: {SP_MD}px;
    padding: 0 {SP_XS}px; color: {p['text_muted']}; font-weight: 600; }}
/* 分组框内的表单行更紧凑（设置面板 19 行，每行省 8px ≈ 省下 150px 总高；
   仅 QGroupBox 后代命中，主窗口控件不受影响） */
QGroupBox QLineEdit, QGroupBox QSpinBox, QGroupBox QComboBox {{
    padding: {FIELD_PAD_V_GB}px 9px; min-height: {FIELD_CONTENT_H_GB}px; }}
/* 分组框内同样把总高**钉死**：下拉与微调都是 38（= 分区高度常量 FIELD_H_GB）；
   内容高取**偶数**（26）→ 总高偶数 → 基样式对半分不留 1px 接缝。
   实测：内容 26 → 总高 38（偶，无缝）/ 25 → 37（奇，缝在 y=18）。 */
QGroupBox QComboBox {{ max-height: {FIELD_CONTENT_H_GB}px; }}
QGroupBox QSpinBox {{ min-height: {FIELD_CONTENT_H_GB}px;
    max-height: {FIELD_CONTENT_H_GB}px; }}
QGroupBox QPushButton {{ padding: 5px 12px; min-height: 26px; }}
/* 复选框/单选：**必须显式 background: transparent**。全局 `QWidget {{ background:
   BG }}` 会把复选框整行涂成窗口灰底（浅色 {p['bg']}）贴在白卡片上 → 一条灰带
   （用户反馈"设置面板复选框行灰带"的根因：QLabel 早就单独声明了 transparent，
    QCheckBox/QRadioButton 漏了）。
   注意：**不为复选框/单选写 ::indicator 子控件规则**——一旦出现该规则，
   QStyleSheetStyle 就不再转发 PE_IndicatorCheckBox/RadioButton 给基样式
   （实测：加了 width/height 甚至 background:transparent 都会让勾选框整块消失），
   指示器改由 ui/style.py 自绘（16×16 圆角方框 + accent 实底 + 白对勾，
   颜色绘制时现读色板 → 热切主题跟随）。箭头**不**走这条路：箭头用
   `image:` 是 QSS 原生支持的能力（上面 ::drop-down 一组规则），无需自绘。 */
QCheckBox, QRadioButton {{ background: transparent; border: none;
    spacing: {SP_SM}px; min-height: 26px; }}
/* 禁用态文案转三级灰：复选框的**指示器**由 ui/style.py 自绘为灰底灰勾，
   但文字颜色 QSS 不管的话仍是正文色 → 看起来像还能点（设置面板"直连"档
   会一次禁用 5 个控件）。 */
QCheckBox:disabled, QRadioButton:disabled {{ color: {p['text_dim']}; }}
/* 表单里的"行容器"（路径输入框 + 浏览… 按钮）：同样是普通 QWidget，会被全局
   QWidget 规则涂成窗口灰底 → 统一命名 #formRow 后显式透明。
   不用 `QGroupBox QWidget` 兜底：后代选择器在 Qt QSS 里特异性高于 `QLineEdit`
   /`QComboBox`，会把输入控件自己的底也刷成透明（误伤）。 */
QWidget#formRow {{ background: transparent; }}
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
BG_PRESSED = LIGHT["bg_pressed"]
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
BG_COMPARTMENT = LIGHT["bg_compartment"]

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
