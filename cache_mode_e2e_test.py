"""缓存模式真链路验收（cache_mode_e2e）：关预览转正 → 继续缓存 → 重启续传 → 清理/配额保护。

与 moov/gui 测试不同，本套件起**真 libtorrent 会话**：本机做种端 127.0.0.1 闭环供数据，
SessionManager 走真解析/真落盘/真 fastresume，验证 plan/06 迅雷式缓存的生命周期承诺
（假句柄单测覆盖不到落盘/续传/清理三者的真实交互）。

两个实测坑（保留注释防后人踩）：
- libtorrent 的 download_rate_limit 只管远端 peer，本机/局域网 peer 走
  local_download_rate_limit（默认 0=不限），不补设则 48MB 秒下完、看不到"未完成"窗口；
- 目录字节和恒等于文件大小（预分配存储），不能当进度用——真进度看 file_progress。

用法：.venv/Scripts/python cache_mode_e2e_test.py（退出码 0/1/2 约定同全项目）
"""
from __future__ import annotations

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_support import (Checker, WorkSpace, build_payload, dir_byte_snapshot,
                          magnet_uri, make_torrent, start_seeder, wait_until)

from core import cache_guard, cache_quota           # noqa: E402
from core.fetcher import SessionManager             # noqa: E402
from core.models import file_disk_path              # noqa: E402

SEED_PORT, PEER_PORT = 6911, 6912
VID = "movie/demo.mp4"
VID_BYTES = 48 * 1024 * 1024        # 48MB：2MB/s 下约 25s，保证关预览/重启时都还没下完
RATE_KBPS = 2048


def main() -> int:
    ck = Checker("缓存模式真链路验收")
    ws = WorkSpace("mv_d6_")
    os.makedirs(ws.cache, exist_ok=True)
    seed_ses = None
    mgr = mgr2 = None
    try:
        payload = build_payload(ws.payload, {VID: VID_BYTES,
                                             "pics/a.jpg": 200 * 1024,
                                             "readme.txt": 1024})
        ti = make_torrent(payload)
        ih = str(ti.info_hash())
        seed_ses, _ = start_seeder(ti, os.path.dirname(payload), SEED_PORT)
        time.sleep(1)
        print(f"[0] 做种端 127.0.0.1:{SEED_PORT}  info_hash={ih}")

        got, err = [], []
        mgr = SessionManager(ws.cache, listen_port=PEER_PORT,
                            cache_mode_get=lambda: "convert")
        mgr.on_metadata = lambda r: got.append(r)
        mgr.on_error = lambda m: err.append(m)
        mgr.start()
        mgr.apply_rate_limit(RATE_KBPS)
        # 本机 peer 走 libtorrent 的 local 通道，另有 local_download_rate_limit
        # 管控（默认 0=不限）；不补设则 48MB 秒下完，看不到"未完成"窗口
        mgr._ses.apply_settings({"local_download_rate_limit": RATE_KBPS * 1024})
        mgr.resolve(magnet_uri(ih))
        mgr.connect_peer("127.0.0.1", SEED_PORT)

        ck.check(wait_until(lambda: bool(got) or bool(err), 60, 0.5,
                            "元数据"),
                 f"元数据获取（err={err[:1]}）")
        if not got:
            return ck.report()
        r = got[0]
        vid = next((f for f in r.view_files if f.is_video), None)
        ck.check(vid is not None and vid.size == VID_BYTES,
                 f"定位视频文件 {getattr(vid, 'path', None)} {vid and vid.size}")

        # ---------- 腿1：预览中（引擎全量落盘 + 播放窗口插队） ----------
        ck.section("腿1 预览中")
        mgr.start_preview(vid)
        prev_dir = mgr._preview_dir(ih)
        t1 = time.time()
        p1 = 0
        while time.time() - t1 < 15:
            st = mgr.status() or {}
            p1 = _prog(mgr, vid)
            print(f"    t={time.time() - t1:4.1f}s 进度={p1 / 1048576:7.2f}MB "
                  f"限速值={st.get('download_rate')}B/s 状态={st.get('state')}",
                  flush=True)
            if p1 >= 3 * 1024 * 1024 or p1 >= VID_BYTES:
                break
            time.sleep(1)
        ck.check(p1 > 0, f"预览已下 {p1 / 1048576:.2f}MB")
        ck.check(p1 < VID_BYTES, f"此刻尚未下完（{p1 / 1048576:.2f}MB < "
                                 f"{VID_BYTES / 1048576:.0f}MB）")

        # ---------- 腿2：关预览 → 转正 ----------
        ck.section("腿2 关预览=转正（convert 档）")
        before = dir_byte_snapshot(prev_dir)
        mgr.stop_preview()
        tasks = {t["info_hash"]: t for t in mgr.tasks()}
        t = tasks.get(ih)
        ck.check(t is not None, "任务清单出现该任务（关预览后自动转正）")
        if t is None:
            return ck.report()
        ck.check(t["state"] == "DOWNLOADING", f"状态={t['state']}")
        ck.check(os.path.normcase(os.path.abspath(t["save_path"]))
                 == os.path.normcase(os.path.abspath(prev_dir)),
                 f"落盘仍在预览目录（零重下）：{t['save_path']}")
        ck.check(t.get("selected") == [vid.path],
                 f"selected 钉死预览文件：{t.get('selected')}")
        rec = mgr._registry.torrents.get(ih)
        ck.check(rec is not None and rec.download is True, "rec.download=True")
        ck.check(rec is not None and not rec.handle.status().paused,
                 "句柄未被 pause（引擎继续跑）")
        ck.check(os.path.normcase(prev_dir) in
                 {os.path.normcase(d) for d in mgr.protected_dirs()},
                 "目录进入 protected_dirs 保护名单")

        time.sleep(6)
        p2 = _prog(mgr, vid)
        # 注：目录字节和恒等于文件大小（libtorrent 预分配存储），不能当进度用；
        # 真实进度指标是 file_progress。
        ck.check(p2 > p1, f"关预览后 6s 仍在下（file_progress {p1 / 1048576:.2f}"
                          f"MB → {p2 / 1048576:.2f}MB；目录分配尺寸 {before}B 恒定）")

        # ---------- 腿3：清理 / 配额保护 ----------
        ck.section("腿3 清理与配额不误删")
        bogus = os.path.join(ws.cache, ".preview", "f" * 40)
        os.makedirs(bogus, exist_ok=True)
        with open(os.path.join(bogus, "junk.bin"), "wb") as f:
            f.write(b"x" * 4096)
        cache_guard.clear_cache_contents(ws.cache, keep_dirs=mgr.protected_dirs())
        ck.check(os.path.isdir(prev_dir), "手动清理：转正目录幸存")
        ck.check(not os.path.isdir(bogus), "手动清理：无关预览目录被清掉")
        cache_quota.enforce_preview_limit(
            os.path.join(ws.cache, ".preview"), 1,
            keep_dirs=mgr.protected_dirs())
        ck.check(os.path.isdir(prev_dir), "LRU 配额（1MB 上限）：转正目录幸存")

        # ---------- 腿4：退出 → 重启续传 ----------
        ck.section("腿4 退出与重启")
        at_stop = _prog(mgr, vid)
        mgr.shutdown()
        mgr = None
        tj = os.path.join(ws.cache, ".tasks.json")
        resume = os.path.join(ws.cache, ".resume", f"{ih}.fastresume")
        ck.check(os.path.isfile(tj), ".tasks.json 已写")
        ck.check(os.path.isfile(resume), f"fastresume 已写：{os.path.basename(resume)}")
        if os.path.isfile(tj):
            import json
            with open(tj, "r", encoding="utf-8") as f:
                data = json.load(f)
            recs = data.get("tasks", data) if isinstance(data, dict) else data
            one = recs.get(ih) if isinstance(recs, dict) else \
                next((x for x in recs if x.get("info_hash") == ih), None)
            ck.check(one is not None, "清单含该任务")
            ck.check(one and one.get("selected") == [vid.path],
                     f"清单 selected 仍钉死预览文件：{one and one.get('selected')}")

        got2 = []
        mgr2 = SessionManager(ws.cache, listen_port=PEER_PORT + 1,
                             cache_mode_get=lambda: "convert")
        mgr2.on_metadata = lambda x: got2.append(x)
        mgr2.start()
        mgr2.apply_rate_limit(RATE_KBPS)
        mgr2._ses.apply_settings({"local_download_rate_limit": RATE_KBPS * 1024})
        t2 = {x["info_hash"]: x for x in mgr2.tasks()}.get(ih)
        ck.check(t2 is not None, "重启后任务被恢复（清单驱动）")
        if t2:
            ck.check(os.path.normcase(os.path.abspath(t2["save_path"]))
                     == os.path.normcase(os.path.abspath(prev_dir)),
                     "恢复后 save_path 仍是原预览目录（续传命中已下分块）")
            ck.check(t2.get("selected") == [vid.path],
                     f"恢复后 selected 未膨胀成全集：{t2.get('selected')}")
            # 恢复的任务不是"当前预览句柄"，status()/connect_peer 打不到它——
            # 直接对恢复句柄量真进度并直连做种端（后台续传的真实形态）。
            rec2 = mgr2._registry.torrents.get(ih)
            h2 = rec2.handle if rec2 else None
            ck.check(h2 is not None and not h2.status().paused,
                     "恢复的句柄在运行（未 pause，后台自己接着下）")
            if h2 is not None:
                h2.connect_peer(("127.0.0.1", SEED_PORT))
                restored = wait_until(
                    lambda: _hp(h2, vid) >= max(0, at_stop - 1024 * 1024),
                    60, 0.5, "重启后进度承接")
                ck.check(restored, f"零重下：关机前 {at_stop / 1048576:.2f}MB → "
                                   f"恢复后 {_hp(h2, vid) / 1048576:.2f}MB（fastresume 承接）")
                n0 = _hp(h2, vid)
                grew = wait_until(lambda: _hp(h2, vid) > n0, 30, 0.5, "续传增长")
                ck.check(grew, f"重启后继续缓存：{n0 / 1048576:.2f}MB → "
                               f"{_hp(h2, vid) / 1048576:.2f}MB")
        return ck.report()
    finally:
        for m in (mgr, mgr2):
            try:
                if m is not None:
                    m.shutdown()
            except Exception:
                pass
        seed_ses = None
        shutil.rmtree(ws.root, ignore_errors=True)


def _prog(mgr, vid) -> int:
    st = mgr.status() or {}
    fp = st.get("file_progress") or []
    return fp[vid.index] if len(fp) > vid.index else 0


def _hp(handle, vid) -> int:
    """任意句柄的指定文件已下字节（任务级进度；status() 只覆盖当前预览）。"""
    try:
        fp = handle.file_progress() or []
    except Exception:
        return 0
    return fp[vid.index] if len(fp) > vid.index else 0


if __name__ == "__main__":
    sys.exit(main())