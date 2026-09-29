"""下载/缓存吞吐系统化诊断探针（diag_throughput）。

用途
----
「下载与缓存速度只有几十 KB/s，千兆链路（≈125 MB/s）完全用不上」时，
把**怀疑**变成**数字**。分六段，彼此独立，可指定段运行，全程零外网：

  T1 会话设置体检      —— 关键吞吐键的生效值 vs 千兆推荐值，列缺口与缺键
  T2 缓存写盘能力      —— cache_dir 顺序写 / 16KiB 随机写 MB/s（磁盘天花板）
  T3 分块校验能力      —— SHA1 吞吐（单线程/多线程），判断 hashing_threads 是否够
  T4 回环端到端吞吐    —— 本机做种 ↔ 本程序会话对拷，离线可复现的 MB/s 基准
  T5 单连接带宽时延积  —— 实际 SO_RCVBUF × RTT → 单连接上限，反推所需 peer 数
  T6 请求流水线预算    —— 在途请求覆盖的 BDP、超时/重连节奏对爬坡的影响

为什么必须有 T4
---------------
前三段是「静态体检」，只能说明参数缺不缺；只有 T4 能在**不依赖外网、不依赖
做种者**的前提下给出端到端的 MB/s 数字，从而回答唯一关键的问题：
**瓶颈在本程序的软件栈，还是在链路/swarm 供给？**
控制组用裸 libtorrent 会话 + 同一份 `build_session_settings`，
实验组用真实 `SessionManager` + `start_preview`（含调度器与 UI 轮询）。
两者之差 = 本程序自身引入的开销。

判据（本机基线 2026-09-17）
--------------------------
  T1-a 千兆必需键缺失/未生效（无论值大小）        → 见缺口清单
  T1-b 限速键（download/local_download）≠ 0      → 先排除人为限速
  T1-c dht_bootstrap_nodes 条目数 < 3            → 单点 bootstrap 失败即无 peer
  T2-a 顺序写 < 125 MB/s                         → 缓存盘先于网络成为瓶颈
  T2-b 16KiB 随机写 < 60 MB/s                    → BT 写盘模式跟不上千兆
  T3-a 单线程 SHA1 < 250 MB/s                    → hashing_threads 需 ≥4
  T4-a 回环峰值 < 60 MB/s                        → 本地软件栈存在瓶颈
  T4-b 实验组峰值较控制组下降 > 30%               → 调度器/UI 轮询在压带宽
  T5-a 单连接上限 < 10 MB/s @50ms RTT            → 必须显式放大 socket 缓冲
  T6-a 在途请求覆盖的 BDP < 目标速率 × RTT        → 请求流水线不足

用法
----
    .\\.venv\\Scripts\\python.exe diag_throughput.py            # 全部六段
    .\\.venv\\Scripts\\python.exe diag_throughput.py T1 T5 T6   # 指定段
    .\\.venv\\Scripts\\python.exe diag_throughput.py --quick    # 跳过 T4（省时）
    .\\.venv\\Scripts\\python.exe diag_throughput.py --size 64  # T4 载荷 MB（默认 128）

退出码：0=无异常发现 1=存在超阈项 2=依赖缺失（不假装通过）
"""
from __future__ import annotations

import hashlib
import os
import random
import shutil
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt  # noqa: E402
    from core import session as session_mod  # noqa: E402
    from core.fetcher import SessionManager  # noqa: E402
except Exception as e:                      # 依赖缺失：显式 SKIP，不假装通过
    print(f"依赖缺失，无法执行吞吐诊断：{e}")
    sys.exit(2)

# 长跑探针：被超时杀掉时必须保住已产出的现场（历史教训：缓冲未 flush 全丢）
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

# ---- 目标与阈值 ----------------------------------------------------------
LINK_MBPS = 1000
# 「跑满千兆」的务实目标：千兆 = 125 MB/s 线速，扣掉 BT 协议/IP/uTP 开销，
# 可用载荷速率按 90% 计 ≈ 112 MB/s。优化验收以此为准，不追 125。
TARGET_MBS = LINK_MBPS / 8 * 0.9

TH_SEQ_WRITE_MBS = 125.0        # 顺序写下限
TH_RAND_WRITE_MBS = 60.0        # 16KiB 随机写下限
TH_SHA1_MBS = 250.0             # 单线程 SHA1 下限
TH_LOOP_PEAK_MBS = 60.0         # 回环峰值下限
TH_LOOP_SCHED_DROP = 0.30       # 实验组允许的峰值跌幅
TH_SINGLE_CONN_MBS = 10.0       # 单连接下限（50ms RTT）
TH_DHT_NODES_MIN = 3            # DHT bootstrap 节点数下限
RTTS_MS = (20, 50, 100, 200)    # 参与 BDP 计算的代表性 RTT

BLOCK = 16 * 1024               # BT 块（piece 之下）固定 16KiB

# ---- 千兆必需键：键 → (推荐值, 依据) -------------------------------------
GIGABIT_KEYS: dict = {
    "recv_socket_buffer_size": (2 * 1024 * 1024,
        "接收缓冲=OS 默认(0)。单连接吞吐 ≈ 缓冲/RTT，千兆×50ms 需 ≥6MB 在途"),
    "send_socket_buffer_size": (1024 * 1024,
        "发送缓冲=OS 默认(0)。BT 是互惠协议，上行憋住会被对端 choke"),
    "send_buffer_watermark": (3 * 1024 * 1024,
        "默认 512KB：上行突发偏低，对端算我们的上传速率偏保守"),
    "send_buffer_watermark_factor": (150,
        "默认 50：与 watermark 联动，千兆上行需要更激进的水位因子"),
    "send_buffer_low_watermark": (64 * 1024,
        "默认 10KB：低水位过小会让发送队列频繁打空重填"),
    "request_queue_time": (10,
        "默认 3s：高 BDP 下请求会在队列里排超 3s 被误判超时并重发"),
    "min_reconnect_time": (20,
        "默认 60s：首次连接失败的 peer 要等 60s 才重试，直接拖慢爬坡"),
    "peer_connect_timeout": (8,
        "默认 15s：黑洞 peer 白占 15s 连接槽"),
    "hashing_threads": (4,
        "校验是纯 CPU 重活；单线程(默认 1)时校验队列堆积 = 速率不低但 have 不涨"),
    "aio_threads": (12,
        "默认 10；稀疏文件+多任务+边下边播随机写，写盘排队会反压下载"),
    "announce_to_all_trackers": (True,
        "默认 False：磁力链多 tracker 同 tier 时只问一个，peer 发现面变窄"),
    "announce_to_all_tiers": (True,
        "默认 False：同上，只问首个 tier"),
}

# 人为限速键（必须为 0，否则任何优化都无意义）
RATE_KEYS = ("download_rate_limit", "local_download_rate_limit",
             "upload_rate_limit", "local_upload_rate_limit")

RESULTS: list = []


def record(tag: str, ok: bool, detail: str) -> None:
    RESULTS.append((tag, ok, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {tag} {detail}")


def mbps(nbytes: float, seconds: float) -> float:
    return (nbytes / (1024 * 1024)) / seconds if seconds > 0 else 0.0


def _peak_mbs(samples: list, window: int = 5) -> float:
    """峰值取滑窗均值（对 0.2s 采样，window=5 ⇒ 1s 平滑），避免瞬时尖峰充数。

    入参是**字节/秒**序列，返回 MB/s —— 单位换算必须在这里做。
    历史教训：漏了这一步会把 8e9 B/s 当成 7.7e6「MB/s」写进结论，
    而 libtorrent 的 ``download_payload_rate`` 在传输**刚结束**那一拍会给出
    极短的积分窗口（128MB/16ms），瞬时值虚高两三个数量级——测峰值必须
    剔除已完成的采样点（见 ``_case_*``）。
    """
    if not samples:
        return 0.0
    w = max(1, min(window, len(samples)))
    if len(samples) < w:
        return mbps(sum(samples) / len(samples), 1.0)
    return max(mbps(sum(samples[i:i + w]) / w, 1.0)
               for i in range(len(samples) - w + 1))


def _fmt_mbs(v: float) -> str:
    return f"{v:8.2f} MB/s"


# ==========================================================================
# T1 会话设置体检
# ==========================================================================

def t1_settings() -> None:
    print("\n--- T1 会话设置体检（生效值 vs 千兆推荐值）---")
    settings = session_mod.build_session_settings(6881, 3)
    ses = lt.session(settings)
    try:
        got = ses.get_settings()
    finally:
        ses = None

    missing, below = [], []
    for k, (want, why) in GIGABIT_KEYS.items():
        if k not in got:
            missing.append(k)
            print(f"  缺键  {k:32s} 本绑定不支持（写了会被静默丢弃）")
            continue
        cur = got[k]
        same = (cur == want)
        flag = "  " if same else "!!"
        print(f"{flag}{k:32s} 当前={cur!r:>10} 推荐={want!r:>10}  {why if not same else ''}")
        if not same:
            below.append(k)
    record(f"T1-a 千兆必需键", not missing and not below,
           f"缺键 {missing or '无'}，待补 {below or '无'}（共 {len(GIGABIT_KEYS)} 项）")

    rates = {k: got.get(k, "<缺键>") for k in RATE_KEYS}
    record("T1-b 无人为限速",
           all(v == 0 for v in rates.values() if isinstance(v, int)),
           f"{rates}")

    nodes = str(got.get("dht_bootstrap_nodes", "") or "")
    n_nodes = len([x for x in nodes.split(",") if x.strip()])
    record("T1-c DHT bootstrap 节点数 ≥ %d" % TH_DHT_NODES_MIN,
           n_nodes >= TH_DHT_NODES_MIN, f"当前 {n_nodes} 个：{nodes}")

    cpu = os.cpu_count() or 1
    print(f"  环境：CPU {cpu} 核；connections_limit={got.get('connections_limit')}"
          f" active_downloads={got.get('active_downloads')}"
          f" cache_size={got.get('cache_size')}"
          f"（≈{int(got.get('cache_size', 0)) * 16 // 1024} MB 写缓存）")
    print(f"  连接性：enable_upnp={got.get('enable_upnp')}"
          f" enable_natpmp={got.get('enable_natpmp')}"
          f"（False = 只能主动外连，可连接 peer 池减半）")


# ==========================================================================
# T2 缓存写盘能力
# ==========================================================================

def t2_disk(cache_dir: str, size_mb: int = 256) -> None:
    print(f"\n--- T2 缓存写盘能力（{cache_dir}）---")
    os.makedirs(cache_dir, exist_ok=True)
    free = shutil.disk_usage(cache_dir).free
    print(f"  可用空间 {free // (1024 * 1024)} MB")

    seq_path = os.path.join(cache_dir, "_diag_seq.bin")
    rand_path = os.path.join(cache_dir, "_diag_rand.bin")
    try:
        buf = os.urandom(1024 * 1024)
        n = max(16, size_mb)
        t0 = time.perf_counter()
        with open(seq_path, "wb", buffering=0) as f:
            for _ in range(n):
                f.write(buf)
            f.flush()
            os.fsync(f.fileno())
        dt = time.perf_counter() - t0
        seq = mbps(n * 1024 * 1024, dt)
        print(f"  顺序写 {n} MB 用时 {dt:.2f}s →" + _fmt_mbs(seq))
        record("T2-a 顺序写 ≥ %.0f MB/s" % TH_SEQ_WRITE_MBS,
               seq >= TH_SEQ_WRITE_MBS, _fmt_mbs(seq))

        # 类 BT 写盘：大文件内 16KiB 随机偏移写（libtorrent 的落盘形态）
        total = 512 * 1024 * 1024
        with open(rand_path, "wb") as f:
            f.truncate(total)
        rnd = random.Random(42)
        chunk = os.urandom(BLOCK)
        ops = (64 * 1024 * 1024) // BLOCK
        t0 = time.perf_counter()
        with open(rand_path, "r+b", buffering=0) as f:
            for _ in range(ops):
                f.seek(rnd.randrange(total // BLOCK) * BLOCK)
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        dt = time.perf_counter() - t0
        rw = mbps(ops * BLOCK, dt)
        print(f"  16KiB 随机写 {ops} 次（{ops * BLOCK // (1024 * 1024)} MB）"
              f"用时 {dt:.2f}s →" + _fmt_mbs(rw)
              + f"  ({ops / dt:.0f} IOPS)")
        print(f"  换算千兆所需：{TARGET_MBS:.0f} MB/s ÷ 16KiB = "
              f"{TARGET_MBS * 1024 * 1024 / BLOCK:.0f} IOPS"
              f"（实测 {'够' if rw >= TH_RAND_WRITE_MBS else '不够'}）")
        record("T2-b 16KiB 随机写 ≥ %.0f MB/s" % TH_RAND_WRITE_MBS,
               rw >= TH_RAND_WRITE_MBS, _fmt_mbs(rw))
    finally:
        for p in (seq_path, rand_path):
            try:
                os.remove(p)
            except OSError:
                pass


# ==========================================================================
# T3 分块校验能力
# ==========================================================================

def t3_hash(size_mb: int = 256, threads: int = 4) -> None:
    print("\n--- T3 分块校验能力（SHA1 吞吐，hashlib 代理指标）---")
    print("  说明：libtorrent 用自带 SHA1（含 x86 SHA 扩展加速），与 hashlib 不同源；")
    print("        本段给的是**量级判据**，不是 libtorrent 的精确值。")
    blk = os.urandom(16 * 1024 * 1024)
    reps = max(4, size_mb // 16)

    t0 = time.perf_counter()
    for _ in range(reps):
        hashlib.sha1(blk).digest()
    one = mbps(reps * len(blk), time.perf_counter() - t0)
    print("  单线程" + _fmt_mbs(one))

    per = max(1, reps // threads)
    errs: list = []

    def worker():
        try:
            for _ in range(per):
                hashlib.sha1(blk).digest()
        except Exception as e:      # 线程内异常不得静默
            errs.append(e)

    ts = [threading.Thread(target=worker) for _ in range(threads)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    mt = mbps(per * len(blk) * threads, time.perf_counter() - t0)
    print(f"  {threads} 线程" + _fmt_mbs(mt)
          + f"  （并行效率 {mt / max(one * threads, 1e-9):.2f}）")
    need = TARGET_MBS / max(one, 1e-9)
    print(f"  千兆校验需求：{TARGET_MBS:.0f} MB/s ÷ {one:.0f} MB/s = "
          f"{need:.2f} 核 ⇒ hashing_threads 建议 ≥ {max(2, int(need) + 1)}")
    record("T3-a 单线程 SHA1 ≥ %.0f MB/s" % TH_SHA1_MBS,
           one >= TH_SHA1_MBS, _fmt_mbs(one))


# ==========================================================================
# T4 回环端到端吞吐
# ==========================================================================

def _make_payload(root: str, size_mb: int, rel: str, piece_len: int) -> str:
    """生成载荷（单文件）。首 8 字节写块序号，保证分块互异。"""
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = max(2, size_mb * 1024 * 1024 // piece_len)
    seedbuf = os.urandom(piece_len)
    with open(path, "wb", buffering=0) as f:
        for i in range(n):
            f.write(i.to_bytes(8, "little"))
            f.write(seedbuf[8:])
    return path


def _make_torrent(payload_root: str, out: str, piece_len: int):
    import warnings         # 局部 import：只为本段的 deprecated 调用降噪
    with warnings.catch_warnings():
        # 2.1 绑定把 file_storage 重载标了 deprecated，但它仍是唯一可用入口
        warnings.simplefilter("ignore", DeprecationWarning)
        fs = lt.file_storage()
        lt.add_files(fs, payload_root)
        try:
            ct = lt.create_torrent(fs, piece_size=piece_len)
        except TypeError:               # 老绑定：无 piece_size 关键字
            ct = lt.create_torrent(fs)
        lt.set_piece_hashes(ct, os.path.dirname(payload_root))
        entry = ct.generate()
    ti = lt.torrent_info(entry)
    with open(out, "wb") as f:
        f.write(lt.bencode(entry))
    return ti


def _seed_session(ti, parent: str, port: int):
    """做种端会话：只负责喂数据，参数给足以免它自己先成为瓶颈。"""
    ses = lt.session({
        "listen_interfaces": f"0.0.0.0:{port}",
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


def _summarize(series: list, elapsed: float, total: int) -> dict:
    """把 ``[(相对时刻, 累计已完成字节)]`` 折成「峰值 / 稳态 / 首字节」三件套。

    采样纪律（不遵守则数据不可用，两条都是实测踩出来的）：
      - **速率一律由累计量差分算出**，绝不采信 ``download_payload_rate``：
        它是 libtorrent 内部短窗口积分，实测在传输尾段会给出虚高数倍到几个
        数量级的瞬时值——本探针早期版本据此把 8e9 B/s 写成「7.7e6 MB/s」，
        同一轮里「1 秒窗峰值 25 MB/s」与「256MB/2.8s = 91 MB/s」互相矛盾，
        根因就在这里。
      - 峰值取 **1 秒滑窗**的最大值（窗口不足 0.5s 的点不参评），
        不用单点最大、也不用全段均值——前者被尾段虚高污染，后者把爬坡
        段和稳态段混在一起，看不出「跑不满」到底是起不来还是上不去。
    """
    if len(series) < 2:
        return dict(peak=0.0, steady=0.0, first=None, elapsed=elapsed,
                    done=series[-1][1] if series else 0, total=total,
                    series=series)
    first = next((t for t, d in series if d > 0), None)
    peak, j = 0.0, 0
    for i in range(len(series)):
        while series[i][0] - series[j][0] > 1.0:
            j += 1
        dt = series[i][0] - series[j][0]
        if dt >= 0.5:
            peak = max(peak, mbps(series[i][1] - series[j][1], dt))
    t_last, done = series[-1]
    return dict(peak=peak, steady=mbps(done, t_last) if t_last > 0 else 0.0,
                first=first, elapsed=elapsed, done=done, total=total,
                series=series)


def _case_bare(ti, tpath: str, save_dir: str, seed_port: int, leech_port: int,
               total_bytes: int, timeout: float) -> dict:
    """控制组：裸 libtorrent 会话 + 同一份 build_session_settings，不做任何调度。"""
    os.makedirs(save_dir, exist_ok=True)
    ses = lt.session(session_mod.build_session_settings(leech_port, 3))
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = save_dir
    h = ses.add_torrent(atp)
    h.resume()
    h.connect_peer(("127.0.0.1", seed_port))
    series, polls, done = [], [], 0
    t0 = time.perf_counter()
    try:
        while time.perf_counter() - t0 < timeout:
            s = h.status()
            done = int(s.total_done)
            series.append((time.perf_counter() - t0, done))
            if done >= total_bytes:
                break
            time.sleep(0.05)
        elapsed = time.perf_counter() - t0
    finally:
        ses.remove_torrent(h, 0)
        ses = None
    out = _summarize(series, elapsed, total_bytes)
    out["polls"] = polls
    return out


def _case_app(ti, tpath: str, cache_dir: str, seed_port: int, leech_port: int,
              total_bytes: int, timeout: float) -> dict:
    """实验组：真实 SessionManager + start_preview（含调度器 + UI 轮询开销）。

    轮询节奏取 0.05s，比生产 ``_refresh_status`` 的 700ms **密 14 倍**——
    宁可把本程序自身的轮询开销放大一个数量级来测，也不放过任何 GIL 争用。
    """
    got, err = [], []
    mgr = SessionManager(cache_dir, listen_port=leech_port, active_downloads=3)
    mgr.on_metadata = got.append
    mgr.on_error = err.append
    mgr.start()
    series, polls, done = [], [], 0
    t0 = time.perf_counter()
    try:
        mgr.resolve(tpath)
        mgr.connect_peer("127.0.0.1", seed_port)
        while time.perf_counter() - t0 < timeout and not got and not err:
            mgr.status()
            time.sleep(0.05)
        if not got:
            return {"error": err[0] if err else "元数据超时", "polls": polls,
                    "series": [], "peak": 0.0, "steady": 0.0, "first": None,
                    "elapsed": time.perf_counter() - t0, "done": 0,
                    "total": total_bytes}
        r = got[0]
        vid = next((f for f in r.view_files if f.is_video), None) or r.files[0]
        mgr.start_preview(vid)
        while time.perf_counter() - t0 < timeout:
            tp = time.perf_counter()
            st = mgr.status()            # UI 轮询真实成本（含全部热路径）
            polls.append((time.perf_counter() - tp) * 1000.0)
            done = int(float(st.get("total_done") or 0))
            series.append((time.perf_counter() - t0, done))
            if done >= total_bytes:
                break
            time.sleep(0.05)
        elapsed = time.perf_counter() - t0
    finally:
        mgr.shutdown()
    out = _summarize(series, elapsed, total_bytes)
    out["polls"] = polls
    return out


def _report_case(name: str, r: dict) -> None:
    """打印一组的峰值/稳态/首字节/完成度（一行，便于两组上下对照）。"""
    if r.get("error"):
        print(f"  {name} 失败：{r['error']}")
        return
    first = f"{r['first']:.2f}s" if r.get("first") is not None else "n/a"
    print(f"  {name} 峰值 {_fmt_mbs(r['peak'])}"
          f"  稳态 {_fmt_mbs(r['steady'])}"
          f"  首字节 {first:>6s}"
          f"  完成 {r['done'] * 100 // max(r['total'], 1)}%"
          f"  用时 {r['elapsed']:.1f}s")


def t4_loopback(cache_dir: str, size_mb: int, piece_len: int,
                seed_port: int = 6971, leech_port: int = 6972,
                timeout: float = 90.0) -> None:
    print(f"\n--- T4 回环端到端吞吐（{size_mb} MB / 块长 "
          f"{piece_len // 1024} KB，零外网）---")
    tmp = tempfile.mkdtemp(prefix="mv_tp_")
    seed_ses = None
    try:
        payload = os.path.join(tmp, "payload")
        _make_payload(payload, size_mb, "video/movie.mp4", piece_len)
        tpath = os.path.join(tmp, "local.torrent")
        ti = _make_torrent(payload, tpath, piece_len)
        total = os.path.getsize(os.path.join(payload, "video", "movie.mp4"))
        print(f"  载荷 {total // (1024 * 1024)} MB，分块 {ti.num_pieces()} 个"
              f"（块长 {ti.piece_length() // 1024} KB）")
        # **必须持有引用**：只写 `_seed_session(...)` 而不接住返回值，会话会被
        # 立即 GC（本地测得的表现是「两端都 0%，跑满超时」，无任何报错）。
        seed_ses = _seed_session(ti, os.path.dirname(payload), seed_port)
        time.sleep(1.5)

        bare = _case_bare(ti, tpath, os.path.join(tmp, "bare"),
                          seed_port, leech_port, total, timeout)
        _report_case("控制组(裸引擎) ", bare)
        app = _case_app(ti, tpath, os.path.join(cache_dir, "_diag_t4"),
                        seed_port, leech_port + 1, total, timeout)
        _report_case("实验组(本程序) ", app)
        if app.get("polls"):
            ps = sorted(app["polls"])
            print(f"  UI 轮询成本：中位 {ps[len(ps) // 2]:.2f} ms"
                  f"  最大 {ps[-1]:.2f} ms（{len(ps)} 次，含 status() 全链路）")

        record("T4-a 回环峰值 ≥ %.0f MB/s（控制组）" % TH_LOOP_PEAK_MBS,
               bare["peak"] >= TH_LOOP_PEAK_MBS, _fmt_mbs(bare["peak"]))
        if app.get("error"):
            record("T4-b 实验组峰值跌幅 ≤ %.0f%%" % (TH_LOOP_SCHED_DROP * 100),
                   False, f"实验组失败：{app['error']}")
            record("T4-c 首字节 ≤ 2s", False, "实验组未跑通")
        else:
            drop = (1 - app["peak"] / bare["peak"]) if bare["peak"] > 0 else 1.0
            record("T4-b 实验组峰值跌幅 ≤ %.0f%%" % (TH_LOOP_SCHED_DROP * 100),
                   drop <= TH_LOOP_SCHED_DROP,
                   f"峰值 {app['peak']:.1f} vs {bare['peak']:.1f}"
                   f" → 跌 {drop * 100:.0f}%")
            record("T4-c 首字节 ≤ 2s（回环）",
                   (app.get("first") or 99.0) <= 2.0,
                   f"首字节 {app.get('first')}")
        best = max(bare["peak"], float(app.get("peak") or 0.0))
        print(f"  判读：本机栈天花板 {best:.0f} MB/s，是目标 {TARGET_MBS:.0f} MB/s 的 "
              f"{best / TARGET_MBS * 100:.0f}%（回环 RTT≈0，最理想条件）")
        print("        磁盘/校验能力见 T2/T3；二者若远超目标而外网速率仍低，"
              "瓶颈在 swarm 供给与链路 BDP（T5/T6），不在本机 IO。")
    finally:
        seed_ses = None                 # 显式释放，避免做种端拖住端口
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(os.path.join(cache_dir, "_diag_t4"), ignore_errors=True)


# ==========================================================================
# T5 单连接带宽时延积
# ==========================================================================

def t5_bdp() -> None:
    print("\n--- T5 单连接带宽时延积（socket 缓冲 × RTT）---")
    s = socket.socket()
    default_buf = s.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    s.close()

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    cli = socket.socket()
    cli.connect(srv.getsockname())
    conn, _ = srv.accept()
    live = conn.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF)
    cli.close()
    conn.close()
    srv.close()

    configured = int(session_mod.build_session_settings(6881, 3)
                     .get("recv_socket_buffer_size", 0) or 0)
    # 0 = 交回 OS 决定，此时以实测的 OS 默认值为准；显式配置则以配置值为准。
    # 口径取**保守**的那个：Windows 会对 setsockopt 的请求值放大，按配置值算
    # 不会高估收益。
    effective = configured if configured > 0 else live
    src = "配置值" if configured > 0 else "OS 默认（未配置）"
    print(f"  OS 未连接默认 SO_RCVBUF : {default_buf // 1024} KB")
    print(f"  本机连接实得            : {live // 1024} KB（回环 RTT≈0，自动调优不增长）")
    print(f"  recv_socket_buffer_size : 配置 {configured // 1024} KB"
          f" → 判定用 {effective // 1024} KB（{src}）")
    print(f"  目标速率                : {TARGET_MBS:.1f} MB/s")
    worst = None
    for rtt in RTTS_MS:
        cap = (effective / (rtt / 1000.0)) / (1024 * 1024)
        need = TARGET_MBS / cap if cap > 0 else float("inf")
        print(f"    RTT {rtt:3d} ms → 单连接上限 {cap:7.2f} MB/s，"
              f"跑满千兆需 {need:6.1f} 条连接")
        if rtt == 50:
            worst = cap
    record("T5-a 单连接上限 ≥ %.0f MB/s @50ms RTT" % TH_SINGLE_CONN_MBS,
           worst is not None and worst >= TH_SINGLE_CONN_MBS,
           f"实测 {worst:.2f} MB/s（缓冲 {effective // 1024} KB，{src}）")
    if configured <= 0:
        print("  结论：未显式放大缓冲时，单 peer 上限被 RTT 硬卡"
              "（64KB @50ms = 1.25MB/s），真实 swarm 的 peer 数远达不到"
              "上面列出的需求值 → 必须配 recv_socket_buffer_size。")
    else:
        print(f"  结论：缓冲 {effective // 1024} KB 下 50ms RTT 单连接理论上限"
              f" {worst:.1f} MB/s；按真实 swarm 常见 10~30 个活跃 peer 计，聚合"
              f" {worst * 10:.0f}~{worst * 30:.0f} MB/s，已≥千兆目标。")


# ==========================================================================
# T6 请求流水线预算
# ==========================================================================

def t6_pipeline() -> None:
    print("\n--- T6 请求流水线预算 ---")
    settings = session_mod.build_session_settings(6881, 3)
    ses = lt.session(settings)
    try:
        got = ses.get_settings()
    finally:
        ses = None
    q = int(got.get("max_out_request_queue", 0))
    inflight = q * BLOCK
    print(f"  max_out_request_queue {q} × 16KiB = {inflight / (1024 * 1024):.1f} MB 在途")
    ok = True
    for rtt in RTTS_MS:
        bdp = TARGET_MBS * 1024 * 1024 * (rtt / 1000.0)
        enough = inflight >= bdp
        ok = ok and enough
        print(f"    RTT {rtt:3d} ms → BDP {bdp / (1024 * 1024):6.1f} MB，"
              f"在途覆盖 {'足够' if enough else '不足'}")
    record("T6-a 在途请求覆盖 BDP（全部 RTT）", ok,
           f"在途 {inflight / (1024 * 1024):.1f} MB")

    rq = int(got.get("request_queue_time", 0))
    rt = int(got.get("request_timeout", 0))
    pt = int(got.get("piece_timeout", 0))
    mr = int(got.get("min_reconnect_time", 0))
    pc = int(got.get("peer_connect_timeout", 0))
    cs = int(got.get("connection_speed", 0))
    cl = int(got.get("connections_limit", 0))
    print(f"  超时链：request_queue_time={rq}s request_timeout={rt}s"
          f" piece_timeout={pt}s")
    print(f"  爬坡链：connection_speed={cs}/s → 铺满 {cl} 连接需 "
          f"{cl / max(cs, 1):.1f}s；peer_connect_timeout={pc}s；"
          f"min_reconnect_time={mr}s（失败 peer 冷却）")
    # 判据：首连失败冷却 >30s 视为拖慢爬坡（磁力链早期 peer 失败率很高）
    record("T6-b 失败 peer 冷却 ≤ 30s", mr <= 30, f"min_reconnect_time={mr}s")
    # 判据：队列等待上限应 ≥ 最坏排队时间（高 BDP 下 request 在队列里排队）
    record("T6-c request_queue_time ≥ 10s", rq >= 10, f"当前 {rq}s")


# ==========================================================================

SECTIONS = {
    "T1": t1_settings,
    "T2": t2_disk,
    "T3": t3_hash,
    "T4": t4_loopback,
    "T5": t5_bdp,
    "T6": t6_pipeline,
}


def main(argv: list) -> int:
    quick = "--quick" in argv
    size = 256
    if "--size" in argv:
        try:
            size = max(8, int(argv[argv.index("--size") + 1]))
        except Exception:
            pass
    wanted = [a for a in argv[1:] if a in SECTIONS]
    if not wanted:
        wanted = ["T1", "T2", "T3", "T4", "T5", "T6"]
    if quick and "T4" in wanted:
        wanted.remove("T4")

    cache_dir = os.path.join(tempfile.gettempdir(), "mv_diag_tp")
    print("=" * 78)
    print(f"下载/缓存吞吐诊断：段={','.join(wanted)}  目标={TARGET_MBS:.1f} MB/s"
          f"（千兆 90%）  缓存目录={cache_dir}")
    print("=" * 78)

    t_start = time.perf_counter()
    for tag in wanted:
        fn = SECTIONS[tag]
        try:
            if tag == "T2":
                fn(cache_dir)
            elif tag == "T4":
                fn(cache_dir, size, 512 * 1024)
            else:
                fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            record(f"{tag} 执行异常", False, str(e))
    dt = time.perf_counter() - t_start

    bad = [t for t, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 78)
    print(f"汇总：{len(RESULTS)} 项判据，{len(bad)} 项超阈，用时 {dt:.1f}s")
    for tag in bad:
        detail = next(d for t, _ok, d in RESULTS if t == tag)
        print(f"  - {tag}  {detail}")
    print("退出码 " + ("1（存在超阈项）" if bad else "0（无异常发现）"))
    print("=" * 78)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
