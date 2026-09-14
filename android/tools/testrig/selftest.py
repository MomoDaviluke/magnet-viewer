"""本机自证：同一进程里「做种 + 用磁力链拉元数据 + 实际下载 + 校验字节」。

用途：在把 App 装进模拟器之前，先证明「夹具 + 磁力链(x.pe) + 协议栈」这条 seam 本身是通的。
失败时打印 libtorrent 的告警流（连接/握手/元数据/错误），便于定位是连接没建还是协议没走完。

用法：
    ./.venv/Scripts/python android/tools/testrig/selftest.py
"""
from __future__ import annotations

import hashlib
import pathlib
import shutil
import sys
import time
import warnings

import libtorrent as lt

warnings.filterwarnings("ignore")
R = pathlib.Path(__file__).resolve().parent
TORRENT = R / "fixture.torrent"
PAYLOAD = R / "payload"
LEECH = R / "leech_selftest"
SEED_PORT = 16900          # 写死端口：见下方 listen_interfaces 的注释


def dump_alerts(ses, since: float, label: str, limit: int = 8) -> None:
    out = []
    for a in ses.pop_alerts():
        name = type(a).__name__
        if isinstance(a, lt.listen_failed_alert) or "error" in name.lower() or "peer" in name.lower():
            out.append(f"{name}: {getattr(a, 'message', lambda: '')()}")
    for line in out[-limit:]:
        print(f"   [{label}] {line}")


def main() -> int:
    shutil.rmtree(LEECH, ignore_errors=True)
    LEECH.mkdir(parents=True, exist_ok=True)
    ti = lt.torrent_info(str(TORRENT))

    # ---- 做种会话 ----
    seed = lt.session({
        # 回环 + uTP 在 Windows 上会 sock_bind 失败（实测 WSAEACCES）；且 listen_interfaces 用 :0
        # 时 libtorrent 会按网卡各绑一个**不同的**随机端口，listen_port() 只返回其中一个 →
        # leecher 会拨错端口（实测连接被拒 10061）。所以这里**端口写死**。
        "listen_interfaces": f"0.0.0.0:{SEED_PORT}",
        "enable_dht": False, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
        "alert_mask": lt.alert.category_t.all_categories,
    })
    satp = lt.add_torrent_params()
    satp.ti = ti
    satp.save_path = str(R)      # 种子内部根目录名是 payload → save_path 用父目录
    sh = seed.add_torrent(satp)
    sh.set_flags(lt.torrent_flags.seed_mode)
    seed.wait_for_alert(3000)
    seed.pop_alerts()
    port = seed.listen_port()
    print(f"做种会话：is_listening={seed.is_listening()}｜端口={SEED_PORT}")

    # ---- 下载会话（磁力链语义：只有 info_hash + peer 提示）----
    leech = lt.session({
        "listen_interfaces": "0.0.0.0:0",
        "enable_dht": False, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
        "alert_mask": lt.alert.category_t.all_categories,
    })
    atp = lt.add_torrent_params()
    atp.info_hashes = ti.info_hashes()
    atp.save_path = str(LEECH)
    atp.peers = [("127.0.0.1", port)]
    lh = leech.add_torrent(atp)
    print(f"下载会话：peer 提示 [('127.0.0.1', {port})]")
    t0 = time.time()
    meta = None
    while time.time() - t0 < 30:
        if lh.status().has_metadata:
            meta = time.time() - t0
            break
        time.sleep(0.2)
    st = lh.status()
    print(f"元数据：{'✓ %.1fs' % meta if meta else '✗ 30s 超时'}｜peers={st.num_peers}｜state={st.state}")
    if not meta:
        dump_alerts(leech, t0, "leech")
        dump_alerts(seed, t0, "seed")
        return 1

    ti2 = lh.torrent_file()
    print(f"  {ti2.name()}｜文件 {ti2.num_files()} 个（含 pad）｜总 {ti2.total_size()} 字节")

    t1 = time.time()
    while time.time() - t1 < 60:
        if lh.status().total_done >= ti2.total_size():
            break
        time.sleep(0.3)
    st = lh.status()
    print(f"下载：{'✓ %.1fs' % (time.time() - t1) if st.total_done >= ti2.total_size() else '△ 未完成'}｜"
          f"已下 {st.total_done}/{ti2.total_size()} 字节｜速率 {st.download_rate} B/s")

    ok = True
    for name in ("fixture.mp4", "fixture-tail.mp4"):
        got = LEECH / "payload" / name
        if not got.exists():
            got = next((p for p in LEECH.rglob(name)), None)
        want = PAYLOAD / name
        if got and got.exists():
            same = (hashlib.sha256(got.read_bytes()).hexdigest()
                    == hashlib.sha256(want.read_bytes()).hexdigest())
            ok &= same
            print(f"  {'✓' if same else '✗'} {name} 字节一致（{got.stat().st_size}）")
        else:
            ok = False
            print(f"  ✗ {name} 未落盘")
    print("\n结论：seam " + ("可用 ✓" if ok else "有问题 ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
