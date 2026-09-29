"""缓存与吞吐性能专项验收（perf_cache_test）。

针对「磁力链解析慢、下载起速慢」的两条主链路做优化后的**功能正确性 + 改善
指标**验收。全程 127.0.0.1 闭环（本机做种），不依赖外网。

覆盖的优化项（与 core/ 内注释的 P-1~P-4 编号对应）：
  §A 元数据缓存纯函数层  —— 编码往返 / 路径防穿越 / 损坏自愈 / LRU / 命中率
  §B P-1  冷解析 vs 热解析：同一磁力链第二次解析**零网络往返**（实测耗时对比）
  §C P-1d 下载任务命中缓存：跳过 META_FETCH，入表即 DOWNLOADING
  §D P-1b 重启恢复：fastresume 带 info 段 + 元数据缓存兜底，不再全体重拉元数据
  §E P-2  会话吞吐参数生效（含真会话回读校验）
  §F P-3  告警即时唤醒：wait_for_alert vs sleep 的 A/B 延迟实测
  §G P-4  DHT 路由表状态读写的容错与落盘路径
  §H      缓存清理保留名单（.meta / .dht_state 不被误删）

用法：.\\.venv\\Scripts\\python.exe perf_cache_test.py
退出码：0=通过 1=失败 2=SKIP（依赖缺失，绝不假装通过）
"""
from __future__ import annotations

import os
import shutil
import statistics
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt  # noqa: E402
    from core import metacache, session  # noqa: E402
    from core.cache_guard import clear_cache_contents, ensure_cache_dir  # noqa: E402
    from core.fetcher import SessionManager  # noqa: E402
    from core.parser import bdecode  # noqa: E402
    from test_support import (Checker, WorkSpace, build_payload,  # noqa: E402
                              magnet_uri, make_torrent, start_seeder,
                              wait_until)
except Exception as e:          # 依赖缺失：显式 SKIP，绝不假装通过
    print(f"依赖缺失，无法执行性能专项：{e}")
    sys.exit(2)

SEED_PORT, PEER_A, PEER_B, PEER_C, PEER_D = 6971, 6972, 6973, 6974, 6975


# --------------------------------------------------------------------------
# §A 元数据缓存纯函数层
# --------------------------------------------------------------------------

def section_pure(ck: Checker, ti) -> None:
    ck.section("§A 元数据缓存：编码往返 / 防穿越 / 自愈 / LRU / 命中率")
    tmp = tempfile.mkdtemp(prefix="mv_perf_pure_")
    try:
        ih = str(ti.info_hash())
        blob = metacache.encode_meta(ti)
        ck.check(bool(blob) and len(blob) > 0, f"encode_meta 产出 {len(blob)} 字节")
        back = metacache.decode_meta(blob)
        ck.check(back is not None and str(back.info_hash()) == ih,
                 "decode_meta 往返 info_hash 一致（v2 混合种子同样成立）")
        ck.check(back is not None and back.name() == ti.name(),
                 f"往返后种子名一致（{ti.name()}）")
        ck.check(b"info" in bdecode(blob), "落盘内容含 info 段（标准 .torrent）")

        # 路径防穿越
        ck.check(metacache.safe_meta_path(tmp, "../evil") is None,
                 "非法 info_hash → safe_meta_path 返回 None（不拼路径）")
        try:
            metacache.meta_path(tmp, "tmp-123")
            ck.check(False, "非法 info_hash 走 meta_path 必须抛 ValueError")
        except ValueError:
            ck.check(True, "非法 info_hash 走 meta_path 抛 ValueError（防穿越）")

        metacache.reset_stats()
        ck.check(metacache.load_torrent_info(tmp, ih) is None,
                 "空缓存未命中返回 None")
        ck.check(metacache.stats()["misses"] == 1, "未命中计一次 miss")

        ck.check(metacache.save_torrent_info(tmp, ih, ti) is not None,
                 "save_torrent_info 落盘成功")
        hit = metacache.load_torrent_info(tmp, ih)
        ck.check(hit is not None and str(hit.info_hash()) == ih,
                 "命中缓存：取回 torrent_info 且 info_hash 一致")
        ck.check(metacache.stats()["hits"] == 1, "命中计一次 hit")
        ck.check(metacache.has_meta(tmp, ih), "has_meta 为真")
        ck.check(abs(metacache.hit_rate() - 0.5) < 1e-9,
                 f"命中率统计正确（{metacache.hit_rate():.0%}）")

        # 损坏自愈：写垃圾 → 视为未命中并删除（下次可重写）
        p = metacache.meta_path(tmp, ih)
        with open(p, "wb") as f:
            f.write(b"not-a-torrent")
        ck.check(metacache.load_torrent_info(tmp, ih) is None,
                 "损坏缓存按未命中处理（不抛异常）")
        ck.check(metacache.stats()["corrupt"] == 1, "损坏计一次 corrupt")
        ck.check(not os.path.isfile(p), "损坏文件已删除（自愈，下次可重写）")

        # LRU 剪枝
        for i in range(4):
            metacache.write_meta(tmp, "a" * 39 + str(i), b"x" * 8)
            time.sleep(0.01)
        # 2026-09-17 等待超时专项后 `save_torrent_info` 会同时落盘 v1/v2 别名键
        # （混合种子的 info_hash() ≠ v1，只落一个键会丢掉 v1 btih 的命中），
        # 于是「.meta 目录条目数」与「元数据份数」不再一一对应。改为断言真正的
        # 不变量——**剪到上限、保留最新**——期望删除数按剪前实际条目数推算。
        meta_d = metacache.meta_dir(tmp)
        before = [e.name for e in os.scandir(meta_d) if e.is_file()]
        n = metacache.prune_meta(tmp, max_files=2)
        exp = max(0, len(before) - 2)
        ck.check(n == exp,
                 f"LRU 剪枝删到上限（实删 {n}，期望 {exp}，剪前 {len(before)} 条）")
        left = sorted(os.listdir(meta_d))
        ck.check(len(left) == 2 and left[-1].startswith("aaa"),
                 f"保留的是最新的 2 条（{left}）")
        metacache.reset_stats()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# §B 冷解析 vs 热解析（P-1 核心指标）
# --------------------------------------------------------------------------

def section_resolve(ck: Checker, ti, payload_parent) -> None:
    ck.section("§B P-1 同一磁力链二次解析：零网络往返（冷/热实测对比）")
    ws = WorkSpace("mv_perf_resolve_")
    seed_ses = None
    try:
        ih = str(ti.info_hash())
        uri = magnet_uri(ih)
        seed_ses, _h = start_seeder(ti, payload_parent, SEED_PORT)
        time.sleep(0.8)

        # ---- 冷解析（缓存为空，必须走 ut_metadata 握手）----
        got_c, err_c = [], []
        m1 = SessionManager(ws.cache, listen_port=PEER_A)
        m1.on_metadata = lambda r: got_c.append(r)
        m1.on_error = lambda m: err_c.append(m)
        m1.start()
        t0 = time.time()
        m1.resolve(uri)
        m1.connect_peer("127.0.0.1", SEED_PORT)
        ok_cold = wait_until(lambda: got_c or err_c, 45, 0.1)
        t_cold = time.time() - t0
        ck.check(ok_cold and bool(got_c),
                 f"冷解析拿到元数据（{t_cold:.2f}s）{err_c[:1]}")
        ck.check(metacache.has_meta(ws.cache, ih),
                 "元数据已落盘 .meta/<ih>.torrent")
        m1.shutdown()

        # ---- 热解析（同 cache_dir，全新会话，**不连任何 peer**）----
        # 采样 3 次取中位数：单次采样在「全量回归并行负载」下抖动明显
        # （实测 0.005s ~ 0.105s），中位数才是稳定的口径。
        hits_before = metacache.stats()["hits"]
        got_h, hots, ok_hot = [], [], True
        for _i in range(3):
            got_i = []
            m2 = SessionManager(ws.cache, listen_port=PEER_B)
            m2.on_metadata = lambda r: got_i.append(r)
            m2.start()
            t1 = time.time()
            m2.resolve(uri)        # 不给 peer：命中缓存才能拿到文件树
            ok_hot = ok_hot and wait_until(lambda: got_i, 10, 0.02)
            hots.append(time.time() - t1)
            m2.shutdown()
            if got_i:
                got_h = got_i
        t_hot = statistics.median(hots)
        ck.check(ok_hot, f"热解析拿到元数据（中位 {t_hot:.3f}s，全程未连 peer）")
        ck.check(metacache.stats()["hits"] > hits_before,
                 "热解析确为缓存命中（hits 计数增加）")
        if got_h and got_c:
            same = (got_h[0].info_hash == got_c[0].info_hash
                    and got_h[0].total_size == got_c[0].total_size
                    and [f.path for f in got_h[0].view_files]
                    == [f.path for f in got_c[0].view_files])
            ck.check(same, "热解析结果与冷解析**逐字段一致**（不影响功能）")
        # 绝对阈值而非倍数：本机闭环的冷解析本身只有 0.3s 量级（做种端在同机、
        # 无需 DHT 爬路由表），倍数会被冷侧噪声放大成假红；真正的判据是
        # 「热解析只花一次磁盘读 + add_torrent，与网络无关」。
        ck.check(t_hot < 0.5, f"热解析耗时中位 {t_hot:.3f}s < 0.5s（无网络往返）")
        ck.check(t_cold > t_hot,
                 f"冷 {t_cold:.2f}s > 热 {t_hot:.3f}s（提速 "
                 f"{t_cold / max(t_hot, 1e-6):.0f}×）")
        print(f"    指标：冷解析 {t_cold:.2f}s → 热解析中位 {t_hot:.3f}s"
              f"（采样 {['%.3f' % h for h in hots]}；"
              f"本机闭环，现网 DHT 场景冷解析通常数秒~数十秒）")
    finally:
        if seed_ses is not None:
            seed_ses = None
        ws.cleanup()


# --------------------------------------------------------------------------
# §C 下载任务跳过 META_FETCH（P-1d）
# --------------------------------------------------------------------------

def section_add_task(ck: Checker, ti) -> None:
    ck.section("§C P-1d 下载任务命中缓存：入表即 DOWNLOADING，无 META_FETCH")
    ws = WorkSpace("mv_perf_task_")
    try:
        ih = str(ti.info_hash())
        uri = magnet_uri(ih)
        # 命中组：预置元数据缓存（等价于「用户先前解析过该资源」）
        metacache.save_torrent_info(ws.cache, ih, ti)
        m = SessionManager(ws.cache, listen_port=PEER_C)
        m.start()
        tid = m.add_task(uri)
        snap = {t["info_hash"]: t for t in m.tasks()}
        t_hit = snap.get(ih, {})
        ck.check(tid == ih, "add_task 返回 info_hash")
        ck.check(t_hit.get("state") != "META_FETCH",
                 f"命中缓存：任务入表即 {t_hit.get('state')}（跳过 META_FETCH）")
        ck.check(int(t_hit.get("total_size") or 0) > 0,
                 f"命中缓存：文件清单即时可用（total_size={t_hit.get('total_size')}）")
        ck.check(m.task_result(ih) is not None,
                 "命中缓存：task_result 立即可读（UI 不需要等元数据）")
        m.shutdown()

        # 对照组：无缓存 → 基线行为仍是 META_FETCH（保证未劣化既有语义）
        ws2 = WorkSpace("mv_perf_task2_")
        try:
            m2 = SessionManager(ws2.cache, listen_port=PEER_D)
            m2.start()
            m2.add_task(uri)
            snap2 = {t["info_hash"]: t for t in m2.tasks()}
            ck.check(snap2.get(ih, {}).get("state") == "META_FETCH",
                     "无缓存对照：仍按基线进入 META_FETCH（语义未变）")
            m2.shutdown()
        finally:
            ws2.cleanup()
    finally:
        ws.cleanup()


# --------------------------------------------------------------------------
# §D 重启恢复不再重拉元数据（P-1b / P-1c）
# --------------------------------------------------------------------------

def section_restore(ck: Checker, ti, payload_parent) -> None:
    ck.section("§D P-1b/c 重启恢复：fastresume 含 info 段 + 元数据缓存兜底")
    ws = WorkSpace("mv_perf_restore_")
    seed_ses = None
    try:
        ih = str(ti.info_hash())
        seed_ses, _h = start_seeder(ti, payload_parent, SEED_PORT)
        time.sleep(0.8)
        m = SessionManager(ws.cache, listen_port=PEER_A)
        m.start()
        m.add_task(magnet_uri(ih))
        m.connect_peer("127.0.0.1", SEED_PORT, task_id=ih)
        ok = wait_until(lambda: m.task_result(ih) is not None, 45, 0.2)
        ck.check(ok, "首次运行：元数据到达（做种端直连）")
        _ = wait_until(
            lambda: any(t["info_hash"] == ih and t["progress"] > 0
                        for t in m.tasks()), 30, 0.2)
        m.shutdown()

        rp = os.path.join(ws.cache, ".resume", f"{ih}.fastresume")
        if os.path.isfile(rp):
            with open(rp, "rb") as f:
                raw = f.read()
            data = bdecode(raw)
            ck.check(b"info" in data,
                     "fastresume 含 info 段（save_info_dict 生效，"
                     "恢复无需重拉元数据）")
        else:
            ck.check(False, f"fastresume 未落盘：{rp}")

        # 重启：全新会话、同一缓存目录
        m2 = SessionManager(ws.cache, listen_port=PEER_B)
        m2.start()
        restored = wait_until(
            lambda: ih in {t["info_hash"] for t in m2.tasks()}, 15, 0.2)
        ck.check(restored, "重启后任务从清单恢复")
        st = {t["info_hash"]: t for t in m2.tasks()}.get(ih, {})
        ck.check(st.get("state") != "META_FETCH",
                 f"重启恢复状态 = {st.get('state')}（未退回 META_FETCH）")
        ck.check(m2.task_result(ih) is not None,
                 "重启恢复：torrent_info 即时可用（零元数据网络往返）")
        ck.check(int(st.get("total_size") or 0) > 0,
                 f"重启恢复：文件清单完整（total_size={st.get('total_size')}）")
        m2.shutdown()
    finally:
        if seed_ses is not None:
            seed_ses = None
        ws.cleanup()


# --------------------------------------------------------------------------
# §E 会话吞吐参数（P-2）
# --------------------------------------------------------------------------

def section_throughput(ck: Checker) -> None:
    ck.section("§E P-2 会话吞吐参数：构造值 + 真会话回读")
    s = session.build_session_settings(6881, 3)
    expect = {
        "connections_limit": 400,
        "connection_speed": 60,
        "unchoke_slots_limit": 20,
        "num_optimistic_unchoke_slots": 2,
        "file_pool_size": 240,
        "cache_size": 4096,
        "prioritize_partial_pieces": True,
        "tick_interval": 250,
        "auto_manage_interval": 5,
        "max_peerlist_size": 6000,
    }
    for k, v in expect.items():
        ck.check(s.get(k) == v, f"{k} = {s.get(k)}（期望 {v}）")
    ck.check(s["active_downloads"] == 3 and s["enable_dht"] is True,
             "基线参数未被吞吐项覆盖（active_downloads / enable_dht 原样）")
    ck.check(s["proxy_type"] == 0,
             "代理项仍最后写入（无代理时显式回落直连，未被覆盖）")

    # 真会话回读：确认 libtorrent 真的接受了这些键（键名拼错会静默丢弃）
    ws = WorkSpace("mv_perf_ses_")
    try:
        m = SessionManager(ws.cache, listen_port=PEER_C)
        m.start()
        real = m._ses.get_settings()
        for k in ("unchoke_slots_limit", "connection_speed", "file_pool_size",
                  "prioritize_partial_pieces", "tick_interval",
                  "max_peerlist_size", "num_optimistic_unchoke_slots",
                  "auto_manage_interval"):
            ck.check(real.get(k) == expect[k],
                     f"真会话回读 {k} = {real.get(k)}（期望 {expect[k]}）")
        m.shutdown()
    finally:
        ws.cleanup()


# --------------------------------------------------------------------------
# §F 告警即时唤醒 A/B（P-3）
# --------------------------------------------------------------------------

class _WaitSession:
    """假会话（新行为）：wait_for_alert 阻塞到有告警（libtorrent 真实语义）。"""

    def __init__(self):
        self.alerts = []
        self.evt = threading.Event()

    def push(self, a):
        self.alerts.append(a)
        self.evt.set()

    def pop_alerts(self):
        out, self.alerts = self.alerts, []
        return out

    def wait_for_alert(self, ms):
        self.evt.wait(ms / 1000.0)
        self.evt.clear()


class _SleepSession:
    """假会话（旧行为）：**没有** wait_for_alert，只能固定 sleep 轮询。"""

    def __init__(self):
        self.alerts = []

    def push(self, a):
        self.alerts.append(a)

    def pop_alerts(self):
        out, self.alerts = self.alerts, []
        return out


def _measure_alert_latency(with_wait: bool, samples: int = 6) -> float:
    """测「告警入队 → 被 handle 」的平均延迟（秒）。"""
    from core.registry import TaskRegistry
    from core.session import SessionCore, SessionDeps
    handled, stamps = [], {}

    def on_meta(_rec):
        handled.append(time.time())

    ses = _WaitSession() if with_wait else _SleepSession()
    reg = TaskRegistry(cache_dir=tempfile.gettempdir(), ses_get=lambda: ses)
    d = SessionDeps(
        listen_port=0, active_downloads=3, lock=reg.lock,
        ses_get=lambda: ses, ses_set=lambda v: None,
        running_get=lambda: running, running_set=lambda v: None,
        thread_get=lambda: None, thread_set=lambda v: None,
        last_sweep_get=lambda: time.time(), last_sweep_set=lambda v: None,
        metadata_timeout_get=lambda: 90.0, metadata_timeout_set=lambda v: None,
        torrents_get=lambda: {}, tasks_get=lambda: {}, tasks_set=lambda v: None,
        hash_key=lambda h: "", find_record=lambda h: None,
        current_record=lambda: None, tasks_loader=lambda: {},
        restore_task=lambda t: False,
        thread_factory=lambda target: threading.Thread(target=target,
                                                       daemon=True),
        scheduler_get=lambda: None, persist_tasks=lambda: None,
        write_resume_from_alert=lambda a: None, request_resume=lambda r: None,
        drain_resume_alerts=lambda: None,
        on_metadata_received=on_meta, on_download_finished=lambda r: None,
        emit_error=lambda m: None, clear_resolving=lambda: None,
        clear_runtime_state=lambda: None)
    core = SessionCore(d)
    # 只保留分发：把 handle_alert 换成「记录时间戳」的替身
    core.handle_alert = lambda a: handled.append(time.time())  # type: ignore
    running = True
    t = threading.Thread(target=core.alert_loop, daemon=True)
    t.start()
    time.sleep(0.3)
    lat = []
    for _ in range(samples):
        ses.push(object())
        t0 = time.time()
        if wait_until(lambda: len(handled) >= len(lat) + 1, 3, 0.002):
            lat.append(handled[len(lat)] - t0)
        time.sleep(0.05)
    running = False
    t.join(timeout=3)
    return (sum(lat) / len(lat)) if lat else 999.0


def section_alert(ck: Checker) -> None:
    ck.section("§F P-3 告警响应：wait_for_alert（新）vs 固定 sleep（旧）A/B")
    lat_new = _measure_alert_latency(True)
    lat_old = _measure_alert_latency(False)
    ck.check(lat_new < 0.02,
             f"wait_for_alert：平均响应 {lat_new * 1000:.1f}ms（<20ms）")
    ck.check(lat_old > lat_new * 3,
             f"固定 sleep 对照：{lat_old * 1000:.1f}ms —— 新版快 "
             f"{lat_old / max(lat_new, 1e-6):.1f}×（≥3× 即达标）")
    print(f"    指标：告警到达→处理 平均 {lat_old * 1000:.1f}ms → "
          f"{lat_new * 1000:.1f}ms（理论上限均为 150ms 周期）")


# --------------------------------------------------------------------------
# §G DHT 路由表状态（P-4）
# --------------------------------------------------------------------------

class _StateSession:
    """假会话：只实现 save_state / load_state，用于验证状态读写容错。"""

    def __init__(self, state=None):
        self.state = state if state is not None else {b"dht": {}}
        self.loaded = None

    def save_state(self, flags=0):
        return self.state

    def load_state(self, entry, flags=0):
        self.loaded = entry


def section_dht_state(ck: Checker) -> None:
    ck.section("§G P-4 DHT 路由表持久化：路径接线 + 读写容错")
    ws = WorkSpace("mv_perf_dht_")
    try:
        from core.session import SessionCore, SessionDeps
        reg_path = os.path.join(ws.cache, ".dht_state")
        m = SessionManager(ws.cache, listen_port=PEER_C)
        ck.check(m._sess.deps.dht_state_path == reg_path,
                 f"SessionManager 传入 dht_state_path（{reg_path}）")

        # 空依赖的 SessionCore 直接验证读写（不启真会话）
        def _mk():
            return SessionDeps(
                listen_port=0, active_downloads=3, lock=threading.Lock(),
                ses_get=lambda: None, ses_set=lambda v: None,
                running_get=lambda: False, running_set=lambda v: None,
                thread_get=lambda: None, thread_set=lambda v: None,
                last_sweep_get=lambda: 0.0, last_sweep_set=lambda v: None,
                metadata_timeout_get=lambda: 90.0,
                metadata_timeout_set=lambda v: None,
                torrents_get=lambda: {}, tasks_get=lambda: {},
                tasks_set=lambda v: None, hash_key=lambda h: "",
                find_record=lambda h: None, current_record=lambda: None,
                tasks_loader=lambda: {}, restore_task=lambda t: False,
                thread_factory=lambda target: None,
                scheduler_get=lambda: None, persist_tasks=lambda: None,
                write_resume_from_alert=lambda a: None,
                request_resume=lambda r: None, drain_resume_alerts=lambda: None,
                on_metadata_received=lambda r: None,
                on_download_finished=lambda r: None,
                emit_error=lambda m: None, clear_resolving=lambda: None,
                clear_runtime_state=lambda: None, dht_state_path=reg_path)

        core = SessionCore(_mk())
        ses = _StateSession({b"dht state": b"\x01\x02\x03"})
        core._save_dht_state(ses)
        ck.check(os.path.isfile(reg_path), "退出时落盘 .dht_state（原子写）")
        ses2 = _StateSession()
        core._load_dht_state(ses2)
        ck.check(ses2.loaded is not None and b"dht state" in ses2.loaded,
                 "启动时回灌 DHT 已知节点")
        # 容错：垃圾文件 / 缺失文件不得抛异常
        with open(reg_path, "wb") as f:
            f.write(b"garbage")
        core._load_dht_state(_StateSession())
        ck.check(True, "损坏的 .dht_state 被吞掉（仅告警，不阻断启动）")
        os.remove(reg_path)
        core._load_dht_state(_StateSession())
        ck.check(True, "缺失的 .dht_state 静默跳过（首次启动）")
        # 空路由表不写空壳
        core._save_dht_state(_StateSession({}))
        ck.check(not os.path.isfile(reg_path), "路由表为空时不落空壳文件")
    finally:
        ws.cleanup()


# --------------------------------------------------------------------------
# §H 清理保留名单
# --------------------------------------------------------------------------

def section_keep(ck: Checker, ti) -> None:
    ck.section("§H 缓存清理：.meta / .dht_state 纳入保留名单")
    tmp = tempfile.mkdtemp(prefix="mv_perf_keep_")
    try:
        cache_c = os.path.join(tmp, "cache")
        ensure_cache_dir(cache_c)
        metacache.save_torrent_info(cache_c, str(ti.info_hash()), ti)
        with open(os.path.join(cache_c, ".dht_state"), "wb") as f:
            f.write(b"de")
        for rel in (".preview/ih1", "downloads/t1"):
            os.makedirs(os.path.join(cache_c, *rel.split("/")),
                        exist_ok=True)
        with open(os.path.join(cache_c, "scatter.bin"), "wb") as f:
            f.write(b"x")
        clear_cache_contents(cache_c)
        ck.check(metacache.has_meta(cache_c, str(ti.info_hash())),
                 ".meta/ 保留（清缓存不摧毁命中率）")
        ck.check(os.path.isfile(os.path.join(cache_c, ".dht_state")),
                 ".dht_state 保留")
        ck.check(os.path.isdir(os.path.join(cache_c, "downloads")),
                 "downloads/ 仍保留（既有语义不变）")
        ck.check(not os.path.exists(os.path.join(cache_c, "scatter.bin")),
                 "散文件照常清除（清理本身仍然有效）")
        ck.check(not os.path.exists(os.path.join(cache_c, ".preview")),
                 ".preview/ 照常清除（既有语义不变）")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ck = Checker("缓存与吞吐性能专项")
    print("=== 缓存与吞吐性能专项验收（127.0.0.1 闭环）===")
    root = tempfile.mkdtemp(prefix="mv_perf_")
    try:
        payload = build_payload(os.path.join(root, "payload"),
                                {"movie/demo.mp4": 400 * 1024,
                                 "pics/a.jpg": 60 * 1024})
        ti = make_torrent(payload, piece_size=16 * 1024)
        print(f"    载荷就绪：info_hash={str(ti.info_hash())}")
        section_pure(ck, ti)
        section_resolve(ck, ti, os.path.dirname(payload))
        section_add_task(ck, ti)
        section_restore(ck, ti, os.path.dirname(payload))
        section_throughput(ck)
        section_alert(ck)
        section_dht_state(ck)
        section_keep(ck, ti)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
