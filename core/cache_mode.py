"""阶段 B（plan/06）：convert/hold 两档缓存模式常量（值域即校验，不新增校验逻辑）。

preview_cache_mode 配置项取值：
- ``convert``（默认，迅雷式）：关闭预览自动转正为持久下载任务，
  引擎继续按 file-priority 全量缓存（fastresume/任务清单/配额保护全套）；
- ``hold``：基线行为——停止预览即暂停冻结（清优先级 + pause + 撤 auto_managed）。
"""
PREVIEW_CACHE_CONVERT = "convert"
PREVIEW_CACHE_HOLD = "hold"
PREVIEW_CACHE_MODES = (PREVIEW_CACHE_CONVERT, PREVIEW_CACHE_HOLD)
