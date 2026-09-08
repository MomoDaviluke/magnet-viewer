"""下载管理模块验收测试（t7：验收矩阵落地 + 全量回归）。

依据 plan/00_download_module_plan.md（D1 验收矩阵 / D2 边界）与
plan/t4_acceptance_plan.md。判定复用本机做种端闭环（seed_mode + SessionManager
+ connect_peer 直连，同 local_magnet_test 底盘）；暂停/恢复用磁盘字节快照；
续传用做种端上传字节增量证明「不重下」。

覆盖（MVP 7 + 增强 2 + 边界 5+）：
  §2 添加磁力链 → 下载中（MVP1/2）
  §3 暂停：5s 磁盘快照不变（MVP3）
  §4 恢复：字节续增、进度单调不回退（MVP4）
  §5 删除任务：handle 移除 + 目录释放 + 重添同 hash 无残留（MVP5）
  §6 退出重启续传：.tasks.json + fastresume 读回、上传增量证不重下（MVP6）
  §7 完成：落盘字节=声明值、state=COMPLETED（MVP7）
  §8 多任务并发：2 种子同时下载均 100%（增强1）
  §9 带宽限制：download_rate ≤ 设定值 ±20%（增强2）
  §10 边界：重复 hash 拒绝 / per-task 看门狗互不拖累 / safe_rel_path 防穿越 /
      resume 损坏静默重建 / 缓存被外部清理不崩溃

退出码约定（README.md:90）：0=通过，1=失败，2=SKIP（依赖缺失显式跳过）。
用法：python download_mgr_test.py（与 regression_run.py 分开跑；两脚本都跑
通过 = D4 验收闭环）。
"""
from __future__ import annotations

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import libtorrent as lt  # noqa: E402

import test_support as ts  # noqa: E402
from core.fetcher import (STATE_COMPLETED, STATE_DOWNLOADING,  # noqa: E402
                          STATE_FAILED, STATE_META_FETCH, STATE_PAUSED,
                          STATE_QUEUED, SessionManager)
from core.models import contiguous_bytes, human_size  # noqa: E402
from core.resume import resume_path  # noqa: E402
from core.taskstore import task_file_path  # noqa: E402

TIMEOUT_META, TIMEOUT_DATA = 60, 90
RATE_SLOW = 256 * 1024        # 慢速会话（暂停/恢复/并发观察窗口用）
RATE_LIMIT_TEST = 512 * 1024  # 带宽限制验收值


def _slow_session(mgr: SessionManager, rate: int = RATE_SLOW) -> None:
    """给会话加下载限速（回环默认忽略限速，须关掉 ignore_limits_on_local_network）。

    仅用于让「暂停/恢复/并发」有可观察的时间窗口；带宽验收见 §9。
    """
    mgr._ses.apply_settings({"download_rate_limit": rate,
                             "ignore_limits_on_local_network": False})


def _task(mgr: SessionManager, ih: str) -> dict:
    return next(t for t in mgr.tasks() if t["id"] == ih)


def _wait_task_state(mgr: SessionManager, ih: str, states: set,
                     timeout: float = TIMEOUT_DATA) -> dict:
    """等待任务进入任一目标状态，返回最终快照。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        for t in mgr.tasks():
            if t["id"] == ih and t["state"] in states:
                return t
        time.sleep(0.5)
    raise TimeoutError(f"任务 {ih[:12]}… 未在 {timeout}s 内达到 {states}；"
                       f"当前状态={[t['state'] for t in mgr.tasks() if t['id'] == ih]}")


def _setup(ws: ts.WorkSpace, seed_port: int, peer_port: int,
           files: dict[str, int] | None = None,
           slow: bool = False, metadata_timeout: float | None = None,
           **mgr_kw):
    """构造载荷 + 做种端 + 会话管理器；返回 (ti, ih, seed_ses, seed_h, mgr)。

    ⚠️ 做种端 session 必须由调用方持有引用，否则 Python GC 会销毁它
    （libtorrent python 绑定：session 包装对象析构即停服）——与
    local_magnet_test.py:77 同源（seed_ses 存活到 main 末尾）。
    """
    ts.build_payload(ws.payload, files)
    ti = ts.make_torrent(ws.payload)
    ih = str(ti.info_hash())
    seed_ses, seed_h = ts.start_seeder(ti, os.path.dirname(ws.payload), seed_port)
    time.sleep(0.5)
    mgr = SessionManager(ws.cache, listen_port=peer_port, **mgr_kw)
    mgr.start(metadata_timeout=metadata_timeout)
    if slow:
        _slow_session(mgr)
    return ti, ih, seed_ses, seed_h, mgr


def _add_and_connect(mgr: SessionManager, ih: str, seed_port: int,
                     source: str | None = None) -> str:
    """add_task(magnet) 并直连做种端，返回 task_id。"""
    tid = mgr.add_task(source or ts.magnet_uri(ih))
    mgr.connect_peer("127.0.0.1", seed_port, task_id=tid)
    return tid


# --------------------------------------------------------------------------
# §2 添加磁力链 → 下载中（MVP1/MVP2）
# --------------------------------------------------------------------------

def section_add_to_downloading(ck: ts.Checker) -> None:
    ck.section("§2 添加磁力链 → 下载中（MVP #1/#2）")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(ws, 7131, 7141,
                                     files={"movie/demo.mp4": 2 * 1024 * 1024,
                                            "readme.txt": 512},
                                     slow=True)
        got: list = []
        mgr.on_metadata = got.append
        try:
            tid = mgr.add_task(ts.magnet_uri(ih))
            ck.check(tid == ih and len(tid) in (40, 64),
                     f"add_task 返回 info_hash 任务 id（{tid[:12]}…）")
            t0 = _task(mgr, ih)
            ck.check(t0["state"] in (STATE_META_FETCH, STATE_QUEUED),
                     f"初始状态 META_FETCH/QUEUED（实际 {t0['state']}）")
            dl = os.path.join(mgr.download_dir, ih)
            ck.check(t0["save_path"] == os.path.normpath(dl),
                     f"落盘目录隔离 downloads/<ih>（{t0['save_path']}）")
            ck.check(os.path.isdir(dl), "任务目录已创建")
            mgr.connect_peer("127.0.0.1", 7131, task_id=tid)

            ok_meta = ts.wait_until(lambda: bool(got), TIMEOUT_META, desc="元数据回调")
            ck.check(ok_meta, "on_metadata 回调触发（下载任务就绪）")
            if ok_meta:
                r = got[0]
                ck.check(r.info_hash == ih, "回调 info_hash 与做种端一致")
                ck.check(len(r.view_files) == 2, "文件数正确（2 个可见文件）")
                pad_sum = sum(f.size for f in r.files if f.is_pad)
                ck.check(r.total_size + pad_sum == ti.total_size(),
                         f"total_size 与种子声明一致（可见 {human_size(r.total_size)}"
                         f" + pad {human_size(pad_sum)} = 声明 {human_size(ti.total_size())}）")
                ck.check(_task(mgr, ih)["total_size"] == r.total_size,
                         "任务清单 total_size 与回调一致")

            t1 = _wait_task_state(mgr, ih, {STATE_DOWNLOADING, STATE_COMPLETED})
            ck.check(t1["state"] == STATE_DOWNLOADING,
                     f"元数据→下载中（state={t1['state']}）")
            p0 = t1["progress"]
            time.sleep(1.2)
            p1 = _task(mgr, ih)["progress"]
            ck.check(p1 > p0, f"total_done 单调增长（{p0:.2f}→{p1:.2f}）")
            ck.check(0 < p1 < 1.0, "下载进行中（未瞬完、未停滞）")
        finally:
            mgr.shutdown()


# --------------------------------------------------------------------------
# §3 暂停 + §4 恢复（MVP3/MVP4，同一条生命线）
# --------------------------------------------------------------------------

def section_pause_resume(ck: ts.Checker) -> None:
    ck.section("§3 暂停（MVP #3）：暂停后 5s 磁盘字节快照不变")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(ws, 7151, 7161,
                                     files={"movie/demo.mp4": 2 * 1024 * 1024,
                                            "readme.txt": 512},
                                     slow=True)
        dl = os.path.join(mgr.download_dir, ih)
        try:
            tid = _add_and_connect(mgr, ih, 7151)
            _wait_task_state(mgr, ih, {STATE_DOWNLOADING})
            got_progress = ts.wait_until(
                lambda: _task(mgr, ih)["progress"] > 0.05, 30,
                desc="暂停前已有进度")
            ck.check(got_progress,
                     f"暂停前已有进度（{_task(mgr, ih)['progress']:.2f}）")

            ck.check(mgr.pause_task(ih) is True, "pause_task 返回 True")
            tp = _task(mgr, ih)
            ck.check(tp["state"] == STATE_PAUSED, f"任务状态 PAUSED（{tp['state']}）")

            time.sleep(2.0)             # 等 libtorrent 把在途分块落盘完毕
            snap1 = ts.dir_byte_snapshot(dl)
            time.sleep(5.0)
            snap2 = ts.dir_byte_snapshot(dl)
            ck.check(snap1 == snap2 and snap1 > 0,
                     f"暂停后 5s 磁盘字节快照不变（{human_size(snap1)}）")
            pp = _task(mgr, ih)["progress"]
            time.sleep(2.0)
            ck.check(_task(mgr, ih)["progress"] == pp,
                     "暂停期间进度不再推进")

            ck.section("§4 恢复（MVP #4）：字节续增、已有 piece 不重下")
            ck.check(mgr.resume_task(ih) is True, "resume_task 返回 True")
            tr = _wait_task_state(mgr, ih, {STATE_DOWNLOADING, STATE_COMPLETED})
            ck.check(tr["state"] in (STATE_DOWNLOADING, STATE_COMPLETED),
                     f"恢复后回到下载（{tr['state']}）")
            grew = ts.wait_until(
                lambda: ts.dir_byte_snapshot(dl) > snap2 + 64 * 1024,
                TIMEOUT_DATA, desc="恢复后字节续增")
            ck.check(grew, "恢复后磁盘字节续增（> 暂停时快照）")

            # 进度单调不回退：多次采样只升不降（piece 下载序不回退）
            seq = [tr["progress"]]
            t0 = time.time()
            while time.time() - t0 < 6:
                st = mgr.tasks()
                for x in st:
                    if x["id"] == ih:
                        seq.append(x["progress"])
                time.sleep(0.8)
            regress = [i for i in range(1, len(seq)) if seq[i] < seq[i - 1] - 1e-6]
            ck.check(not regress and seq[-1] >= seq[0],
                     f"恢复后进度单调不回退（{len(seq)} 次采样，最终 {seq[-1]:.2f}）")

            tf = _wait_task_state(mgr, ih, {STATE_COMPLETED})
            ck.check(tf["state"] == STATE_COMPLETED, "恢复后完成下载")
        finally:
            mgr.shutdown()


# --------------------------------------------------------------------------
# §5 删除任务（MVP5）
# --------------------------------------------------------------------------

def section_remove(ck: ts.Checker) -> None:
    ck.section("§5 删除任务（MVP #5）：handle 移除 + 目录释放 + 重添无残留")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(ws, 7171, 7181,
                                     files={"movie/demo.mp4": 2 * 1024 * 1024,
                                            "readme.txt": 512},
                                     slow=True)
        dl = os.path.join(mgr.download_dir, ih)
        try:
            tid = _add_and_connect(mgr, ih, 7171)
            _wait_task_state(mgr, ih, {STATE_DOWNLOADING})
            time.sleep(1.5)             # 留一点进度
            ck.check(ts.dir_byte_snapshot(dl) > 0, "删除前任务有落盘数据")

            ck.check(mgr.remove_task(ih, delete_files=False) is True,
                     "remove_task(delete_files=False) 返回 True")
            ck.check(not any(t["id"] == ih for t in mgr.tasks()),
                     "任务列表已移除该任务（handle 注销）")
            time.sleep(1.0)
            ck.check(os.path.isdir(dl), "delete_files=False 保留磁盘文件")

            # 再删一次（带删文件），目录应释放
            tid2 = _add_and_connect(mgr, ih, 7171)
            _wait_task_state(mgr, ih, {STATE_DOWNLOADING})
            time.sleep(1.0)
            ck.check(mgr.remove_task(ih, delete_files=True) is True,
                     "remove_task(delete_files=True) 返回 True")
            gone = ts.wait_until(lambda: not os.path.isdir(dl), 15,
                                 desc="downloads/<ih> 目录释放")
            ck.check(gone, "downloads/<ih> 目录已释放（未残留）")

            # 重添同 hash：无残留续传（目录全新、进度从低处重新开始）
            tid3 = _add_and_connect(mgr, ih, 7171)
            ck.check(tid3 == ih and len(mgr.tasks()) == 1,
                     "重添同 hash 成功（不重复入表）")
            tk = _wait_task_state(mgr, ih, {STATE_DOWNLOADING, STATE_COMPLETED})
            ck.check(tk["progress"] < 0.6,  # 慢速会话下刚起步，不是从旧进度续传
                     f"重添后从全新进度开始（{tk['progress']:.2f}，非旧进度残留）")
        finally:
            mgr.shutdown()


# --------------------------------------------------------------------------
# §6 退出重启续传（MVP6）
# --------------------------------------------------------------------------

def section_restart_resume(ck: ts.Checker) -> None:
    ck.section("§6 退出重启续传（MVP #6）：.tasks.json + fastresume 读回")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(ws, 7191, 7201,
                                     files={"movie/demo.mp4": 2 * 1024 * 1024,
                                            "readme.txt": 512},
                                     slow=True)
        try:
            tid = _add_and_connect(mgr, ih, 7191)
            _wait_task_state(mgr, ih, {STATE_DOWNLOADING})
            got_prog = ts.wait_until(
                lambda: _task(mgr, ih)["progress"] >= 0.2, 60,
                desc="退出前下载 ≥20%")
            saved_prog = _task(mgr, ih)["progress"]
            ck.check(got_prog, f"退出前已下载 {saved_prog:.2f}（≥20%）")
            saved_upload = seed_h.status().total_upload

            mgr.shutdown()              # 触发 .tasks.json + fastresume 落盘
            ck.check(os.path.isfile(task_file_path(ws.cache)),
                     ".tasks.json 已落盘")
            rp = resume_path(ws.cache, ih)
            ok_rs = ts.wait_until(lambda: os.path.isfile(rp), 8,
                                  desc="fastresume 落盘")
            ck.check(ok_rs, "fastresume 已落盘（.resume/<ih>.fastresume）")

            # 重建 SessionManager（同 cache_dir）：任务与进度应恢复
            mgr2 = SessionManager(ws.cache, listen_port=7202)
            mgr2.start()
            got: list = []
            mgr2.on_metadata = got.append
            rest = [t for t in mgr2.tasks() if t["id"] == ih]
            ck.check(len(rest) == 1, "重启后任务列表恢复（tasks.json 读回）")
            ck.check(rest[0]["save_path"] == os.path.normpath(
                os.path.join(mgr2.download_dir, ih)), "重启后任务落盘目录一致")
            ck.check(rest[0]["state"] in
                     (STATE_META_FETCH, STATE_DOWNLOADING, STATE_PAUSED,
                      STATE_COMPLETED),
                     f"重启后状态合法（{rest[0]['state']}）")
            mgr2.connect_peer("127.0.0.1", 7191, task_id=ih)

            # 进度恢复：重连后应快速回到退出前水位（fastresume 位图生效）
            t0 = time.time()
            reached = False
            while time.time() - t0 < TIMEOUT_DATA:
                for x in mgr2.tasks():
                    if x["id"] == ih and x["progress"] >= saved_prog - 0.02:
                        reached = True
                        break
                if reached:
                    break
                time.sleep(0.5)
            ck.check(reached, f"重启续传回到退出前水位（≥{saved_prog - 0.02:.2f}）")

            tf = _wait_task_state(mgr2, ih, {STATE_COMPLETED})
            ck.check(tf["state"] == STATE_COMPLETED, "重启续传后下载完成")

            # 做种端上传增量 ≈ 剩余量（不是全量重下）
            delta_upload = seed_h.status().total_upload - saved_upload
            remain = max(0, ti.total_size() - saved_prog * ti.total_size())
            ck.check(delta_upload <= remain * 1.5 + 512 * 1024,
                     f"续传未全量重下（上传增量 {human_size(delta_upload)}"
                     f" ≈ 剩余 {human_size(remain)}）")
            mgr2.shutdown()
        finally:
            mgr.shutdown()


# --------------------------------------------------------------------------
# §7 完成（MVP7）
# --------------------------------------------------------------------------

def section_finished(ck: ts.Checker) -> None:
    ck.section("§7 完成（MVP #7）：落盘字节=声明值、state=COMPLETED")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(ws, 7211, 7221)
        dl = os.path.join(mgr.download_dir, ih)
        try:
            tid = _add_and_connect(mgr, ih, 7211)
            tf = _wait_task_state(mgr, ih, {STATE_COMPLETED})
            ck.check(tf["state"] == STATE_COMPLETED, "state == COMPLETED")
            ck.check(tf["progress"] >= 0.999, "progress == 1.0")
            # 声明值 = 非 .pad 文件总大小（libtorrent 不把 BEP-47 pad 落盘，
            # 见 probe：disk 恰为可见文件之和）；tasks()["total_size"] 同源。
            # 注意：torrent_finished 告警可领先磁盘可见落盘几步（异步磁盘
            # 队列，实测差量为最后几个文件条目，≤5s 到位）——以磁盘实际
            # 落盘为断言对象（有界等待，不弱化完整性）。
            declared = tf["total_size"]
            disk = ts.dir_byte_snapshot(dl)
            disk_ok = disk == declared or ts.wait_until(
                lambda: ts.dir_byte_snapshot(dl) == declared, 15,
                desc="完成落盘字节到位")
            disk = ts.dir_byte_snapshot(dl)
            ck.check(disk_ok and disk == declared,
                     f"全部落盘字节=声明值（{human_size(disk)}/{human_size(declared)}）")
            ck.check(tf["finished_at"] is not None, "finished_at 已记录")
        finally:
            mgr.shutdown()


# --------------------------------------------------------------------------
# §8 多任务并发（增强1）
# --------------------------------------------------------------------------

def section_concurrent(ck: ts.Checker) -> None:
    ck.section("§8 多任务并发（增强 #1）：2 种子同时下载均达 100%")
    with ts.WorkSpace() as ws:
        p1 = os.path.join(ws.root, "p1")
        p2 = os.path.join(ws.root, "p2")
        ts.build_payload(p1, {"a.mp4": 2 * 1024 * 1024, "a.txt": 512})
        ts.build_payload(p2, {"b.mp4": 2 * 1024 * 1024, "b.txt": 512})
        ti1, ti2 = ts.make_torrent(p1), ts.make_torrent(p2)
        ih1, ih2 = str(ti1.info_hash()), str(ti2.info_hash())
        s1, h1 = ts.start_seeder(ti1, ws.root, 7231)
        s2, h2 = ts.start_seeder(ti2, ws.root, 7232)
        time.sleep(0.5)
        mgr = SessionManager(ws.cache, listen_port=7241, active_downloads=3)
        mgr.start()
        _slow_session(mgr)
        try:
            t1 = mgr.add_task(ts.magnet_uri(ih1, "p1"))
            t2 = mgr.add_task(ts.magnet_uri(ih2, "p2"))
            ck.check(t1 == ih1 and t2 == ih2 and len(mgr.tasks()) == 2,
                     "双任务入表成功")
            mgr.connect_peer("127.0.0.1", 7231, task_id=t1)
            mgr.connect_peer("127.0.0.1", 7232, task_id=t2)

            # 中期采样：两者都在下载中（并发，而非串行）
            both = ts.wait_until(
                lambda: all(t["state"] == STATE_DOWNLOADING
                            for t in mgr.tasks()) and all(0 < t["progress"] < 1
                                                          for t in mgr.tasks()),
                TIMEOUT_DATA, desc="双任务同时下载中")
            ck.check(both, "两个任务同时处于下载中（并发执行）")

            f1 = _wait_task_state(mgr, ih1, {STATE_COMPLETED})
            f2 = _wait_task_state(mgr, ih2, {STATE_COMPLETED})
            d1 = ts.dir_byte_snapshot(os.path.join(mgr.download_dir, ih1))
            d2 = ts.dir_byte_snapshot(os.path.join(mgr.download_dir, ih2))
            ck.check(f1["state"] == f2["state"] == STATE_COMPLETED,
                     "两个任务均达 100%")
            # 声明值 = 非 .pad 文件总大小（libtorrent 不把 pad 落盘），
            # 双任务各自校验，互不串扰
            ck.check(d1 == f1["total_size"] and d2 == f2["total_size"],
                     f"双任务落盘字节均=声明值（{human_size(d1)}/{human_size(f1['total_size'])}"
                     f"，{human_size(d2)}/{human_size(f2['total_size'])}）")
        finally:
            mgr.shutdown()


# --------------------------------------------------------------------------
# §9 带宽限制（增强2）
# --------------------------------------------------------------------------

def section_rate_limit(ck: ts.Checker) -> None:
    ck.section("§9 带宽限制（增强 #2）：download_rate ≤ 设定值 ±20%")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(ws, 7251, 7261,
                                     files={"big.bin": 6 * 1024 * 1024})
        dl = os.path.join(mgr.download_dir, ih)
        try:
            _slow_session(mgr, RATE_LIMIT_TEST)   # 512KB/s
            tid = _add_and_connect(mgr, ih, 7251)
            t = _wait_task_state(mgr, ih, {STATE_DOWNLOADING})
            t_start = time.time()
            tf = _wait_task_state(mgr, ih, {STATE_COMPLETED})
            elapsed = time.time() - t_start
            payload = ti.total_size()
            # 有效速率 = payload 字节 / 下载耗时（慢速会话下耗时主要都在下载）
            eff_rate = payload / max(elapsed, 0.001)
            limit = RATE_LIMIT_TEST
            ck.check(eff_rate <= limit * 1.2,
                     f"实测速率 {human_size(eff_rate)}/s ≤ 限速 {human_size(limit)}/s ×1.2")
            ck.check(eff_rate >= limit * 0.3,
                     f"实测速率不至于过低（{human_size(eff_rate)}/s）")
            ck.check(tf["state"] == STATE_COMPLETED, "限速下仍完成下载")
            ck.check(ts.dir_byte_snapshot(dl) == tf["total_size"],
                     "限速完成后落盘字节=声明值")
        finally:
            mgr.shutdown()


# --------------------------------------------------------------------------
# §10 边界（MVP 子集 + 增强项）
# --------------------------------------------------------------------------

def section_boundaries(ck: ts.Checker) -> None:
    ck.section("§10 边界清单")

    # ---- B1 重复添加同 info_hash：拒绝（add 两次任务数不变） ----
    ck.section("§10-B1 重复 hash 拒绝")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(ws, 7271, 7281)
        try:
            tid1 = _add_and_connect(mgr, ih, 7271)
            tid2 = mgr.add_task(ts.magnet_uri(ih))
            ck.check(tid2 == tid1 and len(mgr.tasks()) == 1,
                     f"重复添加返回同 id，任务数不变（{len(mgr.tasks())}）")
        finally:
            mgr.shutdown()

    # ---- B2 元数据超时按任务独立（per-task 看门狗互不拖累） ----
    ck.section("§10-B2 per-task 看门狗独立")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(ws, 7291, 7301,
                                     metadata_timeout=5.0)
        ghost = "0123456789abcdef0123456789abcdef01234567"  # 无做种端
        try:
            ga = mgr.add_task(ts.magnet_uri(ghost, "ghost"))
            ck.check(ga == ghost, "无做种任务入表")
            tid = _add_and_connect(mgr, ih, 7291)
            ok = ts.wait_until(
                lambda: any(t["id"] == ghost and t["state"] == STATE_FAILED
                            for t in mgr.tasks()),
                20, desc="幽灵任务超时失败")
            ck.check(ok, "无做种任务独立超时 → FAILED")
            g = _task(mgr, ghost)
            ck.check("超时" in g["error"], f"失败原因可见（{g['error'][:24]}…）")
            real = _task(mgr, ih)
            ck.check(real["state"] in (STATE_DOWNLOADING, STATE_COMPLETED),
                     f"看门狗互不拖累：真实任务继续下载（{real['state']}）")
        finally:
            mgr.shutdown()

    # ---- B3 同名/路径冲突：safe_rel_path 仍防穿越 ----
    ck.section("§10-B3 save_subdir 防穿越")
    with ts.WorkSpace() as ws:
        ts.build_payload(ws.payload, {"a.mp4": 512 * 1024})
        ti = ts.make_torrent(ws.payload)
        ih = str(ti.info_hash())
        ss, seed_h = ts.start_seeder(ti, os.path.dirname(ws.payload), 7311)
        mgr = SessionManager(ws.cache, listen_port=7321)
        mgr.start()
        try:
            tid = mgr.add_task(ts.magnet_uri(ih),
                               save_subdir="../../escape_evil")
            t = _task(mgr, ih)
            norm = os.path.normpath(t["save_path"])
            dl_root = os.path.normpath(mgr.download_dir)
            inside = norm == os.path.join(dl_root, "escape_evil")
            ck.check(inside, f"save_subdir 穿越被净化（落点 {norm}）")
            ck.check(".." not in t["save_subdir"], "save_subdir 无 .. 残留")
        finally:
            mgr.shutdown()

    # ---- B4 resume data 损坏：静默重建、从头校验，不崩溃 ----
    ck.section("§10-B4 fastresume 损坏静默重建")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(ws, 7331, 7341,
                                     files={"movie/demo.mp4": 2 * 1024 * 1024,
                                            "readme.txt": 512},
                                     slow=True)
        try:
            tid = _add_and_connect(mgr, ih, 7331)
            _wait_task_state(mgr, ih, {STATE_DOWNLOADING})
            ts.wait_until(lambda: _task(mgr, ih)["progress"] > 0.05, 30,
                          desc="损坏前已有进度")
            mgr.pause_task(ih)
            ts.wait_until(lambda: os.path.isfile(resume_path(ws.cache, ih)),
                          8, desc="fastresume 落盘")
            mgr.shutdown()
            rp = resume_path(ws.cache, ih)
            ck.check(os.path.isfile(rp), "fastresume 已生成")
            with open(rp, "wb") as f:      # 人为损坏
                f.write(b"NOT-A-BENCODE!")
            mgr2 = SessionManager(ws.cache, listen_port=7342)
            mgr2.start()                    # 不应崩溃/阻断
            rest = [t for t in mgr2.tasks() if t["id"] == ih]
            ck.check(len(rest) == 1, "损坏后重启：任务仍恢复（不阻断启动）")
            ck.check("重启恢复失败" not in str(rest[0].get("error") or ""),
                     f"损坏 fastresume 静默降级（state={rest[0]['state']}）")
            mgr2.connect_peer("127.0.0.1", 7331, task_id=ih)
            if rest[0]["state"] == STATE_PAUSED:
                mgr2.resume_task(ih)      # 损坏前暂停过：重启恢复为 PAUSED，须恢复
            tf = _wait_task_state(mgr2, ih, {STATE_COMPLETED}, timeout=90)
            ck.check(tf["state"] == STATE_COMPLETED, "损坏后从头校验并完成下载")
            mgr2.shutdown()
        finally:
            mgr.shutdown()

    # ---- B5 缓存目录被外部清理：缺失分块重下，不 404 崩溃 ----
    ck.section("§10-B5 缓存被外部清理不崩溃")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(ws, 7351, 7361,
                                     files={"movie/demo.mp4": 2 * 1024 * 1024,
                                            "readme.txt": 512},
                                     slow=True)
        dl = os.path.join(mgr.download_dir, ih)
        try:
            tid = _add_and_connect(mgr, ih, 7351)
            _wait_task_state(mgr, ih, {STATE_DOWNLOADING})
            ts.wait_until(lambda: _task(mgr, ih)["progress"] > 0.05, 30,
                          desc="清理前已有进度")
            mgr.shutdown()
            ck.check(ts.dir_byte_snapshot(dl) > 0, "清理前有落盘数据")
            shutil.rmtree(dl, ignore_errors=True)     # 外部清理
            ck.check(ts.dir_byte_snapshot(dl) == 0, "外部已清空任务目录")
            mgr2 = SessionManager(ws.cache, listen_port=7362)
            mgr2.start()                    # 不应崩溃
            rest = [t for t in mgr2.tasks() if t["id"] == ih]
            ck.check(len(rest) == 1, "清理后重启：任务恢复（不崩溃）")
            mgr2.connect_peer("127.0.0.1", 7351, task_id=ih)
            tf = _wait_task_state(mgr2, ih, {STATE_COMPLETED}, timeout=90)
            # libtorrent 的 torrent_finished 告警可领先磁盘可见落盘（异步磁盘
            # 队列；实测差量为最后 512B 文件条目，5s 内到位，重启读回完整）。
            # 这里以磁盘实际落盘为断言对象（有界等待，不弱化完整性）。
            snap = ts.dir_byte_snapshot(dl)
            disk_ok = snap == tf["total_size"] or ts.wait_until(
                lambda: ts.dir_byte_snapshot(dl) == tf["total_size"], 15,
                desc="缺失分块重下后磁盘落盘到位")
            snap = ts.dir_byte_snapshot(dl)
            ck.check(tf["state"] == STATE_COMPLETED and disk_ok
                     and snap == tf["total_size"],
                     f"缺失分块重下并完成（不 404 崩溃）state={tf['state']} "
                     f"snap={snap} total={tf['total_size']}")
            mgr2.shutdown()
        finally:
            mgr.shutdown()


def section_stream_safety(ck: ts.Checker) -> None:
    """§11 下载中任务流服务安全：分块级可用性，绝不整文件喂零数据。

    回归防护对象：历史「partial file / Invalid data」缺陷——下载中任务文件
    若按静态整文件服务，未下载的稀疏零区域会被直接喂给播放器。
    """
    import urllib.error
    import urllib.request

    from core.models import contiguous_bytes
    from core.stream_server import StreamServer

    ck.section("§11 下载中任务流服务安全（防 partial file 回归）")
    with ts.WorkSpace() as ws:
        ti, ih, ss, seed_h, mgr = _setup(
            ws, 7391, 7401,
            files={"movie/demo.mp4": 4 * 1024 * 1024, "readme.txt": 512})
        try:
            # 极慢限速：把「断言窗口」控制在头部点播的小区间内，
            # 避免任务自然下载把缺失区间填满（乱序/deadline 下载的时序陷阱）
            _slow_session(mgr, 16 * 1024)
            tid = _add_and_connect(mgr, ih, 7391)
            tf = _wait_task_state(mgr, ih, {STATE_DOWNLOADING}, timeout=60)
            ck.check(tf is not None and tf["state"] == STATE_DOWNLOADING,
                     "任务进入下载中")
            res = mgr.task_result(ih)
            ck.check(res is not None, "任务元数据可反查（task_result）")
            if res is None:
                return
            vid = next((f for f in res.files if f.is_video), res.files[0])
            disk = os.path.join(tf["save_path"], *vid.path.split("/"))

            # 1) 下载中任务文件的分块映射必须命中（修复核心：不再按静态整文件服务）
            pm = mgr.piece_map_for_path(disk)
            ck.check(pm is not None, "下载中任务文件分块映射命中（piece_map_for_path）")
            if pm is None:
                return

            # 2) 先暂停冻结，再按需点播头部 64KB 并恢复——断言窗口内
            #    下载量被限制在头部小区间，缺失区间确定存在
            ck.check(mgr.pause_task(tid), "暂停任务（冻结下载）")
            ck.check(mgr.demand_for_path(disk, 0, 64 * 1024),
                     "点播头部 64KB（demand_for_path）")
            ck.check(mgr.resume_task(tid), "恢复任务（仅下载点播窗口）")
            ok = ts.wait_until(
                lambda: (p := mgr.piece_map_for_path(disk)) is not None
                        and contiguous_bytes(p) >= 4096,
                60, desc="头部连续前缀就绪")
            ck.check(ok, "头部连续前缀 ≥4KB")
            ck.check(mgr.pause_task(tid), "再次暂停（断言窗口冻结）")
            if not ok:
                return
            rel = os.path.relpath(disk, ws.cache).replace("\\", "/")
            srv = StreamServer(ws.cache, pieces_cb=mgr.piece_map_for_path,
                               demand_cb=mgr.demand_for_path, wait_timeout=0)
            srv.start()
            try:
                url = srv.url_for(rel)
                with urllib.request.urlopen(
                        urllib.request.Request(
                            url, headers={"Range": "bytes=0-1023"}),
                        timeout=10) as resp:
                    body = resp.read()
                    ck.check(resp.status == 206 and len(body) == 1024,
                             f"已下载头块 206（{resp.status}, {len(body)}B）")
                # 3) 缺失 piece 区间（暂停后确定存在，除非 100%）：绝不数据泄露
                pm4 = mgr.piece_map_for_path(disk)
                missing = next(
                    (p for p in range(pm4.start_piece, pm4.end_piece + 1)
                     if not pm4.have(p)), None)
                if missing is None:
                    ck.skip("任务已 100% 下载，无缺失区间可断言")
                else:
                    seg = min((missing - pm4.start_piece) * pm4.piece_length,
                              max(0, vid.size - 1024))
                    seg_resp = None
                    try:
                        with urllib.request.urlopen(
                                urllib.request.Request(
                                    url, headers={"Range": f"bytes={seg}-"}),
                                timeout=10) as resp:
                            body = resp.read()
                            seg_resp = (resp.status, len(body))
                    except urllib.error.HTTPError as e:
                        seg_resp = (e.code,
                                    e.headers.get("Content-Range", ""))
                    ck.check(seg_resp is not None
                             and seg_resp[0] in (416, 503),
                             f"缺失 piece 区间 → {seg_resp}"
                             f"（期望 416/503，非数据响应）")
                # 4) 任务级按需补拉生效（播放器要哪段先下哪段）
                tail = max(0, vid.size - 4096)
                ck.check(mgr.demand_for_path(disk, tail, vid.size),
                         "任务级按需补拉已触发（demand_for_path）")
            finally:
                srv.shutdown()
        finally:
            mgr.shutdown()
            ss = None


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    ck = ts.Checker("download_mgr_test（下载管理模块验收）")
    ck.section("下载管理模块验收（MVP 7 + 增强 2 + 边界 5+1）")
    print("  做种端拓扑：≤2 seed_mode 做种端 + 1 多任务 SessionManager（127.0.0.1 闭环）")

    # §1 任务 API 契约（t5 落地断言）
    ck.section("§1 任务 API 契约（t5 落地断言）")
    api = {"add_task", "pause_task", "resume_task", "remove_task",
           "focus_task", "tasks", "download_dir",
           "piece_map_for_path", "demand_for_path", "task_result"}
    missing = {m for m in api if not hasattr(SessionManager, m)}
    ck.check(not missing, f"任务操作 API 齐全（缺 {missing or '无'}）")
    ck.check(callable(SessionManager.tasks) and callable(SessionManager.add_task),
             "tasks/add_task 为可调用成员")

    if missing:
        ck.skip("任务 API 缺失——§2~§10 无法执行（依赖 t1/t5 落地）")
        return ck.report()

    section_add_to_downloading(ck)
    section_pause_resume(ck)
    section_remove(ck)
    section_restart_resume(ck)
    section_finished(ck)
    section_concurrent(ck)
    section_rate_limit(ck)
    section_boundaries(ck)
    section_stream_safety(ck)

    return ck.report()


if __name__ == "__main__":
    sys.exit(main())