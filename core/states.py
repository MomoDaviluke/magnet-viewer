"""任务生命周期常量层（叶子模块，不 import core 内任何东西）。

搬迁的背景：``SessionManager`` 拆分后，状态取值与 bootstrap tracker 列表同时
被 ``fetcher``（Facade）、``persist``（阶段 1）、以及后续的 ``registry`` /
``taskops`` 使用。若继续留在 fetcher，会出现 ``fetcher → persist → fetcher``
的循环导入，因此下沉到本模块。

兼容保证（Wave 2 更新）：``core.fetcher`` 的历史再导出已移除，本模块是
``STATE_*`` / ``DOWNLOAD_STATES`` / ``BOOTSTRAP_TRACKERS`` 的唯一权威来源，
调用方（download_mgr_test / session_test / proxycheck_test 等）一律
``from core.states import ...`` 直连。
"""
from __future__ import annotations

# 任务生命周期状态机（对齐 plan/t1 §3 与 downloads_pane 的 STATE_META 命名）：
# QUEUED → META_FETCH → VALIDATE → DOWNLOADING ⇄ PAUSED → COMPLETED → STOPPED；
# FAILED / DELETED 为终态；默认完成后自动停止（不做种，决策 D3）。
STATE_QUEUED = "QUEUED"
STATE_META_FETCH = "META_FETCH"
STATE_VALIDATE = "VALIDATE"
STATE_DOWNLOADING = "DOWNLOADING"
STATE_PAUSED = "PAUSED"
STATE_COMPLETED = "COMPLETED"
STATE_STOPPED = "STOPPED"
STATE_FAILED = "FAILED"
STATE_SEEDING = "SEEDING"
STATE_DELETED = "DELETED"
STATE_READY = "READY"        # 内部态：仅查看清单/预览（review 记录专用，
                             # 不持久化、不进入 tasks() 快照）

DOWNLOAD_STATES = {STATE_QUEUED, STATE_META_FETCH, STATE_VALIDATE,
                   STATE_DOWNLOADING, STATE_PAUSED, STATE_COMPLETED,
                   STATE_STOPPED, STATE_FAILED, STATE_SEEDING, STATE_DELETED}

# 公共 tracker，提升冷门磁力链的 peer 发现率
BOOTSTRAP_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]

# 兜底 tracker（HTTP/HTTPS，**与 BOOTSTRAP_TRACKERS 分开**）——
# 诊断 D-2026-09-18：本机 UDP 出站大面积受阻（5 个公共 DHT 引导节点实测
# 仅 1 个响应），udp:// tracker 大概率同样不可达，而磁力链若不带 ``tr=``
# 参数，peer 发现就只剩 DHT 一条路。这里补一组走 TCP 443/80 的 tracker 作为
# 第二条通道，由 ``extra_trackers`` 开关控制（默认关闭，保持既有 5 条 udp
# 的行为与相关断言不变）。
FALLBACK_TRACKERS = [
    "http://tracker.opentrackr.org:1337/announce",
    "https://tracker.opentrackr.org:443/announce",
    "http://open.tracker.cl:80/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "https://tracker.torrent.eu.org:443/announce",
]
