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
