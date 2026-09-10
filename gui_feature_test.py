"""GUI 功能校验：主窗口实例化 + 拖放 + 输入历史自动补全 + 文件树展开。

无头运行：QT_QPA_PLATFORM=offscreen
用法：.\\.venv\\Scripts\\python.exe gui_feature_test.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (QModelIndex, QItemSelectionModel, QMimeData,  # noqa: E402
                            QStringListModel, QUrl)
from PySide6.QtWidgets import QApplication  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import RECENT_LIMIT, AppConfig, DEFAULTS  # noqa: E402
from core.parser import parse_torrent_file  # noqa: E402
from ui.add_download_dialog import AddDownloadDialog  # noqa: E402
from ui.downloads_pane import COL_NAME, DownloadsPane  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

OK, FAIL = [], []


def check(cond, msg):
    (OK if cond else FAIL).append(msg)
    print(("  [OK] " if cond else "  [FAIL] ") + msg)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    tmp = tempfile.mkdtemp(prefix="mv_gui_")
    # 箭头绘制样式：必须在任何 apply_theme（挂 QSS）**之前**装（与 main.py 同序；
    # QSS 会包成 QStyleSheetStyle 代理，本样式才是它转发箭头绘制时的基样式）。
    from ui.style import install as _install_chevron  # noqa: E402
    _install_chevron(app)

    # ---------------------------------------------------------------- [0] 设置接线
    # 回归 P0-3：设置面板的「默认下载目录」「默认并发下载数」必须真正生效。
    # MainWindow 构造前写入测试配置，构造后断言，finally 恢复用户全部设置。
    print("\n[0] 设置接线：默认下载目录 / 默认并发下载数（P0-3 回归）")
    cfg0 = AppConfig()
    _orig_all = {k: cfg0.get(k) for k in DEFAULTS}
    dl_out = os.path.join(tmp, "dl_outside")
    os.makedirs(dl_out, exist_ok=True)
    cfg0.set("download_dir", dl_out)
    cfg0.set("default_concurrency", 5)
    cfg0.set("download_rate_limit", 777)
    cfg0.set("logging_enabled", False)
    cfg0.set("cache_limit_mb", 8)
    w = None
    try:
        w = MainWindow()
        check(os.path.normpath(w.session.download_dir) == os.path.normpath(dl_out),
              "download_dir 设置已接入 SessionManager")
        check(w.session._active_downloads == 5,
              "default_concurrency 设置已接入 SessionManager")
        bases = w.server._httpd.RequestHandlerClass.base_dirs
        check(any(os.path.normpath(b) == os.path.normpath(dl_out) for b in bases),
              "StreamServer base_dirs 含 download_dir（多根）")
        from core import logutil
        check(not logutil.is_enabled(),
              "logging_enabled=False 启动接线生效（P2-18）")
        if hasattr(w.session._ses, "get_settings"):
            _rl = int(w.session._ses.get_settings().get("download_rate_limit") or 0)
            check(_rl == 777 * 1024,
                  f"download_rate_limit 启动接线生效（{_rl // 1024} KB/s）")
        check("8.0 MB" in w.status_panel.cache.text(),
              "cache_limit_mb 状态栏占用显示已接入")
    finally:
        for k, v in _orig_all.items():
            cfg0.set(k, v)
    check(w is not None, "MainWindow 构造成功（配置恢复后仍可继续测试）")

    # ---------------------------------------------------------------- [1] 实例化
    print("\n[1] MainWindow 实例化")
    w.show()
    app.processEvents()
    check(w.windowTitle().startswith("磁力链"), "窗口标题正常")
    check(w.input is not None, "输入框存在")
    check(w.tabs.count() >= 2, f"页签数量 {w.tabs.count()} >= 2")

    # ---------------------------------------------------------------- [2] 拖放
    print("\n[2] 拖放支持")
    check(bool(w.acceptDrops()), "setAcceptDrops(True) 已启用")

    # 2a. 拖入磁力链文本 -> 应被接受
    md = QMimeData()
    md.setText("magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567")
    from PySide6.QtGui import QDragEnterEvent
    from PySide6.QtCore import QPoint, Qt

    ev = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction | Qt.MoveAction, md,
                         Qt.LeftButton, Qt.NoModifier)
    w.dragEnterEvent(ev)
    check(ev.isAccepted(), "磁力链文本 dragEnter 被接受")

    # 2b. 拖入 .torrent 文件 -> 应被接受
    md2 = QMimeData()
    md2.setUrls([QUrl.fromLocalFile(os.path.join(tmp, "x.torrent"))])
    ev2 = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction | Qt.MoveAction, md2,
                          Qt.LeftButton, Qt.NoModifier)
    w.dragEnterEvent(ev2)
    check(ev2.isAccepted(), ".torrent 文件 dragEnter 被接受")

    # 2c. 拖入无关文本 -> 应被拒绝
    md3 = QMimeData()
    md3.setText("hello world")
    ev3 = QDragEnterEvent(QPoint(5, 5), Qt.CopyAction | Qt.MoveAction, md3,
                          Qt.LeftButton, Qt.NoModifier)
    w.dragEnterEvent(ev3)
    check(not ev3.isAccepted(), "无关文本 dragEnter 被拒绝")

    # ---------------------------------------------------------------- [3] 历史
    print("\n[3] 输入历史 / 自动补全")
    check(hasattr(w, "_recent_model") and isinstance(w._recent_model,
                                                     QStringListModel),
          "_recent_model 已创建")
    check(w.input.completer() is not None, "输入框已挂载 QCompleter")
    check(w.input.completer().model() is w._recent_model,
          "completer 使用 _recent_model")

    # 历史测试会写入真实 QSettings（注册表）：先备份，结束后恢复，避免污染用户历史
    import json as _json
    from core.config import AppConfig as _AppConfig
    _save_cfg = _AppConfig()
    _orig_recent = _save_cfg.recent()
    try:
        sample = [f"magnet:?xt=urn:btih:{i:040d}" for i in range(3)]
        cur = list(w.cfg.recent())
        for s in sample:
            cur = w.cfg.push_recent(s)
        check(cur[:3] == sample[::-1], "新条目置顶（去重 + 逆序）")
        check(len(cur) <= RECENT_LIMIT, f"历史条数 {len(cur)} <= {RECENT_LIMIT}")

        for i in range(RECENT_LIMIT + 10):
            cur = w.cfg.push_recent(f"item-{i}")
        check(len(cur) == RECENT_LIMIT, f"历史上限生效：{len(cur)} == {RECENT_LIMIT}")
        check(w.cfg.recent() == cur, "QSettings 持久化读回一致")
    finally:
        _save_cfg.set("recent", _json.dumps(_orig_recent, ensure_ascii=False))
        print("  [OK] QSettings 历史已恢复（不污染用户数据）"
              if w.cfg.recent() == _orig_recent
              else "  [FAIL] QSettings 历史恢复失败")

    # ---------------------------------------------------------------- [4] 文件树
    print("\n[4] 文件树")
    torrents = [f for f in os.listdir(".") if f.endswith(".torrent")]
    if not torrents:
        import libtorrent as lt
        src = os.path.join(tmp, "payload")
        files = {
            "Season 01/ep1.mp4": os.urandom(200 * 1024),
            "Season 01/ep2.mp4": os.urandom(120 * 1024),
            "Season 02/ep1.mp4": os.urandom(90 * 1024),
            "Extras/pics/a.jpg": os.urandom(40 * 1024),
            "Extras/pics/b.png": os.urandom(16 * 1024),
            "readme.txt": b"hello magnet viewer",
        }
        for rel, data in files.items():
            p = os.path.join(src, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        fs = lt.file_storage()
        lt.add_files(fs, src)
        t = lt.create_torrent(fs, 16 * 1024)
        lt.set_piece_hashes(t, os.path.dirname(src))
        tpath = os.path.join(tmp, "gui_nested.torrent")
        with open(tpath, "wb") as f:
            f.write(lt.bencode(t.generate()))
        torrents = [tpath]
        print(f"  (已生成嵌套目录测试种子: {tpath})")
    if torrents:
        res = parse_torrent_file(torrents[0])
        w.tree.populate(res)
        model = w.tree.model()
        check(model.rowCount() > 0, f"模型根行数 {model.rowCount()} > 0")

        def walk(parent, depth=0, out=None):
            out = out if out is not None else []
            for r in range(model.rowCount(parent)):
                idx = model.index(r, 0, parent)
                out.append((depth, model.data(idx)))
                if model.hasChildren(idx):
                    print(f"        L{depth} 目录: {model.data(idx)}")
                    walk(idx, depth + 1, out)
            return out

        rows = walk(QModelIndex())
        n_files = len([d for _, d in rows if "." in d])
        check(n_files == len(res.view_files),
              f"叶子文件行 {n_files} == 可见文件 {len(res.view_files)}")
        check(any(d.endswith("Season 01") for _, d in rows),
              "二级目录（Season 01）在模型中可见")
        check(not any(".pad" in d for _, d in rows),
              "模型中没有 .pad 填充文件")
        check(max(d for d, _ in rows) >= 2,
              f"存在三级及以上层级（最深 depth={max(d for d, _ in rows)}）")

        # 目录行必须处于展开态
        collapsed = []

        def check_expanded(parent=QModelIndex()):
            for r in range(model.rowCount(parent)):
                idx = model.index(r, 0, parent)
                if model.hasChildren(idx) and not w.tree.isExpanded(idx):
                    collapsed.append(model.data(idx))
                check_expanded(idx)

        check_expanded()
        check(not collapsed, f"无折叠目录（折叠：{collapsed}）")

        # 本地 .torrent 的关键链路：磁盘绝对路径 -> TorrentFile 映射
        # （cache_dir 未注入时键会退化成相对路径，_pieces_map/_demand_range 全部查不到，
        #   预览随即退化成按完整静态文件服务，把未下载的稀疏零数据喂给播放器）
        w._on_metadata(res)
        keys = list(w._path_to_file)
        check(bool(keys), f"_path_to_file 非空（{len(keys)} 项）")
        check(all(os.path.isabs(k) for k in keys),
              "所有映射键均为绝对路径")
        sample = res.view_files[0]
        expect = os.path.normpath(os.path.join(
            w.cache_dir, *sample.path.split("/")))
        check(expect in w._path_to_file,
              f"可按绝对路径命中文件（{sample.name}）")

    # ---------------------------------------------------------------- [4b] 清理保留名单
    print("\n[4b] 清理缓存保留名单（P0-1 回归）")
    from core.cache_guard import (clear_cache_contents,  # noqa: E402
                                  ensure_cache_dir)
    cache_c = os.path.join(tmp, "cache_clean")
    ensure_cache_dir(cache_c)
    for rel in ("downloads/t1", ".resume", ".preview/ih1"):
        os.makedirs(os.path.join(cache_c, *rel.split("/")), exist_ok=True)
    with open(os.path.join(cache_c, ".tasks.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(cache_c, "scatter.bin"), "w") as f:
        f.write("x")
    n = clear_cache_contents(cache_c)
    check(n >= 2, f"清除条目数 {n} >= 2（.preview 与散文件）")
    check(os.path.isdir(os.path.join(cache_c, "downloads")),
          "downloads/ 保留（用户下载数据）")
    check(os.path.isfile(os.path.join(cache_c, ".tasks.json")),
          ".tasks.json 保留（任务清单）")
    check(os.path.isdir(os.path.join(cache_c, ".resume")),
          ".resume/ 保留（续传数据）")
    check(not os.path.exists(os.path.join(cache_c, ".preview")),
          ".preview/ 已清除")
    check(not os.path.exists(os.path.join(cache_c, "scatter.bin")),
          "散文件已清除")

    # ---- [4b-2] keep_dirs：活任务目录及其内容整棵跳过（阶段 C C1）----
    # convert 转正任务落盘仍在 .preview/<ih>（零重下），「手动清理/退出清理」
    # 必须复核 protected_dirs() 后排除活任务目录，否则引擎对着空目录重下（永动机）。
    live = os.path.join(cache_c, ".preview", "cd" * 20)
    os.makedirs(os.path.join(live, "sub"), exist_ok=True)
    for rel in ("vid.bin", os.path.join("sub", "deep.bin")):
        with open(os.path.join(live, rel), "wb") as f:
            f.write(b"x" * 8)
    dead = os.path.join(cache_c, ".preview", "dead" * 20)
    os.makedirs(dead, exist_ok=True)
    n2 = clear_cache_contents(cache_c, keep_dirs={live})
    check(os.path.isfile(os.path.join(live, "vid.bin"))
          and os.path.isfile(os.path.join(live, "sub", "deep.bin")),
          "keep_dirs 命中目录连同**内容**整体幸存（嵌套子文件不删）")
    check(not os.path.exists(dead), "同父 .preview 下的无保目录照常清除")
    check(os.path.isdir(os.path.join(cache_c, ".preview")),
          "父目录 .preview 因含受保子目录而保留")
    check(os.path.isdir(os.path.join(cache_c, "downloads"))
          and os.path.isfile(os.path.join(cache_c, ".tasks.json")),
          "keep_dirs 不影响 CLEANUP_KEEP 保留名单")
    check(n2 == 1, f"keep_dirs 下计数=1（dead 目录；.preview 父目录保留不计，"
                   f"scatter.bin 已在上方基线清除中消耗，实得 {n2}）")
    # 不传 keep_dirs：与基线逐字一致（向后兼容）——整个 .preview 连活目录清光
    n3 = clear_cache_contents(cache_c)
    check(not os.path.exists(os.path.join(cache_c, ".preview")),
          "不传 keep_dirs：.preview 整棵清除（基线行为逐字一致）")
    check(n3 == 1, f"不传 keep_dirs 顶层计数与基线一致（仅 .preview，实得 {n3}）")

    # ---------------------------------------------------------------- [4b] 设置对话框清理路径
    # 设置对话框「立即清理缓存」路径：必须同样走保留名单（不误删下载数据）
    import ui.settings_dialog as _sd  # noqa: E402
    _sd.QMessageBox.information = staticmethod(lambda *a, **k: None)
    _sd.QMessageBox.warning = staticmethod(lambda *a, **k: None)
    os.makedirs(os.path.join(cache_c, ".preview", "ih2"), exist_ok=True)
    # 注入活目录提供器（阶段 C C1：UI 不直闯 core 内部——主窗口把
    # session.protected_dirs 闭包注入对话框）：对话框清理须跳过活转正任务目录
    live2 = os.path.join(cache_c, ".preview", "ef" * 20)
    os.makedirs(live2, exist_ok=True)
    with open(os.path.join(live2, "keep.bin"), "wb") as f:
        f.write(b"x")
    dlg = _sd.SettingsDialog(AppConfig(), cache_c, on_clear_cache=None,
                             keep_dirs_get=lambda: {live2}, parent=w)
    dlg.cache_edit.setText(cache_c)
    dlg._clear_cache()
    check(os.path.isdir(os.path.join(cache_c, "downloads")),
          "SettingsDialog 清理后 downloads/ 仍保留（P0-1 不误删下载数据）")
    check(os.path.isfile(os.path.join(cache_c, ".tasks.json")),
          "SettingsDialog 清理后 .tasks.json 仍保留")
    check(not os.path.exists(os.path.join(cache_c, ".preview", "ih2")),
          "SettingsDialog 清理已清除无保预览目录")
    check(os.path.isfile(os.path.join(live2, "keep.bin")),
          "SettingsDialog 经 keep_dirs_get 跳过活任务目录（C1 接线）")
    # 未注入 keep_dirs_get（兼容旧调用方）：基线行为不变，.preview 整棵清除
    dlg = _sd.SettingsDialog(AppConfig(), cache_c, on_clear_cache=None,
                             parent=w)
    dlg.cache_edit.setText(cache_c)
    dlg._clear_cache()
    check(not os.path.exists(os.path.join(cache_c, ".preview")),
          "SettingsDialog 未注入 keep_dirs_get：.preview 整棵清除（基线兼容）")

    # ---- [4b-3] MainWindow 接线：清理入口复核活任务目录 + 退出快照时机 ----
    # 假“活转正任务”：注册表塞 save_path=.preview/<ih> 的记录（不碰引擎）。
    ih3 = "ab" * 20
    live3 = os.path.join(cache_c, ".preview", ih3)
    os.makedirs(os.path.join(live3, "data"), exist_ok=True)
    with open(os.path.join(live3, "data", "chunk.bin"), "wb") as f:
        f.write(b"y")
    decoy = os.path.join(cache_c, ".preview", "de" * 20)
    os.makedirs(decoy, exist_ok=True)
    from core.registry import TaskRecord  # noqa: E402
    with w.session._registry.lock:
        w.session._registry.torrents[ih3] = TaskRecord(save_path=live3)
    try:
        pd = {os.path.normcase(p) for p in w.session.protected_dirs()}
        check(os.path.normcase(live3) in pd,
              "session.protected_dirs() 含 .preview/<ih> 活任务目录（测试装置）")
        check(os.path.normcase(live3) in
              {os.path.normcase(p) for p in w._live_cache_dirs()},
              "MainWindow._live_cache_dirs 与 protected_dirs 同源透传")

        # 手动清理入口：_clear_cache_now 跳过活转正任务目录（C1 核心）
        orig_cache_dir = w.cache_dir
        orig_stop = w._stop_preview
        try:
            w.cache_dir = cache_c
            w._stop_preview = lambda: None   # 避免动真实会话
            w._clear_cache_now()
        finally:
            w.cache_dir = orig_cache_dir
            w._stop_preview = orig_stop
        check(os.path.isfile(os.path.join(live3, "data", "chunk.bin")),
              "_clear_cache_now 跳过活转正任务目录（C1：不再互噬）")
        check(not os.path.exists(decoy),
              "_clear_cache_now 照常清除无保预览目录（非全保留）")
        check(os.path.isdir(os.path.join(cache_c, "downloads")),
              "_clear_cache_now 保留名单不倒退（downloads 仍在）")

        # 退出清理时机：名单快照必须在 session.shutdown **之前**抓——
        # shutdown 会 clear_runtime_state 清空注册表，事后 protected_dirs() 为空。
        # 假 _sess.shutdown 复刻“清空注册表”效应，令事后快照必然失效。
        seq: list = []
        orig_sd = w.session._sess.shutdown

        def fake_sd():
            with w.session._registry.lock:
                w.session._registry.torrents.clear()
            seq.append("session.shutdown")
        w.session._sess.shutdown = fake_sd
        orig_srv = w.server.shutdown
        w.server.shutdown = lambda: seq.append("server.shutdown")
        orig_clear = w._clear_preview_cache_now
        w._clear_preview_cache_now = lambda keep_dirs=(), log_key="": (
            seq.append(("clear",
                        {os.path.normcase(p) for p in keep_dirs})))
        cfg_exit = w.cfg
        orig_exit_flag = cfg_exit.get("clear_cache_on_exit")
        cfg_exit.set("clear_cache_on_exit", True)
        orig_cache_dir = w.cache_dir
        w.cache_dir = cache_c
        try:
            w.close()
            # 阶段 A（plan A2）：close 已异步化——顺序断言必须等后台收尾跑完，
            # 否则读数随线程调度抖动（旧实现同步阻塞，close() 返回即已排序）。
            _t0 = time.time()
            while (not getattr(w, "_shutdown_done", False)
                   and time.time() - _t0 < 6):
                app.processEvents()
                time.sleep(0.02)
            app.processEvents()
        finally:
            w.session._sess.shutdown = orig_sd
            w.server.shutdown = orig_srv
            w._clear_preview_cache_now = orig_clear
            cfg_exit.set("clear_cache_on_exit", orig_exit_flag)
            w.cache_dir = orig_cache_dir
        check([s if not isinstance(s, tuple) else s[0] for s in seq]
              == ["session.shutdown", "server.shutdown", "clear"],
              f"closeEvent 顺序：shutdown → server → 清理（实得 {seq}）")
        got_keep = seq[2][1] if len(seq) == 3 and isinstance(seq[2], tuple) \
            else set()
        check(os.path.normcase(live3) in got_keep,
              "closeEvent：清理拿到的保护名单含活任务目录"
              "（快照在 session.shutdown 之前抓，非事后空名单）")
    finally:
        with w.session._registry.lock:
            w.session._registry.torrents.pop(ih3, None)

    # ---- [4b-4] LRU 配额保护名单 fail-closed（阶段 D D0，审查 Important-1）----
    # _enforce_cache_quota 的 keep 回调必须**裸调** protected_dirs：会话异常
    # 时上抛 → cache_quota._norm_keep 的 None 分支 → 本轮保守不删。旧的
    # _live_cache_dirs 空集兜底（fail-open，只该服务手动清理入口）在 LRU
    # 路径上会把「名单故障」放大成「全部活目录无保可删」，方向相反。
    print("\n[4b-4] LRU keep 回调 fail-closed（D0）")
    import ui.main_window as _mw  # noqa: E402
    quota_cache = os.path.join(tmp, "cache_d0")
    proot = os.path.join(quota_cache, ".preview")
    os.makedirs(proot, exist_ok=True)
    for _ih, _mt in (("a1" * 20, 1_000_000_000), ("b2" * 20, 2_000_000_000)):
        _d = os.path.join(proot, _ih)
        os.makedirs(_d, exist_ok=True)
        _fp = os.path.join(_d, "chunk.bin")
        with open(_fp, "wb") as _f:
            _f.write(b"x" * 900 * 1024)          # 两份都超 1MB 上限
        os.utime(_fp, (_mt, _mt))
    orig_cd, orig_lim = w.cache_dir, w.cfg.get("cache_limit_mb")
    w.cache_dir = quota_cache
    w.cfg.set("cache_limit_mb", 1)
    try:
        def _boom_dirs():
            raise RuntimeError("会话已停机")
        _orig_pd = w.session.protected_dirs
        w.session.protected_dirs = _boom_dirs
        try:
            w._enforce_cache_quota()      # 异常必须被 fail-closed 消化，不外抛
        finally:
            w.session.protected_dirs = _orig_pd
        check(os.path.isdir(os.path.join(proot, "a1" * 20))
              and os.path.isdir(os.path.join(proot, "b2" * 20)),
              "protected_dirs 抛异常：LRU 本轮零删除（fail-closed，活目录全幸存）")

        # 白盒：装配进 cache_quota 的闭包确实透传异常（UI 层不吞）。
        # spy 截获 enforce_preview_limit 收到的 keep 回调后单独引爆。
        captured: dict = {}
        _orig_enforce = _mw.enforce_preview_limit

        def _spy(root, limit_mb, keep_dirs=None, warn=None):
            captured["keep"] = keep_dirs
            return 0, 0
        _mw.enforce_preview_limit = _spy
        w.session.protected_dirs = _boom_dirs
        raised = False
        try:
            w._enforce_cache_quota()
            # 闭包在调用时才取 protected_dirs：须在还原前引爆
            try:
                captured["keep"]()
            except RuntimeError:
                raised = True
        finally:
            _mw.enforce_preview_limit = _orig_enforce
            w.session.protected_dirs = _orig_pd
        check("keep" in captured and raised,
              "_enforce_cache_quota 透传的 keep 闭包裸调 protected_dirs："
              "异常上抛给 cache_quota（不经 _live_cache_dirs 空集兜底）")
    finally:
        w.cache_dir = orig_cd
        w.cfg.set("cache_limit_mb", orig_lim)

    # ---- [4b-5] 设置对话框：预览缓存模式下拉（阶段 D D2）----
    # convert/hold 两档 UI 化（阶段 B 裁掉项）：值域来自
    # core.cache_mode.PREVIEW_CACHE_MODES（消灭零引用常量），初值读配置、
    # _save 回写配置。
    print("\n[4b-5] 设置对话框「预览缓存模式」combo（D2）")
    from core.cache_mode import (PREVIEW_CACHE_CONVERT,  # noqa: E402
                                 PREVIEW_CACHE_HOLD, PREVIEW_CACHE_MODES)
    cfg2 = AppConfig()
    _orig_all2 = {k: cfg2.get(k) for k in DEFAULTS}
    try:
        cfg2.set("preview_cache_mode", PREVIEW_CACHE_HOLD)
        dlg = _sd.SettingsDialog(cfg2, cache_c, on_clear_cache=None, parent=w)
        check(hasattr(dlg, "cache_mode"),
              "SettingsDialog 新增 cache_mode 下拉（D2）")
        check([dlg.cache_mode.itemData(i)
               for i in range(dlg.cache_mode.count())]
              == list(PREVIEW_CACHE_MODES),
              "combo 值域逐字=PREVIEW_CACHE_MODES（不自行发明第三档）")
        from ui.settings_dialog import CACHE_MODE_LABELS  # noqa: E402
        check(set(CACHE_MODE_LABELS) == set(PREVIEW_CACHE_MODES),
              "D5 Minor-b：CACHE_MODE_LABELS 键集与 PREVIEW_CACHE_MODES 值域"
              "双向对齐（无孤儿标签/无缺标签模式）")
        check(dlg.cache_mode.currentData() == PREVIEW_CACHE_HOLD,
              "combo 初值读自 preview_cache_mode 配置（hold）")
        # D5 Minor-c：存量非法值（garbage）回落：index 0 + currentData 有效
        cfg2.set("preview_cache_mode", "garbage")
        dlg_g = _sd.SettingsDialog(cfg2, cache_c, on_clear_cache=None, parent=w)
        check(dlg_g.cache_mode.currentIndex() == 0
              and dlg_g.cache_mode.currentData() in PREVIEW_CACHE_MODES,
              "D5 Minor-c：garbage 存量值回落 index 0 且 currentData 有效")
        dlg_g.deleteLater()
        cfg2.set("preview_cache_mode", PREVIEW_CACHE_HOLD)
        dlg.cache_mode.setCurrentIndex(
            dlg.cache_mode.findData(PREVIEW_CACHE_CONVERT))
        dlg._save()
        check(AppConfig().get("preview_cache_mode") == PREVIEW_CACHE_CONVERT,
              "_save 把 combo 选择回写 preview_cache_mode=convert")
        dlg.deleteLater()
    finally:
        for k, v in _orig_all2.items():
            cfg2.set(k, v)

    # ---- [4b-6] convert 档播放中「后台缓存完整文件」文案（阶段 D D3）----
    # 纯函数产文案，播放位置优先语义只在 convert 档出现；hold 档文案不变。
    print("\n[4b-6] convert 档后台缓存文案（D3）")
    from ui.main_window import background_cache_text  # noqa: E402
    from ui.preview_player import VideoPreviewWidget  # noqa: E402
    from core.models import TorrentFile as _TF  # noqa: E402
    pf_d3 = _TF(1, "root/big.mkv", 1000, 0, 0, 0)
    t1 = background_cache_text(PREVIEW_CACHE_CONVERT, pf_d3, [0, 420])
    check(t1 == "后台缓存完整文件：42.0%（播放位置优先）",
          f"convert 档：file_progress→百分比文案（实得 {t1!r}）")
    check(background_cache_text(PREVIEW_CACHE_HOLD, pf_d3, [0, 420]) is None,
          "hold 档：无后台缓存文案（行为不变）")
    check(background_cache_text(PREVIEW_CACHE_CONVERT, pf_d3, []) is None,
          "file_progress 缺失：宁可不显示也不误显示")
    check(background_cache_text(PREVIEW_CACHE_CONVERT, pf_d3, [0, 1000])
          == "后台缓存完整文件：100.0%（播放位置优先）",
          "满进度文案照常（100.0%）")
    check(background_cache_text(PREVIEW_CACHE_CONVERT, None, [0, 420]) is None,
          "无预览文件：None")
    vp8 = VideoPreviewWidget()
    vp8.update_buffer(0.5, 200 * 1024, t1)
    check("后台缓存完整文件：42.0%" in vp8.buffer_label.text()
          and "缓冲 50.0%" in vp8.buffer_label.text(),
          "update_buffer 拼接后台缓存注记（缓冲语义保留）")
    vp8.update_buffer(0.5, 200 * 1024)
    check("后台缓存" not in vp8.buffer_label.text(),
          "不传注记：缓冲栏与基线文案一致")
    vp8.deleteLater()

    # ---- [4b-6c] 缓存占用口径 + 开播门控文案（plan/07 阶段 3）----
    # 真机用户看到「缓存 4.1 GB / 2.0 GB」——按**预分配尺寸**算，稀疏文件恒
    # 等于文件大小（实际才下 59MB），既误导又像爆缓存。口径改用**已下载
    # 字节**（file_progress 汇总）；预览缓存上限判定仍按目录占用（保守）。
    # 开播门控文案区分「等数据」与「等索引块」（此前只有一句合并文案）。
    print("\n[4b-6c] 缓存占用口径改已下载字节 + 门控文案区分（阶段 3）")
    from core.cache_quota import downloaded_bytes  # noqa: E402
    from ui.main_window import cache_usage_text  # noqa: E402
    from ui.preview_player import (WAIT_BOTH, WAIT_DATA, WAIT_INDEX,  # noqa: E402
                                   waiting_text)
    _gi = 1024 ** 3
    # 口径：file_progress 汇总（真机 59MB 场景不再显示预分配 4.1GB）
    check(downloaded_bytes([59 * 1024 * 1024]) == 59 * 1024 * 1024,
          "downloaded_bytes 汇总 = 已下载字节（59MB ≠ 预分配 4.1GB）")
    check(downloaded_bytes([10, 20, 30]) == 60, "downloaded_bytes 多文件求和")
    check(downloaded_bytes([]) == 0 and downloaded_bytes(None) == 0,
          "downloaded_bytes 空/None → 0（无预览时口径安全）")
    check(downloaded_bytes([-5, 7, None, "x"]) == 7,
          "downloaded_bytes 非法项/负值容错（不产生负数占用）")
    txt = cache_usage_text(59 * 1024 * 1024, 2 * _gi)
    check("59.0 MB" in txt and "2.0 GB" in txt and "4.1 GB" not in txt,
          f"有配额文案 = 已下载 / 上限（实得 {txt!r}）")
    check(cache_usage_text(0, 0) == "",
          "无配额且零下载 → 空串（调用方隐藏标签，避免常驻噪音）")
    check("59.0 MB" in cache_usage_text(59 * 1024 * 1024, 0),
          "无配额但确有下载 → 显示已下载量")
    check(cache_usage_text(0, 2 * _gi).endswith("2.0 GB"),
          "有配额时 0 字节也显示上限（用户可见配额生效）")
    # 门控文案：等数据 / 等索引块 / 缺省（缺省与旧文案逐字一致）
    check(waiting_text("a.mp4", 1000, WAIT_DATA)
          == "缓冲中，等待数据就绪：a.mp4（1000 B）",
          f"等数据文案（实得 {waiting_text('a.mp4', 1000, WAIT_DATA)!r}）")
    check(waiting_text("a.mp4", 1000, WAIT_INDEX)
          == "缓冲中，等待索引块就绪：a.mp4（1000 B）",
          "等索引块文案")
    check(waiting_text("a.mp4", 1000)
          == "缓冲中，等待数据与索引块就绪：a.mp4（1000 B）",
          "缺省（WAIT_BOTH）文案与旧行为逐字一致")
    check(WAIT_BOTH not in (WAIT_DATA, WAIT_INDEX),
          "阶段常量互不相等（downstream 判据不歧义）")
    # 组件接线：set_waiting → 合并文案；set_waiting_stage 切单句
    vp9 = VideoPreviewWidget()
    vp9.set_waiting("a.mp4", 1000)
    check("等待数据与索引块就绪" in vp9.title.text(), "set_waiting 默认合并文案")
    vp9.set_waiting_stage(WAIT_DATA)
    check("等待数据就绪" in vp9.title.text()
          and "索引块" not in vp9.title.text(), "等待期切「等数据」文案")
    vp9.set_waiting_stage(WAIT_INDEX)
    check("等待索引块就绪" in vp9.title.text() and "数据" not in vp9.title.text(),
          "等待期切「等索引块」文案")
    vp9.set_stream("", "a.mp4", 1000)
    check("正在流式播放" in vp9.title.text(), "开播后正常标题（阶段文案不残留）")
    vp9.deleteLater()

    # 受控单测：主窗口开播门控按「缺数据 / 缺索引」分别下发阶段文案，
    # 且门控放行判据与文案彼此独立（文案不得反过来驱动门控）。
    _orig_pending = w._pending_video
    _orig_status = w.session.status
    try:
        _gp = _TF(0, "root/big.mp4", 8 * 1024 * 1024, 0, 0, 0)
        _stages = []
        _orig_stage = w.preview.video.set_waiting_stage
        _orig_setstream = w.preview.video.set_stream
        _streamed = []
        w.preview.video.set_waiting_stage = lambda s: _stages.append(s)
        w.preview.video.set_stream = lambda *a, **k: _streamed.append(a)
        w._pending_video = (_gp, "http://x/big")
        _base = {"num_seeds": 0, "num_peers": 0, "preview_file": None}
        w.session.status = lambda: dict(
            _base, contiguous=0, tail_entry_ready=False,
            buffer=0.0, download_rate=0, file_progress=[])
        w._refresh_status()
        check(_stages[-1:] == [WAIT_DATA] and not _streamed,
              f"缺头数据 → 文案「等数据」且不放行（stages={_stages}）")
        w.session.status = lambda: dict(
            _base, contiguous=8 * 1024 * 1024, tail_entry_ready=False,
            buffer=1.0, download_rate=0, file_progress=[])
        w._refresh_status()
        check(_stages[-1:] == [WAIT_INDEX] and not _streamed,
              "头数据就绪、缺索引 → 文案「等索引块」且不放行")
        w.session.status = lambda: dict(
            _base, contiguous=8 * 1024 * 1024, tail_entry_ready=True,
            buffer=1.0, download_rate=0, file_progress=[])
        w._refresh_status()
        check(bool(_streamed) and w._pending_video is None,
              "数据+索引齐 → 放行开播（门控与文案解耦）")
    finally:
        w.preview.video.set_waiting_stage = _orig_stage
        w.preview.video.set_stream = _orig_setstream
        w.session.status = _orig_status
        w._pending_video = _orig_pending
        w.preview.reset()

    # ---- [4b-7] D4 Minor：顶层 listdir 失败语义 + closeEvent 单次读配置 ----
    print("\n[4b-7] D4 收尾：listdir 失败上抛→-1 / closeEvent 配置单读")
    # (a) cache_guard.clear_cache_contents 顶层 listdir 失败必须**上抛**
    # OSError（基线语义：os.listdir 裸调）——静默返回 0 会让用户以为
    # 「清理成功」而目录原封未动。调用方 _clear_preview_cache_now 已有
    # try→-1 兜底，UI 层据此提示清理失败。
    import ui.main_window as _mw2  # noqa: E402
    fail_dir = os.path.join(tmp, "cache_listdir_fail")
    os.makedirs(fail_dir, exist_ok=True)
    _mw2.ensure_cache_dir(fail_dir)     # 写受管标记，过守卫
    _orig_listdir = os.listdir
    def _boom_listdir(p):
        raise OSError(13, "Permission denied")
    os.listdir = _boom_listdir
    try:
        raised = False
        try:
            clear_cache_contents(fail_dir)
        except OSError:
            raised = True
        check(raised, "顶层 listdir 失败：clear_cache_contents 上抛 OSError"
                      "（基线语义，不静默返回 0）")
        orig_cd2 = w.cache_dir
        w.cache_dir = fail_dir
        try:
            rc = w._clear_preview_cache_now()
        finally:
            w.cache_dir = orig_cd2
        check(rc == -1, f"_clear_preview_cache_now 兜底 OSError→返回 -1（实得 {rc}）")
    finally:
        os.listdir = _orig_listdir

    # (b) closeEvent 的 clear_cache_on_exit 只读一次配置（双读合并；False 时
    # 也不再白算名单）。计数探针包住 cfg.get。
    # 阶段 A（plan A2）：close 已异步化**且幂等**——上面那次 close 已把 `w`
    # 真正关掉（_shutting_down 置真），对它的第二次 close 走幂等早退、根本
    # 不读配置。故本段另起一个窗口，测「真实首次关窗恰好读一次」。
    # 等待 _shutdown_done 的语义同旧实现：close() 返回前读完 → 现在读在
    # 后台 worker 里，必须等它跑完再收网（断言本意不变：**恰好一次**）。
    w_b = MainWindow()
    reads: list[str] = []
    _orig_get = w_b.cfg.get
    def _spy_get(key):
        if key == "clear_cache_on_exit":
            reads.append(key)
        return _orig_get(key)
    w_b.cfg.get = _spy_get
    _orig_sd = w_b.session._sess.shutdown
    _orig_srv = w_b.server.shutdown
    _orig_clear2 = w_b._clear_preview_cache_now
    w_b.session._sess.shutdown = lambda: None
    w_b.server.shutdown = lambda: None
    w_b._clear_preview_cache_now = lambda keep_dirs=(), log_key="": None
    try:
        w_b.show()                      # closeEvent 只在可见窗口上派发
        app.processEvents()
        w_b.close()
        _t0 = time.time()
        while (not getattr(w_b, "_shutdown_done", False)
               and time.time() - _t0 < 6):
            app.processEvents()
            time.sleep(0.02)
        app.processEvents()
    finally:
        w_b.cfg.get = _orig_get
        w_b.session._sess.shutdown = _orig_sd
        w_b.server.shutdown = _orig_srv
        w_b._clear_preview_cache_now = _orig_clear2
    check(len(reads) == 1,
          f"closeEvent 单次读 clear_cache_on_exit（实读 {len(reads)} 次）")

    # ---------------------------------------------------------------- [4c] 画廊隔离路径
    print("\n[4c] 画廊磁盘路径拼接（P0-2 回归）")
    import base64  # noqa: E402
    from core.models import ParseResult, TorrentFile  # noqa: E402
    from ui.gallery import GalleryWidget  # noqa: E402
    png_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    ih2 = "cd" * 20
    sub2 = f".preview/{ih2}"
    gcache = os.path.join(tmp, "cache_gallery")
    pic_rel = "root/pics/a.png"
    pic_path = os.path.join(gcache, *sub2.split("/"), *pic_rel.split("/"))
    os.makedirs(os.path.dirname(pic_path), exist_ok=True)
    with open(pic_path, "wb") as f:
        f.write(png_1x1)
    gf = TorrentFile(0, pic_rel, len(png_1x1), 0, 0, 0)
    gres = ParseResult(info_hash=ih2, name="root", total_size=len(png_1x1),
                       piece_size=1024, num_pieces=1, files=[gf],
                       source="magnet", cache_dir=gcache, save_subdir=sub2)
    gal = GalleryWidget()
    gal.set_result(gres)
    gal._show_index(0)
    check(gal._pixmap is not None and not gal._pixmap.isNull(),
          "画廊大图从隔离路径加载成功（save_subdir 已拼接）")
    check(gal.viewer.pixmap() is not None,
          "大图区已渲染 pixmap（不再停留「下载中…」）")

    # ---------------------------------------------------------------- [4d] 评审 P0 防回归
    print("\n[4d] 评审 P0 防回归（添加下载 / 下载页选中保持）")
    dlg = AddDownloadDialog({}, "防回归.bin", 1024)
    check(isinstance(dlg.priority(), int),
          "AddDownloadDialog.priority() 可调用并返回 int"
          "（P0-1：方法不再被同名 QSpinBox 控件遮蔽）")
    dlg.priority_spin.setValue(3)
    check(dlg.priority() == 3, "priority() 读回 spin 值（3）")

    def mk_task(h: str, name: str) -> dict:
        return {"info_hash": h, "name": name, "state": "DOWNLOADING",
                "progress": 0.1, "down_rate": 0, "eta": None,
                "priority": 1, "save_path": "", "selected_files": []}

    pane = DownloadsPane()
    pane.set_tasks([mk_task("a" * 40, "task-A"), mk_task("b" * 40, "task-B")])
    idx0 = pane.tree.model().index(0, COL_NAME)
    pane.tree.selectionModel().select(
        idx0, QItemSelectionModel.SelectionFlag.Select
        | QItemSelectionModel.SelectionFlag.Rows)
    pane.set_tasks([mk_task("a" * 40, "task-A"),
                    mk_task("b" * 40, "task-B")])   # 模拟 700ms 状态轮询刷新
    sel = pane.selected_task() or {}
    check(sel.get("name") == "task-A",
          "set_tasks 全量刷新后选中保持（P0-2：700ms 轮询不再清掉选中）")
    check("task-A" in pane.details.text(),
          "刷新后详情区显示选中任务（不再退回「选择任务查看详情」）")
    # ------------------------------------------------- [4b] 进度条 seek 防抖
    # 回归「拖动进度条后无法播放 + 进度条失灵」的 UI 侧成因：
    # ① 双重 seek —— sliderReleased 已跳转，释放引发的 valueChanged 又启动
    #    400ms 防抖，防抖回调再次 setPosition 并二次发射 seek_requested，
    #    第二次跳转会打断第一次的连接重建；
    # ② 滑块弹回 —— 跳转生效前（seek 异步、未下载区间还要等数据）广播的仍是
    #    旧位置，若程序性回写就会把用户拖到的位置覆盖回去。
    print("\n[4b] 播放器进度条：拖动只跳一次 / 跳转生效前不回写")
    from PySide6.QtMultimedia import QMediaPlayer
    from ui.preview_player import VideoPreviewWidget
    vp = VideoPreviewWidget()
    vp._size = 10 * 1024 * 1024                  # 10MB，供字节换算
    vp.slider.setRange(0, 100000)                # 模拟时长 100s
    vp.player.duration = lambda: 100000          # 无媒体源时伪造时长（仅换算用）
    seeks = []
    vp.seek_requested.connect(lambda b: seeks.append(b))
    vp.slider.setValue(50000)
    app.processEvents()
    vp.slider.sliderReleased.emit()              # 模拟拖动释放
    app.processEvents()
    check(len(seeks) == 1, f"拖动释放只触发一次 seek 请求（实际 {len(seeks)} 次）")
    check(vp._seek_target == 50000, "已记录跳转目标（生效前抑制回写）")
    vp._on_position(0)                            # 跳转未生效时的旧位置广播
    check(vp.slider.value() == 50000, "旧位置广播未把滑块弹回")
    _t0 = time.time()                             # 等防抖窗口结束（400ms）
    while time.time() - _t0 < 0.7:
        app.processEvents()
        time.sleep(0.05)
    check(len(seeks) == 1, f"防抖窗口结束后未重复 seek（实际 {len(seeks)} 次）")
    vp._on_position(50000)                        # position 追上目标
    check(vp._seek_target is None, "position 追上目标后解除回写抑制")
    vp._on_position(52000)
    check(vp.slider.value() == 52000, "解除抑制后滑块恢复跟随播放位置")
    vp.deleteLater()

    # ------------------------------------------------- [4c] 出错后续播位置
    # 回归：播放中出错 → 自动重试的 set_stream 会重新 setSource，从头开始播
    # （体感就是「拖动后突然从头重播、进度条失灵」）。必须带着出错位置续播。
    print("\n[4c] 播放出错后重试：回到出错位置而非从头")
    vp2 = VideoPreviewWidget()
    vp2._size = 10 * 1024 * 1024
    vp2.player.duration = lambda: 100000
    vp2.player.position = lambda: 42000
    vp2._seek_target = None
    vp2._on_player_error(QMediaPlayer.ResourceError, "模拟错误")
    check(vp2.take_resume_ms() == 42000, "出错时记录当前播放位置")
    vp2._mark_seek(60000)                        # 跳转尚未生效时出错
    vp2._on_player_error(QMediaPlayer.ResourceError, "模拟错误")
    check(vp2.take_resume_ms() == 60000, "跳转中出错优先记录跳转目标")
    check(vp2.take_resume_ms() is None, "取走后清空（不被下次重试误用）")
    seeks2 = []
    vp2.seek_requested.connect(lambda b: seeks2.append(b))
    vp2._resume_ms = 60000                       # 模拟 set_stream(resume_ms=…)
    vp2._on_media_status(QMediaPlayer.BufferedMedia)
    check(len(seeks2) == 1 and seeks2[0] == int(60000 / 100000 * vp2._size),
          f"续播时同步通知调度器（{seeks2}）")
    check(vp2._seek_target == 60000, "续播期间抑制回写，避免进度条弹回")
    check(vp2._resume_ms is None, "续播位置只应用一次")
    vp2.deleteLater()

    # ------------------------------------------------- [4d] 停止/换片/出错后的跳转隔离
    # A1：stop() 不清跳转状态 → 400ms 后把旧跳转作用到后续文件；
    # A2：错误态下 setPosition 无效，滑块必须禁用，否则「拖了没反应」；
    # A4：换片同理；A3：时长未广播时用滑块范围兜底换算。
    print("\n[4d] 停止·换片·出错后的跳转隔离与时长兜底")
    vp3 = VideoPreviewWidget()
    vp3._size = 10 * 1024 * 1024
    vp3.slider.setRange(0, 100000)
    vp3.player.duration = lambda: 100000
    seeks3 = []
    vp3.seek_requested.connect(lambda b: seeks3.append(b))
    vp3.slider.setValue(40000)
    vp3.stop()                                    # A1
    _t0 = time.time()
    while time.time() - _t0 < 0.7:
        app.processEvents()
        time.sleep(0.05)
    check(not seeks3, f"stop() 后不再发射跳转（{seeks3}）")
    vp3.slider.setRange(0, 100000)
    vp3.slider.setValue(60000)                    # A4：换片前的悬空点击
    app.processEvents()
    vp3.set_stream("file:///nonexistent_demo.mp4", "new.mp4", 5 * 1024 * 1024)
    _t0 = time.time()
    while time.time() - _t0 < 0.7:
        app.processEvents()
        time.sleep(0.05)
    check(not seeks3, f"换片后旧跳转不作用到新片（{seeks3}）")

    vp5 = VideoPreviewWidget()                    # A2
    check(vp5.slider.isEnabled(), "初始状态滑块可用")
    vp5._on_player_error(QMediaPlayer.ResourceError, "模拟错误")
    check(not vp5.slider.isEnabled(), "出错后滑块禁用（避免拖了没反应）")
    vp5.set_stream("file:///nonexistent_demo.mp4", "again.mp4", 8 * 1024 * 1024)
    check(vp5.slider.isEnabled(), "重新开播后滑块恢复可用")

    vp6 = VideoPreviewWidget()                    # A3
    vp6._size = 10 * 1024 * 1024
    vp6.player.duration = lambda: 0               # 时长尚未广播
    vp6.slider.setRange(0, 100000)
    seeks6 = []
    vp6.seek_requested.connect(lambda b: seeks6.append(b))
    vp6.slider.setValue(50000)
    vp6.slider.sliderReleased.emit()
    check(len(seeks6) == 1 and seeks6[0] == 5 * 1024 * 1024,
          f"时长未广播时用滑块范围兜底换算（{seeks6}）")
    vp3.deleteLater()
    vp5.deleteLater()
    vp6.deleteLater()

    # ------------------------------------------------- [4e] 预取/反馈/缓冲着色
    print("\n[4e] 拖动预取节流 · 跳转反馈文案 · 缓冲分段着色")
    from ui.preview_player import BufferedSlider
    vp7 = VideoPreviewWidget()
    vp7._size = 10 * 1024 * 1024
    vp7.slider.setRange(0, 100000)
    vp7.player.duration = lambda: 100000
    scrubs = []
    vp7.scrub_preview.connect(lambda b: scrubs.append(b))
    # 拖动中快速移动 6 次（间隔 <300ms 节流窗）→ 只应预取 1~2 次
    for v in (10000, 20000, 30000, 40000, 50000, 60000):
        vp7.slider.sliderMoved.emit(v)
        app.processEvents()
    check(1 <= len(scrubs) <= 2, f"拖动中预取按 300ms 节流（{len(scrubs)} 次）")
    check(scrubs and scrubs[0] == int(10000 / 100000 * vp7._size),
          f"预取换算为字节偏移（{scrubs[:1]}）")
    # 跳转反馈：跳转后、position 追上前，缓冲栏显示「跳转中」
    vp7.slider.setValue(70000)
    vp7.slider.sliderReleased.emit()
    vp7.update_buffer(0.1, 0)
    check("跳转中" in vp7.buffer_label.text(), "跳转期间缓冲栏显示跳转文案")
    vp7._on_position(70000)                    # position 追上目标
    vp7.update_buffer(0.5, 200 * 1024)
    check("跳转中" not in vp7.buffer_label.text(), "追上后恢复常规缓冲文案")

    bs = BufferedSlider()
    bs.setRange(0, 100)
    bs.set_segments([(0, 30 * 1024 * 1024), (60 * 1024 * 1024, 90 * 1024 * 1024)],
                    100 * 1024 * 1024)
    pm1 = bs.grab()                            # 触发 paintEvent（分段着色路径）
    check(not pm1.isNull() and bs._segments and
          abs(bs._segments[0][1] - 0.3) < 1e-9, "缓冲分段存储与绘制不崩溃")
    bs.clear_segments()
    pm2 = bs.grab()
    check(not pm2.isNull() and not bs._segments, "清空分段后可正常重绘")
    bs.deleteLater()
    vp7.deleteLater()

    # ------------------------------------------------- [4f] 画廊切换配额（A2 防回归）
    # 画廊切未下载图片直接 start_preview 会绕过 _enforce_cache_quota
    # （对比 _open_preview 视频路径）——连刷大量图片缓存无上限增长。
    # 修复语义：start_preview 前必须先触发配额；同文件重复点击走早退分支，
    # 不得触发配额（也不得重复 begin 预览）。
    print("\n[4f] 画廊切换未下载文件触发缓存配额（A2）")
    from core.models import TorrentFile as _TF  # noqa: E402
    # I2 整改（阶段 A 审查）：主窗口在 [4b-3] 已被**真正关窗**（_shutdown_started
    # 置真），而 _on_gallery_file 现带「停机窗口守卫」会首行早退。本段把「已关
    # 窗口」当**游离载体**复用来测画廊逻辑（不另起真会话/真流服务），故临时复位
    # 守卫标志——纯测试装置，不改产品语义（用例结束原样恢复）。
    _saved_started = w._shutdown_started
    w._shutdown_started = False
    _orig_start = w.session.start_preview
    _orig_preview_file = w._preview_file
    quota_calls, started = [], []
    w._enforce_cache_quota = lambda: quota_calls.append(1)
    w.session.start_preview = lambda f: started.append(f)
    try:
        gf1 = _TF(3, "root/pics/c.png", 4096, 0, 0, 0)
        w._on_gallery_file(gf1)
        check(len(quota_calls) == 1 and started == [gf1],
              f"画廊切未下载文件：配额先触发（quota {len(quota_calls)} 次，"
              f"start {len(started)} 次）")
        w._on_gallery_file(gf1)   # 同文件重复点击 → 早退分支
        check(len(quota_calls) == 1 and len(started) == 1,
              "同文件重复点击早退：不触发配额、不重复预览")
        gf2 = _TF(4, "root/pics/d.png", 4096, 0, 0, 0)
        w._on_gallery_file(gf2)
        check(len(quota_calls) == 2 and len(started) == 2,
              "切换到另一张图再次触发配额")
    finally:
        w.session.start_preview = _orig_start
        w._preview_file = _orig_preview_file
        w._shutdown_started = _saved_started   # 还原守卫标志（见本段开头注释）

    # ---------------------------------------------------------------- [4g] 双主题
    # 用户拍板：浅色为主（默认）+ 深色保留可切 + system 跟随；运行时可热切换。
    # 断言面：配置默认值 / 色板与 QSS 生成 / apply_theme 热切换（app.styleSheet
    # 真的换）/ 模块级常量同步（自绘控件读得到新色）/ 设置面板保存即生效。
    print("\n[4g] 双主题：色板 / QSS / 热切换 / 配置默认值（浅色为默认）")
    import ui.theme as _theme  # noqa: E402
    from ui.theme import (DARK, DEFAULT_MODE, LIGHT, THEME_MODES,  # noqa: E402
                          apply_theme, qss)
    from ui.settings_dialog import THEME_LABELS  # noqa: E402
    from ui.downloads_pane import _state_meta  # noqa: E402
    cfg_t = AppConfig()
    _orig_theme_all = {k: cfg_t.get(k) for k in DEFAULTS}
    try:
        check(DEFAULTS["ui_theme"] == "light" and DEFAULT_MODE == "light",
              "ui_theme 配置默认值 = light（浅色为默认主题，用户拍板）")
        check(list(THEME_MODES) == ["light", "dark", "system"],
              f"主题值域 light/dark/system（实得 {list(THEME_MODES)}）")

        # ① 色板驱动：qss(LIGHT) 含浅底色且不含深底色（反向同理）
        q_light, q_dark = qss(LIGHT), qss(DARK)
        check(LIGHT["bg"] in q_light and DARK["bg"] not in q_light,
              f"qss(LIGHT) 含浅底色 {LIGHT['bg']} 且不含深底色 {DARK['bg']}")
        check(DARK["bg"] in q_dark and LIGHT["bg"] not in q_dark,
              f"qss(DARK) 含深底色 {DARK['bg']} 且不含浅底色 {LIGHT['bg']}")

        # ② 热切换：apply_theme 重建样式表，app.styleSheet() 真的换掉
        check(apply_theme(app, "dark") == "dark",
              "apply_theme(app,'dark') 生效并返回实际主题名")
        ss_dark = app.styleSheet()
        check(DARK["bg"] in ss_dark and DARK["bg_panel"] in ss_dark
              and LIGHT["bg"] not in ss_dark,
              f"切深色后 app.styleSheet() 含深色 token"
              f"（{DARK['bg']} / {DARK['bg_panel']}）")
        check(_theme.BG == DARK["bg"]
              and _theme.SLIDER_SEGMENT == DARK["segment"],
              "模块级常量随激活色板同步（BG / SLIDER_SEGMENT = 深色板）")
        check(apply_theme(app, "light") == "light"
              and LIGHT["bg"] in app.styleSheet()
              and DARK["bg"] not in app.styleSheet(),
              "切回浅色：app.styleSheet() 含浅色 token 且深色 token 消失")
        check(_theme.BG == LIGHT["bg"]
              and _theme.SLIDER_SEGMENT == (0, 0, 0, 40),
              "浅色板常量同步（缓冲分段 = 半透明黑，深色板为半透明白）")
        # ③ system 档：真机拿不到 OS 深浅时回退 light；本机离屏 colorScheme() 恒
        #    Unknown → 用桩替换 QGuiApplication 正面验证「Dark→dark / 未知→light /
        #    异常→light」三分支（system_mode 内部是调用时 import，可用模块属性桩）。
        got_sys = apply_theme(app, "system")
        check(got_sys in ("light", "dark"),
              f"apply_theme(app,'system') 解析为实际深浅（实得 {got_sys!r}）")
        from PySide6.QtCore import Qt as _Qt  # noqa: E402
        _gui_mod = sys.modules["PySide6.QtGui"]
        _orig_gui_app = _gui_mod.QGuiApplication
        try:
            class _FakeHints:
                def __init__(self, scheme):
                    self._scheme = scheme

                def colorScheme(self):
                    return self._scheme

            for _name, _scheme, _want in (
                    ("Dark", _Qt.ColorScheme.Dark, "dark"),
                    ("Unknown", _Qt.ColorScheme.Unknown, "light")):
                _gui_mod.QGuiApplication = type(
                    "_G", (), {"styleHints": staticmethod(
                        lambda s=_scheme: _FakeHints(s))})
                check(_theme.resolve_mode("system") == _want,
                      f"system 档：colorScheme()={_name} → {_want}")
            _gui_mod.QGuiApplication = type(
                "_G", (), {"styleHints": staticmethod(
                    lambda: (_ for _ in ()).throw(RuntimeError("无 GUI 平台")))})
            check(_theme.resolve_mode("system") == "light",
                  "system 档：colorScheme() 取不到（异常）→ 回退 light")
        finally:
            _gui_mod.QGuiApplication = _orig_gui_app
        check(apply_theme(app, "garbage") == "light",
              "非法主题值回退 light（不抛错）")
        # ④ 自绘控件取色：下载页状态色现读激活色板（热切换后跟着变）
        apply_theme(app, "dark")
        check(_state_meta("PAUSED")[2] == DARK["text_dim"]
              and _state_meta("DOWNLOADING")[2] == DARK["accent"],
              "下载页状态色跟随激活色板（深色板取值）")
        apply_theme(app, "light")
        check(_state_meta("DOWNLOADING")[2] == LIGHT["accent"],
              "切回浅色后状态色随之更新（未固化导入期快照）")

        # ⑤ 设置面板：三项中英对照下拉 + **保存即热切换**
        cfg_t.set("ui_theme", "dark")
        cfg_t.set("proxy_type", "none")     # 避免代理校验弹模态（本段只测主题）
        dlg_t = _sd.SettingsDialog(cfg_t, w.cache_dir, on_clear_cache=None,
                                   parent=w)
        check([dlg_t.theme.itemData(i) for i in range(dlg_t.theme.count())]
              == list(THEME_MODES),
              "设置面板「界面主题」下拉值域逐字 = THEME_MODES（不发明取值）")
        check(set(THEME_LABELS) == set(THEME_MODES)
              and "Light" in THEME_LABELS["light"]
              and "Dark" in THEME_LABELS["dark"]
              and "System" in THEME_LABELS["system"],
              "下拉文案中英对照（浅色 Light / 深色 Dark / 跟随系统 System）")
        check(dlg_t.theme.currentData() == "dark",
              "下拉初值读自 ui_theme 配置（dark）")
        apply_theme(app, "light")           # 先从浅色起，验证 _save 切到深色
        dlg_t.theme.setCurrentIndex(dlg_t.theme.findData("dark"))
        dlg_t._save()
        check(AppConfig().get("ui_theme") == "dark",
              "_save 把选择回写 ui_theme=dark")
        check(DARK["bg"] in app.styleSheet(),
              "_save 立即重建样式表（保存即热切换，无需重启）")
        cfg_t.set("ui_theme", "garbage")
        dlg_g = _sd.SettingsDialog(cfg_t, w.cache_dir, on_clear_cache=None,
                                   parent=w)
        check(dlg_g.theme.currentIndex() == 0
              and dlg_g.theme.currentData() in THEME_MODES,
              "存量非法 ui_theme 回落 index 0 且 currentData 有效")
        dlg_g.deleteLater()
        dlg_t.deleteLater()
    finally:
        for k, v in _orig_theme_all.items():
            cfg_t.set(k, v)
        apply_theme(app, "light")     # 收尾恢复默认浅色（后续用例不受影响）

    # ---------------------------------------------------------------- [4h] 打磨
    # 用户反馈（附截图）：①下拉/微调箭头渲染成灰方块（QSS 三角 hack Qt 不支持）
    # ②按钮硬、无层次 ③Save/Cancel 英文 ④设置面板过高/无分组/路径只看到末尾。
    # 断言面：ChevronStyle 真被调用 + 箭头**形状与配色**像素证据（浅深双主题，
    # 证明颜色是绘制时现读色板，热切换跟随）→ 方案 A 成立；按钮 hover/pressed
    # 色阶像素；中文按钮；设置面板四分组 + 表单规格 + 路径光标在开头。
    print("\n[4h] 打磨：箭头雪佛龙 / 按钮层次 / 中文按钮 / 设置面板四分组")
    import ui.style as _style  # noqa: E402
    import ui.theme as theme  # noqa: E402
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt  # noqa: E402
    from PySide6.QtGui import QColor, QMouseEvent  # noqa: E402
    from PySide6.QtTest import QTest  # noqa: E402
    from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialogButtonBox,  # noqa: E402
                                   QGridLayout, QGroupBox, QLineEdit,
                                   QProxyStyle, QPushButton, QScrollArea,
                                   QSpinBox, QStyle, QStyleOptionComboBox,
                                   QStyleOptionSpinBox, QVBoxLayout, QWidget)

    def _dist(c, hexs) -> int:
        """QColor 与目标色值的 |ΔR|+|ΔG|+|ΔB|（0 = 完全一致）。"""
        w_ = QColor(hexs)
        return (abs(c.red() - w_.red()) + abs(c.green() - w_.green())
                + abs(c.blue() - w_.blue()))

    def _eq_pts(sub, hexs, x0, x1, y0, y1, tol=2):
        """区域里与目标色值距离 <= tol 的像素点（用于「某色真的被画出来」断言）。"""
        out = []
        for y in range(y0, y1):
            for x in range(x0, x1):
                if _dist(sub.pixelColor(x, y), hexs) <= tol:
                    out.append((x, y))
        return out

    # ---- ① 色板/按钮 QSS 条款（不写渐变/阴影：Qt QSS 不支持 box-shadow）----
    _q_light = qss(LIGHT)
    _q_dark = qss(DARK)
    check("bg_pressed" in LIGHT and "bg_pressed" in DARK
          and LIGHT["bg_pressed"] != LIGHT["bg_hover"]
          and DARK["bg_pressed"] != DARK["bg_hover"],
          f"新增色板键 bg_pressed（按下比 hover 深一档：浅 "
          f"{LIGHT['bg_pressed']} / 深 {DARK['bg_pressed']}）")
    for _tag, _pal in (("浅", LIGHT), ("深", DARK)):
        _contrast = (_dist(QColor(_pal["alt_row"]), _pal["bg_panel"]))
        check(_contrast >= 25,
              f"{_tag}色 alt_row 与面板底可辨（位差 {_contrast} ≥ 25，旧值仅 12/15）")
    check(f"QPushButton:pressed {{ background: {LIGHT['bg_pressed']};" in _q_light
          and f"QPushButton:hover {{ background: {LIGHT['bg_hover']}; "
              f"border-color: {LIGHT['border_strong']};" in _q_light,
          "次级按钮 hover=bg_hover+border_strong / pressed=bg_pressed（色阶层次）")
    check(f"QPushButton:focus {{ border: 1px solid {LIGHT['accent']};" in _q_light,
          "次级按钮 :focus 边框转 accent（键盘可见性）")
    check(f"QPushButton#primary {{ background: {LIGHT['accent']}; border: none;"
          in _q_light
          and f"QPushButton#primary:pressed {{ background: "
              f"{LIGHT['accent_pressed']};" in _q_light
          and f"QPushButton#primary:disabled {{ background: "
              f"{LIGHT['bg_hover']}; color: {LIGHT['text_dim']};" in _q_light,
          "主按钮 accent 实底无边框 + hover/pressed/disabled 三档")
    check(f"QPushButton#ghost {{ background: transparent; border: none;" in _q_light
          and f"QPushButton#ghost:hover {{ background: {LIGHT['bg_hover']}; "
              f"color: {LIGHT['text']};" in _q_light,
          "幽灵按钮：透明底无边框，hover 才有底")
    check("qlineargradient" not in _q_light and "box-shadow" not in _q_light,
          "不用渐变/阴影（靠圆角 + 柔和边框 + hover/pressed 色阶做平滑感）")

    # ---- ② 箭头/分区改由 **QSS 子控件 + SVG 资源** 承担 ----
    #    上一轮用 QProxyStyle 自算几何（分区矩形 / 对半分 / 分隔线 / 雪佛龙坐标）
    #    实测算错位：分区只画出右上角一小块、箭头不垂直居中、微调的分隔线横穿
    #    到输入框外。本轮**作废自绘几何**，改回 Qt 标准做法（几何由样式引擎算）。
    #    断言面：① QSS 里确有 drop-down / up-button / down-button / *-arrow 规则
    #    且规格齐全；② ui/style.py 里再没有任何箭头/分区绘制；③ 资源文件与
    #    asset_path() 解析（打包自检的前置）。
    def _flat(text):
        return " ".join(text.split())

    def _no_comments(text):
        """去掉 /* ... */ 注释：断言只针对**规则本身**，注释里引用的反例
        （如「实测 height: 50% 被当 50px」）不该把断言自己绊倒。"""
        out, i, n = [], 0, len(text)
        while i < n:
            if text.startswith("/*", i):
                j = text.find("*/", i + 2)
                i = n if j < 0 else j + 2
                continue
            out.append(text[i])
            i += 1
        return "".join(out)

    _qf = _flat(_no_comments(_q_light))    # 浅色 QSS 规则压成单行，便于逐字符断言
    _qfd = _flat(_no_comments(_q_dark))
    _R = theme.R_MD
    _dn_l = theme.asset_path("chevron-down-light.svg").replace("\\", "/")
    _up_l = theme.asset_path("chevron-up-light.svg").replace("\\", "/")
    _dn_d = theme.asset_path("chevron-down-dark.svg").replace("\\", "/")

    check(f"QComboBox::drop-down {{ subcontrol-origin: border; "
          f"subcontrol-position: top right; width: 26px; "
          f"height: {theme.FIELD_H}px; "
          f"background: {LIGHT['bg_compartment']}; "
          f"border-left: 1px solid {LIGHT['border']}; "
          f"border-top-right-radius: {_R}px; "
          f"border-bottom-right-radius: {_R}px; }}" in _qf
          and f"QGroupBox QComboBox::drop-down {{ height: {theme.FIELD_H_GB}px; }}" in _qf,
          f"下拉分区：QSS ::drop-down 贴右上 / 宽 26px / **height 显式写死 "
          f"{theme.FIELD_H}px（分组框内 {theme.FIELD_H_GB}px）= 控件总高** / "
          f"bg_compartment / 左边 1px 分隔线 / 右侧两角圆角 8")
    check(theme.FIELD_H == theme.FIELD_CONTENT_H + 2 * theme.FIELD_PAD_V + 2
          and theme.FIELD_H_GB == theme.FIELD_CONTENT_H_GB + 2 * theme.FIELD_PAD_V_GB + 2,
          f"分区高度常量由「内容高 + 上下 padding + 上下 border」推出"
          f"（独立 {theme.FIELD_CONTENT_H}+{theme.FIELD_PAD_V}×2+2={theme.FIELD_H}，"
          f"分组框 {theme.FIELD_CONTENT_H_GB}+{theme.FIELD_PAD_V_GB}×2+2="
          f"{theme.FIELD_H_GB}）")
    check(f"QComboBox {{ max-height: {theme.FIELD_CONTENT_H}px; }}" in _qf
          and f"QSpinBox {{ min-height: {theme.FIELD_CONTENT_H}px; "
              f"max-height: {theme.FIELD_CONTENT_H}px; }}" in _qf
          and f"QGroupBox QComboBox {{ max-height: {theme.FIELD_CONTENT_H_GB}px; }}" in _qf
          and f"QGroupBox QSpinBox {{ min-height: {theme.FIELD_CONTENT_H_GB}px;" in _qf
          and theme.FIELD_H % 2 == 0 and theme.FIELD_H_GB % 2 == 0,
          f"输入类总高**钉死**（min == max）且与下拉同高：独立 {theme.FIELD_H}px / "
          f"分组框 {theme.FIELD_H_GB}px，均为**偶数** → 基样式对半分不留 1px 接缝")

    # 「Qt 样式表不认百分比长度」的**实测**依据（不是理论推断）：临时追加一条
    # 百分比高度规则，看 QStyleSheetStyle 真的给出什么高度。这正是上一轮
    # `height: 50%` 把分隔线顶出控件外的根因。
    _pct_holder = QWidget()
    _pct_holder.setFixedSize(320, 120)
    _pct_lay = QVBoxLayout(_pct_holder)
    _pct_combo = QComboBox()
    _pct_combo.addItem("A")
    _pct_lay.addWidget(_pct_combo)
    _pct_holder.show()
    app.processEvents()
    _pct = {}
    for _spec in ("100%", "50%"):
        app.setStyleSheet(theme.QSS
                          + f"QComboBox::drop-down {{ height: {_spec}; }}")
        app.processEvents()
        _ocp = QStyleOptionComboBox()
        _pct_combo.initStyleOption(_ocp)
        _pct[_spec] = QApplication.style().subControlRect(
            QStyle.CC_ComboBox, _ocp, QStyle.SC_ComboBoxArrow, _pct_combo)
    app.setStyleSheet(theme.QSS)
    app.processEvents()
    _ocp = QStyleOptionComboBox()
    _pct_combo.initStyleOption(_ocp)
    _dd_std = QApplication.style().subControlRect(
        QStyle.CC_ComboBox, _ocp, QStyle.SC_ComboBoxArrow, _pct_combo)
    check(_pct["100%"].height() == 100 and _pct["50%"].height() == 50
          and _pct["100%"].height() != _pct_combo.height(),
          f"实测：Qt 把百分比长度当 **px** 吃掉（height:100% → "
          f"{_pct['100%'].height()}px、50% → {_pct['50%'].height()}px，控件高 "
          f"{_pct_combo.height()}px）→ 分区高度只能写绝对像素")
    check(_pct_combo.height() == theme.FIELD_H
          and _dd_std.height() == theme.FIELD_H
          and _dd_std.top() == 0 and _dd_std.bottom() == _pct_combo.height() - 1,
          f"独立下拉：控件总高 {_pct_combo.height()}px == 声明分区高 "
          f"{theme.FIELD_H}px，分区矩形 {_dd_std.getRect()} **满高**"
          f"（顶 {_dd_std.top()} vs 控件顶+1 容差内；底 {_dd_std.bottom()} == "
          f"控件底 {_pct_combo.height() - 1}）")
    _pct_holder.close()
    _pct_holder.deleteLater()
    app.processEvents()
    check(f'QComboBox::down-arrow {{ image: url("{_dn_l}"); '
          f"width: 12px; height: 12px; }}" in _qf,
          f"下拉箭头：::down-arrow image 指向 SVG 资源（不是 CSS 三角 hack）")
    check(f"QSpinBox::up-button {{ subcontrol-origin: border; "
          f"subcontrol-position: top right; width: 26px; "
          f"background: {LIGHT['bg_compartment']}; "
          f"border-left: 1px solid {LIGHT['border']}; "
          f"border-bottom: 1px solid {LIGHT['border']}; "
          f"border-top-right-radius: {_R}px; }}" in _qf
          and f"QSpinBox::down-button {{ subcontrol-origin: border; "
              f"subcontrol-position: bottom right; width: 26px; "
              f"background: {LIGHT['bg_compartment']}; "
              f"border-left: 1px solid {LIGHT['border']}; "
              f"border-bottom-right-radius: {_R}px; }}" in _qf,
          "微调分区：::up-button / ::down-button **同宽 26px + 同底 + 同圆角**"
          "（与下拉完全一致），中间以 1px border 分隔")
    check(f'QSpinBox::up-arrow {{ image: url("{_up_l}"); width: 12px; height: 12px; }}' in _qf
          and f'QSpinBox::down-arrow {{ image: url("{_dn_l}"); width: 12px; height: 12px; }}' in _qf,
          "微调箭头：up/down-arrow 指向对应 SVG，尺寸与下拉**同为 12×12**")
    check(f"QComboBox::drop-down:hover {{ background: {LIGHT['bg_hover']}; }}" in _qf
          and f"QSpinBox::up-button:hover {{ background: {LIGHT['bg_hover']}; }}" in _qf
          and f"QSpinBox::down-button:hover {{ background: {LIGHT['bg_hover']}; }}" in _qf,
          "三处分区 hover 均转 bg_hover（悬停反馈一致）")
    check("height: 50%" not in _qf and "height: 100%" not in _qf
          and "%" not in _qf.split("QComboBox::drop-down")[1].split("}")[0],
          "分区高度**不写百分比**（Qt 样式表把 100% 当 100px：见下方实测断言），"
          "对半分交给基样式")
    check("chevron-down-light.svg" in _qf and "chevron-up-light.svg" in _qf
          and "chevron-down-dark.svg" in _qfd and "chevron-up-dark.svg" in _qfd
          and "chevron-down-dark.svg" not in _qf
          and "chevron-down-light.svg" not in _qfd
          and "dark" not in _dn_l and "dark" in _dn_d,
          "浅/深两套 QSS 各自指向对应 SVG（浅→-light、深→-dark，不串色）")

    # ui/style.py 已删净箭头/分区绘制（源码级：这些名字一个都不许留）
    _style_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "ui", "style.py"), encoding="utf-8").read()
    _dead = [t for t in ("PE_IndicatorArrow", "PE_IndicatorSpin", "COMPARTMENT_W",
                         "_compartment_rect", "_half_rect", "_paint_compartment",
                         "_paint_divider", "_draw_chevron", "_chevron(", "CHEVRON_W",
                         "_paint_corner_cleanup")
             if t in _style_src]
    check(not _dead,
          f"ui/style.py 已删净箭头/分区绘制（残留 {_dead or '无'}）")
    check(issubclass(_style.ChevronStyle, QProxyStyle)
          and callable(getattr(_style, "install", None))
          and not hasattr(_style, "COMPARTMENT_W")
          and not hasattr(_style, "CHEVRON_W")
          and _style.INDICATOR_SIZE == 16,
          "ui/style.py 只剩复选框/单选指示器自绘（ChevronStyle + install(app)，"
          "16×16 指示器；分区常量已随绘制一起删除）")

    # 资源文件（打包 datas 的前置）+ asset_path 缺文件时的告警钩子
    check(len(theme.ASSET_FILES) == 4
          and all(os.path.isfile(theme.asset_path(n)) for n in theme.ASSET_FILES),
          f"ui/assets 4 个箭头资源齐备（{', '.join(theme.ASSET_FILES)}）")
    _svg_facts = {}
    for _n in theme.ASSET_FILES:
        _txt = open(theme.asset_path(_n), encoding="utf-8").read()
        _svg_facts[_n] = ('viewBox="0 0 12 12"' in _txt
                          and 'stroke-width="1.6"' in _txt
                          and 'stroke-linecap="round"' in _txt
                          and 'fill="none"' in _txt and _txt.count("<path") == 1
                          and (LIGHT["text_muted"] in _txt if "light" in _n
                               else DARK["text_muted"] in _txt))
    check(all(_svg_facts.values()),
          f"4 个 SVG 规格：12×12 视窗 / 无填充 / stroke 1.6 圆头 / 描边 = 对应主题 "
          f"text_muted（浅 {LIGHT['text_muted']}、深 {DARK['text_muted']}）")
    _warns = []
    _orig_lw = theme.log_warning
    theme.log_warning = lambda tag, msg: _warns.append((tag, msg))
    try:
        _miss = theme.asset_path("definitely-missing.svg")
    finally:
        theme.log_warning = _orig_lw
    check(len(_warns) == 1 and _warns[0][0] == "ui.theme.asset_path"
          and not os.path.isfile(_miss),
          f"asset_path 缺文件时 log_warning（打包后资源自检靠它）：{_warns[:1]}")

    # ---- ③ 箭头像素证据（QSS 子控件 + SVG 渲染）：居中 / 尺寸 / 颜色 ----
    #    分区矩形取样式引擎自己的 subControlRect（QSS 的几何就是它算的），再在
    #    渲染图上逐像素核对：分区底 == bg_compartment、箭头质心落在分区中心
    #    ±2px、下拉与微调的箭头包围盒**逐像素等大**、描边色 == 色板 text_muted。
    def _compartment_rects(combo, spin):
        st = QApplication.style()
        oc = QStyleOptionComboBox()
        combo.initStyleOption(oc)
        os_ = QStyleOptionSpinBox()
        spin.initStyleOption(os_)
        return (st.subControlRect(QStyle.CC_ComboBox, oc,
                                  QStyle.SC_ComboBoxArrow, combo),
                st.subControlRect(QStyle.CC_SpinBox, os_,
                                  QStyle.SC_SpinBoxUp, spin),
                st.subControlRect(QStyle.CC_SpinBox, os_,
                                  QStyle.SC_SpinBoxDown, spin))

    def _ink(sub, rect, ref, y0=None, y1=None):
        """分区矩形里的「墨迹」=（与分区底距离 > 120）的像素，即箭头本身。"""
        pts = []
        for y in range(rect.top() if y0 is None else y0,
                       (rect.bottom() + 1) if y1 is None else y1):
            for x in range(rect.left(), rect.right() + 1):
                if _dist(sub.pixelColor(x, y), ref) > 120:
                    pts.append((x, y))
        return pts

    def _bbox(pts):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return (min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1)

    def _arrow_probe(mode_name):
        theme.apply_theme(app, mode_name)
        pal = theme.current_palette()
        holder = QWidget()
        holder.setFixedSize(320, 260)
        lay = QVBoxLayout(holder)
        # 放进 QGroupBox：与设置面板同上下文（微调框的偶数高度规则由
        # `QGroupBox QSpinBox` 命中，上下半才严丝合缝无接缝）
        box = QGroupBox("外观")
        box_lay = QVBoxLayout(box)
        combo = QComboBox()
        combo.addItems(["浅色 Light（默认）", "深色 Dark"])
        box_lay.addWidget(combo)
        spin = QSpinBox()
        spin.setRange(0, 600)
        spin.setValue(600)
        box_lay.addWidget(spin)
        lay.addWidget(box)
        holder.show()
        app.processEvents()
        img = holder.grab().toImage()
        co = combo.mapTo(holder, QPoint(0, 0))
        so = spin.mapTo(holder, QPoint(0, 0))
        csub = img.copy(co.x(), co.y(), combo.width(), combo.height())
        ssub = img.copy(so.x(), so.y(), spin.width(), spin.height())
        c_rect, up_rect, dn_rect = _compartment_rects(combo, spin)
        mid = spin.height() // 2
        out = (csub, ssub, c_rect, up_rect, dn_rect,
               _ink(csub, c_rect, pal["bg_compartment"]),
               _ink(ssub, up_rect, pal["bg_compartment"], up_rect.top(), mid),
               _ink(ssub, dn_rect, pal["bg_compartment"], mid, spin.height()),
               pal)
        holder.close()
        holder.deleteLater()
        app.processEvents()
        return out

    _arrow_color = {}
    _arrow_box = {}
    for _mode in ("light", "dark"):
        (_csub, _ssub, _crect, _uprect, _dnrect,
         _cink, _uink, _dink, _pal) = _arrow_probe(_mode)
        check(_dist(_csub.pixelColor(_crect.left() + 3, _crect.top() + 3),
                    _pal["bg_compartment"]) == 0
              and _dist(_ssub.pixelColor(_uprect.left() + 3, _uprect.top() + 3),
                        _pal["bg_compartment"]) == 0
              and _dist(_ssub.pixelColor(_dnrect.left() + 3, _dnrect.bottom() - 3),
                        _pal["bg_compartment"]) == 0,
              f"[{_mode}] 下拉分区与微调上下半分区底 == bg_compartment "
              f"（{_pal['bg_compartment']}）")
        check(_crect.right() == _csub.width() - 1
              and _uprect.right() == _dnrect.right() == _ssub.width() - 1,
              f"[{_mode}] 分区右边缘 == 控件右边缘 - 1px 边框"
              f"（下拉 {_crect.right()}/{_csub.width() - 1}，"
              f"微调 {_uprect.right()}/{_ssub.width() - 1}）")
        check(_crect.width() == _uprect.width() == _dnrect.width()
              and _crect.left() == _uprect.left() == _dnrect.left()
              and _uprect.bottom() + 1 == _dnrect.top(),
              f"[{_mode}] 下拉与微调分区**同宽同左缘**（各 {_crect.width()}px = QSS 声明 "
              f"26px + 1px 左分隔线；左缘 {_crect.left()}），微调上下半**严丝合缝**"
              f"（up {_uprect.top()}..{_uprect.bottom()} / down {_dnrect.top()}.."
              f"{_dnrect.bottom()}，无 1px 接缝）")
        check(len(_cink) >= 6 and len(_uink) >= 6 and len(_dink) >= 6,
              f"[{_mode}] 三处箭头都有真实绘制像素"
              f"（下拉 {len(_cink)} / 微调↑ {len(_uink)} / 微调↓ {len(_dink)}）")
        for _nm, _pts, _rect in (("下拉", _cink, _crect),
                                 ("微调↑", _uink, _uprect),
                                 ("微调↓", _dink, _dnrect)):
            if not _pts:
                continue
            _cx = sum(p[0] for p in _pts) / len(_pts)
            _cy = sum(p[1] for p in _pts) / len(_pts)
            _ex = (_rect.left() + _rect.right()) / 2.0
            _ey = (_rect.top() + _rect.bottom()) / 2.0
            check(abs(_cx - _ex) <= 2 and abs(_cy - _ey) <= 2,
                  f"[{_mode}] {_nm}箭头居中于分区（质心 {_cx:.1f},{_cy:.1f} vs "
                  f"分区中心 {_ex:.1f},{_ey:.1f}，Δ ≤ 2px）")
        _arrow_box[_mode] = (_bbox(_cink), _bbox(_uink), _bbox(_dink))
        _best = min(_cink, key=lambda p: _dist(_csub.pixelColor(*p),
                                               _pal["text_muted"]))
        _arrow_color[_mode] = _csub.pixelColor(*_best).name()
        check(_dist(_csub.pixelColor(*_best), _pal["text_muted"]) == 0
              and sum(1 for p in _cink
                      if _dist(_csub.pixelColor(*p), _pal["text_muted"]) <= 2) >= 3,
              f"[{_mode}] 箭头描边色 == 色板 text_muted"
              f"（{_csub.pixelColor(*_best).name()}，核心 {len(_cink)} 像素中有 "
              f"{sum(1 for p in _cink if _dist(_csub.pixelColor(*p), _pal['text_muted']) <= 2)}"
              f" 个完全同色）")
        _rows = {}
        for _x, _y in _cink:
            _rows.setdefault(_y, []).append(_x)
        _ord = sorted(_rows)
        _span = {y: max(_rows[y]) - min(_rows[y]) + 1 for y in _ord}
        check(len(_ord) >= 3
              and all(len(_rows[y]) < _span[y] for y in _ord[:2])
              and _span[_ord[-1]] <= 2 and _span[_ord[0]] >= 6,
              f"[{_mode}] 雪佛龙是**空心折线**（首行跨度 {_span[_ord[0]]} 只有 "
              f"{len(_rows[_ord[0]])} 个像素 → 两臂分离；末行跨度 {_span[_ord[-1]]} "
              f"收成顶点）——实心三角/方块不会有缝")
    check(_arrow_color.get("light") == LIGHT["text_muted"]
          and _arrow_color.get("dark") == DARK["text_muted"],
          f"箭头色随主题：浅 {_arrow_color.get('light')}（={LIGHT['text_muted']}）/ "
          f"深 {_arrow_color.get('dark')}（={DARK['text_muted']}）")
    check(_arrow_box["light"][0][2:] == _arrow_box["light"][1][2:]
          == _arrow_box["light"][2][2:]
          and _arrow_box["dark"][0][2:] == _arrow_box["dark"][1][2:],
          f"下拉与微调箭头包围盒尺寸**逐像素相等**（下拉 "
          f"{_arrow_box['light'][0][2:]} / 微调↑ {_arrow_box['light'][1][2:]} / "
          f"微调↓ {_arrow_box['light'][2][2:]}）——上一轮 12/10 混用正是"
          f"「两套样子不一致」的来源")

    # ---- ④ 按钮 hover/pressed 真上色（QTest 真鼠标移动 + QMouseEvent 按下）----
    def _btn_probe(mode_name, object_name=None):
        theme.apply_theme(app, mode_name)
        pal = theme.current_palette()
        holder = QWidget()
        holder.setFixedSize(300, 120)
        lay = QVBoxLayout(holder)
        btn = QPushButton("测试按钮")
        if object_name:
            btn.setObjectName(object_name)
        lay.addWidget(btn)
        holder.show()
        app.processEvents()
        # 先把指针挪开按钮：连续多个持有点位相同时 Qt 不再重发移动事件 →
        # hover 不生效（实测：第二个起就丢），必须先离开再进入。
        QTest.mouseMove(holder, QPoint(2, 2))
        app.processEvents()
        normal = btn.grab().toImage().pixelColor(8, 4)
        QTest.mouseMove(btn, QPoint(btn.width() // 2, btn.height() // 2))
        app.processEvents()
        hover = btn.grab().toImage().pixelColor(8, 4)
        _mk = lambda t, btns: QMouseEvent(  # noqa: E731
            t, QPointF(10, 10), QPointF(10, 10), QPointF(10, 10),
            Qt.LeftButton, btns, Qt.NoModifier)
        QApplication.sendEvent(btn, _mk(QEvent.Type.MouseButtonPress,
                                        Qt.LeftButton))
        app.processEvents()
        pressed = btn.grab().toImage().pixelColor(8, 4)
        QApplication.sendEvent(btn, _mk(QEvent.Type.MouseButtonRelease,
                                        Qt.NoButton))
        holder.close()
        holder.deleteLater()
        app.processEvents()
        return normal, hover, pressed, pal

    for _mode in ("light", "dark"):
        _n, _h, _p, _pal = _btn_probe(_mode)
        check(_dist(_h, _pal["bg_hover"]) == 0,
              f"[{_mode}] hover 底色 = bg_hover（{_h.name()}）")
        check(_dist(_p, _pal["bg_pressed"]) == 0,
              f"[{_mode}] pressed 底色 = bg_pressed（{_p.name()}）")
        check(_dist(_n, _pal["bg_input"]) == 0,
              f"[{_mode}] 常态底色 = bg_input（{_n.name()}）")
    theme.apply_theme(app, "light")
    _n, _h, _p, _pal = _btn_probe("light", object_name="primary")
    check(_dist(_p, _pal["accent_pressed"]) == 0
          and _dist(_h, _pal["accent_hover"]) == 0,
          f"主按钮 hover/pressed = accent_hover/accent_pressed"
          f"（{_h.name()} / {_p.name()}），实底无边框")

    # ---- ⑤ 中文按钮 + 设置面板四分组 + 表单规格 + 路径光标在开头 ----
    cfg_p = AppConfig()
    _orig_p = {k: cfg_p.get(k) for k in DEFAULTS}
    cfg_p.set("proxy_type", "none")     # 避免代理校验弹模态
    try:
        dlg = _sd.SettingsDialog(cfg_p, w.cache_dir, on_clear_cache=None,
                                 parent=w)
        dlg.show()
        app.processEvents()
        check(dlg.windowTitle() == "设置" and dlg.minimumWidth() == 600,
              f"设置对话框：标题「设置」+ 最小宽 600（实得 {dlg.minimumWidth()}）")
        check(dlg.btn_save is not None and dlg.btn_cancel is not None
              and dlg.btn_save.text() == "保存" and dlg.btn_cancel.text() == "取消",
              "保存/取消按钮中文（改造前 Save/Cancel）")
        check(dlg.btn_save.objectName() == "primary",
              "「保存」是主按钮（#primary 样式）")
        _add = AddDownloadDialog(AppConfig(), "演示", 1024, default_save_dir=tmp)
        _abb = _add.findChild(QDialogButtonBox)
        check(_abb.button(QDialogButtonBox.Save).text() == "保存"
              and _abb.button(QDialogButtonBox.Cancel).text() == "取消",
              "「添加下载任务」对话框按钮同步中文（全界面文案统一）")
        _add.deleteLater()

        _groups = dlg.findChildren(QGroupBox)
        check([g.title() for g in _groups] == ["界面", "网络与代理", "缓存与预览", "下载"],
              f"设置面板分四组（实得 {[g.title() for g in _groups]}）")
        _grids = [g.layout() for g in _groups]
        check(all(isinstance(f, QGridLayout) for f in _grids),
              "每组一个 QGridLayout（v3：标签列/控件列两列，跨列行不再留空标签列）")
        check(all(f.spacing() == 8 and f.contentsMargins().left() == 12
                  and f.contentsMargins().top() == 12
                  and f.contentsMargins().right() == 12
                  and f.contentsMargins().bottom() == 12 for f in _grids),
              "表单规格：spacing=8（SP_SM）/ contentsMargins=(12,12,12,12)（SP_MD）")
        check(all(f.columnMinimumWidth(0) == 112 and f.columnStretch(1) == 1
                  for f in _grids),
              "表单规格：标签列最小宽 112（四组同值）+ 控件列 stretch=1")
        _labb = [(f.itemAtPosition(0, 0).widget().width(),
                  f.itemAtPosition(0, 0).widget().x()) for f in _grids]
        check(len(set(_labb)) == 1 and _labb[0][0] == 112,
              f"四组标签列**实测**等宽（宽/起点 {_labb}）——v2 每组各算各的，参差不齐")
        check(all(f.itemAtPosition(0, 0).widget().alignment()
                  == (Qt.AlignRight | Qt.AlignVCenter) for f in _grids),
              "表单规格：标签右对齐垂直居中")
        check(all(f.itemAtPosition(0, 1) is not None
                  and f.itemAtPosition(0, 1).widget() is not None for f in _grids),
              "表单规格：控件落在第 1 列（四组列位一致）")
        # 组内上下内边距 / 卡片间距 = 12（token，非魔法数）
        check(dlg._content.layout().spacing() == 12,
              f"四组卡片间距统一 12px（实得 {dlg._content.layout().spacing()}）")
        check("QGroupBox::title" in qss(LIGHT)
              and "font-weight: 600" in qss(LIGHT).split("QGroupBox::title")[1].split("}")[0],
              "分组标题字重 600（QGroupBox::title font-weight）")

        check(isinstance(dlg._scroll, QScrollArea)
              and dlg.height() <= max(360, int(
                  QApplication.primaryScreen().availableGeometry().height() * 0.92)) + 1,
              f"内容超出屏幕时由 QScrollArea 承载（窗口高 {dlg.height()}，"
              f"内容自然高 {dlg.content_height()}）")
        dlg.resize(660, 420)
        app.processEvents()
        check(dlg._scroll.verticalScrollBar().maximum() > 0,
              "窗口被压小 → 出现垂直滚动条（内容不被截断）")
        dlg.resize(660, dlg.full_height())
        app.processEvents()
        check(dlg._scroll.verticalScrollBar().maximum() <= 8,
              f"全展开（{dlg.height()}px）→ 无需滚动"
              f"（滚动条上限 {dlg._scroll.verticalScrollBar().maximum()}）")
        check(dlg.full_height() < 1400,
              f"设置面板总高 {dlg.full_height()}px < 旧版 ~1440px（用户截图）")
        _bb = dlg.findChild(QDialogButtonBox)
        _tl = _bb.mapTo(dlg, QPoint(0, 0))
        check(dlg.width() - (_tl.x() + _bb.width()) <= 24
              and _tl.y() > dlg.height() * 0.7,
              f"保存/取消在右下角（右边距 {dlg.width() - (_tl.x() + _bb.width())}px，"
              f"y={_tl.y()}/{dlg.height()}）")

        check(dlg.cache_edit.cursorPosition() == 0
              and dlg.download_edit.cursorPosition() == 0
              and dlg.cache_edit.text() != "",
              f"路径类输入框光标停在开头（光标 {dlg.cache_edit.cursorPosition()}，"
              f"文本 {dlg.cache_edit.text()!r}）")

        # ------------------------------------------------------------ [4h-v3]
        # 用户反馈「设置面板还是没改好」后按控制方定位的三条根因修完的**像素/
        # 属性证据**：①复选框行灰带（QCheckBox 漏 transparent）②微调/下拉右侧
        # 分区与箭头（QProxyStyle 绘制，QSS 不能碰）③复选框选中色跟主题；
        # 外加四条真实瑕疵：标签列宽对齐 / 直连禁用代理字段 / 密码掩码 /
        # 路径 tooltip。全部为运行时断言，缺一即 FAIL。
        print("\n[4h-v3] 设置面板：灰带 / 分区 / 指示器 / 联动 / 掩码 / tooltip")

        # ① 复选框行背景 == 卡片底（浅色必须是 #ffffff，不是窗口灰底 #f4f5f7）
        for _mode in ("light", "dark"):
            theme.apply_theme(app, _mode)
            app.processEvents()
            _pal = theme.current_palette()
            _img = dlg.grab().toImage()
            _bad = []
            for _cb in (dlg.proxy_peer, dlg.clear_on_exit, dlg.seed_after,
                        dlg.logging_enabled):
                _o = _cb.mapTo(dlg, QPoint(0, 0))
                _y = _o.y() + _cb.height() // 2
                for _fx in (0.78, 0.9, 0.98):
                    _px = _img.pixelColor(_o.x() + int(_cb.width() * _fx), _y)
                    if _dist(_px, _pal["bg_panel"]) != 0:
                        _bad.append((_cb.text()[:8], _fx, _px.name()))
            check(not _bad,
                  f"[{_mode}] 复选框行底 == 卡片底 {_pal['bg_panel']}"
                  f"（灰带根因：QCheckBox 曾漏 background:transparent；"
                  f"异常点 {_bad[:3]} 共 {len(_bad)}）")
        check(_dist(QColor(LIGHT["bg"]), LIGHT["bg_panel"]) > 0
              and QColor(DARK["bg"]) != QColor(DARK["bg_panel"]),
              f"断言有效性：窗口底与卡片底本就可辨（浅 {LIGHT['bg']} vs "
              f"{LIGHT['bg_panel']}）——若漏 transparent 必然被抓到")
        check("QCheckBox, QRadioButton { background: transparent;" in qss(LIGHT)
              and f"QWidget#formRow {{ background: transparent; }}" in qss(LIGHT),
              "QSS：QCheckBox/QRadioButton 与 #formRow 均显式 transparent")

        # ② 下拉/微调右侧「按钮」区（QSS 子控件 + SVG 资源）——像素级断言：
        #    分区底 == bg_compartment、微调中缝 1px == border 且**不越界**、
        #    下拉与微调**同宽/同左缘/同底色**、上下半严丝合缝、控件左 2/3 无
        #    游离线条（旧 bug：自绘的分隔线横穿输入框）。
        #    几何取样式引擎自己的 subControlRect —— QSS 的几何就是它算的；
        #    上一轮自绘几何算错位，本轮**不自己算**。
        _st = QApplication.style()
        _oc = QStyleOptionComboBox()
        dlg.theme.initStyleOption(_oc)
        _osb = QStyleOptionSpinBox()
        dlg.rate_limit.initStyleOption(_osb)
        _cc = _st.subControlRect(QStyle.CC_ComboBox, _oc,
                                 QStyle.SC_ComboBoxArrow, dlg.theme)
        _cu = _st.subControlRect(QStyle.CC_SpinBox, _osb,
                                 QStyle.SC_SpinBoxUp, dlg.rate_limit)
        _cd = _st.subControlRect(QStyle.CC_SpinBox, _osb,
                                 QStyle.SC_SpinBoxDown, dlg.rate_limit)
        check(_cc.width() == _cu.width() == _cd.width() == 26 + 1
              and _cc.left() == _cu.left() == _cd.left(),
              f"下拉与微调分区**同宽同左缘**（各 {_cc.width()}px = QSS 声明 26px + "
              f"1px 左分隔线；左缘 x={_cc.left()}）——上一轮两套各自算几何，参差")
        check(_cc.right() == dlg.theme.width() - 1
              and _cu.right() == _cd.right() == dlg.rate_limit.width() - 1,
              f"分区右边缘 == 控件右边缘 - 1px 边框（下拉 {_cc.right()}/"
              f"{dlg.theme.width() - 1}，微调 {_cu.right()}/{dlg.rate_limit.width() - 1}）")
        check(_cu.bottom() + 1 == _cd.top()
              and _cd.bottom() == dlg.rate_limit.height() - 1
              and _cu.height() == _cd.height(),
              f"微调上下半**严丝合缝**（up {_cu.top()}..{_cu.bottom()} / down "
              f"{_cd.top()}..{_cd.bottom()}，各 {_cu.height()}px；控件高 "
              f"{dlg.rate_limit.height()} 为偶数 → 基样式对半分不留 1px 接缝）")
        check(_cc.height() == dlg.theme.height() == theme.FIELD_H_GB
              and _cc.bottom() == _cc.height() - 1
              and _cu.height() + _cd.height() == dlg.rate_limit.height(),
              f"**声明分区高 == 实测控件总高**（下拉 {_cc.height()}px == "
              f"FIELD_H_GB {theme.FIELD_H_GB}px == 控件高 {dlg.theme.height()}px，"
              f"分区底 {_cc.bottom()} = 控件底 {dlg.theme.height() - 1}；"
              f"微调上半 {_cu.height()} + 下半 {_cd.height()} == 整高 "
              f"{dlg.rate_limit.height()}）")
        _mid_w = (dlg.rate_limit.height() - 1) / 2.0
        check(abs(_cu.bottom() - _mid_w) <= 1,
              f"微调中缝分隔线落在**控件垂直中心** ±1px（分隔线 y={_cu.bottom()} vs "
              f"中心 {_mid_w}）")

        # 空白文本控件：专门用于「左 2/3 有无游离线条」——正文会干扰判定
        _holder = QWidget()
        _holder.setFixedSize(320, 200)
        _hlay = QVBoxLayout(_holder)
        _ecombo = QComboBox()
        _ecombo.addItem("")
        _espin = QSpinBox()
        _espin.setRange(0, 600)
        _espin.setSpecialValueText(" ")          # 空显示（纯空格：不落墨）
        _espin.setValue(0)
        _hlay.addWidget(_ecombo)
        _hlay.addWidget(_espin)
        _holder.show()
        app.processEvents()
        _ecombo.setFocus()          # 焦点移开微调框：不画文本光标（否则左 2/3
        app.processEvents()         # 会有一条 1×6 的竖线，污染「无游离像素」判定）

        for _mode in ("light", "dark"):
            theme.apply_theme(app, _mode)
            app.processEvents()
            _pal = theme.current_palette()
            _img = dlg.grab().toImage()

            def _sub_of(w):
                o = w.mapTo(dlg, QPoint(0, 0))
                return _img.copy(o.x(), o.y(), w.width(), w.height())

            _scombo = _sub_of(dlg.theme)
            _sspin = _sub_of(dlg.rate_limit)
            check(_dist(_scombo.pixelColor(_cc.left() + 3, _cc.top() + 3),
                        _pal["bg_compartment"]) == 0
                  and _dist(_sspin.pixelColor(_cu.left() + 3, _cu.top() + 3),
                            _pal["bg_compartment"]) == 0
                  and _dist(_sspin.pixelColor(_cd.left() + 3, _cd.bottom() - 3),
                            _pal["bg_compartment"]) == 0,
                  f"[{_mode}] 下拉分区·微调上半·微调下半**底均为 bg_compartment**"
                  f"（{_pal['bg_compartment']}）")
            check(_scombo.pixelColor(_cc.left() + 3, _cc.top() + 3).name()
                  == _sspin.pixelColor(_cu.left() + 3, _cu.top() + 3).name(),
                  f"[{_mode}] 下拉与微调分区**底色相同**"
                  f"（{_scombo.pixelColor(_cc.left() + 3, _cc.top() + 3).name()}）")

            _area = _cc.width() * (_cc.height() - 2)
            _bgpts = _eq_pts(_scombo, _pal["bg_compartment"],
                             _cc.left(), _cc.right() + 1,
                             _cc.top() + 1, _cc.bottom(), 2)
            check(len(_bgpts) >= int(_area * 0.6),
                  f"[{_mode}] 下拉分区底覆盖率 {len(_bgpts)}/{_area} == "
                  f"bg_compartment（≥60%；缺口是圆角与雪佛龙本身）")
            _upa = _cu.width() * _cu.height()
            _dna = _cd.width() * _cd.height()
            _bps = _eq_pts(_sspin, _pal["bg_compartment"], _cu.left(),
                           _cu.right() + 1, _cu.top(), _cu.bottom() + 1, 2)
            _bpd = _eq_pts(_sspin, _pal["bg_compartment"], _cd.left(),
                           _cd.right() + 1, _cd.top(), _cd.bottom() + 1, 2)
            check(len(_bps) >= int(_upa * 0.6) and len(_bpd) >= int(_dna * 0.6),
                  f"[{_mode}] 微调上半 {len(_bps)}/{_upa}、下半 {len(_bpd)}/{_dna} "
                  f"像素 == bg_compartment（同底色）")

            _drow = _cu.bottom()
            _dv = [x for x in range(_cu.left(), _cu.right() + 1)
                   if _dist(_sspin.pixelColor(x, _drow), _pal["border"]) <= 1]
            check(len(_dv) == _cu.width(),
                  f"[{_mode}] 微调中缝 1px 分隔线 == border 且**贯穿整条分区**"
                  f"（{_pal['border']}：{len(_dv)}/{_cu.width()} 像素，行 y={_drow}）")
            _stray = [(x, y) for y in range(3, dlg.rate_limit.height() - 3)
                      for x in range(3, _cu.left() - 2)
                      if _dist(_sspin.pixelColor(x, y), _pal["border"]) <= 1]
            check(not _stray,
                  f"[{_mode}] 分隔线**不越界**：分区左缘（x={_cu.left()}）以左无任何 "
                  f"border 色像素（旧 bug 回归防护；异常 {_stray[:3]} 共 {len(_stray)}）")

            # 反向断言：控件左 2/3 内部不得有任何非背景像素（没有横穿输入框的线）
            # 微调框需先清空内部编辑框文本：specialValueText(" ") 的空白字符在
            # **无字体的离屏环境**会渲染成豆腐方块（10×10 方框），污染判定。
            _espin.lineEdit().clear()
            app.processEvents()
            # 下拉分区满高（不允许「锚在右上角的小块」）：按行统计分区列的底色
            _rows_ok = [_y for _y in range(_cc.top() + 1, _cc.bottom())
                        if _dist(_scombo.pixelColor(_cc.left() + 3, _y),
                                 _pal["bg_compartment"]) == 0]
            check(len(_rows_ok) >= _cc.height() - 4 and _rows_ok[0] <= _cc.top() + 2
                  and _rows_ok[-1] >= _cc.bottom() - 2,
                  f"[{_mode}] 下拉分区**满高**（{len(_rows_ok)}/{_cc.height()} 行 == "
                  f"bg_compartment，覆盖 y {_rows_ok[0]}..{_rows_ok[-1]}）——"
                  f"判据：分区行覆盖 ≥ 控件高 − 4，且两端各留 ≤ 2px 圆角")
            _himg = _holder.grab().toImage()
            _oc_e = QStyleOptionComboBox()
            _ecombo.initStyleOption(_oc_e)
            _osp_e = QStyleOptionSpinBox()
            _espin.initStyleOption(_osp_e)
            _edges = {
                "下拉(空文本)": _st.subControlRect(QStyle.CC_ComboBox, _oc_e,
                                                   QStyle.SC_ComboBoxArrow, _ecombo),
                "微调(空文本)": _st.subControlRect(QStyle.CC_SpinBox, _osp_e,
                                                   QStyle.SC_SpinBoxUp, _espin)}
            for _nm, _w in (("下拉(空文本)", _ecombo), ("微调(空文本)", _espin)):
                _o = _w.mapTo(_holder, QPoint(0, 0))
                _eb = _himg.copy(_o.x(), _o.y(), _w.width(), _w.height())
                _bad = [(x, y) for y in range(3, _w.height() - 3)
                        for x in range(3, _w.width() * 2 // 3)
                        if _dist(_eb.pixelColor(x, y), _pal["bg_input"]) > 24]
                check(not _bad,
                      f"[{_mode}] {_nm} 左 2/3 无游离像素（旧 bug：分割线横穿输入框）"
                      f"；异常 {_bad[:3]} 共 {len(_bad)}")
                # 控件**中心行**上、分区左边界以左不得有任何非背景像素
                _crow = _w.height() // 2
                _lft = _edges[_nm].left()
                _rowbad = [(x, _crow) for x in range(3, max(3, _lft - 2))
                           if _dist(_eb.pixelColor(x, _crow), _pal["bg_input"]) > 24]
                check(not _rowbad,
                      f"[{_mode}] {_nm} 中心行 y={_crow}、分区左缘 x={_lft} 以左无"
                      f"背景外像素（游离线回归防护；异常 {_rowbad[:3]} 共 "
                      f"{len(_rowbad)}）")
        _holder.close()
        _holder.deleteLater()
        app.processEvents()

        # ③ 复选框指示器自绘（启用态）：选中 = accent 实底 + 白色对勾；
        #    未选 = border_strong 描边。禁用态另算（灰底灰勾），见下。
        dlg.proxy_type.setCurrentIndex(dlg.proxy_type.findData("socks5"))
        dlg.proxy_peer.setChecked(True)
        dlg.seed_after.setChecked(False)
        app.processEvents()
        check(all(w.isEnabled() for w in dlg.proxy_fields()),
              "指示器用例前置：代理字段已启用（禁用态画的是灰底灰勾）")
        for _mode in ("light", "dark"):
            theme.apply_theme(app, _mode)
            app.processEvents()
            _pal = theme.current_palette()
            _img = dlg.grab().toImage()

            def _ind(w):
                o = w.mapTo(dlg, QPoint(0, 0))
                return _img.copy(o.x(), o.y(), 22, w.height())

            _on = _ind(dlg.proxy_peer)
            _off = _ind(dlg.seed_after)
            _acc = _eq_pts(_on, _pal["accent"], 0, 20, 0, _on.height(), 8)
            _stroke = _eq_pts(_off, _pal["border_strong"], 0, 20, 0,
                              _off.height(), 30)
            check(len(_acc) >= 40,
                  f"[{_mode}] 选中指示器 accent 实底 {len(_acc)}px"
                  f"（{_pal['accent']}，跟随主题）")
            check(len(_stroke) >= 4,
                  f"[{_mode}] 未选指示器描边 == border_strong"
                  f"（{_pal['border_strong']}：{len(_stroke)}px）")
            check(_style.INDICATOR_SIZE == 16
                  and _style.INDICATOR_RADIUS == 4,
                  f"[{_mode}] 指示器规格 16×16 圆角 4（模块常量）")
            if _acc:
                _ax0 = min(p[0] for p in _acc)
                _ax1 = max(p[0] for p in _acc)
                _ay0 = min(p[1] for p in _acc)
                _ay1 = max(p[1] for p in _acc)
                check(_ax1 - _ax0 >= 11 and _ay1 - _ay0 >= 11,
                      f"[{_mode}] 指示器实底 {_ax1 - _ax0 + 1}×{_ay1 - _ay0 + 1}"
                      f"（纯 accent 核心；外接含 1.4px 描边 ≈16×16）")
                _mark = _eq_pts(_on, _theme.ON_ACCENT, _ax0 + 2, _ax1 - 1,
                                _ay0 + 2, _ay1 - 1, 8)
                check(len(_mark) >= 3,
                      f"[{_mode}] 选中指示器内有白色对勾 {len(_mark)}px"
                      f"（{_theme.ON_ACCENT}）")
        # 切回浅色：accent 实底仍在（证明颜色是绘制时现读，不是导入期快照）
        theme.apply_theme(app, "light")
        app.processEvents()
        _img_l = dlg.grab().toImage()
        _o_l = dlg.proxy_peer.mapTo(dlg, QPoint(0, 0))
        _on_l = _img_l.copy(_o_l.x(), _o_l.y(), 22, dlg.proxy_peer.height())
        _acc_light = _eq_pts(_on_l, LIGHT["accent"], 0, 20, 0, _on_l.height(), 8)
        check(len(_acc_light) >= 40, "切回浅色仍是 accent 实底（颜色绘制时现读）")

        # ③b 禁用态指示器：灰底 + 灰勾（不再是 accent）
        for _mode in ("light", "dark"):
            theme.apply_theme(app, _mode)
            dlg.proxy_type.setCurrentIndex(dlg.proxy_type.findData("none"))
            app.processEvents()
            _pal = theme.current_palette()
            _img = dlg.grab().toImage()
            _o = dlg.proxy_peer.mapTo(dlg, QPoint(0, 0))
            _dis = _img.copy(_o.x(), _o.y(), 22, dlg.proxy_peer.height())
            _grey = _eq_pts(_dis, _pal["bg_hover"], 0, 20, 0, _dis.height(), 8)
            _noacc = _eq_pts(_dis, _pal["accent"], 0, 20, 0, _dis.height(), 8)
            check(len(_grey) >= 40 and len(_noacc) == 0,
                  f"[{_mode}] 禁用态指示器：灰底 bg_hover {len(_grey)}px / "
                  f"accent {len(_noacc)}px（禁用不再用强调色）")
            dlg.proxy_type.setCurrentIndex(dlg.proxy_type.findData("socks5"))
            app.processEvents()

        # ④ 直连时代理字段禁用 / 切回可用；密码掩码；路径 tooltip
        check(len(dlg.proxy_fields()) == 5,
              f"代理联动覆盖 5 个控件（{len(dlg.proxy_fields())}）")
        dlg.proxy_type.setCurrentIndex(dlg.proxy_type.findData("none"))
        app.processEvents()
        check(all(not w.isEnabled() for w in dlg.proxy_fields()),
              "代理类型=不使用代理（直连）→ 5 个代理控件全部 isEnabled()==False")
        dlg.proxy_type.setCurrentIndex(dlg.proxy_type.findData("socks5"))
        app.processEvents()
        check(all(w.isEnabled() for w in dlg.proxy_fields()),
              "切回 SOCKS5 → 5 个代理控件全部恢复 isEnabled()==True")
        dlg.proxy_type.setCurrentIndex(dlg.proxy_type.findData("http"))
        app.processEvents()
        check(all(w.isEnabled() for w in dlg.proxy_fields()),
              "再切 HTTP → 仍全部可用（HTTP 也走代理）")
        dlg.proxy_type.setCurrentIndex(dlg.proxy_type.findData("none"))
        app.processEvents()
        check(all(not w.isEnabled() for w in dlg.proxy_fields()),
              "再切回直连 → 重新全部禁用（联动是双向的）")

        check(dlg.proxy_pass.echoMode() == QLineEdit.Password,
              f"密码框掩码：echoMode == Password（实得 {dlg.proxy_pass.echoMode()}）")
        check(dlg.cache_edit.toolTip() == dlg.cache_edit.text()
              and dlg.download_edit.toolTip() == dlg.download_edit.text()
              and dlg.cache_edit.text() != "",
              f"路径框 tooltip == 完整路径（缓存 {dlg.cache_edit.toolTip()[:36]!r}…）")
        _newpath = os.path.join(tmp, "very", "long", "path", "for", "tooltip")
        dlg.cache_edit.setText(_newpath)
        app.processEvents()
        check(dlg.cache_edit.toolTip() == _newpath,
              "路径框 tooltip 随内容更新（textChanged → setToolTip）")

        # ⑤「不限」语义：限速 0 值显示「不限」（存取语义不变，仍是 KB/s 的 0）
        dlg.rate_limit.setValue(0)
        app.processEvents()
        check(dlg.rate_limit.specialValueText() == "不限"
              and dlg.rate_limit.text() == "不限" and dlg.rate_limit.value() == 0,
              f"下载限速 0 值显示「{dlg.rate_limit.text()}」（specialValueText 生效）")
        check(dlg.cache_limit.specialValueText() == "不限制",
              f"缓存上限 0 值显示「{dlg.cache_limit.specialValueText()}」")
        dlg.deleteLater()
    finally:
        for k, v in _orig_p.items():
            cfg_p.set(k, v)
        theme.apply_theme(app, "light")

    # ---------------------------------------------------------------- [5] 清理
    print("\n[5] 收尾")
    w._stop_preview()
    w.close()
    app.processEvents()
    check(True, "close() 无异常")

    print(f"\n{'=' * 56}")
    print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
