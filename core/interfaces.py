"""接口 Protocol 声明（Wave 3 任务 2.1）：核心子系统能力面的显式契约。

用途
----
为「未来替换实现 / 插件化」预留声明式接口面（technical_roadmap Phase 2 扩展性基础）：

- ``Resolver``        解析与元数据编排（当前唯一实现：core.resolver.ResolverCore）
- ``StreamTransport`` 本地流媒体文件服务（当前唯一实现：core.stream_server.StreamServer）
- ``CachePolicy``     预览缓存配额/清理策略（当前唯一实现：core.fetcher.SessionManager
                      的 facade 委托段——UI 消费清理策略的唯一入口，见 §2.2 白名单）
- ``ThemeSource``     主题供给（当前唯一实现：ui.theme 模块本体）

纪律（为什么这样写）
--------------------
1. **只声明、不重构**：现有实现零改动，也不写 ``class ResolverCore(Resolver)``
   这类显式继承声明——Protocol 是结构化类型（鸭子类型），实现满足方法面即天然
   是「实现」；显式继承反而把接口变成基类依赖，违反行为零变化红线。
   满足性由 tests/contract_check.py 的 issubclass/isinstance 结构检查当回归门禁：
   实现改名 / 改序 / 丢方法 = 立刻红。
2. 方法签名与现有实现逐字一致（参数名 / 默认值 / 返回类型）。
3. 本模块**不做任何重导入**（不碰 libtorrent / Qt / PySide6）：注解里涉及其它
   模块的类型放在 TYPE_CHECKING 块内——配合 ``from __future__ import annotations``
   （注解全部惰性化为字符串、运行时永不求值），interfaces.py 保持纯声明、
   可被任何层安全 import。
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    # 仅供注解引用（运行时不导入，见上方纪律 3）
    from core.registry import TaskRecord

__all__ = ["Resolver", "StreamTransport", "CachePolicy", "ThemeSource"]


@runtime_checkable
class Resolver(Protocol):
    """磁力链/.torrent 解析与元数据编排的对外接口面。

    签名提取自 core/resolver.py ResolverCore（contract_check [4] 段同表冻结）。
    """

    def resolve(self, source: str):
        """解析磁力链或 .torrent 文件（仅查看清单，不下载）。"""
        ...

    def begin_resolve(self):
        """新解析前的换代：旧「当前任务」降级/让位，gen 递增防竞态。"""
        ...

    def connect_peer(self, ip: str, port: int, wait_handle: float = 5.0,
                     task_id: "str | None" = None) -> None:
        """手动添加 Peer（跳过 DHT 发现）。"""
        ...

    def on_download_finished(self, rec: TaskRecord):
        """torrent_finished_alert：任务完成（做种/停止分支由实现决定）。"""
        ...

    def on_metadata_received(self, rec: TaskRecord):
        """metadata_received_alert 处理：per-task 就绪。"""
        ...


@runtime_checkable
class StreamTransport(Protocol):
    """本地流媒体文件服务的生命周期 + 取 URL + 应答诊断接口面。

    签名提取自 core/stream_server.py StreamServer（serve 路径 = url_for 生成的
    带 token 的 127.0.0.1 URL；构造签名被契约冻结、不属于接口面本身）。
    """

    def start(self):
        """启动服务线程（绑定端口在构造期已完成）。"""
        ...

    def shutdown(self):
        """停止服务并释放 socket（协作式停止事件先行，见实现注释）。"""
        ...

    def url_for(self, rel_path: str) -> str:
        """相对路径 → 带访问 token 的播放 URL。"""
        ...

    def last_response_note(self) -> str:
        """最近一次流应答的可读摘要（空串 = 本会话无应答记录）。"""
        ...


@runtime_checkable
class CachePolicy(Protocol):
    """预览缓存配额/清理的策略供给面（UI 经 facade 消费清理策略的三入口）。

    签名提取自 core/fetcher.py SessionManager 委托段（本体在 cache_quota/
    registry）：配额执行 + 保护名单 + 清理目标根。未来替换淘汰策略
    （LRU→频率权重等）只需提供满足本面的实现。
    """

    def preview_root(self) -> str:
        """预览缓存根目录 ``<cache_dir>/.preview``（清理目标）。"""
        ...

    def enforce_preview_cache_limit(self, limit_mb: int, keep_dirs,
                                    warn=None) -> "tuple[int, int]":
        """执行配额：超限清理，返回 (清理后总占用字节, 本次释放字节)。

        ``keep_dirs`` 接受集合或零参回调（TOCTOU 逐条复核语义由实现决定）。
        """
        ...

    def protected_dirs(self) -> "set[str]":
        """清理保护名单：活跃任务落盘目录，任何清理策略绝不涉足。"""
        ...


@runtime_checkable
class ThemeSource(Protocol):
    """主题供给面：色板读取 + 模式解析 + 应用入口。

    签名提取自 ui/theme.py（模块本体即当前唯一实现；contract_check 用
    isinstance(theme, ThemeSource) 做结构检查——runtime_checkable 对模块
    按 hasattr 逐成员验证）。apply_theme 的默认值写字面 "light"：实现里是
    DEFAULT_MODE 常量，theme_check R4 已冻结其取值 == "light"，二者等价。
    """

    def current_palette(self) -> "dict":
        """当前激活色板（17+4 固定键集，见 ui/theme.py 调色板注释）。"""
        ...

    def active_mode(self) -> str:
        """当前生效主题名（system 已解析为 light/dark）。"""
        ...

    def resolve_mode(self, mode: str) -> str:
        """把请求模式解析为实际生效主题名（非法值回退默认，绝不抛错）。"""
        ...

    def apply_theme(self, app: Any, mode: str = "light") -> str:
        """设置/热切换界面主题，返回实际生效的主题名。"""
        ...
