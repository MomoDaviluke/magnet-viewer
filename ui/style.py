"""Qt 样式代理：自绘「细雪佛龙」箭头 + 右侧分区 + 复选框/单选指示器。

为什么需要本文件
----------------
改造前 `ui/theme.py` 的下拉箭头用的是 CSS 三角 hack
（``width:0;height:0;border-left/right: transparent;border-top: <实色>``）。
**Qt 的样式表引擎不支持这套画法**：它把 border 当成真实边框画出来，
于是箭头渲染成一个灰方块（用户截图实证）。Qt 样式表对箭头只认
``image:``（图/ SVG）或交给「基样式」去画——所以正确做法是回到
QStyle 绘制层自绘。

同一批打磨还修了两件事，都在这里收口：

1. **右侧分区**（用户反馈「微调框/下拉框右边的分区老气、两套风格不统一」）。
   分区**不能**用 QSS 画：只要 QSS 里出现 ``::drop-down`` / ``::down-arrow`` /
   ``::up-button`` / ``::down-button`` 任一子控件规则，``QStyleSheetStyle`` 就
   不再把箭头 primitive 转发给基样式（实测调用计数 0，箭头成方块或整根消失）。
   所以整个分区（圆角底 + 中间 1px 分隔 + 内缩的雪佛龙）都在本文件的
   ``drawPrimitive`` 里用 QPainter 画出来：下拉框一整块、微调框上/下两半。
   分区的边界**从 ``widget.rect()`` 推**（不是 ``option.rect``——后者只是箭头
   字形的小矩形），保证下拉/微调分区同宽、同底、同箭头尺寸。

2. **复选框/单选指示器**（用户反馈「选中色不跟主题」）。Qt 默认指示器用的是
   Qt 调色板高亮色（不随 ``ui.theme`` 走），这里改成自绘 16×16 圆角方框：
   未选 = 输入底 + ``border_strong`` 描边，悬停描边转 accent，选中 = accent 实底
   + 白色对勾，禁用 = 灰底灰勾。

配色
----
所有颜色**在绘制时**读 ``ui.theme`` 模块属性 / ``theme.current_palette()``，
与项目「自绘控件不得固化导入期色值快照」的约定一致——热切换主题后立即跟随，
**无需重建 style**。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractSpinBox, QComboBox, QProxyStyle, QStyle)

import ui.theme as theme      # 颜色在绘制时现读（热切换跟随）

# ---- 雪佛龙几何（逻辑像素；与 ui/theme.py 的 8px 网格/字号体系相称）----
CHEVRON_W = 4.5         # 左右端点间距的一半 -> 总宽
CHEVRON_H = 2.6         # 顶点到底边的垂直距离的一半 -> 总高
CHEVRON_STROKE = 1.6    # 描边宽度（细线，避免"大块箭头"的粗糙感）

# ---- 右侧分区规格（下拉框与微调框**统一**：同宽 / 同底 / 同箭头）----
COMPARTMENT_W = 24          # 分区宽度（≈26px 档；8px 网格上的紧凑值）
COMPARTMENT_INSET = 1       # 距控件外框内缩（外框 1px，分区不压边框）
COMPARTMENT_DIVIDER = 1     # 微调框上下半之间的分隔线厚度

# ---- 复选框/单选指示器规格 ----
INDICATOR_SIZE = 16         # = QSS 里 QCheckBox::indicator 的 width/height
INDICATOR_RADIUS = 4
INDICATOR_STROKE = 1.4      # 描边宽度（1.4 -> 反锯齿后仍是清晰的 1px 观感）

_DIR = {
    QStyle.PE_IndicatorArrowDown: "down",
    QStyle.PE_IndicatorArrowUp: "up",
    QStyle.PE_IndicatorArrowLeft: "left",
    QStyle.PE_IndicatorArrowRight: "right",
    QStyle.PE_IndicatorSpinDown: "down",
    QStyle.PE_IndicatorSpinUp: "up",
}
_CHEVRON_ELEMENTS = frozenset(_DIR)

# 需要连同分区一起画的自绘元素 → (半区, 雪佛龙方向)；半区 None = 整块（下拉框）
_COMPARTMENT = {
    QStyle.PE_IndicatorArrowDown: (None, "down"),
    QStyle.PE_IndicatorSpinUp: ("top", "up"),
    QStyle.PE_IndicatorSpinDown: ("bottom", "down"),
}
# 指示器自绘元素
_INDICATOR_CHECK = frozenset((QStyle.PE_IndicatorCheckBox,
                              QStyle.PE_IndicatorRadioButton))


def _field_widget(widget) -> bool:
    """只有下拉框/微调框才画分区（QHeaderView 排序箭头等仍走原雪佛龙）。"""
    return isinstance(widget, (QComboBox, QAbstractSpinBox))


def _enabled(option) -> bool:
    return bool(getattr(option, "state", QStyle.State_None) & QStyle.State_Enabled)


def _hovered(option) -> bool:
    return bool(getattr(option, "state", QStyle.State_None) & QStyle.State_MouseOver)


def _compartment_rect(widget) -> QRect:
    """分区矩形（widget 自身坐标系）：贴右、上下留 1px、宽度统一。"""
    inner = widget.rect().adjusted(COMPARTMENT_INSET, COMPARTMENT_INSET,
                                   -COMPARTMENT_INSET, -COMPARTMENT_INSET)
    width = max(1, min(COMPARTMENT_W, inner.width()))
    return QRect(inner.right() - width + 1, inner.top(), width, inner.height())


def _half_rect(comp: QRect, half: str) -> QRect:
    """微调框分区的上半 / 下半（中间留出分隔线的行）。"""
    h = comp.height() // 2
    if half == "top":
        return QRect(comp.x(), comp.y(), comp.width(), h)
    return QRect(comp.x(), comp.y() + h, comp.width(), comp.height() - h)


class ChevronStyle(QProxyStyle):
    """自绘箭头/分区/复选框指示器；其余 primitive 一律转发给基样式。"""

    def __init__(self, base_style=None):
        super().__init__(base_style)

    # 供测试/门禁断言的绘制计数（证明 proxy 真的被调用，而不是被 QSS 短路）
    draw_calls: int = 0
    compartment_calls: int = 0
    indicator_calls: int = 0

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        spec = _COMPARTMENT.get(element)
        if spec is not None and painter is not None and _field_widget(widget):
            half, direction = spec
            rect = _compartment_rect(widget)
            if half is None:
                self._paint_corner_cleanup(rect, widget, painter)
                self._paint_compartment(rect, painter, top=True, bottom=True)
                center = rect.center()
            else:
                part = _half_rect(rect, half)
                self._paint_corner_cleanup(rect, widget, painter)
                self._paint_compartment(part, painter,
                                        top=(half == "top"),
                                        bottom=(half == "bottom"))
                self._paint_divider(rect, painter)
                center = part.center()
            self._draw_chevron(direction,
                               QPointF(center.x() + 0.5, center.y() + 0.5),
                               painter, _enabled(option))
            type(self).compartment_calls += 1
            type(self).draw_calls += 1
            return

        if element in _INDICATOR_CHECK and painter is not None:
            rect = getattr(option, "rect", None)
            if rect is not None and rect.width() > 0 and rect.height() > 0:
                self._paint_indicator(element, rect, option, painter, widget)
                type(self).indicator_calls += 1
                return

        direction = _DIR.get(element)
        if direction is not None and self._chevron(direction, option, painter):
            type(self).draw_calls += 1
            return
        super().drawPrimitive(element, option, painter, widget)

    # ---------- 分区 ----------

    @staticmethod
    def _paint_corner_cleanup(comp: QRect, widget, painter) -> None:
        """擦掉圆角外的旧版 3D bevel 残留。

        根因：基样式/QSS 引擎会在输入类控件内侧画一圈**方角**的立体明暗框
        （一道亮灰 + 一道暗灰，Windows 传统"凹槽"观感——用户反馈的"老气"）。
        圆角边框只裁掉了它的一部分：分区把右侧直线段盖住后，两个右角仍会露出
        1–3px 的深色点（深色主题下尤其扎眼）。这里把「控件圆角轮廓**之外**、
        分区所在列、上下各内缩 1px」的那一小片区域按输入底色重涂一遍
        （那里本来就是输入底色，只是被 bevel 越界画脏了），几何不动、观感干净。
        """
        pal = theme.current_palette()
        radius = float(int(theme.R_MD))
        outer = QPainterPath()
        outer.addRoundedRect(QRectF(widget.rect()), radius, radius)
        band = QPainterPath()
        band.addRect(QRectF(comp.x(), comp.top(), comp.width(),
                            comp.height()))
        sliver = band.subtracted(outer)
        if sliver.isEmpty():
            return
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setClipPath(sliver, Qt.ReplaceClip)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(pal["bg_input"]))
            painter.drawRect(comp)
            painter.setClipping(False)
        finally:
            painter.restore()

    @staticmethod
    def _paint_compartment(rect: QRect, painter, top: bool, bottom: bool) -> None:
        """圆角分区底（只圆右侧：整块两角 / 上·下半各一角），颜色现读色板。"""
        pal = theme.current_palette()
        radius = max(0.0, float(int(theme.R_MD) - COMPARTMENT_INSET))
        left, topf = float(rect.left()), float(rect.top())
        right, bot = float(rect.right() + 1), float(rect.bottom() + 1)
        path = QPainterPath()
        path.moveTo(left, topf)
        if top:
            path.lineTo(right - radius, topf)
            path.quadTo(right, topf, right, topf + radius)
        else:
            path.lineTo(right, topf)
        if bottom:
            path.lineTo(right, bot - radius)
            path.quadTo(right, bot, right - radius, bot)
        else:
            path.lineTo(right, bot)
        path.lineTo(left, bot)
        path.closeSubpath()

        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(pal["bg_compartment"]))
            painter.drawPath(path)
        finally:
            painter.restore()

    @staticmethod
    def _paint_divider(comp: QRect, painter) -> None:
        """微调框上下半之间的 1px 分隔线（颜色 = 色板 border；不反锯齿 → 实心 1px）。"""
        pal = theme.current_palette()
        y = comp.top() + comp.height() // 2
        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, False)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(pal["border"]))
            painter.drawRect(QRect(comp.left(), y, comp.width(),
                                   COMPARTMENT_DIVIDER))
        finally:
            painter.restore()

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

    # ---------- 雪佛龙 ----------

    @staticmethod
    def _chevron(direction: str, option, painter) -> bool:
        """在 ``option.rect`` 内画一枚雪佛龙；几何不可用时返回 False 交回基样式。"""
        rect = getattr(option, "rect", None)
        if rect is None or rect.width() <= 0 or rect.height() <= 0:
            return False
        if painter is None:
            return False
        center = rect.center()
        return ChevronStyle._draw_chevron(
            direction, QPointF(center.x() + 0.5, center.y() + 0.5),
            painter, _enabled(option))

    @staticmethod
    def _draw_chevron(direction: str, center: QPointF, painter,
                      enabled: bool) -> bool:
        if painter is None:
            return False
        color = QColor(theme.TEXT_MUTED if enabled else theme.TEXT_DIM)

        cx, cy = center.x(), center.y()
        w, h = CHEVRON_W / 2.0, CHEVRON_H / 2.0
        path = QPainterPath()
        if direction in ("down", "up"):
            tip = cy + h if direction == "down" else cy - h
            tail = cy - h if direction == "down" else cy + h
            path.moveTo(cx - w, tail)
            path.lineTo(cx, tip)
            path.lineTo(cx + w, tail)
        else:
            tip = cx + w if direction == "right" else cx - w
            tail = cx - w if direction == "right" else cx + w
            path.moveTo(tail, cy - h)
            path.lineTo(tip, cy)
            path.lineTo(tail, cy + h)

        painter.save()
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            pen = QPen(color)
            pen.setWidthF(CHEVRON_STROKE)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
        finally:
            painter.restore()
        return True


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
