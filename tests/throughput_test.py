"""千兆吞吐专项护栏（throughput_test）。

对应 `plan/10_gigabit_throughput_plan.md`：下载/缓存速率远低于链路带宽时，
把「千兆必需」的会话参数、发现面冗余、BDP 自洽性与**端到端不劣化**钉成护栏。

本套件与 `diag_throughput.py` 的分工：
  - 探针（diag_throughput）负责**测量与判读**，输出数字，允许有超阈项；
  - 本套件负责**守卫承诺**，只断言不会被误改回退的硬性质，秒级/十秒级。

分六段：
  §A 千兆必需键并入会话 settings（纯函数层，秒级）
  §B 真会话回读：键真实存在且值生效（拼错的键会被 libtorrent 静默丢弃）
  §C 无人为限速（否则一切优化无意义）
  §D DHT bootstrap 冗余（单点失败 = 无 peer = 几十 KB/s）
  §E BDP 自洽性（纯算术：缓冲 ÷ RTT、在途请求 × 16KiB，见论证）
  §F 回环端到端：**本程序相对裸引擎不劣化**（真 libtorrent，零外网）

为什么 §F 用相对判据而不是绝对 MB/s：
    绝对吞吐取决于跑测的机器（本机 20 核 NVMe 实测 127 MB/s，CI runner 远低于此）。
    我们真正能负责的性质是「本程序自己的调度器 / UI 轮询没有吃掉带宽」，
    该性质与机器无关，用「实验组 ≥ 控制组 × 0.5」表达最稳。绝对数字只打印不断言
    （下限 5 MB/s 仅作为「链路根本没跑起来」的哨兵，防做种会话被 GC 之类的哑失败）。

退出码：0=通过 1=失败 2=SKIP（依赖缺失，绝不假装通过）
用法：python tests/throughput_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import libtorrent as lt  # noqa: E402
    import test_support as ts  # noqa: E402
    from core import session as session_mod  # noqa: E402
    from core.fetcher import SessionManager  # noqa: E402
except Exception as e:                      # 依赖缺失：显式 SKIP
    print(f"依赖缺失，无法执行千兆吞吐护栏：{e}")
    sys.exit(2)

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# ---- 承诺的「千兆必需键」--------------------------------------------------
# 刻意在测试侧独立抄写一份（而非从 THROUGHPUT_SETTINGS 反推）：本套件的职责是
# 守住**对外承诺**，若与实现同源就失去了拦截作用（同 contract_check 的基线表）。
GIGABIT_MUST: dict = {
    # 千兆第一瓶颈：单连接吞吐 ≈ 缓冲 ÷ RTT，0 = 让 OS 决定（实测 64KB）
    "recv_socket_buffer_size": 2 * 1024 * 1024,
    # 互惠协议：上行发送缓冲憋住会被对端 choke
    "send_socket_buffer_size": 1024 * 1024,
    "send_buffer_watermark": 3 * 1024 * 1024,
    "send_buffer_watermark_factor": 150,
    "send_buffer_low_watermark": 64 * 1024,
    # 高 BDP 下请求排队不得被判超时重发
    "request_queue_time": 10,
    # 爬坡：失败 peer 冷却与黑洞 peer 占槽
    "min_reconnect_time": 20,
    "peer_connect_timeout": 8,
    # 校验/写盘并发
    "hashing_threads": 4,
    "aio_threads": 12,
    # 在途请求窗口（1500 × 16KiB = 23.4MB ≥ 200ms RTT 的千兆 BDP）
    "max_out_request_queue": 1500,
    # peer 发现面
    "announce_to_all_trackers": True,
    "announce_to_all_tiers": True,
}

RATE_KEYS = ("download_rate_limit", "local_download_rate_limit",
             "upload_rate_limit", "local_upload_rate_limit")
DHT_MIN_NODES = 3
BLOCK = 16 * 1024
TARGET_MBS = 1000 / 8 * 0.9
SEED_PORT, LEECH_PORT = 6981, 6982
PAYLOAD_MB = 64
PIECE = 512 * 1024
CASE_TIMEOUT = 45.0


# --------------------------------------------------------------------------
# §A 纯函数层
# --------------------------------------------------------------------------

def section_settings_pure(ck: ts.Checker) -> None:
    ck.section("§A 千兆必需键并入会话 settings（纯函数层）")
    st = session_mod.build_session_settings(6881, 3)
    bad = [k for k, v in GIGABIT_MUST.items() if st.get(k) != v]
    ck.check(not bad, f"{len(GIGABIT_MUST)} 个千兆必需键全部按承诺并入"
                      f"（偏差 {bad or '无'}）")
    for k, v in GIGABIT_MUST.items():
        ck.check(st.get(k) == v, f"{k} = {st.get(k)}（承诺 {v}）")
    # 缓冲类键不得退回 0：0 在 libtorrent 里等于「让 OS 决定」，实测 64KB
    ck.check(all(int(st.get(k, 0)) > 0
                 for k in ("recv_socket_buffer_size", "send_socket_buffer_size")),
             "收发缓冲均显式非 0（0 = 交回 OS，本机实测仅 64KB）")
    # 覆盖顺序：吞吐项必须能覆盖基线里的同名键（connections_limit 是典型）
    ck.check(st["connections_limit"] == session_mod.THROUGHPUT_SETTINGS
             ["connections_limit"],
             "吞吐批次覆盖基线同名键（顺序：基线→发现→吞吐→代理）")


# --------------------------------------------------------------------------
# §B 真会话回读
# --------------------------------------------------------------------------

def section_settings_live(ck: ts.Checker) -> None:
    ck.section("§B 真会话回读：键真实存在且值生效")
    st = session_mod.build_session_settings(6881, 3)
    ses = lt.session(st)
    try:
        got = ses.get_settings()
    finally:
        ses = None
    missing = [k for k in GIGABIT_MUST if k not in got]
    ck.check(not missing,
             f"全部 {len(GIGABIT_MUST)} 个键在本机 libtorrent 中真实存在"
             f"（缺失 {missing or '无'}）—— 拼错的键会被静默丢弃，不报错")
    mismatch = [k for k, v in GIGABIT_MUST.items()
                if k in got and got[k] != v]
    ck.check(not mismatch, f"真会话回读全部生效（未生效 {mismatch or '无'}）")


# --------------------------------------------------------------------------
# §C 无人为限速
# --------------------------------------------------------------------------

def section_no_throttle(ck: ts.Checker) -> None:
    ck.section("§C 无人为限速")
    st = session_mod.build_session_settings(6881, 3)
    ses = lt.session(st)
    try:
        got = ses.get_settings()
    finally:
        ses = None
    for k in RATE_KEYS:
        ck.check(got.get(k) == 0, f"{k} = 0（不限）")
    ck.check(session_mod.build_session_settings(6881, 3).get(
        "download_rate_limit", None) is None,
        "会话构造本身不写入限速（限速只由 apply_rate_limit 热更新，"
        "避免默认值把用户带宽钉死）")


# --------------------------------------------------------------------------
# §D DHT bootstrap 冗余
# --------------------------------------------------------------------------

def section_dht(ck: ts.Checker) -> None:
    ck.section("§D DHT bootstrap 冗余")
    nodes = [x.strip() for x in session_mod.DHT_BOOTSTRAP_NODES.split(",")
             if x.strip()]
    ck.check(len(nodes) >= DHT_MIN_NODES,
             f"bootstrap 节点 {len(nodes)} 个 ≥ {DHT_MIN_NODES}"
             f"（默认只有 1 个，单点不可达即整条发现链失效）")
    ck.check(all(n.count(":") == 1 and n.split(":")[1].isdigit()
                 for n in nodes),
             "节点格式均为 host:port（libtorrent 按逗号分隔解析）")
    ck.check(len(set(nodes)) == len(nodes), "节点无重复")
    ses = lt.session(session_mod.build_session_settings(6881, 3))
    try:
        got = ses.get_settings().get("dht_bootstrap_nodes", "")
    finally:
        ses = None
    ck.check(got == session_mod.DHT_BOOTSTRAP_NODES,
             f"真会话回读一致（{len(got.split(','))} 个节点）")


# --------------------------------------------------------------------------
# §E BDP 自洽性（纯算术）
# --------------------------------------------------------------------------

def section_bdp(ck: ts.Checker) -> None:
    ck.section("§E BDP 自洽性（纯算术：缓冲 ÷ RTT、在途 × 16KiB）")
    st = session_mod.build_session_settings(6881, 3)
    buf = int(st["recv_socket_buffer_size"])
    # 单连接理论下限：50ms RTT 是跨省链路的常见往返
    cap_50 = (buf / 0.050) / (1024 * 1024)
    ck.check(cap_50 >= 10.0,
             f"50ms RTT 单连接上限 {cap_50:.1f} MB/s ≥ 10（缓冲 {buf // 1024} KB）")
    inflight = int(st["max_out_request_queue"]) * BLOCK / (1024 * 1024)
    for rtt in (20, 50, 100, 200):
        bdp = TARGET_MBS * (rtt / 1000.0)
        ck.check(inflight >= bdp,
                 f"在途 {inflight:.1f} MB ≥ {rtt}ms RTT 的 BDP {bdp:.1f} MB")
    ck.check(int(st["request_queue_time"]) >= 10,
             f"request_queue_time {st['request_queue_time']}s ≥ 10"
             f"（高 BDP 下请求排队不得被判超时重发）")
    ck.check(int(st["min_reconnect_time"]) <= 30,
             f"min_reconnect_time {st['min_reconnect_time']}s ≤ 30"
             f"（失败 peer 冷却拖慢爬坡）")


# --------------------------------------------------------------------------
# §F 回环端到端（真 libtorrent）
# --------------------------------------------------------------------------

def _payload(root: str, size_mb: int, piece: int) -> str:
    """生成单文件视频载荷（首 8 字节写块序号，保证分块互异）。"""
    path = os.path.join(root, "video", "movie.mp4")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = max(2, size_mb * 1024 * 1024 // piece)
    seedbuf = os.urandom(piece)
    with open(path, "wb", buffering=0) as f:
        for i in range(n):
            f.write(i.to_bytes(8, "little"))
            f.write(seedbuf[8:])
    return path


def _torrent(payload: str, out: str, piece: int):
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        fs = lt.file_storage()
        lt.add_files(fs, payload)
        ct = lt.create_torrent(fs, piece_size=piece)
        lt.set_piece_hashes(ct, os.path.dirname(payload))
        entry = ct.generate()
    ti = lt.torrent_info(entry)
    with open(out, "wb") as f:
        f.write(lt.bencode(entry))
    return ti


def _series_stats(series: list, total: int) -> dict:
    """由**累计已完成字节**差分算速率（不采信 download_payload_rate）。"""
    if len(series) < 2:
        return {"peak": 0.0, "first": None, "done": series[-1][1] if series else 0}
    first = next((t for t, d in series if d > 0), None)
    peak, j = 0.0, 0
    for i in range(len(series)):
        while series[i][0] - series[j][0] > 1.0:
            j += 1
        dt = series[i][0] - series[j][0]
        if dt >= 0.5:
            mb = (series[i][1] - series[j][1]) / (1024 * 1024) / dt
            peak = max(peak, mb)
    return {"peak": peak, "first": first, "done": series[-1][1], "total": total}


def _seed(ti, parent: str) -> object:
    ses = lt.session({
        "listen_interfaces": f"0.0.0.0:{SEED_PORT}",
        "enable_dht": False, "enable_lsd": False,
        "enable_upnp": False, "enable_natpmp": False,
        "cache_size": 8192, "aio_threads": 12,
    })
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = parent
    atp.flags |= lt.torrent_flags.seed_mode
    ses.add_torrent(atp).resume()
    return ses


def _leech_bare(ti, save_dir: str, total: int) -> dict:
    os.makedirs(save_dir, exist_ok=True)
    ses = lt.session(session_mod.build_session_settings(LEECH_PORT, 3))
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = save_dir
    h = ses.add_torrent(atp)
    h.resume()
    h.connect_peer(("127.0.0.1", SEED_PORT))
    series = []
    t0 = time.perf_counter()
    try:
        while time.perf_counter() - t0 < CASE_TIMEOUT:
            done = int(h.status().total_done)
            series.append((time.perf_counter() - t0, done))
            if done >= total:
                break
            time.sleep(0.05)
    finally:
        ses.remove_torrent(h, 0)
        ses = None
    return _series_stats(series, total)


def _leech_app(tpath: str, cache: str, total: int) -> dict:
    got, err = [], []
    mgr = SessionManager(cache, listen_port=LEECH_PORT + 1, active_downloads=3)
    mgr.on_metadata = got.append
    mgr.on_error = err.append
    mgr.start()
    series = []
    t0 = time.perf_counter()
    try:
        mgr.resolve(tpath)
        mgr.connect_peer("127.0.0.1", SEED_PORT)
        while time.perf_counter() - t0 < CASE_TIMEOUT and not got and not err:
            mgr.status()
            time.sleep(0.05)
        if not got:
            return {"peak": 0.0, "first": None, "done": 0, "total": total,
                    "error": err[0] if err else "元数据超时"}
        r = got[0]
        vid = next((f for f in r.view_files if f.is_video), None) or r.files[0]
        mgr.start_preview(vid)
        while time.perf_counter() - t0 < CASE_TIMEOUT:
            done = int(float(mgr.status().get("total_done") or 0))
            series.append((time.perf_counter() - t0, done))
            if done >= total:
                break
            time.sleep(0.05)
    finally:
        mgr.shutdown()
    return _series_stats(series, total)


def section_loopback(ck: ts.Checker) -> None:
    ck.section("§F 回环端到端：本程序相对裸引擎不劣化（真 libtorrent，零外网）")
    tmp = tempfile.mkdtemp(prefix="mv_thr_")
    seed_ses = None
    try:
        payload = _payload(os.path.join(tmp, "payload"), PAYLOAD_MB, PIECE)
        tpath = os.path.join(tmp, "local.torrent")
        ti = _torrent(payload, tpath, PIECE)
        total = os.path.getsize(payload)
        # 必须持有引用：不接住返回值，做种会话会被立即 GC
        # （症状是「两端都 0% 且跑满超时」，无任何报错）
        seed_ses = _seed(ti, os.path.dirname(payload))
        time.sleep(1.0)

        bare = _leech_bare(ti, os.path.join(tmp, "bare"), total)
        app = _leech_app(tpath, os.path.join(tmp, "cache"), total)
        print(f"    控制组（裸 libtorrent）峰值 {bare['peak']:.1f} MB/s，"
              f"首字节 {bare['first']}，完成 {bare['done'] * 100 // total}%")
        print(f"    实验组（本程序）        峰值 {app['peak']:.1f} MB/s，"
              f"首字节 {app['first']}，完成 {app['done'] * 100 // total}%")

        ck.check(bare["peak"] >= 5.0,
                 f"控制组链路确实跑起来了（峰值 {bare['peak']:.1f} MB/s ≥ 5，"
                 f"哨兵判据）")
        ck.check(app.get("error") is None,
                 f"实验组无错误（{app.get('error') or '无'}）")
        ck.check(app["done"] >= total,
                 f"本程序把载荷完整落盘（{app['done'] * 100 // total}%）")
        # 相对判据：本程序的调度器 + UI 轮询不得吃掉一半以上带宽
        ok = bare["peak"] > 0 and app["peak"] >= bare["peak"] * 0.5
        ck.check(ok, f"实验组峰值不劣化 >50%（{app['peak']:.1f} vs "
                     f"{bare['peak']:.1f} MB/s）")
        ck.check(app["first"] is not None and app["first"] <= 5.0,
                 f"首字节 ≤5s（{app['first']}）")
    finally:
        seed_ses = None
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------

def main() -> int:
    ck = ts.Checker("throughput_test（千兆吞吐专项：参数与端到端护栏）")
    print("\n--- 千兆吞吐护栏（2026-09-17 立项）---")
    section_settings_pure(ck)
    section_settings_live(ck)
    section_no_throttle(ck)
    section_dht(ck)
    section_bdp(ck)
    section_loopback(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
