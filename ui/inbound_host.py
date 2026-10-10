"""入站能力探测协作者（Wave 2 拆分自 ui/main_window.py，见 ARCHITECTURE.md §2.3）。

为什么单独成模块：「检测入站能力」是 hint 行按钮的一条独立小链——后台线程
探测（约 3s SSDP 等待）→ Qt 信号回主线程 → 弹窗展示 → 复位按钮。它同时
自带跨线程信号桥（_InboundBridge），与主窗口的解析桥（_Bridge）互不相干；
抽出来后 MainWindow 不再持有第二条信号桥的接线细节。

探测调用面经 SessionManager facade（inbound_selfcheck / inbound_hint_text，
§2.2：UI 不直连 core.inbound）。停机守卫（I2）随入口方法一起搬入本类
（读宿主 ``win._shutdown_started``，晚绑定）——本簇入口不在 close_lag
§G 的 22 入口清点表内，守卫语义（早退不弹模态）逐字保留。
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox


class _InboundBridge(QObject):
    """入站能力探测的跨线程信号桥（D-2026-09-18）。

    探测会阻塞约 3 秒（SSDP 网络等待），必须跑在后台线程；
    结果经 Qt 信号回到主线程展示，绝不跨线程直接操作控件。
    ``done`` 携带 ``inbound_report`` 返回的 dict；``failed`` 携带异常文本。
    """
    done = Signal(dict)
    failed = Signal(str)


class InboundHost:
    """入站探测协作者：后台线程 + 信号桥 + 结果弹窗（宿主晚绑定）。"""

    def __init__(self, win):
        self._win = win
        self._running = False
        self._bridge = _InboundBridge()
        self._bridge.done.connect(self._on_done)
        self._bridge.failed.connect(self._on_failed)

    def detect(self):
        """点击「检测入站能力」：后台线程探测 + 主线程展示结果。

        探测（``SessionManager.inbound_selfcheck`` facade 委托）会阻塞约
        3 秒（SSDP M-SEARCH 网络等待），必须跑在后台线程，否则界面冻结。
        core.inbound 的导入由 facade 延迟到调用点，模块缺失/探测异常统一
        冒泡到 ``failed`` 信号兜底，不连累主窗口整体导入。
        """
        win = self._win
        if win._shutdown_started:
            return                      # 停机窗口守卫：不启新工作、不弹模态
        if self._running:
            return                      # 防重入：探测进行中忽略再次点击
        self._running = True
        win.btn_inbound.setEnabled(False)
        win.btn_inbound.setText("检测中…")
        # 监听端口取 session（启动时固定），取不到回落 6881（契约兜底）
        try:
            port = int(getattr(win.session, "listen_port", 6881) or 6881)
        except Exception:
            port = 6881

        def _run():
            try:
                # facade 委托（§2.2）：inbound 探测经 SessionManager.inbound_selfcheck
                report = win.session.inbound_selfcheck(listen_port=port,
                                                       timeout=3.0)
                self._bridge.done.emit(report)
            except Exception as e:                # 探测本身异常 → 失败兜底
                self._bridge.failed.emit(str(e))

        threading.Thread(target=_run, daemon=True).start()

    def _on_done(self, report: dict):
        """后台探测成功：主线程格式化并弹窗展示（更新控件必须在主线程）。"""
        win = self._win
        if win._shutdown_started:
            self._finish()                       # 关窗中不弹模态，仅复位按钮
            return
        try:
            text = win.session.inbound_hint_text(report)
        except Exception:
            text = "检测完成，但结果格式化失败，请重试。"
        QMessageBox.information(win, "入站能力检测结果", text)
        self._finish()

    def _on_failed(self, msg: str):
        """后台探测异常：主线程弹失败兜底文案，绝不崩溃。"""
        win = self._win
        if win._shutdown_started:
            self._finish()
            return
        QMessageBox.warning(
            win, "入站能力检测失败",
            f"检测未能完成：{msg}\n\n可稍后重试，或直接在路由器做端口转发后再试。")
        self._finish()

    def _finish(self):
        """复位按钮状态（无论成功/失败/关窗都调用）。"""
        win = self._win
        self._running = False
        win.btn_inbound.setEnabled(True)
        win.btn_inbound.setText("检测入站能力")
