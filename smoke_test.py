"""冒烟测试（无 GUI）：
1) 用 libtorrent 生成一个含图片和视频后缀的测试种子；
2) 用自研 parser 解析，校验文件树/大小/piece 区间/info_hash；
3) 启动本地流服务并验证 Range 请求（含分块级可用性 / 尾部 moov 窗口）；
4) 校验 core/ui 模块可完整导入。
"""
import hashlib
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import libtorrent as lt  # noqa: E402

from core.config import lt_proxy_settings  # noqa: E402
from core.fetcher import SessionManager  # noqa: E402
from core.models import (PieceMap, TorrentFile, contiguous_bytes,  # noqa: E402
                         disk_root, file_disk_path, human_size,
                         range_available, safe_rel_path)
from core.parser import bencode, parse_torrent_file, torrent_info_hash  # noqa: E402
from core.stream_server import StreamServer, _is_within  # noqa: E402
from core.scheduler import PreviewScheduler, tail_piece_window  # noqa: E402


def make_test_torrent(dirpath: str) -> str:
    src = os.path.join(dirpath, "payload")
    os.makedirs(os.path.join(src, "pics"), exist_ok=True)
    files = {
        "video/demo.mp4": os.urandom(300 * 1024),
        "pics/a.jpg": os.urandom(48 * 1024),
        "pics/b.png": os.urandom(16 * 1024),
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
    out = os.path.join(dirpath, "test.torrent")
    with open(out, "wb") as f:
        f.write(lt.bencode(t.generate()))
    return out


def main():
    tmp = tempfile.mkdtemp(prefix="mv_smoke_")
    torrent = make_test_torrent(tmp)
    print("[1] 测试种子已生成:", torrent)

    r = parse_torrent_file(torrent)
    assert r.source == "torrent" and len(r.info_hash) == 40
    # libtorrent 生成种子会插入 BEP-47 .pad 对齐填充文件：索引必须与 libtorrent 一致
    # （预览时 prioritize_files / file_progress 依赖该索引），但展示与体积统计需过滤
    assert len(r.view_files) == 4 and len(r.files) >= 4, \
        f"文件数错误: 全量 {len(r.files)} / 可见 {len(r.view_files)}"
    pads = [f for f in r.files if f.is_pad]
    assert len(pads) == len(r.files) - len(r.view_files)
    sizes = {f.name: f.size for f in r.view_files}
    assert sizes["demo.mp4"] == 300 * 1024
    assert sum(f.size for f in r.view_files) == r.total_size
    for f in r.files:
        assert f.start_piece <= f.end_piece
    vid = next(f for f in r.view_files if f.is_video)
    imgs = [f for f in r.view_files if f.is_image]
    assert vid.is_video and len(imgs) == 2 and all(i.is_image for i in imgs)
    print(f"[2] parser 通过：{len(r.view_files)} 可见文件（含 {len(r.files) - len(r.view_files)} "
          f"个 .pad 填充）, 共 {human_size(r.total_size)}, "
          f"piece={r.piece_size}B, ih={r.info_hash[:16]}…")

    # ---------------- [2a] 本地 .torrent 必须注入 cache_dir ----------------
    # 主窗口用它建「磁盘绝对路径 -> TorrentFile」映射；若为空则键退化成相对路径，
    # _pieces_map 与 _demand_range 全部查不到 → 预览退化为按完整静态文件服务，
    # 把未下载的稀疏零数据喂给播放器（即此前修掉的 moov 问题会原样复发）。
    mgr_i = SessionManager(os.path.join(tmp, "cacheI"))
    mgr_i.start()
    got_i: list = []
    mgr_i.on_metadata = got_i.append
    mgr_i.resolve(torrent)
    for _ in range(100):  # 本地种子无需网络，元数据应立即回调
        if got_i:
            break
        time.sleep(0.05)
    mgr_i.shutdown()
    assert got_i, "本地 .torrent 未触发元数据回调"
    ri = got_i[0]
    assert ri.cache_dir == os.path.abspath(os.path.join(tmp, "cacheI")), \
        f"cache_dir 未注入或不一致: {ri.cache_dir!r}"
    disk_i = file_disk_path(ri.cache_dir, ri.files[0])
    assert os.path.isabs(disk_i), f"磁盘路径应为绝对路径: {disk_i}"
    assert _is_within(os.path.normpath(ri.cache_dir), os.path.normpath(disk_i))
    print(f"[2a] 本地 .torrent 注入 cache_dir 通过：{ri.cache_dir}")

    # ---------------- [2c] BEP-52 种子版本 ----------------
    # libtorrent 2.x 的 create_torrent() 默认产出 v1+v2 混合种子（meta version=2）。
    # BEP-52 规定 v2/混合种子的 info_hash 是 SHA-256，而非 v1 的 SHA-1。
    def _gen_torrent(tdir: str, flag: int | None) -> tuple[str, dict]:
        src = os.path.join(tdir, "src")
        os.makedirs(os.path.join(src, "sub"), exist_ok=True)
        for rel in ("a.mkv", "sub/b.txt"):
            p = os.path.join(src, *rel.split("/"))
            with open(p, "wb") as f:
                f.write(os.urandom(64 * 1024))
        fs = lt.file_storage()
        lt.add_files(fs, src)
        ct = lt.create_torrent(fs, flags=flag) if flag is not None \
            else lt.create_torrent(fs)
        lt.set_piece_hashes(ct, os.path.dirname(src))
        gen = ct.generate()
        out = os.path.join(tdir, "v.torrent")
        with open(out, "wb") as f:
            f.write(lt.bencode(gen))
        return out, gen[b"info"]

    # 混合种子（默认）：必须用 SHA-256，且要与 libtorrent info_hash() 一致
    d1 = os.path.join(tmp, "v2hybrid")
    os.makedirs(d1)
    tp1, info1 = _gen_torrent(d1, None)
    assert int(info1.get(b"meta version", 1)) == 2, "默认应为混合(v2)种子"
    want = str(lt.torrent_info(tp1).info_hash())
    got_h = torrent_info_hash(info1)
    assert got_h == want, f"混合种子 info_hash 错误：{got_h} != {want}"
    assert parse_torrent_file(tp1).info_hash == want, "parser 未使用 SHA-256"
    assert len(got_h) == 40, f"应统一为 40 hex（与磁力链入口一致），实际 {len(got_h)}"
    print(f"[2c] 混合 v2 种子 info_hash 通过（SHA-256 前 20 字节）：{got_h[:16]}…")

    # 纯 v1 种子：仍走 SHA-1
    d2 = os.path.join(tmp, "v1only")
    os.makedirs(d2)
    tp2, info2 = _gen_torrent(d2, lt.create_torrent.v1_only)
    assert b"meta version" not in info2, "v1_only 不应含 meta version"
    want1 = str(lt.torrent_info(tp2).info_hash())
    assert torrent_info_hash(info2) == want1, "v1 种子应走 SHA-1"
    assert parse_torrent_file(tp2).info_hash == want1
    print(f"[2c] 纯 v1 种子 info_hash 通过（SHA-1）：{want1[:16]}…")

    # 纯 v2 种子：本解析器暂不支持，必须给出明确报错而非 KeyError
    d3 = os.path.join(tmp, "v2only")
    os.makedirs(d3)
    tp3, info3 = _gen_torrent(d3, lt.create_torrent.v2_only)
    assert b"file tree" in info3 and b"files" not in info3, "应为纯 v2 结构"
    try:
        parse_torrent_file(tp3)
        raise AssertionError("纯 v2 种子应被拒绝")
    except ValueError as e:
        assert "v2" in str(e) and "file tree" in str(e), \
            f"报错信息应说明原因，实际：{e}"
    print("[2c] 纯 v2 种子给出明确报错（而非 KeyError）")

    # ---------------- [2d] taskstore / resume 纯函数持久化 ----------------
    # 无网络、无 libtorrent 会话的确定性单测（补齐 README「纯函数测试缺失」）：
    # .tasks.json 原子写/去重/损坏容错 + fastresume 路径约定/编解码。
    from core.taskstore import (load_tasks, normalize_info_hash, remove_task,
                                save_tasks, task_from_result, upsert_task,
                                encode_file, decode_file)  # noqa: E402
    from core.resume import (decode_resume, encode_resume, resume_dir,
                             resume_path, write_resume)  # noqa: E402
    from core.parser import bdecode as mv_bdecode  # noqa: E402

    # 用 [2] 已解析的本地种子构造任务记录（source=torrent 的 result）
    tdir = os.path.join(tmp, "taskstore")
    t1 = task_from_result(r, state="downloading", save_path="downloads/demo",
                          priority=1, retries=0, created_at=1000.0)
    assert t1["info_hash"] == r.info_hash and len(t1["info_hash"]) == 40
    assert len(t1["files"]) == len(r.files) and len(t1["selected"]) == len(r.view_files)
    assert decode_file(encode_file(r.files[0])) == r.files[0]
    assert decode_file({"index": "x"}) is None

    # --- 去重：同 info_hash 后写覆盖前写、大小写不敏感 ---
    tasks, replaced = upsert_task({}, t1)
    assert not replaced and len(tasks) == 1 and tasks[r.info_hash]["retries"] == 0
    t2 = dict(t1)
    t2["state"], t2["retries"] = "paused", 2
    tasks, replaced = upsert_task(tasks, t2)
    assert replaced and len(tasks) == 1
    assert tasks[r.info_hash]["state"] == "paused" and tasks[r.info_hash]["retries"] == 2
    tasks, removed = remove_task(tasks, r.info_hash.upper())   # 大小写不敏感
    assert removed and tasks == {}
    tasks, removed = remove_task(tasks, r.info_hash)           # 已删除再删 → False
    assert not removed
    try:
        upsert_task({}, {"name": "no-ih"})                     # 非法记录拒绝写入
        raise AssertionError("非法任务记录应抛 ValueError")
    except ValueError:
        pass

    # --- roundtrip：save -> load 完全一致（含中文名/特殊字符）---
    fake = dict(t1)
    fake["info_hash"] = "a" * 40
    fake["name"] = "剧集 第一季/[字幕组] 第01集 #1&x+100%.mp4"
    tasks, _ = upsert_task(tasks, t1)
    tasks, _ = upsert_task(tasks, fake)
    path_written = save_tasks(tdir, tasks)
    assert path_written == os.path.join(tdir, ".tasks.json")
    assert os.path.isfile(path_written) and not os.path.exists(path_written + ".tmp")
    loaded = load_tasks(tdir)
    assert loaded == tasks, f"save/load roundtrip 不一致：{loaded}"
    assert loaded["a" * 40]["name"] == fake["name"]             # 中文名不丢字
    print(f"[2d] taskstore roundtrip/去重通过：{len(tasks)} 任务（含中文名）原子落盘")

    # --- 损坏容错：垃圾/截断/版本超前/结构错误 → 静默空表，绝不抛异常 ---
    for junk in (b"\x00\xff\xfe not json at all",
                 b'{"version": 1, "tasks": {"',                 # 截断
                 "不是 JSON".encode("utf-8")):
        with open(path_written, "wb") as f:
            f.write(junk)
        assert load_tasks(tdir) == {}, f"损坏文件应静默返回空表: {junk[:24]!r}"
    with open(path_written, "w", encoding="utf-8") as f:        # 未来版本拒读
        f.write(json.dumps({"version": 99, "tasks": {}}))
    assert load_tasks(tdir) == {}
    for bad in (["not", "a", "dict"], {"version": 1, "tasks": []}):
        with open(path_written, "w", encoding="utf-8") as f:
            f.write(json.dumps(bad, ensure_ascii=False))
        assert load_tasks(tdir) == {}, f"结构错误应返回空表: {bad}"
    assert load_tasks(os.path.join(tmp, "no_such_cache")) == {}  # 文件不存在
    # 部分损坏：一条合法 + 一条非法（键即穿越/非法 ih）→ 只保留合法并告警
    evil = dict(t1)
    evil["info_hash"] = "../evil"                                # 路径穿越尝试
    warns = []

    def _disk_form(t):                                           # 内存态 → 磁盘形态
        d = dict(t)
        d["files"] = [encode_file(f) for f in t["files"]]
        return d

    with open(path_written, "w", encoding="utf-8") as f:
        f.write(json.dumps({"version": 1, "tasks": {
            r.info_hash: _disk_form(t1), "../evil": _disk_form(evil)}},
            ensure_ascii=False))
    loaded = load_tasks(tdir, warn=warns.append)
    assert loaded == {r.info_hash: t1}, f"合法记录应保留: {loaded}"
    assert warns and any("非法" in w for w in warns)
    print("[2d] taskstore 损坏容错通过：垃圾/截断/版本/结构/穿越键全部静默降级")

    # --- 原子替换：replace 失败不破坏旧文件，成功后无 .tmp 残留 ---
    save_tasks(tdir, tasks)
    before = open(path_written, "rb").read()
    assert before
    real_replace, os.replace = os.replace, (lambda s, d: (_ for _ in ()).throw(
        OSError("simulated replace failure")))
    try:
        try:
            save_tasks(tdir, {})                                  # 写 tmp 成功、替换失败
            raise AssertionError("save_tasks 应向上抛出 OSError")
        except OSError:
            pass
    finally:
        os.replace = real_replace
    assert open(path_written, "rb").read() == before, \
        "replace 失败不得破坏旧文件（原子性）"
    save_tasks(tdir, tasks)                                       # 恢复后正常保存
    assert load_tasks(tdir) == tasks
    assert not os.path.exists(path_written + ".tmp"), "保存后不得残留 .tmp"
    assert normalize_info_hash("A" * 40) == "a" * 40
    assert normalize_info_hash(None) is None and normalize_info_hash("zz") is None
    print("[2d] taskstore 原子替换通过：替换失败旧文件完好、成功后无残留")

    # --- fastresume：路径约定 + 编解码 + 与 libtorrent 解码器互通 ---
    ih = r.info_hash
    rp = resume_path(tdir, ih)
    assert rp == os.path.join(tdir, ".resume", f"{ih}.fastresume")
    assert os.path.dirname(rp) == resume_dir(tdir)
    assert resume_path(tdir, ih.upper()) == rp                   # 大小写归一
    for bad_ih in ("..", "ag" * 20, "x" * 39, "x" * 41, "", "zz" * 20):
        try:
            resume_path(tdir, bad_ih)
            raise AssertionError(f"非法 info_hash 应被拒绝: {bad_ih!r}")
        except ValueError:
            pass
    resume = {b"file-format": b"libtorrent resume file",
              b"file-version": 1,
              b"info-hash": bytes.fromhex(ih),
              b"pieces": bytes(range(256)),
              b"total_uploaded": 12345,
              b"num_seeds": 3,
              b"mapped_files": [b"movie/demo.mp4", b"readme.txt"],
              b"trackers": [[b"udp://tracker.example:1337/announce"]]}
    raw = encode_resume(resume)
    assert decode_resume(raw) == resume                          # roundtrip 恒等
    assert mv_bdecode(raw) == resume                             # 自研解码互通
    assert lt.bdecode(raw) == resume                             # libtorrent 互通
    for junk in (b"", b"not bencode", raw[:-3], b"l1:ae"):        # 损坏 → None
        assert decode_resume(junk) is None, f"损坏数据应返回 None: {junk[:20]!r}"
    p = write_resume(tdir, ih, resume)                           # 原子写盘 + 读回
    assert p == rp and os.path.isfile(rp) and not os.path.exists(rp + ".tmp")
    assert decode_resume(open(rp, "rb").read()) == resume
    print("[2d] fastresume 通过：路径约定/编解码 roundtrip/损坏容错/lt 互通")

    # ---------------- [2e] disk_root：任务落盘根（P0-3 配套） ----------------
    # save_subdir 相对时拼到 cache_dir；为绝对路径（download_dir 在缓存目录外）
    # 时必须原样使用——os.path.join 会把 "D:/dl/ih" 退化成盘符相对路径 "D:dl/ih"。
    _cache_abs = os.path.join(tmp, "cacheX")
    assert disk_root(_cache_abs, "") == os.path.normpath(_cache_abs)
    assert disk_root(_cache_abs, ".preview/ab12") == os.path.normpath(
        os.path.join(_cache_abs, ".preview", "ab12"))
    assert disk_root(_cache_abs, ".preview\\ab12") == os.path.normpath(
        os.path.join(_cache_abs, ".preview", "ab12"))  # 反斜杠归一
    abs_dl = os.path.join(tmp, "dl_outside", "ih")
    assert disk_root(_cache_abs, abs_dl) == os.path.normpath(abs_dl), \
        f"绝对路径必须原样使用，实际：{disk_root(_cache_abs, abs_dl)!r}"
    print("[2e] disk_root 通过：空/相对/反斜杠/绝对路径四种形态")

    # ---------------- [2b] 路径穿越防护 ----------------
    # 恶意种子可声明 path: ["..","..","Windows","win.ini"] 或绝对路径，
    # 原样拼接会让预览 / 流服务逃出缓存目录。
    cases = {
        ("..", "..", "..", "Windows", "win.ini"): "Windows/win.ini",
        ("a", "..", "b"): "a/b",
        (".", "", "c.mp4"): "c.mp4",
        ("C:\\evil", "x.mp4"): "evil/x.mp4",     # 盘符段被丢弃（\ 先归一为 /）
        ("/abs/olute.mp4",): "abs/olute.mp4",    # 根前缀被丢弃
        ("sub\\win\\back.mp4",): "sub/win/back.mp4",
        ("we:ird|na<>me?.mp4",): "we_ird_na__me_.mp4",  # Windows 非法字符替换
        ("..",): "unnamed",
    }
    for segs, expect in cases.items():
        got = safe_rel_path(*segs)
        assert got == expect, f"safe_rel_path{segs} -> {got!r}，期望 {expect!r}"
    assert safe_rel_path() == "unnamed"

    def _torrent_with_path(tdir: str, segs: list[bytes]) -> str:
        """构造 name/path 含穿越段的恶意种子。"""
        src = os.path.join(tdir, "src")
        os.makedirs(os.path.dirname(os.path.join(src, "ok.txt")), exist_ok=True)
        with open(os.path.join(src, "ok.txt"), "wb") as f:
            f.write(b"payload")
        info = {
            b"name": b"../../escaped",
            b"piece length": 16384,
            b"pieces": hashlib.sha1(b"payload").digest(),
            b"files": [{b"length": 7, b"path": segs}],
        }
        out = os.path.join(tdir, "evil.torrent")
        with open(out, "wb") as f:
            f.write(bencode({b"info": info}))
        return out

    evil = _torrent_with_path(tmp, [b"..", b"..", b"..", b"Windows", b"win.ini"])
    rv = parse_torrent_file(evil)
    f0 = rv.files[0]
    assert ".." not in f0.path.split("/") and not os.path.isabs(f0.path), \
        f"恶意路径未被净化: {f0.path}"
    cache0 = os.path.join(tmp, "cache0")
    escaped = file_disk_path(cache0, f0)
    assert _is_within(os.path.normpath(cache0), os.path.normpath(escaped)), \
        f"file_disk_path 逃出缓存目录: {escaped}"
    print(f"[2b] 路径穿越防护通过：恶意路径 {f0.path!r} 被约束在缓存目录内")
    # [2b1] bencode 防御：深层嵌套 / 超长整数 / 超长长度字段均被拒绝（防 DoS）
    from core.parser import bdecode as _bdecode
    for bad, why in ((b"l" * 100 + b"e" * 100, "深度炸弹"),
                     (b"i" + b"9" * 200 + b"e", "超长整数"),
                     (b"9" * 100 + b":x", "超长长度字段")):
        try:
            _bdecode(bad)
            raise AssertionError(f"畸形 bencode 应被拒绝：{why}")
        except ValueError:
            pass
    print("[2b1] bencode 防御通过：深度炸弹/超长整数/超长长度字段全部拒绝")

    # 流服务：直接请求穿越路径必须 404
    cache_t = os.path.join(tmp, "cacheT")
    os.makedirs(cache_t, exist_ok=True)
    with open(os.path.join(cache_t, "in.mp4"), "wb") as f:
        f.write(b"x" * 4096)
    # 同前缀兄弟目录（startswith 校验的绕过目标）
    sib = os.path.join(tmp, "cacheT_evil")
    os.makedirs(sib, exist_ok=True)
    with open(os.path.join(sib, "secret.mp4"), "wb") as f:
        f.write(b"SECRET")
    srv0 = StreamServer(cache_t)
    srv0.start()
    good_url = srv0.url_for("in.mp4")          # 完整合法 URL（含鉴权 token）
    scheme_host = good_url.rsplit("/in.mp4", 1)[0]   # http://127.0.0.1:P
    token_query = good_url.split("?", 1)[1] if "?" in good_url else ""
    for attack in ("../../../evil.torrent",
                   "%2e%2e%2f%2e%2e%2f%2e%2e%2fWindows%2fwin.ini",
                   "/../cacheT_evil/secret.mp4",
                   f"/{os.path.basename(tmp)}/cacheT_evil/secret.mp4"):
        url = f"{scheme_host}/{attack}" + (f"?{token_query}" if token_query else "")
        try:
            urllib.request.urlopen(url, timeout=5)
            raise AssertionError(f"穿越请求未被拒绝: {attack}")
        except urllib.error.HTTPError as e:
            assert e.code == 404, f"{attack} -> {e.code}，期望 404"
    with urllib.request.urlopen(good_url, timeout=5) as resp:
        assert resp.read() == b"x" * 4096, "合法路径应可正常读取"
    # [2b2] 鉴权：无 token / 伪造 Host → 403（防 DNS rebinding 与同机进程直连）
    try:
        urllib.request.urlopen(scheme_host + "/in.mp4", timeout=5)
        raise AssertionError("无 token 请求应被拒绝")
    except urllib.error.HTTPError as e:
        assert e.code == 403, e.code
    try:
        urllib.request.urlopen(
            urllib.request.Request(good_url, headers={"Host": "evil.example"}),
            timeout=5)
        raise AssertionError("伪造 Host 应被拒绝")
    except urllib.error.HTTPError as e:
        assert e.code == 403, e.code
    srv0.shutdown()
    print("[2b] 流服务目录穿越（含 %2e 编码与同前缀兄弟目录）全部返回 404")
    print("[2b2] 鉴权通过：无 token / 伪造 Host 均 403）")

    # 流服务 Range 测试
    cache = os.path.join(tmp, "cache")
    os.makedirs(cache, exist_ok=True)
    rel = vid.path
    disk = file_disk_path(cache, vid)
    os.makedirs(os.path.dirname(disk), exist_ok=True)
    with open(disk, "wb") as f:
        f.write(os.urandom(300 * 1024))

    srv = StreamServer(cache)
    srv.start()
    url = srv.url_for(rel)
    req = urllib.request.Request(url, headers={"Range": "bytes=100-199"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        body = resp.read()
        assert resp.status == 206 and len(body) == 100, (resp.status, len(body))
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.status == 200 and len(resp.read()) == 300 * 1024
    srv.shutdown()
    print("[3] 流服务通过：Range(206) 与全量(200) 均正确, url =", url[:48] + "…")

    # 流服务「已下载前缀钳制」测试（边下边播核心：不暴露稀疏零数据）
    state = {"avail": 100 * 1024}
    srv2 = StreamServer(cache, avail_cb=lambda p: state["avail"], wait_timeout=0)
    srv2.start()
    url2 = srv2.url_for(rel)
    with urllib.request.urlopen(url2, timeout=5) as resp:      # 全量 → 钳制到前缀
        body = resp.read()
        assert resp.status == 200 and len(body) == 100 * 1024, (resp.status, len(body))
    req = urllib.request.Request(url2, headers={"Range": "bytes=0-99"})
    with urllib.request.urlopen(req, timeout=5) as resp:       # 前缀内 Range → 206
        assert resp.status == 206 and len(resp.read()) == 100
    try:                                                        # 超出前缀 → 416
        urllib.request.urlopen(urllib.request.Request(
            url2, headers={"Range": f"bytes={150*1024}-"}), timeout=5)
        raise AssertionError("应当返回 416")
    except urllib.error.HTTPError as e:
        assert e.code == 416, e.code
    state["avail"] = 0
    try:                                                        # 零前缀 → 503
        urllib.request.urlopen(url2, timeout=5)
        raise AssertionError("应当返回 503")
    except urllib.error.HTTPError as e:
        assert e.code == 503, e.code
    srv2.shutdown()
    print("[3b] 前缀钳制通过：200 钳制 / 206 前缀内 / 416 超界 / 503 未就绪")

    # 中文 / 特殊字符文件名端到端往返（真实种子极常见）
    cache_cn = os.path.join(tmp, "cacheCN")
    os.makedirs(os.path.join(cache_cn, "剧集 第一季"), exist_ok=True)
    weird = "剧集 第一季/[字幕组] 第01集 测试#1&x+100%.mp4"
    payload = os.urandom(64 * 1024)
    with open(os.path.join(cache_cn, *weird.split("/")), "wb") as f:
        f.write(payload)
    srv_cn = StreamServer(cache_cn)
    srv_cn.start()
    url_cn = srv_cn.url_for(weird)
    assert "%" in url_cn and " " not in url_cn, f"URL 未正确编码: {url_cn}"
    with urllib.request.urlopen(url_cn, timeout=5) as resp:
        body = resp.read()
    assert body == payload, f"内容不一致：{len(body)} != {len(payload)}"
    # 特殊文件名同样受穿越防护约束（不能借编码绕过）
    try:
        urllib.request.urlopen(srv_cn.url_for("../evil.torrent"), timeout=5)
        raise AssertionError("编码后的穿越请求未被拒绝")
    except urllib.error.HTTPError as e:
        assert e.code == 404, e.code
    srv_cn.shutdown()
    print("[3b2] 中文/特殊字符文件名往返通过：编码、读取一致，且仍受穿越防护约束")

    # 尾部索引窗口（moov 优先）纯逻辑测试
    mp4 = TorrentFile(0, "root/demo.mp4", 100 * 1024 * 1024, 0, 0, 6399)
    mkv = TorrentFile(1, "root/demo.mkv", 100 * 1024 * 1024, 0, 0, 6399)
    win = tail_piece_window(mp4, 16 * 1024)
    assert win and win == list(range(6399 - len(win) + 1, 6400)), win[:3]
    assert win[-1] == 6399 and len(win) <= 128  # 尾部窗口必含末块、有上限
    assert tail_piece_window(mkv, 16 * 1024) == []  # 非 MP4 家族不补拉
    assert tail_piece_window(mp4, 0) == []
    tiny = TorrentFile(2, "root/a.mp4", 300 * 1024, 0, 0, 18)
    assert tail_piece_window(tiny, 16 * 1024) == list(range(19))  # 小文件=整文件
    print(f"[3c] 尾部索引窗口通过：MP4 末块 {win[-1]} 窗口 {len(win)} 块 / MKV 不补拉")

    # 调度器预约窗口（假句柄）：seek 必须立即预约、点播必须有上限、
    # 滚动度量必须跟随播放位置（否则 seek 后 seek 点附近零预约）
    class _FakeTI:
        def __init__(self, pl, n):
            self._pl, self._n = pl, n

        def piece_length(self):
            return self._pl

        def num_files(self):
            return self._n

    class _FakeHandle:
        def __init__(self, pl, n):
            self.ti = _FakeTI(pl, n)
            self.have = set()
            self.deadlines = []
            self.prios = None
            self.cleared = 0

        def torrent_file(self):
            return self.ti

        def have_piece(self, p):
            return p in self.have

        def set_piece_deadline(self, p, d):
            self.deadlines.append(p)

        def clear_piece_deadlines(self):
            self.deadlines.clear()
            self.cleared += 1

        def prioritize_files(self, prios):
            self.prios = list(prios)

        def unset_flags(self, _f):
            pass

        def set_flags(self, _f):
            pass

        def resume(self):
            pass

        def pause(self):
            pass

    pl_s = 1024 * 1024
    f_sched = TorrentFile(0, "root/demo.mp4", 100 * pl_s, 0, 0, 99)   # 100 块
    h_sched = _FakeHandle(pl_s, 3)
    sched = PreviewScheduler()
    sched.begin(h_sched, f_sched)
    dl = set(h_sched.deadlines)
    assert dl >= set(range(0, 61)), "开播顺序窗口未预约"
    assert dl >= {96, 97, 98, 99}, "尾部 moov 窗口未预约"
    assert h_sched.prios == [4, 0, 0], h_sched.prios
    # 拖动到 80MB：必须立即预约 seek 点起的窗口（旧实现只改锚点、不预约）
    h_sched.deadlines.clear()
    sched.seek_to_byte(80 * pl_s)
    dl = set(h_sched.deadlines)
    assert 80 in dl, "seek 后未立即预约 seek 点"
    assert dl >= set(range(80, 100)), sorted(dl)[:5]
    # 点播区间必须有上限：拖动的 `bytes=X-`（到文件尾）不能把剩余全文件置 ASAP
    h_sched.deadlines.clear()
    sched.request_range(0, 100 * pl_s)
    assert len(set(h_sched.deadlines)) <= 60, f"点播无上限：{len(set(h_sched.deadlines))} 块"
    assert min(h_sched.deadlines) == 0, "点播未从请求起点开始"
    # 尾部 moov 点播不得把顺序窗口锚点推到文件尾（否则顺序窗口永久停摆）
    sched.request_range(99 * pl_s, 100 * pl_s)
    h_sched.deadlines.clear()
    sched.tick()
    assert h_sched.deadlines, "尾部点播后顺序窗口停摆"
    # seek 之后：窗口尚未覆盖播放位置时，tick 必须从**播放位置**起预约。
    # 旧实现以「从文件头的连续前缀」为度量 —— seek 后文件头前缀仍停在
    # 旧位置 → 窗口恒被判定为「充裕」→ 永不滚动（seek 点零预约）。
    sched.seek_to_byte(80 * pl_s)
    sched._scheduled_to = 79          # 白盒：模拟窗口尚未覆盖 seek 点
    h_sched.deadlines.clear()
    sched.tick()
    assert set(h_sched.deadlines) >= {80, 81, 82}, \
        f"seek 后 tick 未从播放位置起预约：{sorted(set(h_sched.deadlines))[:5]}"
    # 播放位置之后已下载时，窗口应继续向前滚动（旧实现会退回文件头重排）
    sched.seek_to_byte(10 * pl_s)
    h_sched.have.update(range(10, 70))
    sched._scheduled_to = 9           # 白盒：窗口尚未覆盖
    h_sched.deadlines.clear()
    sched.tick()
    assert h_sched.deadlines and max(h_sched.deadlines) >= 70, \
        f"下载推进后窗口未向前滚动：{sorted(set(h_sched.deadlines))[-3:]}"
    # 跳转必须清掉旧位置的 ASAP 预约：残留的远端分块仍在最高优先级并行
    # 下载，会与新播放位置争带宽 —— 症状是「拖到未缓存区再拖回已缓存区
    # 反而卡顿」。清完要重建尾部 moov 窗口与新位置窗口。
    sched.seek_to_byte(80 * pl_s)
    assert 80 in set(h_sched.deadlines), "未预约新跳转位置"
    h_sched.have.update(range(20, 100))    # 拖回的目标区域已缓存（用户场景）
    sched.seek_to_byte(20 * pl_s)
    after = set(h_sched.deadlines)
    assert 20 in after, "拖回后未预约新位置"
    # 已缓存区域无需再向前预约，残留只能来自旧跳转窗口
    assert not (after & {85, 86, 87}), \
        f"旧跳转窗口残留：{sorted(after & set(range(80, 100)))}"
    assert h_sched.cleared >= 1, "seek 未清理旧 deadline"
    assert after >= {96, 97, 98, 99}, "清理后未重建尾部 moov 窗口"
    print("[3c3] 调度器预约窗口通过：seek 立即预约 / 点播有上限 / 窗口随播放位置滚动 / 跳转清理残留")

    # 分块映射：连续前缀 + 任意区间可用性（moov 在尾部的判定基础）
    pl, size = 16 * 1024, 300 * 1024
    have = {0, 1, 2}   # 头部 3 块 + 尾部 2 块（中间缺失模拟稀疏空洞）
    have |= {17, 18}
    pm = PieceMap(pl, 0, 0, (size - 1) // pl, size, have.__contains__)
    assert contiguous_bytes(pm) == 3 * pl
    assert range_available(pm, 0, 100) and range_available(pm, 17 * pl, size)
    assert not range_available(pm, 100, 17 * pl)  # 跨空洞不可读
    assert range_available(pm, 20, 10)  # 空区间恒真
    print("[3d] 分块映射通过：头部连续 48KB，尾部窗口可精确服务")

    # 流服务「分块级可用性」测试：头部+尾部已下载、中间空洞 → 只服务就绪区间
    # wait_timeout=0：保留"未就绪立即 416"的既有行为（本组用例不测等待）
    srv3 = StreamServer(cache, pieces_cb=lambda path: pm if path == disk else None,
                        wait_timeout=0)
    srv3.start()
    url3 = srv3.url_for(rel)
    with urllib.request.urlopen(url3, timeout=5) as resp:      # 无 Range → 连续前缀
        body = resp.read()
        assert resp.status == 200 and len(body) == 3 * pl, (resp.status, len(body))
    req = urllib.request.Request(url3, headers={"Range": "bytes=0-99"})
    with urllib.request.urlopen(req, timeout=5) as resp:       # 前缀内 → 206
        assert resp.status == 206 and len(resp.read()) == 100
    req = urllib.request.Request(url3, headers={"Range": "bytes=200000-"})
    try:                                                        # 空洞中段 → 416
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("应当返回 416")
    except urllib.error.HTTPError as e:
        assert e.code == 416, e.code
    req = urllib.request.Request(url3, headers={"Range": "bytes=-100"})
    with urllib.request.urlopen(req, timeout=5) as resp:       # 后缀 → 逻辑大小相对
        body = resp.read()
        assert resp.status == 206 and len(body) == 100, (resp.status, len(body))
        assert body == open(disk, "rb").read()[-100:]
    try:                                                        # 空洞段 → 416 + 逻辑总长
        urllib.request.urlopen(urllib.request.Request(
            url3, headers={"Range": "bytes=49152-200000"}), timeout=5)
        raise AssertionError("应当返回 416")
    except urllib.error.HTTPError as e:
        assert e.code == 416 and e.headers.get("Content-Range") == f"bytes */{size}", \
            (e.code, e.headers.get("Content-Range"))
    srv3.shutdown()
    print("[3e] 分块级流服务通过：206 就绪区间 / 416 空洞 / 后缀相对逻辑大小")

    # 「点播 + 等待」：未就绪区间应先向调度器点播、等数据到达后服务，而非立刻 416
    # （这是 MP4 尾部 moov 探测成功的关键：直接 416 会被 FFmpeg 判为 moov not found）
    have2 = {0, 1, 2}
    demanded = []
    pm2 = PieceMap(pl, 0, 0, (size - 1) // pl, size, lambda p: p in have2)

    def demand(path, start, end_excl):
        demanded.append((start, end_excl))

        def fill():
            time.sleep(0.3)
            have2.update(range(0, (size - 1) // pl + 1))  # 模拟数据陆续到达
        threading.Thread(target=fill, daemon=True).start()

    srv4 = StreamServer(cache, pieces_cb=lambda path: pm2 if path == disk else None,
                        demand_cb=demand, wait_timeout=5.0)
    srv4.start()
    url4 = srv4.url_for(rel)
    req = urllib.request.Request(url4, headers={"Range": f"bytes={10 * pl}-{11 * pl - 1}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read()
        assert resp.status == 206 and len(body) == pl, (resp.status, len(body))
    assert demanded and demanded[0][0] == 10 * pl, f"未触发点播: {demanded}"
    srv4.shutdown()

    # 等待超时后仍不可满足 → 退化为 416（不挂死连接）
    srv5 = StreamServer(cache, pieces_cb=lambda path: pm if path == disk else None,
                        demand_cb=lambda p, s, e: None, wait_timeout=0.5)
    srv5.start()
    req = urllib.request.Request(srv5.url_for(rel), headers={"Range": "bytes=200000-"})
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("应当返回 416")
    except urllib.error.HTTPError as e:
        assert e.code == 416, e.code
    srv5.shutdown()
    print("[3f] 点播+等待通过：未就绪区间先点播再等待，超时才退化为 416")

    # 拖动进度条（seek）：FFmpeg 中断当前连接后发无上界请求 `bytes=X-`
    # （X 到文件尾）。旧实现要求「整个剩余区间就绪」→ 大文件必然等满超时
    # 416 → FFmpeg 判为致命错误、播放器进入 ErrorState（症状：拖动后卡死、
    # 进度条失灵）。新实现渐进服务：start 起就绪多少发多少，FFmpeg 读尽
    # Content-Length 后自动续发下一段 Range（与顺序播放同一机制）。
    pl3, size3 = 1024 * 1024, 10 * 1024 * 1024
    disk3 = os.path.join(cache, "seek.mp4")
    with open(disk3, "wb") as f:
        f.write(os.urandom(6 * pl3))       # 只验证前 6 块内容，其余留零
        f.truncate(size3)
    have3 = {0, 1, 5}                      # 头部 2 块 + seek 目标（第 6 块缺失）
    pm3 = PieceMap(pl3, 0, 0, (size3 - 1) // pl3, size3, lambda p: p in have3)
    demanded3 = []
    srv6 = StreamServer(
        cache, pieces_cb=lambda p: pm3 if p == os.path.normpath(disk3) else None,
        demand_cb=lambda p, s, e: demanded3.append((s, e)), wait_timeout=2.0)
    srv6.start()
    url6 = srv6.url_for("seek.mp4")
    t0 = time.time()
    req = urllib.request.Request(url6, headers={"Range": f"bytes={5 * pl3}-"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        cost = time.time() - t0
        assert resp.status == 206, resp.status
        assert len(body) == pl3, f"应只发就绪的 1 块，实际 {len(body)}"
        assert body == open(disk3, "rb").read()[5 * pl3:6 * pl3], "seek 处内容不一致"
        assert resp.headers.get("Content-Range", "").endswith(f"/{size3}"), \
            "Content-Range 总长应为逻辑大小"
        assert cost < 1.5, f"未渐进服务（等了 {cost:.1f}s，疑似仍在等整个区间）"
    assert demanded3 and demanded3[0][0] == 5 * pl3, f"未点播 seek 区间: {demanded3}"
    # seek 点之后连续多块就绪 → 一次给出整个就绪前缀（3 块）
    have3.update({6, 7})
    req = urllib.request.Request(url6, headers={"Range": f"bytes={5 * pl3}-"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        assert resp.status == 206 and len(body) == 3 * pl3, (resp.status, len(body))
    # seek 点完全未就绪 → 仍退化为 416（渐进服务不改「无数据可给」的语义）
    req = urllib.request.Request(url6, headers={"Range": f"bytes={8 * pl3}-"})
    try:
        urllib.request.urlopen(req, timeout=30)
        raise AssertionError("应当返回 416")
    except urllib.error.HTTPError as e:
        assert e.code == 416, e.code
    srv6.shutdown()
    os.remove(disk3)
    print("[3f2] 拖动进度条渐进服务通过：就绪前缀即时 206（未等满超时），无数据仍 416")

    # 配置映射：应用代理配置 → libtorrent 设置键值（纯函数）
    from core.config import lt_proxy_settings
    direct = lt_proxy_settings(None)
    assert direct == {"proxy_type": 0, "proxy_peer_connections": False,
                      "proxy_tracker_connections": False}  # 直连同时重置 tracker
    m = lt_proxy_settings({"type": "socks5", "host": "127.0.0.1", "port": 1080,
                           "peer": True})
    assert m["proxy_type"] == 2 and m["proxy_hostname"] == "127.0.0.1"
    assert m["proxy_port"] == 1080 and m["proxy_peer_connections"] is True
    assert m["proxy_tracker_connections"] is True
    mp = lt_proxy_settings({"type": "http", "host": "p", "port": 8080,
                            "user": "u", "pass": "x"})
    assert mp["proxy_type"] == 5 and mp["proxy_username"] == "u"   # http + 账号 → http_pw
    assert lt_proxy_settings({"type": "socks5", "host": ""})["proxy_type"] == 0  # 空主机回落直连
    print("[3g] 代理配置映射通过：直连/socks5/http_pw/空主机回落（含 tracker 重置）")

    # 会话启动参数：代理 + 自定义元数据超时
    mgr = SessionManager(os.path.join(tmp, "px_cache"), listen_port=6893)
    mgr.start(proxy={"type": "none"}, metadata_timeout=45)
    assert mgr.metadata_timeout == 45
    mgr.shutdown()
    print("[3h] 会话启动参数通过：metadata_timeout=45 生效")

    # [3h2] 流服务多根：download_dir 在缓存目录之外时任务文件仍可服务（P0-3 配套）
    ext_root = os.path.join(tmp, "dl_outside")
    os.makedirs(ext_root, exist_ok=True)
    ext_payload = os.urandom(48 * 1024)
    with open(os.path.join(ext_root, "movie.mp4"), "wb") as f:
        f.write(ext_payload)
    srv_ext = StreamServer(cache, bases=[ext_root])
    srv_ext.start()
    # url_for 携带绝对落盘路径（_stream_rel 在 save_subdir 为绝对路径时的形态）
    url_ext = srv_ext.url_for(os.path.join(ext_root, "movie.mp4"))
    with urllib.request.urlopen(url_ext, timeout=5) as resp:
        body = resp.read()
        assert resp.status == 200 and body == ext_payload, \
            f"多根下的绝对路径应可服务：{resp.status} {len(body)}"
    # 多根之外的绝对路径仍受穿越防护拒绝（base_dirs 不覆盖 → 404）
    outside = os.path.join(tmp, "outside_secret.txt")
    with open(outside, "w") as f:
        f.write("SECRET")
    try:
        urllib.request.urlopen(srv_ext.url_for(outside), timeout=5)
        raise AssertionError("多根之外的绝对路径应被拒绝")
    except urllib.error.HTTPError as e:
        assert e.code == 404, e.code
    srv_ext.shutdown()
    print("[3h2] 流服务多根通过：download_dir 外文件可服务、根外绝对路径仍 404")

    # 模块导入完整性
    from ui.file_tree import FileTreeWidget  # noqa: F401
    from ui.gallery import GalleryWidget  # noqa: F401
    from ui.main_window import MainWindow  # noqa: F401
    from ui.preview_pane import PreviewPane  # noqa: F401
    from ui.preview_player import VideoPreviewWidget  # noqa: F401
    from ui.status_panel import StatusPanel  # noqa: F401
    from ui.downloads_pane import DownloadsPane, format_eta  # noqa: F401
    from ui.add_download_dialog import AddDownloadDialog  # noqa: F401
    # 注意：不要再在**函数内** import 顶层已导入的名字（如 PreviewScheduler）——
    # 那会让它变成整个函数的局部名，函数前段使用会报 UnboundLocalError。
    print("[4] 全部模块导入通过")

    print("\n=== 冒烟测试全部通过 ===")


if __name__ == "__main__":
    main()
