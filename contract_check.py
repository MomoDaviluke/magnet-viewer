"""对外契约自检 —— 重构 `core/fetcher.py` 期间的安全网。

背景
----
`core/fetcher.py` 已 1600+ 行，计划按职责拆为 session / resolver / registry /
taskops / persist / preview 六个模块，`SessionManager` 降级为 Facade。
拆分必须做到**对外行为完全不变**，而"不变"需要机器可判定的标准，
不能靠人眼 review。

本脚本用 `inspect.signature` 把当前对外接口固化为签名指纹
（参数名 + 是否可选，不含类型注解——注解调整不该误报），
任何改名 / 改序 / 新增必选参数 / 删除接口都会被立刻抓到。

契约来源
--------
1. `contract_snapshot.md` 第 9 节「改造后不允许变化的契约断言清单」；
2. 对 `ui/` 与全部测试脚本的实际调用面扫描（含 UI 直访的私有成员，
   那 4 处是历史遗留的破封装，重构期间按决策保留兼容）；

用法
----
    .\\.venv\\Scripts\\python.exe contract_check.py

退出码：`0` 契约一致 · `1` 契约被破坏 · `2` 环境/依赖缺失。
不启 libtorrent 会话、不联网，秒级完成，可在每个重构阶段随时一键自检。
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from core import (cache_guard, cache_quota, models, parser, persist,
                      registry, scheduler, session, states, stream_server)
    from core import fetcher as fetcher_mod
    from core.fetcher import SessionManager
except Exception as e:          # 依赖缺失：显式 SKIP，绝不假装通过
    print(f"依赖缺失，无法执行契约自检：{e}")
    sys.exit(2)

OK: list[str] = []
FAIL: list[str] = []


def check(cond: bool, msg: str) -> None:
    (OK if cond else FAIL).append(msg)
    print(("  [OK] " if cond else "  [FAIL] ") + msg)


def fp(fn) -> list | None:
    """签名指纹：[(参数名, 是否有默认值)]，排除 self。

    只取参数名与可选性，不取类型注解——注解是给人看的，调整它不应
    触发契约失败；但参数数量、顺序、命名变化必须被抓住。
    """
    if fn is None or not callable(fn):
        return None
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    return [(p.name, p.default is not inspect.Parameter.empty)
            for p in inspect.signature(fn).parameters.values()
            if p.name != "self"]


def sig_check(label: str, owner, table: dict) -> None:
    """逐项比对签名指纹表。"""
    for name, expect in table.items():
        fn = getattr(owner, name, None)
        got = fp(fn)
        check(got == expect,
              f"{label}.{name}{'' if got == expect else f' 签名变化：期望 {expect}，实际 {got}'}")


def main() -> int:
    print("=== 对外契约自检（fetcher 重构安全网）===\n")

    # ------------------------------------------------ [1] SessionManager 公开方法
    print("[1] SessionManager 公开方法签名（22 项，基线 2026-09-07）")
    SESSION_SIG = {
        # 生命周期
        "start": [("proxy", True), ("metadata_timeout", True)],
        "shutdown": [],
        "apply_proxy": [("proxy", False)],
        "apply_rate_limit": [("kbps", False)],
        "protected_dirs": [],
        # 解析入口（契约 #1 / #4）
        "resolve": [("source", False)],
        "connect_peer": [("ip", False), ("port", False),
                         ("wait_handle", True), ("task_id", True)],
        # 任务操作（契约 #1）
        "add_task": [("source", False), ("save_subdir", True),
                     ("priority", True), ("seed", True)],
        "set_priority": [("task_id", False), ("priority", False)],
        "pause_task": [("task_id", False)],
        "resume_task": [("task_id", False)],
        "remove_task": [("task_id", False), ("delete_files", True)],
        "focus_task": [("task_id", False)],
        "tasks": [],
        "task_result": [("task_id", False)],
        # 预览（契约 #3）
        "start_preview": [("f", False)],
        "stop_preview": [],
        "have_piece": [("piece", False)],
        "piece_length": [],
        "piece_map_for_path": [("disk_path", False)],
        "demand_for_path": [("disk_path", False), ("start_byte", False),
                            ("end_excl", False)],
        "status": [],
    }
    sig_check("SessionManager", SessionManager, SESSION_SIG)

    # ------------------------------------------------ [2] 属性（property）
    print("\n[2] SessionManager 属性（download_dir / metadata_timeout / current_result）")
    for name in ("download_dir", "metadata_timeout", "current_result"):
        check(hasattr(SessionManager, name), f"属性 {name} 存在")

    # ------------------------------------------------ [3] 实例级兼容属性
    # UI 与测试直访这些成员（历史破封装，决策：重构期间保留兼容）。
    # 均为实例属性，须构造实例检查；__init__ 不启会话，开销可忽略。
    print("\n[3] 实例级兼容属性（UI/测试直访，保留兼容决策）")
    tmp = tempfile.mkdtemp(prefix="mv_contract_")
    try:
        mgr = SessionManager(os.path.join(tmp, "cache"))
    except Exception as e:
        print(f"构造 SessionManager 失败：{e}")
        sys.exit(2)
    for name in ("scheduler", "_ses", "_metadata_timeout", "_active_downloads",
                 "cache_dir", "_handle", "on_metadata", "on_error",
                 "on_file_completed"):
        check(hasattr(mgr, name), f"实例属性 {name} 存在（外部直访依赖）")

    # ------------------------------------------------ [4] 协作模块契约
    print("\n[4] 协作模块签名（拆 fetcher 时的误伤防线）")
    sig_check("models", models, {
        "safe_rel_path": [("segments", False)],
        "file_disk_path": [("cache_dir", False), ("f", False)],   # 契约 #7
        "human_size": [("n", False)],
        "contiguous_bytes": [("pm", False), ("limit", True)],
        "range_available": [("pm", False), ("start", False), ("end_excl", False)],
        "disk_root": [("cache_dir", False), ("save_subdir", True)],
    })
    sig_check("parser", parser, {
        "parse_torrent_file": [("path", False)],
        "parse_magnet_uri": [("uri", False)],
        "torrent_info_hash": [("info", False)],
        "is_pure_v2": [("info", False)],
        "is_torrent_path": [("source", False)],
    })
    sig_check("scheduler.tail_piece_window", scheduler, {
        "tail_piece_window": [("file", False), ("piece_length", False)],
    })
    sig_check("PreviewScheduler", scheduler.PreviewScheduler, {
        "begin": [("handle", False), ("file", False)],              # 契约 #5
        "request_range": [("start_byte", False), ("end_byte", False)],
        "seek_to_byte": [("byte_offset", False)],
        "stop": [],
        "contiguous_progress": [],
        "tail_ready": [],
        "buffer_progress": [],
    })
    sig_check("stream_server", stream_server, {
        "_is_within": [("root", False), ("path", False)],           # 契约 #9
    })
    sig_check("StreamServer", stream_server.StreamServer, {
        "__init__": [("base_dir", False), ("avail_cb", True),
                     ("pieces_cb", True), ("demand_cb", True),
                     ("wait_timeout", True), ("bases", True)],
        "url_for": [("rel_path", False)],
    })
    sig_check("cache_guard", cache_guard, {                          # 契约 #8
        "is_risky_dir": [("path", False)],
        "ensure_cache_dir": [("path", False)],
        "guard_ok_for_cleanup": [("path", False), ("require_marker", True)],
    })
    # persist（阶段 1 抽出）：内部模块，但后续 registry/taskops 仍依赖它，
    # 签名同样冻结——后续阶段只能**使用**它，不得顺手改签名。
    sig_check("persist", persist, {
        "is_within": [("root", False), ("path", False)],
        "task_dir": [("download_dir", False), ("ih", False),
                     ("save_subdir", True)],
        "save_subdir_of": [("cache_dir", False), ("path", False)],
        "safe_task_save_path": [("cache_dir", False), ("download_dir", False),
                                ("ih", False), ("save_path", False)],
        "read_resume": [("cache_dir", False), ("ih", False)],
        # 键/待落盘筛选：临时键 tmp-<id> 必须在这里被拦掉，否则 resume_path 的
        # ValueError 会掀翻退出清理——三个阶段（registry/taskops）都会用到。
        "is_resume_key": [("key", False)],
        "missing_resume_keys": [("cache_dir", False), ("torrents", False)],
        "pending_resume_keys": [("cache_dir", False), ("torrents", False)],
    })
    sig_check("persist.TaskPersistence", persist.TaskPersistence, {
        "persist_tasks": [],
        "read_resume": [("ih", False)],
        "request_resume": [("rec", False)],
        "write_resume_from_alert": [("a", False)],
        "drain_resume_alerts": [("timeout", True)],
        "restore_task": [("t", False)],
        # 目录工具只保留模块级纯函数（无状态，registry/taskops 直接取用），
        # 不在 TaskPersistence 上留一层同签名的实例包装，避免两套入口。
    })
    # registry（阶段 3 抽出）：单锁归属 + 注册表 + TaskRecord 本体（R-2）。
    # 后续 taskops/resolver/preview 都建在它上面，签名与别名集冻结。
    sig_check("registry", registry, {
        "hash_key": [("handle", False)],
        "ih_from_params": [("p", False)],
    })
    sig_check("registry.TaskRegistry", registry.TaskRegistry, {
        "find_record": [("handle", False)],
        "find_record_by_ih": [("ih", False)],
        "current_record": [],
        "current": [],
        "resolving_snapshot": [],
        "protected_dirs": [],
        "put_record_locked": [("ih", False), ("rec", False),
                              ("make_current", True)],
        "apply_current_locked": [("rec", False), ("ih", False)],
        "register_current_locked": [("handle", False), ("result", False),
                                    ("gen", False)],
        "detach_record_locked": [("rec", False)],
        "clear_resolving_locked": [],
        "clear_runtime_state_locked": [],
        "focus_current": [("ih", False)],
        "bump_gen": [],
        "clear_resolving_safe": [],
        "set_resolving": [("v", False), ("started", True)],
        "preview_dir": [("ih", False)],
    })
    check(registry.METADATA_TIMEOUT == 90.0
          and registry.DOWNLOADS_SUBDIR == "downloads"
          and registry.PREVIEW_SUBDIR == ".preview",
          "registry 常量取值不变（90.0/downloads/.preview）")
    _tnames = {f.name for f in __import__("dataclasses").fields(
        registry.TaskRecord)}
    check(_tnames == {"handle", "result", "gen", "resolving", "resolve_started",
                      "state", "timeout", "download", "seed", "priority",
                      "save_path", "source", "error"},
          "TaskRecord 字段集不变（13 项，持久化/UI 快照按名取值）")
    check(all(isinstance(getattr(SessionManager, a), property) for a in
              ("_lock", "_torrents", "_tasks", "_current_ih", "_handle",
               "_result", "_resolving", "_resolve_started", "_gen",
               "_metadata_timeout")),
          "fetcher 10 个注册表别名全为 property 代理（单源，无第二份数据）")
    check(all(hasattr(fetcher_mod, n) for n in
              ("TaskRecord", "TaskRegistry", "METADATA_TIMEOUT",
               "DOWNLOADS_SUBDIR", "PREVIEW_SUBDIR")),
          "core.fetcher 仍再导出 TaskRecord/TaskRegistry/常量（历史 import 不破）")
    # session（阶段 2 抽出）：同 persist 处理——后续阶段只能使用、不得改签名。
    sig_check("session", session, {
        "alert_category_mask": [],
        "build_session_settings": [("listen_port", False),
                                   ("active_downloads", False),
                                   ("proxy", True)],
    })
    sig_check("session.SessionCore", session.SessionCore, {
        "start": [("proxy", True), ("metadata_timeout", True)],
        "apply_proxy": [("proxy", False)],
        "apply_rate_limit": [("kbps", False)],
        "shutdown": [],
        "alert_loop": [],
        "handle_alert": [("a", False)],
        "metadata_watchdog": [("now", True)],
        "resume_sweep": [("now", True)],
    })
    check(session.ALERT_POLL_INTERVAL == 0.15
          and session.SHUTDOWN_JOIN_TIMEOUT == 2.0
          and session.RESUME_SWEEP_INTERVAL == 60.0,
          "session 三个节奏常量不变（0.15 / 2.0 / 60.0）")
    check(all(hasattr(fetcher_mod, n) for n in ("SessionCore", "SessionDeps")),
          "core.fetcher 仍再导出 SessionCore / SessionDeps")

    # states（阶段 1 下沉的常量层）：取值冻结——改状态名 = 破坏磁盘数据兼容
    for name, val in (("STATE_QUEUED", "QUEUED"),
                      ("STATE_META_FETCH", "META_FETCH"),
                      ("STATE_VALIDATE", "VALIDATE"),
                      ("STATE_DOWNLOADING", "DOWNLOADING"),
                      ("STATE_PAUSED", "PAUSED"),
                      ("STATE_COMPLETED", "COMPLETED"),
                      ("STATE_STOPPED", "STOPPED"),
                      ("STATE_FAILED", "FAILED"),
                      ("STATE_SEEDING", "SEEDING"),
                      ("STATE_DELETED", "DELETED"),
                      ("STATE_READY", "READY")):
        check(getattr(states, name, None) == val, f"states.{name} == {val!r}")
    check(len(states.BOOTSTRAP_TRACKERS) == 5
          and all(t.startswith("udp://") for t in states.BOOTSTRAP_TRACKERS),
          "states.BOOTSTRAP_TRACKERS 5 条 udp tracker 不变")
    check(len(states.DOWNLOAD_STATES) == 10,
          "states.DOWNLOAD_STATES 成员数不变（10）")
    check(all(hasattr(fetcher_mod, n) for n in
              ("STATE_QUEUED", "STATE_DOWNLOADING", "STATE_PAUSED",
               "STATE_COMPLETED", "STATE_FAILED", "STATE_META_FETCH",
               "DOWNLOAD_STATES", "BOOTSTRAP_TRACKERS")),
          "core.fetcher 仍再导出 STATE_* / DOWNLOAD_STATES / BOOTSTRAP_TRACKERS")
    sig_check("cache_quota", cache_quota, {
        "dir_size_bytes": [("path", False)],
        "scan_preview_dirs": [("preview_root", False)],
        "enforce_preview_limit": [("preview_root", False), ("limit_mb", False),
                                  ("keep_dirs", True), ("warn", True)],
    })
    check(cache_guard.CACHE_MARKER == ".magnet_viewer_cache",
          "CACHE_MARKER 常量值不变")

    # ------------------------------------------------ 汇总
    print(f"\n=== 契约自检：OK {len(OK)} / FAIL {len(FAIL)} ===")
    if FAIL:
        for m in FAIL:
            print(f"  - {m}")
        print("\nX 契约被破坏：重构不得改变对外接口签名（如需变更，"
              "请同步更新 contract_snapshot.md 与本基线表）")
        return 1
    print("- 对外契约一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
