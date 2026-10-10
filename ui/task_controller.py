"""下载任务操作协作者（Wave 2 拆分自 ui/main_window.py，见 ARCHITECTURE.md §2.3）。

为什么单独成模块：「添加下载流程 + 任务 CRUD 槽（暂停/恢复/删除/优先级/打开
目录）」是下载页签的一整套动作接线——每个槽的形态高度一致（取 task_id →
弹确认/调 facade 任务方法 → 写状态栏反馈），且完全不含视图组装与流媒体
状态。留在 MainWindow 里让主窗口同时背负三种页签的操作语义；抽出来后
MainWindow 只保留「信号接线 + 停机守卫入口槽」，任务动作的契约面集中一处。

边界说明（不硬拆的簇）：``_task_open_preview`` / ``_on_metadata`` 留在
MainWindow——它们的主体是「把 ParseResult 灌进文件树/画廊/页签」的视图装配，
与任务 CRUD 不同轴；强行搬走只会让协作者反向抓一堆窗口控件。

停机守卫（I2）仍在 MainWindow 的入口槽首行——本类方法是守卫通过后的执行体，
不重复守卫（close_lag §G 的 AST 清点认的是入口槽形态；守卫失效时副作用会
打在 session spy 上，与被调模块无关，断言强度不变）。

对话框类（QMessageBox/QInputDialog/AddDownloadDialog）在本模块**顶层**导入：
close_lag_test 的停机窗口 spy 以「模块属性替换」拦截弹窗，测试侧 targets
已同步补上 ui.task_controller 的同名补丁（见 close_lag §I2）。
"""
from __future__ import annotations

import os

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QInputDialog, QMessageBox

from core.logutil import log_warning
from ui.add_download_dialog import AddDownloadDialog

TAB_DOWNLOADS = 2  # 与 main_window 页签常量一致（添加成功后切下载页）


def task_id(task: dict) -> str:
    """任务唯一键：id 优先，回落 info_hash（下载页契约字段）。"""
    tid = task.get("id") or task.get("info_hash") or ""
    return str(tid)


class TaskController:
    """下载任务操作协作者：添加流程 + CRUD 槽（宿主晚绑定）。"""

    def __init__(self, win):
        self._win = win

    # ---------- 添加下载 ----------

    def add_download_flow(self):
        """顶栏「添加下载」：优先取输入框内容，否则弹输入框要来源。"""
        win = self._win
        text = win.input.text().strip()
        if not (text.lower().startswith("magnet:")
                or (os.path.isfile(text)
                    and text.lower().endswith(".torrent"))):
            text, ok = QInputDialog.getText(
                win, "添加下载",
                "磁力链接或 .torrent 文件路径：", text=text)
            if not ok:
                return
        source = text.strip()
        if source:
            self.confirm_add_task(source)

    def confirm_add_task(self, source: str):
        """添加下载确认：子目录 / 优先级 / 完成后做种 → 调 add_task。"""
        win = self._win
        source = (source or "").strip()
        if not source:
            win.status_panel.set_state("添加下载失败：来源为空")
            return
        dlg = AddDownloadDialog(win.cfg, "（任务创建后自动获取名称）", 0,
                                default_save_dir="", parent=win)
        if dlg.exec() != AddDownloadDialog.DialogCode.Accepted:
            return
        save_subdir = dlg.save_path() or None
        try:
            tid = win.session.add_task(source, save_subdir=save_subdir,
                                       priority=dlg.priority(),
                                       seed=dlg.seed_after_complete())
        except Exception as e:
            log_warning("main.add_task", str(e))
            QMessageBox.warning(win, "添加下载失败", str(e))
            return
        win.tabs.setCurrentIndex(TAB_DOWNLOADS)
        # add_task 对重复 info_hash 返回已存在任务 id（自动去重），
        # 新任务与已存在任务统一按此提示
        win.status_panel.set_state(
            f"已提交下载任务：{str(tid)[:16]}…（重复 info_hash 自动去重不重复下载）")

    # ---------- 任务 CRUD 槽（下载页信号 → facade 任务方法 → 状态栏反馈） ----------

    def pause(self, task: dict):
        win = self._win
        tid = task_id(task)
        if tid and win.session.pause_task(tid):
            win.status_panel.set_state(
                f"已暂停任务：{task.get('name') or tid[:16]}")
        else:
            win.status_panel.set_state("暂停失败：任务不存在或状态不可暂停")

    def resume(self, task: dict):
        win = self._win
        tid = task_id(task)
        if tid and win.session.resume_task(tid):
            win.status_panel.set_state(
                f"已恢复任务：{task.get('name') or tid[:16]}")
        else:
            win.status_panel.set_state("恢复失败：任务不存在或状态不可恢复")

    def remove(self, task: dict):
        win = self._win
        tid = task_id(task)
        if not tid:
            return
        name = task.get("name") or tid[:16]
        box = QMessageBox(win)
        box.setWindowTitle("删除任务")
        box.setText(f"删除任务「{name}」？\n\n请选择清理方式：")
        btn_keep = box.addButton("仅删除任务（保留文件）",
                                 QMessageBox.AcceptRole)
        btn_del = box.addButton("删除任务和文件", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_keep or clicked is btn_del:
            ok = win.session.remove_task(tid, delete_files=(clicked is btn_del))
            win.status_panel.set_state(
                f"已删除任务：{name}" if ok else f"删除任务失败：{name}")

    def priority(self, task: dict, delta: int):
        win = self._win
        if not hasattr(win.session, "set_priority"):
            win.status_panel.set_state("优先级调整暂未开放（后续版本接入）")
            return
        cur = int(task.get("priority", 1) or 1)
        try:
            win.session.set_priority(task_id(task), max(0, min(3, cur + delta)))
            win.status_panel.set_state("已调整任务优先级")
        except Exception as e:
            log_warning("main.priority", str(e))
            win.status_panel.set_state("调整优先级失败")

    def open_dir(self, task: dict):
        """下载页「打开目录」：任务落盘目录，缺失回落 downloads/（无模态、无新下载）。"""
        win = self._win
        path = task.get("save_path") or ""
        if not path or not os.path.isdir(path):
            path = os.path.join(win.cache_dir, "downloads")
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))
