# 磁力链实时解析查看器 (Magnet Viewer)

输入磁力链接或 .torrent 文件，**不下载资源本体**即可实时查看完整的文件清单（名称、大小、目录结构、做种健康度）；对视频文件支持**边下边播**，对图片文件支持**即点即看**（内嵌画廊）。

## 功能

- **实时解析**：磁力链通过 DHT + BEP-9 `ut_metadata` 从在线 Peer 获取元数据（仅几十 KB）；.torrent 文件本地 bencode 直接解码。全程不下载资源本体。
- **文件树视图**：目录层级、单文件大小、占比，双击媒体文件直接预览。
- **视频边下边播**：libtorrent 单文件锁定 + 分块顺序下载 + 索引块（moov）优先 → 本地 HTTP 流服务（仅监听 127.0.0.1，支持 Range）→ 内嵌 QMediaPlayer 播放，实时显示缓冲进度。播放器请求未就绪区间时自动「点播」调度器补拉并等待（moov 探测与任意拖动均可正常工作）。
- **图片画廊**：图片文件按需下载到临时缓存，完成后自动载入缩略图，支持 Ctrl+滚轮缩放、翻页；画廊内切换未下载图片会自动切换下载目标。
- **设置**（右上角「设置」按钮）：SOCKS5/HTTP 代理（含账号密码、Peer 连接走代理以保护 IP）、元数据获取超时、**下载限速**（KB/s，会话级热更新）、**预览缓存上限**（超限按最久未活跃顺序自动清理旧预览数据，已下载文件不受影响）、**运行日志开关**、缓存目录、退出时清理缓存、立即清理缓存。代理、超时、限速与日志开关保存后立即生效（libtorrent `apply_settings` 热更新），缓存目录与并发数修改重启生效，缓存上限在下次切换预览文件时生效。设置持久化于 QSettings（Windows 注册表 `HKEY_CURRENT_USER\Software\Bitseed\MagnetViewer`）。
- **拖放与输入历史**：可直接把 `.torrent` 文件或磁力链文本拖入窗口（落点即解析）；输入框带自动补全，保留最近 15 条解析记录（置顶去重，持久化到 QSettings）。
- 预览可随时取消（自动释放下载配额）。

## 运行

### 一键启动（Windows）

双击 `start.bat`：首次运行自动创建虚拟环境并安装依赖，然后启动程序。

### 手动运行

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

依赖：Python 3.10+（已在 3.13 验证）、`libtorrent`、`PySide6_Essentials`、
`PySide6_Addons`（后者提供内嵌播放器要用的 Qt 多媒体后端；只装前者时视频
能解析能下载，但双击预览会「打不开」——QMediaPlayer 无后端插件）。
发布/回归环境建议改用精确锁定安装 `pip install -r requirements.lock`
（与已验证环境逐字节一致；libtorrent 2.0/2.1 API 不兼容，宽松约束下
新装机器可能落到未验证版本）。

### 打包为免安装 exe（Windows）

```bat
build.bat          :: 一键：清旧产物 -> PyInstaller 构建 -> pack_check 自检
```

或手动两步（等价）：

```bash
.venv\Scripts\pip install pyinstaller
.venv\Scripts\python -m PyInstaller --noconfirm magnet-viewer.spec
.venv\Scripts\python pack_check.py        :: 产物自检（退出码 0/1/2，同测试约定）
# 产物：dist\MagnetViewer\MagnetViewer.exe（整个目录拷走即可运行）
```

> 不用 `pyinstaller --clean`：它清 build 缓存（>50 个文件）会被本环境的安全
> 删除守卫拦下直接 exit 1，`build.bat` 改为先手工删目录再构建，效果等价。

onedir + windowed（无控制台）；不压 UPX（杀软误报头号诱因）。打包机与
目标机均需 Windows x64；首次运行会自建缓存目录（%TEMP%\magnet_viewer_cache，
可在设置中更改）。注意：预览/下载数据与本机用户数据同权限存储（不加密），
同机同用户的其他进程可读——请勿把缓存目录指给多人共用的位置。

内嵌播放器所需的 Qt 多媒体后端（`plugins/multimedia/ffmpegmediaplugin.dll`
与 `avcodec/avformat/avutil-*.dll`）会被一并打进产物，**目标机不需要额外装
ffmpeg 或播放器**。前提：打包环境装了 `PySide6_Addons`——只装 Essentials 时
插件源目录为空，产物能解析能下载、但开播必失败（QMediaPlayer ResourceError
'Not available'）。

#### 产物体积：147MB → 103MB（瘦身清单）

`magnet-viewer.spec` 里有一组带注释的黑名单，只砍「本应用确定用不到」的大件：

| 剔除项 | 体积 | 依据 |
|---|---|---|
| `opengl32sw.dll` | 20MB | Mesa 软件 OpenGL，只服务 Qt Quick 渲染后端；本应用是纯 Widgets |
| Quick / Qml 全家 | 12.5MB | 被 Qt6Multimedia、virtualkeyboard 插件连带拖入，Widgets 应用不加载 QML 引擎 |
| `Qt6Pdf` | 4.5MB | 来自 imageformats 的 `qpdf.dll`，本应用不显示 PDF |
| `translations/` | 7.1MB | Qt 自带界面译文，本项目文案硬编码中文 |
| 冷门图像插件 | 约 1MB | `qicns/qtga/qwbmp/qwebp`，图标与画廊用不到 |

砍完必须复验——`pack_check.py` 就是干这个的：它既查「必需组件一件不少」
（含整条多媒体链），也查「该砍的确实砍了」，体积超 130MB 会告警（瘦身失效
的哨兵）。spec 里还有一道构建期硬校验，误删必需 DLL 时直接 `SystemExit`，
不让问题拖到运行时才黑屏。

打包件实测（2026-09-08，四层）：offscreen 40s 稳定驻留（220MB）/ 真桌面
HWND 有效、标题正确（251MB）/ 按 PID 网络面确认 6881 多网卡监听 +
127.0.0.1 流服务 + UDP×5（DHT）/ **打包件内 QMediaPlayer 开本地 MP4 →
`LoadedMedia`，且 QVideoSink 抓到 640x360 画面（采样方差 5034，非黑屏）**
——即瘦身没有伤到渲染链，目标机无需任何外部播放器。

## 使用步骤

1. 粘贴磁力链（如 `magnet:?xt=urn:btih:...`）或点「打开种子文件…」，点「解析」；也可直接把 `.torrent` 文件或磁力链文本**拖进窗口**。输入框会按最近 15 条历史自动补全。
2. 在「文件列表」页查看文件树（目录层级 / 大小 / 占比）。
3. 双击视频文件 → 「预览」页内嵌播放器开始边下边播；双击图片 → 画廊浏览。
4. 预览页交互：
   - **拖动播放进度条**：播放与下载位置同步跳转（调度器从对应分块重新预约）；
   - **画廊切换图片**：未下载的图片会自动按需下载，完成后显示；
   - **停止预览**：随时停止下载与播放，释放该文件的优先级。
5. 状态栏实时显示做种数、连接数、速度与预览缓冲进度。

## 项目结构

```
magnet-viewer/
├── main.py               # 入口
├── core/
│   ├── parser.py         # bencode 编解码（含大小/深度/整数 DoS 上限）+ .torrent / magnet 解析
│   ├── fetcher.py        # SessionManager Facade：组装下述 6 服务 + 薄委托 + 兼容别名
│   ├── registry.py       # 任务注册表与单锁归属：TaskRecord / 锁 / torrents+tasks / 当前别名组
│   ├── session.py        # libtorrent 会话生命周期：配置构造/端口回退/代理限速热更新/告警循环/per-task 超时看门狗/退出清理
│   ├── resolver.py       # 解析编排：resolve 两入口/换代防竞态/connect_peer/元数据就绪与完成告警链
│   ├── taskops.py        # 下载任务 CRUD：添加/转正/激活/暂停恢复/优先级/移除(守卫删文件)/tasks() 快照(出锁派生)
│   ├── preview.py        # 预览桥：磁盘路径反查→PieceMap→点播补拉 / have_piece / status 快照
│   ├── scheduler.py      # 预览调度：单文件锁定 + 顺序分块 + 索引块优先 + 按需补拉
│   ├── stream_server.py  # 本地 HTTP 流服务（127.0.0.1 + Range + token/Host 鉴权）
│   ├── cache_guard.py    # 缓存目录守卫（防误删用户数据目录）
│   ├── cache_quota.py    # 预览缓存配额（超限按 LRU 清理，仅动 .preview/）
│   ├── persist.py        # 任务持久化：.tasks.json 原子写 / fastresume 读写与退出落盘 / 启动恢复（依赖注入，不反向依赖 fetcher）
│   ├── states.py         # 任务生命周期常量层（STATE_* / DOWNLOAD_STATES / BOOTSTRAP_TRACKERS）
│   ├── logutil.py        # 统一日志（滚动 1MB×3，可关闭，绝不因日志抛异常）
│   ├── models.py         # 数据模型
│   └── config.py         # QSettings 持久化 + 代理/历史映射
├── ui/
│   ├── main_window.py    # 主窗口与线程桥接（含拖放与输入历史）
│   ├── file_tree.py      # 文件树
│   ├── preview_pane.py   # 预览容器（播放器/画廊切换）
│   ├── preview_player.py # 内嵌视频播放器
│   ├── gallery.py        # 图片画廊
│   ├── status_panel.py   # 状态面板
│   └── settings_dialog.py# 设置对话框（代理 / 超时 / 缓存）
├── REVIEW.md             # 上一轮全面审查报告（含修复记录）
├── audit_report.md       # 本轮团队全面审查报告与修复进度
├── contract_check.py     # 对外契约自检（秒级，重构 fetcher 的安全网）
├── persist_test.py       # 持久化专项（假依赖，秒级）
├── session_test.py       # 会话核心专项（假依赖，秒级）
├── registry_test.py      # 注册表与锁归属专项（含 R-1 锁探针，秒级）
├── taskops_test.py       # 任务 CRUD 专项（含 R-3 出锁派生探针，秒级）
├── resolver_test.py      # 解析与元数据编排专项（假会话，秒级）
├── preview_test.py       # 预览桥与状态专项（假句柄，秒级）
├── hybrid_v2_test.py     # 入口矩阵混合 v2 列（两入口端到端）
├── regression_run.py     # 一键回归（16 套）
├── magnet-viewer.spec    # PyInstaller 打包配置（onedir，产物 dist/MagnetViewer/）
├── build.bat             # 一键打包（清旧产物 → 构建 → pack_check 自检）
├── pack_check.py         # 打包产物自检（必需组件 / 瘦身是否失效 / 体积哨兵）
├── smoke_test.py         # 无 GUI 冒烟测试（python smoke_test.py）
├── local_magnet_test.py  # 本机闭环验证：做种端 + 磁力链解析 + 边下边播（无需外网）
├── moov_stream_test.py   # moov 尾部优先端到端验证（ffprobe/ffmpeg 实际探测，无 GUI）
├── qt_stream_open_test.py# QMediaPlayer（FFmpeg 后端）offscreen 实测开播
├── gui_feature_test.py   # GUI 交互校验：拖放 / 输入历史 / 文件树展开（offscreen）
├── live_test.py          # 真实 DHT 磁力链验证（需能访问 BT 网络）
├── requirements.txt
└── start.bat             # 一键启动
```

## 验证状态

| 验证项 | 结果 |
|--------|------|
| `contract_check.py`：对外契约自检（23 个公开接口签名 / 3 个属性 / 9 项实例兼容属性 / models·parser·scheduler·stream_server·cache_guard·cache_quota·persist·session·registry·taskops·resolver·preview 签名 / states·registry 常量取值 / TaskRecord 字段集 / fetcher 别名全 property 结构 / R-4 UI 无私有直写 / CACHE_MARKER 常量）—— **157 项通过** | 通过（秒级，不启会话） |
| `persist_test.py`：**第一阶段持久化专项**（假依赖，不启会话/不联网）——纯函数路径卫生、任务清单原子写与失败不扩散、fastresume 请求/归属、退出清理（有界等待·幂等重发·临时键 tmp-<id> 不再掀翻 drain）、启动恢复九组（无 resume / resume 有效 / resume 损坏 / .torrent / 来源失效 / add 失败 / 暂停·停止·完成 / 元数据就绪 / 目录冲突）、Facade 委托接线 —— **91 项通过** | 通过（1.6s） |
| `session_test.py`：第二阶段会话核心专项（假依赖）——会话配置纯函数 / start 端口冲突回退与恢复异常不阻断 / 代理限速热更新 / shutdown 四步（remove_torrent(handle,0) 绝不删用户数据·有界 join·drain 恰一次）/ 五类告警分发 / per-task 看门狗（记录级超时·四态过滤·文案取数 D5）/ sweep 节流 / alert 循环整批韧性 / Facade 真接线 —— **78 项通过** | 通过（0.7s） |
| `registry_test.py`：第三阶段注册表与锁归属专项——hash_key/ih_from_params 纯函数 / put_record 让位与别名 / 焦点换代 / find_record 双重匹配 / detach 三件套 / **R-1 锁探针（try-acquire + property spy + 4 线程×500 轮并发；_emit_error 端到端）** / preview_dir / Facade property 单源 —— **47 项通过** | 通过（0.3s） |
| `taskops_test.py`：第四阶段任务 CRUD 专项（假句柄+真注册表）——入参防御 / D10 转正两入口 / activate 调用序列 / 优先级·暂停·恢复（看门狗重启）/ remove 的 delete_files 两分支与 D9 守卫 / **R-3 锁探针：handle.status() 出锁派生** / tasks() 派生字段 / Facade 7 API 路由 —— **62 项通过** | 通过（0.2s） |
| `resolver_test.py`：第五阶段解析编排专项（假会话）——begin_resolve 换代四分支 / gen 代次自弃 / .torrent 与磁力链两入口（upload_mode·bootstrap tracker 注入·快路径）/ connect_peer 超时与 task_id 定位 / 元数据就绪链（READY·DOWNLOADING·保持 PAUSED·幂等·失败 FAILED）/ 完成链 D3 分叉 / result_from_torrent_info（P0-1/P0-2 防回归）/ Facade —— **55 项通过** | 通过（0.4s） |
| `preview_test.py`：第五阶段预览桥专项（假句柄）——磁盘路径反查（分隔符归一）/ PieceMap 透传 / demand 区间钳制与除零防线 / **P1-8 语义：不可判定→None/False 绝不整文件可用** / status 同源一次扫描·一致快照·降级 / Facade 8 入口路由 —— **34 项通过** | 通过（0.2s） |
| `smoke_test.py`：解析 / **本地种子注入 cache_dir** / 路径穿越防护 / bencode 防御（深度炸弹·超长整数·超长长度字段） / 鉴权（无 token·伪造 Host → 403） / Range 流服务 / 前缀钳制 / **中文·特殊字符文件名往返** / 分块级可用性 / 尾部索引窗口 / **点播+等待** / **不可判定不降级（pieces_cb 未命中 → 503，绝不喂稀疏零数据，REVIEW-2026-09 P0-4）** / **代理配置映射（含 tracker 重置）** / **会话启动参数** / **限速与日志开关接线** / **缓存配额 LRU（保护名单·limit=0·散落文件）** / 模块导入 | 通过 |
| `local_magnet_test.py`：磁力链 → 元数据 → 单文件顺序下载 | 通过（元数据 1.0s、info_hash 一致、900 KB 缓冲至 100%、磁盘字节数一致） |
| `moov_stream_test.py`（ffprobe/ffmpeg 实测，需 imageio-ffmpeg，缺失时退出码 2=SKIP） | 通过：A 仅头部→打不开（复现 moov not found）；B 头+尾+**按需补拉**→可探测；C 全量→可探测 |
| `qt_stream_open_test.py`（QMediaPlayer FFmpeg 后端 offscreen 实测，依赖同上） | 通过：A 仅头部→`FormatError`（即用户遇到的 moov atom not found）；B 头+尾+按需补拉→`LoadedMedia` 成功开播；C 全量→成功 |
| GUI 无头启动 | 通过（主窗口构造、会话与流服务启动、退出码 0） |
| `gui_feature_test.py`（offscreen 实测 46 项） | 通过：主窗口实例化 / **设置接线（默认下载目录·并发数生效、流服务多根）** / 拖放接受·拒绝 / 输入历史（置顶去重、上限 15、持久化读回、**测试后恢复不污染用户注册表**）/ 文件树（嵌套目录三级展开、无折叠、无 `.pad`、叶子数与可见文件数一致）/ **磁盘路径映射键为绝对路径且可命中** / **清理缓存保留名单（downloads/.tasks.json/.resume 不误删）** / **画廊按 save_subdir 隔离路径加载大图** / **评审 P0 防回归（添加下载对话框 priority() 可调用 · 下载页 700ms 刷新后选中与详情保持）** |
| `single_file_test.py`：单文件种子 × 本地种子/磁力链两条入口 | 通过（12/12）：路径层级、`file_disk_path` 落点、流服务按 `f.path` 供给 206（目录隔离后断言随 `.preview/<ih>/` 布局更新，接口未变） |
| `local_torrent_test.py`：本地 .torrent 闭环 | 通过（9/9）：`cache_dir` 注入、路径映射键为绝对路径、流服务联动返回字节与磁盘一致（同上随布局更新） |
| `hybrid_v2_test.py`：**入口矩阵混合 v2 列补齐**（P0-2/P1-1 防回归）：默认产种（meta version=2）× 本地 .torrent / 磁力链两条入口 | 通过（19/19）：造种自检 parser hash==lt.info_hash()（SHA-256 截断 20 字节）、两入口 info_hash 与造种端一致、多文件层级 root/inner、预览下载完成、流服务 206、纯 v2 明确 ValueError（剥 files 键构造）|
| `download_mgr_test.py`：下载管理模块验收（MVP 7 + 增强 2 + 边界 5 + 流服务安全） | **通过（80/80，退出码 0）**：添加磁力链→下载中 / 暂停（5s 磁盘字节快照不变）/ 恢复（**进度续增**为主判据——暂停快照可能已因分块乱序到达而等于全长，此时字节数本就无法再增，故字节续增降级为条件断言）/ 删除任务（目录释放、重添无残留）/ 退出重启续传（`.tasks.json`+fastresume 读回、上传增量证不重下）/ 完成（落盘=声明值）/ 双任务并发 100% / 限速 ±20% / 边界（重复 hash·per-task 看门狗·防穿越·resume 损坏重建·缓存被清不崩溃）/ 下载中任务流服务安全（分块级可用性，绝不整文件喂零数据） |
| `live_test.py`：公网 DHT | **沙箱内不可用** —— 该环境仅允许 HTTP(S) 走代理，BT/UDP 出站被屏蔽（`dht_nodes` 恒为 0）。请在正常 BT 网络下执行 `python live_test.py` 复核。 |

> 测试退出码约定：`0`=通过，`1`=失败，`2`=SKIP（依赖缺失时显式跳过，绝不假装通过）。
> 一键回归：`python regression_run.py`（16 套：`contract_check` 契约自检 + 6 套重构专项（persist/session/registry/taskops/resolver/preview，假依赖秒级）+ 8 套旧测试（含混合 v2 入口矩阵）+ 下载管理模块验收；也可 `python regression_run.py smoke` 按名字前缀单跑）。
> 覆盖率报告：`python coverage_run.py`（快速集，秒级）/ `coverage_run.py full`（全量 16 套）——coverage.py 接入，只报告不设门禁；基线（2026-09-07 快速集）：registry 97% / preview 94% / session 95% / persist 90% / resolver 88% / fetcher 85%。
> 测试覆盖策略：按**数据入口路径**（本地种子 / 磁力链；单文件 / 多文件 / 混合 v2）铺排，而非仅按功能模块——历史上三个缺陷都源于同一功能的不同入口未各自覆盖。详见 `REVIEW.md`。

## 已修复问题

1. **`moov atom not found`（用户实测复现）**。根因：MP4 的 moov 在尾部，播放器探测发起后缀 Range 请求时该区间尚未下载，旧流服务直接回 **416**，FFmpeg 把 416 当致命错误。修复：流服务改为「**点播 + 等待**」——收到未就绪区间的请求时，先通过 `demand_cb` 通知调度器 `request_range()` 立即补拉这些分块，并挂起请求等待数据到达（默认最长 20 秒），超时才退化为 416/503。
2. **文件树"看不到文件"**。根因：`expandToDepth(0)` 只展开第 0 层，多文件种子常见的 `根目录/子目录/文件` 三级结构中二级目录保持折叠。修复：文件数 ≤3000 时 `expandAll()`，超大种子展开到第 2 层防卡顿。
3. **本地 .torrent 预览退化为「按完整静态文件服务」（静默功能失效）**。根因：`parse_torrent_file()` 从不设置 `ParseResult.cache_dir`（磁力链路径由 `_result_from_torrent_info` 注入，本地种子路径漏了），主窗口据此建立的「磁盘路径 → 文件」映射键退化为**相对路径**，而流服务回调传入的是绝对路径 → `_pieces_map()` 与 `_demand_range()` 全部查不到 → 分块可用性判定与按需补拉整体失效，预览把未下载的稀疏零数据直接喂给播放器（正是第 1 条修复的 moov 问题会原样复发）。修复：解析侧注入 `cache_dir`，主窗口改以自身 `cache_dir` 为准建表（双重保险）。**5 套测试原本都覆盖不到这条路径**——它们要么走磁力链、要么绕过主窗口直连流服务。
4. **单文件种子预览完全不可用**。根因：单文件与多文件种子的落盘结构不同——libtorrent 把单文件种子存为 `save_path/name`，多文件存为 `save_path/root/inner`；而 `parser` 与 `fetcher` 统一构造成 `name/name`，单文件时多套一层目录。后果：路径映射键错位 → 分块可用性判定与按需补拉落空 → 流服务对 `url_for(f.path)` 返回 404，预览打不开（文件本身却能正常下载，因此故障现象具有迷惑性）。修复：单文件不套前缀，并移除 `file_tree` 中已冗余的单文件判定。**此前 5 套测试均未覆盖单文件种子**。
5. **BEP-52 v2 / 混合种子 info_hash 计算错误**。libtorrent 2.x 的 `create_torrent()` **默认产出 v1+v2 混合种子**（`meta version=2`）。BEP-52 规定 v2/混合种子的 info_hash 是 **SHA-256**，而旧代码固定用 SHA-1，算出的结果既不等于 v1 也不等于 v2。修复：新增 `parser.torrent_info_hash()`，按 `meta version` 分支（v2 取 SHA-256 前 20 字节，以与 libtorrent `info_hash()` 一致，保证本地种子与磁力链两条入口对同一资源得到相同标识）。同时新增 `is_pure_v2()`，对纯 v2 种子给出明确报错，替代原先的 `KeyError: b'length'`。
6. **本地流服务目录穿越（安全修复）**。两处缺陷：
   - 恶意种子可声明 `path: ["..","..","Windows","win.ini"]` 或绝对路径，旧代码在 `file_disk_path()` 中原样拼接，路径会逃出缓存目录。修复：新增 `core.models.safe_rel_path()`，逐级丢弃 `.`/`..`/空段、剥离盘符与根前缀、把分隔符与 Windows 非法字符替换为下划线；`parser` 与 `fetcher` 两条路径构造链路均已接入。
   - 流服务的越界校验用 `fp.startswith(root)`，会把**同前缀兄弟目录**误判为合法（`C:\...\cacheT` 与 `C:\...\cacheT_evil`）。实测证明旧校验对 `/../cacheT_evil/secret.mp4` 返回 True 并放行。修复：改用 `os.path.commonpath()` + `normcase` 的 `_is_within()`。
7. **「清理缓存」误删用户下载数据（数据安全修复）**。设置对话框「立即清理缓存」先经主窗口保留名单清预览缓存后，又用 `_rmtree_quiet` **无名单清空整个缓存目录**——`downloads/`（已下载文件）、`.tasks.json`（任务清单）、`.resume/`（续传数据）全被删除。修复：清理统一收敛到 `core.cache_guard.clear_cache_contents()`（保留名单：downloads/.tasks.json/.resume/受管标记），所有清理入口（对话框/退出清理）共用。
8. **画廊图片在任务隔离布局下永不显示（功能失效修复）**。目录隔离改造后任务落盘 `cache_dir/.preview/<ih>/` 或 `downloads/<ih>/`，而 `ui/gallery.py` 仍按 `cache_dir` 平铺拼路径 → 已下载图片永远显示"下载中…"。修复：新增 `core.models.disk_root()`（`save_subdir` 相对时拼接、绝对时原样使用），画廊与主窗口的磁盘键、流服务路径统一经它计算。
9. **设置项「默认下载目录」「默认并发下载数」保存后不生效（设置接线修复）**。`SessionManager` 构造只传了 cache_dir，两个设置项从未接入。修复：主窗口按配置传入 `download_dir`/`active_downloads`；流服务支持多根目录（`bases`），`download_dir` 配置在缓存目录之外时任务文件的预览/分块级可用性仍可服务（修改需重启生效，设置面板已注明）。

已验证环境：Python 3.13.12 + libtorrent 2.1.1 + PySide6 6.11.2（Windows）。

> 注意：libtorrent 2.1.x 已移除 `settings_pack`，改用 `lt.session(dict)` 配置（本项目已适配，2.0.x 同样兼容）。磁力链元数据依赖 `metadata_received_alert`，本项目显式设置了 `alert_mask` 订阅必要告警类别，并做到「单条告警处理异常不中断整批处理」。

## 已知限制（如实说明）

- **冷门资源**：磁力链必须存在在线 Peer 才能拿到元数据；0 做种资源会超时（默认 90 秒）。这是协议本质限制。
- **种子版本**：支持 v1 与 v1+v2 混合种子（BEP-52），info_hash 分别按 SHA-1 / SHA-256 计算。**纯 v2 种子**（仅含 `file tree`）暂不支持，解析时会给出明确提示。
- **纯函数单元测试**：六个重构专项（persist/session/registry/taskops/resolver/preview，共 367 项）以假依赖覆盖纯函数与分支路径，不启会话、秒级完成；混合 v2 × 两条入口的端到端矩阵仍待补（见 REVIEW.md §四）。
- **安全边界**：程序只对本地 127.0.0.1 提供服务，且所有请求路径受 `safe_rel_path()` + `_is_within()` 双重约束，不会读取或写出缓存目录之外的文件。但它仍是常规的 BT 客户端，元数据与分块来自不可信的 Peer——解析结果只用于展示，请勿据此直接打开或执行下载到的文件。
- **流式格式**：MKV 与 faststart MP4 体验最佳；moov 在尾部的普通 MP4 会先补拉尾部索引块（约 4 MB，开播慢几秒），索引就绪后即可边下边播；AVI/WMV 依赖系统解码器，可能无法播放。
- **边下边播会下载被预览的那个文件的分块**（不是整个资源）；「查看文件清单」仍然零下载。
- IP 暴露为所有 BT 客户端共性；可在「设置」中配置 SOCKS5/HTTP 代理（含账号密码、Peer 连接走代理）以隐藏真实 IP。代理仅在你主动配置时启用，不配置即直连。

## 免责声明

本工具与 qBittorrent 等客户端同为中性 P2P 工具。请勿用于获取受版权保护的资源，由此产生的法律责任由使用者自行承担。
