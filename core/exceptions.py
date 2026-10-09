"""项目业务异常层级（Wave 1-A2 架构重构：收窄宽泛捕获的地基）。

设计意图
--------
历史教训（见 ``core/logutil.py`` 头部注释）：core/ 曾遍布
宽泛捕获（Exception/BaseException），把 TypeError/AttributeError 这类**编程错误**当成
业务异常吞掉，缺陷被掩盖后排查成本极高。本模块提供一套语义明确的
业务异常层级，配合两条纪律使用：

1. **业务分支**（解析文件、注册任务、恢复数据……）只捕获实际可能
   抛出的具体类型（OSError/ValueError/KeyError/RuntimeError/lt 异常），
   需要向上层传递领域语义时用 ``raise XxxError(...) from e`` 保留原始链；
2. **基础设施兜底**（HTTP handler、alert 循环、进程退出清理、日志器自身）
   允许宽泛捕获（Exception 级别），但必须「有日志记录 + 注释说明为何宽泛」，绝不静默 pass。

层级全部继承 ``MagnetError(Exception)``：UI 侧与三个最终兜底点
（libtorrent alert 循环、HTTP handler、UI 线程回调）原本就以宽泛
捕获收尾，因此本层级不会漏接；反过来，TypeError 等
编程错误不再被业务分支误吞，会直接冒泡到兜底点被记录。

注意：既有对外契约抛出的 ``ValueError``/``RuntimeError``（如
``TaskOps.add_task`` 空源抛 ValueError、会话未启动抛 RuntimeError，
``taskops_test`` 有断言）**不在此层级内、也不做转换**——收窄的是
「捕获侧」，「抛出侧」契约保持稳定，避免破坏回归基线。
"""
from __future__ import annotations


class MagnetError(Exception):
    """项目业务异常基类：所有 core/ 领域错误的共同祖先。

    兜底点捕获 ``Exception`` 即可接住全部子类；调用方想区分
    「业务失败」与「编程错误」时捕获本类。
    """


class ParseError(MagnetError):
    """磁力链 / .torrent 文件解析失败。

    使用场景：resolver/parser 读取种子元数据、bencode 反解、磁力链
    校验等路径中，把 OSError（文件不可读）/ValueError（格式非法）等
    转换为领域语义后向上抛出（``raise ParseError(...) from e``）。
    """


class SessionError(MagnetError):
    """libtorrent 会话启动 / 配置失败。

    使用场景：SessionCore 建会话、写 settings、端口回退重试仍失败等
    「会话级」故障；区别于单任务级的 DownloadError。
    """


class DownloadError(MagnetError):
    """任务下载 / 做种操作失败。

    使用场景：add/pause/resume/remove/优先级/做种切换等任务操作在
    句柄失效或 lt 拒绝时的领域封装。
    """


class CacheError(MagnetError):
    """缓存目录 / 配额 / 清理失败。

    使用场景：cache_guard、cache_quota、metacache 等在目录治理、
    LRU 配额、清理守卫路径上的领域故障。
    """


class StreamError(MagnetError):
    """流媒体服务请求处理失败。

    使用场景：stream_server 的 HTTP 请求处理、字节供给、开播门控
    等路径上的领域故障（单请求失败不应拖垮服务线程）。
    """


class StorageError(MagnetError):
    """任务持久化 / 元数据落盘失败。

    使用场景：persist/taskstore/resume 在任务清单读写、fastresume
    落盘、元数据缓存写入等路径上的领域故障。
    """
