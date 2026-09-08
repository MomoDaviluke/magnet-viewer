"""混合 v2 种子（BEP-52）的端到端验证（本地种子与磁力链两条入口各跑一遍）。

补齐 REVIEW.md §四入口矩阵的「混合 v2」一列：libtorrent 2.x 的
create_torrent() **默认产出 v1+v2 混合种子**（meta version=2），其
info_hash 为 SHA-256 截断前 20 字节（P0-2 修复的分支）；历史上三个缺陷
（本地种子 cache_dir、单文件路径、v2 hash）都源于入口矩阵覆盖不全——
本脚本用真种子的两条入口把 v2 分支钉死：

- info_hash 与 libtorrent handle.info_hash() 一致（不是 SHA-1 全量）；
- 多文件层级 path（root/inner）与真实落盘一致；
- 磁力链入口：元数据到达 → 预览流服务按 f.path 供给 206。

另附纯 v2 明确报错断言（P1-1 方案 a：拒绝而非 KeyError）。

用法：python hybrid_v2_test.py
退出码：0=通过，1=失败，2=SKIP（libtorrent 版本过旧无 v2 支持）。
"""
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt
    if not (hasattr(lt.create_torrent, "v2_only")
            and hasattr(lt, "sha256_hash")):
        print("libtorrent 无 v2 支持（需 2.x），显式 SKIP")
        sys.exit(2)
except Exception as e:      # 依赖缺失：显式 SKIP，绝不假装通过
    print(f"依赖缺失，无法执行混合 v2 验收：{e}")
    sys.exit(2)

from core.fetcher import SessionManager  # noqa: E402
from core.models import file_disk_path  # noqa: E402
from core.parser import is_pure_v2, parse_torrent_file, torrent_info_hash  # noqa: E402
from core.stream_server import StreamServer  # noqa: E402

SEED_PORT, PEER_PORT = 6931, 6932
OK, FAIL = [], []
_tmp_roots = []


def check(cond, msg):
    (OK if cond else FAIL).append(msg)
    print(("  [OK] " if cond else "  [FAIL] ") + msg)


def _cleanup():
    for p in _tmp_roots:
        shutil.rmtree(p, ignore_errors=True)


def make_hybrid(tmp: str) -> tuple[str, str, str]:
    """造 v1+v2 混合多文件种子。返回 (payload 父目录, .torrent 路径, ih)。

    libtorrent 2.x 默认 flag 即混合（v1 与 v2 都写）——这里不传 flags，
    与 README「默认产出混合」的表述保持一致。
    """
    src = os.path.join(tmp, "src")
    root = os.path.join(src, "Show")
    os.makedirs(os.path.join(root, "sub"), exist_ok=True)
    for name, size in (("ep01.mkv", 400 * 1024), ("ep02.mkv", 400 * 1024),
                       ("cover.jpg", 8 * 1024)):
        with open(os.path.join(root, "sub", name), "wb") as f:
            f.write(os.urandom(size))
    fs = lt.file_storage()
    lt.add_files(fs, root)
    ct = lt.create_torrent(fs, 16 * 1024)
    lt.set_piece_hashes(ct, src)
    t = ct.generate()
    tp = os.path.join(tmp, "hybrid.torrent")
    with open(tp, "wb") as f:
        f.write(lt.bencode(t))
    info = t[b"info"]
    meta_v = info.get(b"meta version")
    assert meta_v == 2, f"造种失败：默认应产混合 v2，实得 meta version={meta_v}"
    ih = torrent_info_hash(info)
    # 交叉验证：与 libtorrent 权威 info_hash() 一致（v2 截断 20 字节语义）
    h = lt.torrent_info(tp).info_hash()
    check(str(h).lower() == ih,
          f"造种自检：parser 混合 hash == lt.info_hash()（{ih[:12]}…）")
    return src, tp, ih


def start_seeder(tp: str, src: str, port: int):
    ses = lt.session({
        "listen_interfaces": f"127.0.0.1:{port}",
        "enable_dht": False, "enable_lsd": False,
        "enable_upnp": False, "enable_natpmp": False,
    })
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(tp)
    atp.save_path = src
    atp.flags |= lt.torrent_flags.seed_mode
    ses.add_torrent(atp).resume()
    return ses


def run_case(label: str, tmp: str, source: str, info_hash: str,
             seed_src: str, seed_tp: str) -> None:
    print(f"\n----- {label} -----")
    seed_ses = start_seeder(seed_tp, seed_src, SEED_PORT)
    time.sleep(1)

    cache = os.path.join(tmp, f"cache_{abs(hash(label))}")
    got, err = [], []
    mgr = SessionManager(cache, listen_port=PEER_PORT)
    mgr.on_metadata = got.append
    mgr.on_error = err.append
    mgr.start()
    mgr.resolve(source)
    mgr.connect_peer("127.0.0.1", SEED_PORT)

    t0 = time.time()
    while time.time() - t0 < 60 and not got and not err:
        time.sleep(0.2)
    if not got:
        check(False, f"元数据获取失败：{err[0] if err else '超时'}")
        mgr.shutdown()
        del seed_ses
        return
    check(not err, f"无错误回调（err={err}）")

    r = got[0]
    # 核心断言：入口矩阵列 = 混合 v2 的 info_hash 是 SHA-256 截断值，
    # 两条入口（本地种子/磁力链）必须得到同一标识（P0-2 的立项目标）
    check(r.info_hash == info_hash,
          f"info_hash 与造种端一致（混合 v2 = SHA-256[:20]）：{r.info_hash[:12]}…")
    check(len(r.view_files) == 3, f"可见文件 3 个（实得 {len(r.view_files)}）")
    paths = [f.path for f in r.view_files]
    check(all(p.startswith("Show/") for p in paths),
          f"多文件层级 root/inner 保留：{paths}")

    vid = r.view_files[0]
    save_dir = os.path.join(mgr.cache_dir, *r.save_subdir.split("/")) \
        if getattr(r, "save_subdir", "") else mgr.cache_dir
    disk = file_disk_path(save_dir, vid)

    mgr.start_preview(vid)
    t1 = time.time()
    done = 0
    while time.time() - t1 < 60:
        st = mgr.status()
        prog = (st["file_progress"][vid.index]
                if len(st["file_progress"]) > vid.index else 0)
        done = prog
        if prog >= vid.size:
            break
        time.sleep(0.5)
    check(done >= vid.size,
          f"下载完成：{done}/{vid.size}")
    check(os.path.isfile(disk), f"file_disk_path 指向真实文件：{disk}")

    srv = StreamServer(mgr.cache_dir)
    srv.start()
    try:
        rel = "/".join([s for s in r.save_subdir.split("/") if s] + [vid.path]) \
            if getattr(r, "save_subdir", "") else vid.path
        req = urllib.request.Request(srv.url_for(rel),
                                     headers={"Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            status = resp.status
        check(status == 206 and len(body) == 1024,
              f"流服务可按 f.path 供给（status={status}, {len(body)}B）")
    except urllib.error.HTTPError as e:
        check(False, f"流服务请求失败：HTTP {e.code}（url={srv.url_for(rel)}）")
    finally:
        srv.shutdown()

    mgr.shutdown()
    del seed_ses


def _info_dict_of(tp: str) -> dict:
    from core.parser import bdecode
    with open(tp, "rb") as f:
        return bdecode(f.read())[b"info"]


def section_local(tmp: str, src: str, tp: str, ih: str) -> None:
    """入口 A 前置：解析器对混合/纯 v2 的静态断言 + 本地种子端到端。"""
    print("\n----- 入口 A 前置：解析器对混合 v2 的静态断言 -----")
    r = parse_torrent_file(tp)
    check(r.info_hash == ih, "parse_torrent_file 混合 hash 正确")
    check(not is_pure_v2(_info_dict_of(tp)),
          "混合种子 is_pure_v2=False（不误伤默认产种）")
    # 纯 v2：剥掉 v1 files 键（保留 file tree），解析必须给明确 ValueError
    info = _info_dict_of(tp)
    pure = {k: v for k, v in info.items() if k not in (b"files", b"name")}
    pure[b"meta version"] = 2
    check(is_pure_v2(pure), "构造的纯 v2 info（仅 file tree）被识别")
    fake = os.path.join(os.path.dirname(src), "purev2.torrent")
    with open(fake, "wb") as f:
        f.write(lt.bencode({b"info": pure}))
    try:
        parse_torrent_file(fake)
        check(False, "纯 v2 应抛 ValueError（明确提示），实际未抛")
    except ValueError as e:
        check("v2" in str(e) and "暂不支持" in str(e),
              f"纯 v2 → 含原因 ValueError：{e}")
    except Exception as e:
        check(False, f"纯 v2 应 ValueError 而非 {type(e).__name__}: {e}")
    # 入口 A：本地 .torrent → 预览/流服务
    run_case("入口 A：本地混合 v2 .torrent", tmp, tp, ih, src, tp)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mv_v2_")
    _tmp_roots.append(tmp)
    try:
        src, tp, ih = make_hybrid(tmp)
        section_local(tmp, src, tp, ih)
        # 入口 B：磁力链（btih=v2 hash + tracker -less，靠 connect_peer 直连）
        run_case("入口 B：磁力链（混合 v2 btih）", tmp,
                 f"magnet:?xt=urn:btih:{ih}&dn=Show", ih, src, tp)
    finally:
        _cleanup()
    print("\n" + "=" * 56)
    print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
    for m in FAIL:
        print("  X " + m)
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
