# -*- coding: utf-8 -*-
"""core/proxycheck.py 专项验收 + 会话接线（D-2026-09-18 加固：下载极慢 + 做种报错）。

为什么需要这一套
----------------
实测事故：``proxy_type=socks5`` + ``proxy_host=1.2.3.4``（占位黑洞）在客户端里
**静默生效**，peer/tracker/DHT 引导解析三条通道全灭（60 秒下载 0KB、DHT 节点 0），
而客户端 90 秒后弹出的却是「该资源可能已无做种/无在线 Peer」——文案把根因指向
资源。本套件锁住两件事：**代理不可达必须在启动后数秒内被明确指出**，以及
**未配置代理时的既有文案逐字不变**。

覆盖段：
- §A 纯函数：is_configured / address_text
- §B probe：真实 SOCKS5 握手（本地假代理，不依赖外网）/ 非 SOCKS5 端口 /
     拒绝无认证 / 握手超时 / 连接失败 / 非法端口 / 未配置即健康
- §C 文案：warning_text 含地址与处置建议；hint_text 未配置代理必须为空串
- §D SessionCore 接线：替身缺省不探测 / 未配置代理不探测 /
     不可达 → emit_error 且含地址 / 可达 → 不报错 / 探测抛异常不外逸
- §E 看门狗文案：已配置代理时追加根因提示；未配置时与改动前逐字一致
- §F 两条入口：start() 与 apply_proxy() 都触发自检；探测不阻塞调用方

设计约束（与 session_test 一致）：假依赖 + 本地假代理，秒级、不联网、不绑真实端口。
退出码：0=通过，1=失败，2=SKIP（依赖缺失，绝不假装通过）。
"""
from __future__ import annotations

import dataclasses
import os
import socket
import sys
import threading
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt  # noqa: F401

    import test_support as ts
    from core import proxycheck, session
    from core.fetcher import STATE_META_FETCH
except ImportError as e:    # 依赖缺失：显式 SKIP；语法/逻辑错误会正常冒泡为失败
    print(f"依赖缺失，无法执行 proxycheck 专项验收：{e}")
    sys.exit(2)

IH = "c" * 40


# --------------------------------------------------------------------------
# 假代理（本地回环，不依赖外网）
# --------------------------------------------------------------------------

class _FakeProxy(threading.Thread):
    """回环上的假代理：收到 SOCKS5 问候后回 ``reply``。"""

    def __init__(self, reply: bytes = b"\x05\x00", silent: bool = False):
        super().__init__(daemon=True)
        self.reply = reply
        self.silent = silent
        self._closed = False
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.hits = 0

    def run(self):
        while not self._closed:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            try:
                c.settimeout(2.0)
                c.recv(3)
                self.hits += 1
                if self.silent:
                    # 「接受连接但永不回包」的僵死代理：必须**保持连接打开**，
                    # 立刻 close 会让对端收到 EOF（表现为「不是 SOCKS5」而非超时）
                    time.sleep(2.0)
                else:
                    c.sendall(self.reply)
            except OSError:
                pass
            finally:
                try:
                    c.close()
                except OSError:
                    pass

    def close(self):
        self._closed = True
        try:
            self.sock.close()
        except OSError:
            pass


def _free_port() -> int:
    """占一个端口再立刻释放：用于构造「无人监听」的目标。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _px(port, kind="socks5", host="127.0.0.1"):
    return {"type": kind, "host": host, "port": port}


# --------------------------------------------------------------------------
# 假依赖（真 SessionDeps + 空实现补齐）
# --------------------------------------------------------------------------

def _noop(*_a, **_k):
    return None


def make_deps(**over) -> tuple:
    """构造真 SessionDeps：必填字段自动用空实现补齐，只覆盖关心的项。

    自动补齐而非逐字段罗列：``SessionDeps`` 新增必填字段时本套件不会悄悄失效
    （session_test 那份显式构造在本文件里没必要重复一遍）。
    """
    calls = {"errors": [], "applied": [], "probe": []}
    host = {"ses": None}
    kw = {}
    for name, f in session.SessionDeps.__dataclass_fields__.items():
        if f.default is not dataclasses.MISSING:
            continue
        if f.default_factory is not dataclasses.MISSING:
            continue
        if name in ("listen_port", "active_downloads"):
            kw[name] = 0
        elif name == "lock":
            kw[name] = threading.Lock()
        else:
            kw[name] = _noop
    kw.update({
        "ses_get": lambda: host["ses"],
        "ses_set": lambda v: host.__setitem__("ses", v),
        "emit_error": lambda m: calls["errors"].append(m),
        "metadata_timeout_get": lambda: 90.0,
        "torrents_get": lambda: host.setdefault("torrents", {}),
        "current_record": lambda: host.get("current"),
        "hash_key": lambda h: getattr(h, "ih", "?"),
        "thread_factory": lambda target: _FakeThread(target),
        "tasks_loader": lambda: {},
    })
    kw.update(over)
    return session.SessionDeps(**kw), host, calls


class _FakeThread:
    """假线程：记录目标但不执行（避免真的跑 alert_loop 去 pop_alerts）。"""

    def __init__(self, target):
        self.target = target
        self.started = False

    def start(self):
        self.started = True

    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


class _FakeSession:
    def __init__(self):
        self.settings = []

    def apply_settings(self, s):
        self.settings.append(dict(s))


class _FakeSched:
    """shutdown 只需要 scheduler.stop()。"""

    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def _rec(ih=IH, started=100.0, timeout=None, state=STATE_META_FETCH,
         download=False) -> SimpleNamespace:
    return SimpleNamespace(
        ih=ih, resolving=True, resolve_started=started, timeout=timeout,
        state=state, download=download, error="",
        handle=SimpleNamespace(ih=ih))


# --------------------------------------------------------------------------
# §A 纯函数
# --------------------------------------------------------------------------

def section_pure(ck):
    ck.section("§A 纯函数：is_configured / address_text")
    ck.check(proxycheck.is_configured(None) is False, "None → 未配置")
    ck.check(proxycheck.is_configured({}) is False, "空 dict → 未配置")
    ck.check(proxycheck.is_configured({"type": "none", "host": "1.2.3.4"})
             is False, "type=none 即便有 host 也算未配置（与直连回落同源）")
    ck.check(proxycheck.is_configured({"type": "socks5", "host": ""})
             is False, "socks5 但 host 为空 → 未配置（lt_proxy_settings 走直连）")
    ck.check(proxycheck.is_configured({"type": "socks5", "host": "  "})
             is False, "host 纯空白 → 未配置")
    ck.check(proxycheck.is_configured({"type": "SOCKS5", "host": "1.2.3.4"})
             is True, "类型大小写不敏感且真配置 → 需探测")
    ck.check(proxycheck.address_text({"type": "socks5", "host": "1.2.3.4",
                                      "port": 1080}) == "socks5://1.2.3.4:1080",
             "地址文案 socks5://host:port")
    ck.check(proxycheck.address_text({"type": "http", "host": "h"})
             == "http://h:0", "端口缺失回落 0（不抛异常）")
    ck.check(proxycheck.address_text({"type": "socks5", "host": "h",
                                      "port": "abc"}) == "socks5://h:0",
             "端口非数字回落 0（不抛异常）")


# --------------------------------------------------------------------------
# §B probe
# --------------------------------------------------------------------------

def section_probe(ck):
    ck.section("§B probe：真实 TCP + SOCKS5 握手（本地假代理）")
    ok, why = proxycheck.probe(None, timeout=0.5)
    ck.check(ok is True and why == "", "未配置代理 → (True, '')：「没开代理」不是故障")
    ok, why = proxycheck.probe(_px(0), timeout=0.5)
    ck.check(ok is False and "端口" in why, f"端口 0 → 明确拒绝（{why}）")
    ok, why = proxycheck.probe(_px("abc"), timeout=0.5)
    ck.check(ok is False and "端口" in why, f"端口非数字 → 明确拒绝（{why}）")

    good = _FakeProxy(b"\x05\x00")
    refuse = _FakeProxy(b"\x05\xff")
    notsocks = _FakeProxy(b"HT")        # 像 HTTP 服务：首字节不是 0x05
    silent = _FakeProxy(silent=True)    # accept 后不回数据
    for srv in (good, refuse, notsocks, silent):
        srv.start()
    try:
        ok, why = proxycheck.probe(_px(good.port), timeout=2.0)
        ck.check(ok is True and why == "", "真 SOCKS5 服务（回 05 00）→ 判定可用")
        ck.check(good.hits >= 1, "探测确实完成了握手（服务端收到问候）")

        ok, why = proxycheck.probe(_px(refuse.port), timeout=2.0)
        ck.check(ok is False and "无认证" in why,
                 f"回 05 ff → 拒绝无认证方式（{why}）")

        ok, why = proxycheck.probe(_px(notsocks.port), timeout=2.0)
        ck.check(ok is False and "不是 SOCKS5" in why,
                 f"端口可连但不是 SOCKS5 → 明确判定（{why}）")

        ok, why = proxycheck.probe(_px(silent.port), timeout=0.6)
        ck.check(ok is False and "握手" in why, f"不回数据 → 握手超时（{why}）")

        # http 类型只做 TCP 连通：同一个「非 SOCKS5」端口对 http 代理是可用的
        ok, why = proxycheck.probe(_px(notsocks.port, kind="http"), timeout=2.0)
        ck.check(ok is True and why == "",
                 "http 类型只判 TCP 连通（不套用 SOCKS5 握手）")
    finally:
        for srv in (good, refuse, notsocks, silent):
            srv.close()

    dead = _px(_free_port())
    ok, why = proxycheck.probe(dead, timeout=0.6)
    ck.check(ok is False and "连接" in why, f"无人监听 → 连接失败/超时（{why}）")

    ok, why = proxycheck.probe(_px(1080, host="no-such-host.invalid"),
                               timeout=0.5)
    ck.check(ok is False, f"域名不可解析 → 判定不可用（{why}）")


# --------------------------------------------------------------------------
# §C 文案
# --------------------------------------------------------------------------

def section_text(ck):
    ck.section("§C 文案：warning_text / hint_text")
    p = {"type": "socks5", "host": "1.2.3.4", "port": 1080}
    w = proxycheck.warning_text(p, "连接超时（2 秒内无响应）")
    ck.check("socks5://1.2.3.4:1080" in w, "告警文案含完整代理地址")
    ck.check("连接超时" in w, "告警文案含失败原因")
    ck.check("peer" in w and "tracker" in w and "DHT" in w,
             "告警文案点明三条通道全灭（用户才知道影响面）")
    ck.check("设置" in w, "告警文案给出处置指引")
    ck.check("不可用" in proxycheck.warning_text(p), "无原因时文案仍自洽")

    ck.check(proxycheck.hint_text(None) == "", "未配置代理 → hint 为空串")
    ck.check(proxycheck.hint_text({"type": "none", "host": "1.2.3.4"}) == "",
             "type=none → hint 为空串（既有文案逐字不变）")
    h = proxycheck.hint_text(p, "连接超时")
    ck.check("启动自检" in h and "1.2.3.4" in h,
             "已确认不可达 → hint 直指根因")
    h2 = proxycheck.hint_text(p)
    ck.check("启动自检" not in h2 and "1.2.3.4" in h2,
             "未探测过 → hint 只提示存在代理需一并检查")


# --------------------------------------------------------------------------
# §D SessionCore 接线
# --------------------------------------------------------------------------

def section_wiring(ck):
    ck.section("§D SessionCore 接线：何时探测 / 何时报错")
    p = {"type": "socks5", "host": "1.2.3.4", "port": 1080}

    # D1 替身缺省（proxy_probe 不传 = 既有测试与老构造点）：不探测、不报错
    d, host, calls = make_deps()
    core = session.SessionCore(d)
    core._start_proxy_check(p)
    ck.check(calls["errors"] == [], "proxy_probe 缺省（None）→ 不探测不报错")
    ck.check(core._proxy is not None,
             "但已记下当前代理（供超时文案使用）")

    # D2 未配置代理：即便注入真探测函数也不调用
    seen = []
    d, host, calls = make_deps(proxy_probe=lambda px, t: (seen.append(1),
                                                          (True, ""))[1])
    core = session.SessionCore(d)
    core._start_proxy_check({"type": "none", "host": "1.2.3.4"})
    core._start_proxy_check(None)
    time.sleep(0.05)
    ck.check(seen == [], "未配置代理 → 一次探测都不发（直连不是故障）")
    ck.check(core._proxy is None, "未配置代理 → _proxy 归 None")

    # D3 不可达：异步 emit_error，文案含地址
    ev = threading.Event()
    def probe_bad(px, t):
        ev.set()
        return False, "连接超时（2 秒内无响应）"
    d, host, calls = make_deps(proxy_probe=probe_bad)
    core = session.SessionCore(d)
    core._start_proxy_check(p)
    ck.check(ev.wait(2.0), "探测在后台线程真的被执行")
    ts.wait_until(lambda: calls["errors"], 2.0)
    ck.check(len(calls["errors"]) == 1, "不可达 → 恰好发射一条 emit_error")
    msg = calls["errors"][0] if calls["errors"] else ""
    ck.check("1.2.3.4:1080" in msg, "报错文案含目标代理地址")
    ck.check(core._proxy_reason != "",
             "记下不可达原因（供后续超时文案直指根因）")

    # D4 可达：不打扰用户
    d, host, calls = make_deps(proxy_probe=lambda px, t: (True, ""))
    core = session.SessionCore(d)
    core._start_proxy_check(p)
    time.sleep(0.15)
    ck.check(calls["errors"] == [], "可达 → 不发射任何报错")
    ck.check(core._proxy_reason == "", "可达 → 不留下不可达原因")

    # D5 探测函数抛异常：绝不外逸、绝不误报
    def probe_boom(px, t):
        raise RuntimeError("探测自身炸了")
    d, host, calls = make_deps(proxy_probe=probe_boom)
    core = session.SessionCore(d)
    core._start_proxy_check(p)
    time.sleep(0.15)
    ck.check(calls["errors"] == [], "探测抛异常 → 不误报为「代理不可达」")

    # D6 不阻塞调用方：探测慢 1.0s，调用方应立即返回
    def probe_slow(px, t):
        time.sleep(1.0)
        return True, ""
    d, host, calls = make_deps(proxy_probe=probe_slow)
    core = session.SessionCore(d)
    t0 = time.time()
    core._start_proxy_check(p)
    el = time.time() - t0
    ck.check(el < 0.3, f"探测不阻塞调用方（实测 {el * 1000:.0f}ms < 300ms）")


# --------------------------------------------------------------------------
# §E 看门狗文案
# --------------------------------------------------------------------------

def section_watchdog_text(ck):
    ck.section("§E 看门狗文案：代理根因提示的有无")

    # E1 未配置代理：文案与改动前逐字一致
    d, host, calls = make_deps()
    core = session.SessionCore(d)
    r = _rec()
    host["torrents"] = {IH: r}
    host["current"] = r
    core.metadata_watchdog(now=200.0)
    ck.check(len(calls["errors"]) == 1, "超时发射一条")
    base = "获取元数据超时（>90 秒）：该资源可能已无做种/无在线 Peer"
    ck.check(calls["errors"][0] == base,
             "未配置代理 → 原文案逐字不变（不引入无关提示）")

    # E2 已配置代理且自检确认不可达：直指根因
    d, host, calls = make_deps()
    core = session.SessionCore(d)
    core._start_proxy_check({"type": "socks5", "host": "1.2.3.4", "port": 1080})
    core._proxy_reason = "连接超时（2 秒内无响应）"
    r = _rec()
    host["torrents"] = {IH: r}
    host["current"] = r
    core.metadata_watchdog(now=200.0)
    msg = calls["errors"][0] if calls["errors"] else ""
    ck.check(msg.startswith(base), "仍然保留原文案（不改既有语义）")
    ck.check("1.2.3.4:1080" in msg and "启动自检" in msg,
             "追加指向代理的根因提示（事故里最缺的就是这句）")

    # E3 下载任务落盘 error 也带提示
    d, host, calls = make_deps()
    core = session.SessionCore(d)
    core._start_proxy_check({"type": "socks5", "host": "1.2.3.4", "port": 1080})
    tasks = {IH: {"state": "", "error": ""}}
    r = _rec(download=True)
    host["torrents"] = {IH: r}
    d.tasks_get = lambda: tasks
    core.metadata_watchdog(now=200.0)
    ck.check(tasks[IH]["state"] == "FAILED",
             "下载任务超时 → 清单状态置 FAILED")
    ck.check(r.error.startswith(base) and "1.2.3.4:1080" in r.error,
             "下载任务的 error 落盘同样带代理提示")
    ck.check(tasks[IH]["error"] == r.error, "清单 error 与记录 error 同源")


# --------------------------------------------------------------------------
# §F 两条入口
# --------------------------------------------------------------------------

def section_entrypoints(ck):
    ck.section("§F start() / apply_proxy() 都触发自检")

    # F1 start：真会话（listen_port=0 随机端口，不占 6881）+ 假线程 + 假调度器
    ev = threading.Event()
    sched = _FakeSched()
    d, host, calls = make_deps(
        proxy_probe=lambda px, t: (ev.set(), (True, ""))[1],
        thread_factory=lambda target: _FakeThread(target),
        scheduler_get=lambda: sched)
    core = session.SessionCore(d)
    core.start(proxy={"type": "socks5", "host": "127.0.0.1", "port": 1080},
               metadata_timeout=12.5)
    ck.check(ev.wait(2.0), "start() 入口触发自检")
    ck.check(host["ses"] is not None, "会话已建立")
    core.shutdown()
    ck.check(sched.stopped == 1, "shutdown 照旧停调度器（既有行为不变）")

    # F2 start 且无代理：不探测
    ev2 = threading.Event()
    d, host, calls = make_deps(
        proxy_probe=lambda px, t: (ev2.set(), (True, ""))[1],
        thread_factory=lambda target: _FakeThread(target),
        scheduler_get=lambda: _FakeSched())
    core = session.SessionCore(d)
    core.start(proxy=None, metadata_timeout=12.5)
    ck.check(not ev2.wait(0.3), "start(proxy=None) → 不探测")
    core.shutdown()

    # F3 apply_proxy：会话在 → 触发；会话为 None → 早退不探测
    ev3 = threading.Event()
    d, host, calls = make_deps(
        proxy_probe=lambda px, t: (ev3.set(), (True, ""))[1])
    host["ses"] = _FakeSession()
    core = session.SessionCore(d)
    core.apply_proxy({"type": "socks5", "host": "127.0.0.1", "port": 1080})
    ck.check(ev3.wait(2.0), "apply_proxy() 入口触发自检")
    ck.check(len(host["ses"].settings) == 1,
             "代理设置照旧应用到会话（原有行为不变）")

    ev4 = threading.Event()
    d, host, calls = make_deps(
        proxy_probe=lambda px, t: (ev4.set(), (True, ""))[1])
    core = session.SessionCore(d)          # ses_get → None
    core.apply_proxy({"type": "socks5", "host": "127.0.0.1", "port": 1080})
    ck.check(not ev4.wait(0.3), "会话未启动（ses=None）→ 早退，不探测")


def main() -> int:
    ck = ts.Checker("proxycheck_test（代理可用性自检 + 会话接线专项）")
    print("=" * 72)
    print("proxycheck 专项：纯函数 / 真实 SOCKS5 握手 / 接线 / 文案 / 入口")
    print("=" * 72)
    section_pure(ck)
    section_probe(ck)
    section_text(ck)
    section_wiring(ck)
    section_watchdog_text(ck)
    section_entrypoints(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
