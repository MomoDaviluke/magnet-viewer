"""Qt 样式代理：自绘「细雪佛龙」下拉 / 微调箭头。

为什么需要本文件
----------------
改造前 `ui/theme.py` 的下拉箭头用的是 CSS 三角 hack
（``width:0;height:0;border-left/right: transparent;border-top: <实色>``）。
**Qt 的样式表引擎不支持这套画法**：它把 border 当成真实边框画出来，
于是箭头渲染成一个灰方块（用户截图实证）。Qt 样式表对箭头只认
``image:``（图/ SVG）或交给「基样式」去画——所以正确做法是回到
QStyle 绘制层自绘。

做法
----
``ChevronStyle(QProxyStyle)`` 只接管六个箭头元素
（``PE_IndicatorArrowDown/Up/Left/Right`` 与
``PE_IndicatorSpinUp/SpinDown``），用 QPainter + QPainterPath 画两条
圆头圆角接的描边线段（∨ ∧ < >）；其余元素一律 ``super().drawPrimitive``
转发给基样式，**不改变其他控件观感**。

配色
----
颜色**在绘制时**读 ``ui.theme`` 模块属性（``theme.TEXT_MUTED`` /
禁用时 ``theme.TEXT_DIM``），与项目「自绘控件不得固化导入期色值快照」
的约定一致——热切换主题后箭头颜色立即跟随，**无需重建 style**。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QProxyStyle, QStyle

import ui.theme as theme      # 颜色在绘制时现读（热切换跟随）

# 雪佛龙几何（逻辑像素；与 ui/theme.py 的 8px 网格/字号体系相称）
CHEVRON_W = 4.5         # 左右端点间距的一半 -> 总宽
CHEVRON_H = 2.6         # 顶点到底边的垂直距离的一半 -> 总高
CHEVRON_STROKE = 1.6    # 描边宽度（细线，避免"大块箭头"的粗糙感）

_DIR = {
    QStyle.PE_IndicatorArrowDown: "down",
    QStyle.PE_IndicatorArrowUp: "up",
    QStyle.PE_IndicatorArrowLeft: "left",
    QStyle.PE_IndicatorArrowRight: "right",
    QStyle.PE_IndicatorSpinDown: "down",
    QStyle.PE_IndicatorSpinUp: "up",
}
_CHEVRON_ELEMENTS = frozenset(_DIR)


class ChevronStyle(QProxyStyle):
    """把箭头类 primitive 画成细雪佛龙；其余全部转发给基样式。"""

    def __init__(self, base_style=None):
        super().__init__(base_style)

    # 供测试/门禁断言的绘制计数（证明 proxy 真的被调用，而不是被 QSS 短路）
    draw_calls: int = 0

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        direction = _DIR.get(element)
        if direction is not None and self._chevron(direction, option, painter):
            type(self).draw_calls += 1
            return
        super().drawPrimitive(element, option, painter, widget)

    # ---------- 内部 ----------

    @staticmethod
    def _chevron(direction: str, option, painter) -> bool:
        """在 ``option.rect`` 内画一枚雪佛龙；几何不可用时返回 False 交回基样式。"""
        rect = getattr(option, "rect", None)
        if rect is None or rect.width() <= 0 or rect.height() <= 0:
            return False
        if painter is None:
            return False
        enabled = bool(getattr(option, "state", QStyle.State_None)
                       & QStyle.State_Enabled)
        color = QColor(theme.TEXT_MUTED if enabled else theme.TEXT_DIM)

        cx = rect.center().x() + 0.5
        cy = rect.center().y() + 0.5
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
