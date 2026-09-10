"""Qt 样式代理：**只**自绘复选框/单选指示器（箭头/分区已交还 QSS，见下）。

为什么还需要本文件
------------------
改造前下拉箭头用 CSS 三角 hack（``width:0;height:0;border-* transparent``）——
Qt 样式表引擎**不支持**这套画法（它把 border 当真实边框画 → 箭头成灰方块）。
上一轮改为 ``QProxyStyle`` 自绘细雪佛龙 + 右侧分区，几何自己算，
结果**算错位**：分区只画出右上角一小块、箭头不垂直居中、微调框上下半之间的
1px 分隔线横穿到输入框外（用户放大截图实证）。

本轮定案：**箭头与分区全部交回 QSS 子控件**（``::drop-down`` / ``::up-button``
/ ``::down-button`` / ``::down-arrow`` / ``::up-arrow``，见 ``ui/theme.py`` 的
``qss()``）。几何由样式引擎按控件矩形计算，不会错位；箭头用 ``image:`` 指向
``ui/assets`` 的 SVG 雪佛龙（Qt 原生支持 ``image``）。本文件里的箭头/分区绘制
代码因此**全部删除**。

保留的那部分
------------
**复选框 / 单选指示器**（用户已认可：蓝底白勾）。它不能走 QSS：只要 QSS 里出现
``QCheckBox::indicator`` 规则，``QStyleSheetStyle`` 就不再转发
``PE_IndicatorCheckBox`` / ``PE_IndicatorRadioButton`` 给基样式，指示器整块消失
（且 Qt 默认指示器用调色板高亮色，不随 ``ui.theme`` 走）。所以在绘制层自绘
16×16 圆角方框：未选 = 输入底 + ``border_strong`` 描边（悬停描边转 accent），
选中 = accent 实底 + 白色对勾，禁用 = 灰底灰勾。

配色
----
所有颜色**在绘制时**读 ``ui.theme`` 模块属性 / ``theme.current_palette()``，
与项目「自绘控件不得固化导入期色值快照」的约定一致——热切换主题后立即跟随，
**无需重建 style**。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QProxyStyle, QStyle

import ui.theme as theme      # 颜色在绘制时现读（热切换跟随）

# ---- 复选框/单选指示器规格 ----
INDICATOR_SIZE = 16         # 目标视觉外接尺寸（16×16）
INDICATOR_RADIUS = 4
INDICATOR_STROKE = 1.4      # 描边宽度（1.4 -> 反锯齿后仍是清晰的 1px 观感）

# 指示器自绘元素（唯一被本样式拦截的 primitive）
_INDICATOR_CHECK = frozenset((QStyle.PE_IndicatorCheckBox,
                              QStyle.PE_IndicatorRadioButton))


def _enabled(option) -> bool:
    return bool(getattr(option, "state", QStyle.State_None) & QStyle.State_Enabled)


def _hovered(option) -> bool:
    return bool(getattr(option, "state", QStyle.State_None) & QStyle.State_MouseOver)


class ChevronStyle(QProxyStyle):
    """只拦截复选框/单选指示器 primitive；其余一律转发给基样式。

    类名保留历史名（``main.py`` / ``ui_shot.py`` / 测试按此名安装）——
    本样式**不再**绘制任何箭头或分区，那些由 QSS 子控件承担。
    """

    def __init__(self, base_style=None):
        super().__init__(base_style)

    # 供测试/门禁断言的绘制计数（证明代理真的被调用，而不是被 QSS 短路）
    indicator_calls: int = 0

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        if element in _INDICATOR_CHECK and painter is not None:
            rect = getattr(option, "rect", None)
            if rect is not None and rect.width() > 0 and rect.height() > 0:
                self._paint_indicator(element, rect, option, painter, widget)
                type(self).indicator_calls += 1
                return
        super().drawPrimitive(element, option, painter, widget)

    # ---------- 复选框 / 单选 ----------

    def subElementRect(self, element, option, widget=None):  # noqa: N802
        """把指示器矩形统一成 16×16（基样式默认 14，勾选框显得小气）。

        只改指示器自身尺寸（保持左上角），文本位置由基样式按新矩形重排；
        非指示器元素一律原样转发。
        """
        rect = super().subElementRect(element, option, widget)
        if element in (QStyle.SE_CheckBoxIndicator,
                       QStyle.SE_RadioButtonIndicator) and rect.isValid():
            rect.setWidth(INDICATOR_SIZE)
            rect.setHeight(INDICATOR_SIZE)
        return rect

    @staticmethod
    def _paint_indicator(element, rect, option, painter, widget=None) -> None:
        """16×16 自绘指示器：颜色**绘制时**现读色板（热切主题跟随）。

        尺寸固定 16×16（保持基样式给的左上角）：基样式（含 QStyleSheetStyle）
        给出的指示器矩形是 14×14，直接用它画的勾选框偏小。15×15 内框 + 1.4px
        描边 → 视觉外接 ≈16×16，与 QSS `spacing` 留出的间距不冲突。
        """
        pal = theme.current_palette()
        enabled = _enabled(option)
        checked = bool(getattr(option, "state", QStyle.State_None) & QStyle.State_On)
        box = QRect(rect.x(), rect.y(), INDICATOR_SIZE, INDICATOR_SIZE)
        if widget is not None and box.bottom() > widget.height() - 1:
            box.moveBottom(widget.height() - 1)      # 别越出控件下沿
        if enabled:
            fill = QColor(pal["accent"]) if checked else QColor(pal["bg_input"])
            stroke = QColor(pal["accent"]) if checked else QColor(pal["border_strong"])
            if checked:
                stroke = QColor(pal["accent"])
            elif _hovered(option):
                stroke = QColor(pal["accent"])       # 悬停：描边转 accent
            mark = QColor(theme.ON_ACCENT)
        else:
            fill = QColor(pal["bg_hover"]) if checked else QColor(pal["bg_input"])
            stroke = QColor(pal["border"])
            mark = QColor(pal["text_dim"])

        radius = float(INDICATOR_RADIUS if
                       element == QStyle.PE_IndicatorCheckBox
                       else box.width() / 2.0)
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(stroke)
            pen.setWidthF(INDICATOR_STROKE)
            painter.setPen(pen)
            painter.setBrush(fill)
            inner = QRect(box.x(), box.y(), box.width() - 1, box.height() - 1)
            if element == QStyle.PE_IndicatorCheckBox:
                painter.drawRoundedRect(inner, radius, radius)
            else:
                painter.drawEllipse(inner)
            if checked:
                ChevronStyle._paint_check(box, mark, painter)
        finally:
            painter.restore()

    @staticmethod
    def _paint_check(box: QRect, color: QColor, painter) -> None:
        """对勾（两条圆头线；坐标按 16×16 指示器比例缩放）。"""
        scale = float(box.width()) / float(INDICATOR_SIZE)
        x0 = box.x() + 3.6 * scale
        y0 = box.y() + 8.3 * scale
        x1 = box.x() + 6.7 * scale
        y1 = box.y() + 11.4 * scale
        x2 = box.x() + 12.4 * scale
        y2 = box.y() + 4.6 * scale
        pen = QPen(color)
        pen.setWidthF(1.8 * scale)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        path = QPainterPath(QPointF(x0, y0))
        path.lineTo(QPointF(x1, y1))
        path.lineTo(QPointF(x2, y2))
        painter.drawPath(path)


def install(app):
    """把 ``ChevronStyle`` 装到 QApplication（幂等）。

    必须在 ``ui.theme.apply_theme`` **之前** 调用：之后 QSS 会包成
    QStyleSheetStyle 代理，那时本样式才是它转发绘制时的基样式。
    重复调用不重建（避免样式层层包裹）。无 app 时返回 None。
    """
    if app is None:
        return None
    cur = app.style()
    if isinstance(cur, ChevronStyle):
        return cur
    style = ChevronStyle(cur)
    app.setStyle(style)
    return style
