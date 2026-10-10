# 跨平台行为契约规格（contract_spec.md）

- **来源**：technical_roadmap Phase 4.1 / ADR-0004（双轨独立实现，以行为契约保一致性）
- **日期**：2026-10-10（Wave 4 子代理 A6c 产出）
- **方法**：每节先给**平台无关伪代码**（契约本体），再标注桌面（Python）与安卓（Kotlin）
  的当前实现位置，逐条比对行为并登记差异。**所有行号均以 2026-10-10 当前源码实读核实**
  （Wave 3 教训：历史审计报告行号可能过时——本次核实证实审计 4 项安卓 P2 假设 #12/#13/#14/#16
  均已修复，详见文末「审计假设核实结论」）。
- **差异清单**是后续子代理 A5c 修复安卓的输入。严重度：高=慢链路/大数据下功能退化或数据丢失；
  中=特定场景行为不一致；低=边缘场景或文案级；信息=有意设计差异（默认豁免）。
  处置建议：修复（A5c 执行）/ 豁免（登记为有意差异）/ 观察（桌面侧风险，另行处置）。

术语约定：`ih` = info_hash；`review 任务` = 仅解析看清单不下载；`转正` = 预览任务转下载任务；
`分块/piece` = libtorrent piece；`尾窗` = moov 尾部索引窗口；`入口` = 文件最后 min(2MB, size) 覆盖块。

---

## 1. 磁力解析状态机

### 1.1 契约伪代码

```
状态取值: QUEUED / META_FETCH / VALIDATE / DOWNLOADING / PAUSED / COMPLETED
          / STOPPED / FAILED / SEEDING / DELETED / READY(内部, 仅看清单, 不持久化)

解析(来源):
  换代(旧「当前任务」让位):
    旧当前是下载任务(含 META_FETCH 等待) → 保留运行, 仅更换焦点
    旧当前是已就绪的 review 任务        → 保留注册表, pause + upload_mode 降级
    旧当前是未就绪的 review 任务        → 移除句柄并丢弃记录
    代次 gen += 1; resolving = true; resolve_started = now; current_ih = null
  若来源是本地 .torrent 文件 → 后台线程解析文件, 命中既有下载任务则只切焦点
  若来源是磁力链:
    解析 URI 取 ih; 命中既有下载任务 → 只切焦点(不重复 add)
    save_path = .preview/<ih>; flags |= upload_mode
    无 tr= 时注入 tracker: 5 条 udp; 开关开启再追加 5 条 http/https
    元数据缓存(.meta/<ih>.torrent)命中 → 直接注入 torrent_info(免 DHT 往返)
    add 句柄; 句柄已带元数据 → 立即走「元数据到达」快路径

元数据到达(任务 T):
  T.resolving = false; 幂等: T.result 已存在则跳过
  清单解析(ti → ParseResult): 单文件不套前缀; 真多文件(含单文件套子目录)剥根段;
    路径净化防穿越; total_size 排除 .pad
  元数据先落 .meta 缓存(任何任务类型都落, 写失败无副作用)
  下载任务: 若曾暂停保持 PAUSED, 否则 DOWNLOADING; 更新清单字段; 请求 fastresume;
    非暂停时激活下载(解 upload_mode → 置 auto_managed → 按 selected 文件优先级 → resume)
  review 任务: 状态 = READY(桌面) / PAUSED+download=false(安卓, 见 C15)
  是当前焦点 → 对外发射 on_metadata

元数据失败/超时:
  每任务独立计时(resolve_started, 默认 90s, 记录级可覆盖)
  超时任务: pause 句柄 → FAILED(下载任务写 error 并持久化; review 发射错误文本)
  暂停/停止/完成/失败态任务不看门

完成(torrent_finished):
  seed=true → SEEDING + resume(做种)
  否则(D3 默认) → COMPLETED + pause + 撤 auto_managed(自动停止)
  落盘任务清单 + 请求 fastresume

新解析焦点切换不得破坏既有任务: 旧解析结果按代次/哈希路由自弃(P1-10/P1-11 竞态机制)
```

### 1.2 实现位置（读码核实）

| 侧 | 位置 |
|---|---|
| 桌面 | `core/states.py:18-33`（状态常量/`DOWNLOAD_STATES`）、`:36-56`（BOOTSTRAP/FALLBACK trackers）；`core/resolver.py:132-146`（resolve）、`:148-203`（begin_resolve 换代，gen++ 在 `:201`）、`:205-214`（下载任务复用切焦点）、`:265-314`（_resolve_magnet：upload_mode `:279`、tracker 注入 `:280-284`、元数据缓存命中 `:290-296`、快路径 `:306-314`）、`:384-466`（on_metadata_received：幂等 `:400-401`、激活 `:461-464`）、`:355-383`（on_download_finished：D3 pause `:375-377`）；`core/registry.py:40`（METADATA_TIMEOUT=90.0）、`:70-89`（hash_key/tmp 兜底键）；`core/session.py:743-814`（metadata_watchdog：per-task 计时 `:756-766`、跳过 PAUSED 等 `:760-762`、超时 pause 句柄 `:795-802`）；`core/taskops.py:301-331`（activate_download：置 auto_managed `:318`） |
| 安卓 | `core/states/TaskStates.kt:4-30`（状态常量）、`:33-60`（tracker 列表）、`:73-74`（trackersFor）、`:77`（90s 默认超时）；`engine/BtEngine.kt:664-786`（addMagnet：复用 `:689-704`、upload_mode `:718`、tracker `:716`、缓存命中 `:722-723`、快路径 `:770-777`）；`:1014-1142`（onMetadataReceived：哈希路由 `:1040-1064`、缓存先落 `:1069-1073`、清单 `:1149-1183`、收回/续传 `:1096-1113`、状态 `:1118-1122`、回调 `:1140`）；`:2725-2748`（checkMetadataTimeout）；`:1792-1866`（taskSnapshots 轮询判完成 `:1827-1843`）；`engine/TaskProgress.kt:94-99`（isTaskCompleted 防虚真） |

### 1.3 行为比对

- **一致**：状态取值集合逐字对齐；tracker 两组列表逐条一致；90s 默认超时可热调；元数据缓存
  命中跳 DHT；清单构造四规则（单文件/剥根/净化/排除 pad）一致（`resolver.py:42-86` vs
  `BtEngine.kt:1149-1183`，含「单文件套子目录」判别 `resolver.py:62` vs `BtEngine.kt:1159`）；
  混合种子 v2 截断哈希别名路由（安卓 `:1700-1722` 对应桌面 registry 的 hash_key 兜底）；
  临时键 `tmp-<seq>` 语义一致（`registry.py:88` vs `BtEngine.kt:1595`）。
- **换代机制不同但等效**（豁免）：桌面用全局 `gen` 计数防陈旧解析回写；安卓无 gen，靠
  `@Synchronized` 告警处理 + 按哈希/别名路由归属（`BtEngine.kt:1061-1064`）。语义目标一致
  （旧解析不得污染新任务），实现机理不同，不判差异。
- **差异**：见 C11（超时不 pause 句柄）、C12（完成无 D3 自动停止）、C13（auto_managed
  方向相反）、C14（per-record 超时覆盖缺失）、C15（review 态取值 READY vs PAUSED）、
  C16/C17（.torrent 入口 / connect_peer 缺失）、C18（metadata_failed 即时失败仅安卓有）。

---

## 2. 边下边播调度

### 2.1 契约伪代码

```
参数(两端逐值一致): 窗口字节预算 16MB; 块数夹取 [4, 60]; deadline 步长 400ms 递增;
  尾窗 = min(size, max(2MB, 0.25%·size), 16MB), 块数上限 128; 尾入口 = 最后 min(2MB, size);
  moov 尾窗家族 = {.mp4 .mov .m4v .3gp .3g2 .mj2}

窗口块数 = clamp(ceil(16MB / 块长), 4, 60)   ← 一切「块数上限」的唯一口径

开始预览(句柄 H, 文件 F):
  仅 F 优先级=4, 其余=0; 解 upload_mode; 置 auto_managed; resume
  锚点: play_from = F.start_piece; scheduled_to = start-1
  尾窗(F) 非空 → 预约尾块: 入口块 deadline=400ms(与头窗第2块同级并行),
    其余尾块 deadline = 头窗规模×400 + 1000 + j×400(排队在后)
  立即 tick() 建头窗

tick(周期 ~700ms/1s, 非阻塞取锁, 拿不到跳过本拍):
  尾窗未齐 → 重发尾窗预约(入口保持提前)
  first_missing = 从 play_from 起第一个未落盘块
  防抖裕量 slack = max(1, min(16, 窗口块数/2)); first_missing ≤ scheduled_to - slack → 不动
  否则从 max(first_missing, scheduled_to+1) 起预约 min(F.end, first_missing+n-1),
    deadline 以 play_from 为锚递增

点播(字节区间 [s,e)):  ← 播放器 Range 等待时调用, 幂等
  换算块区间 [first, last], last 夹取 = min(last, first + 窗口块数 - 1)   ← 与头窗同源的字节口径!
  逐块 deadline 从 0 递增; 不移动顺序锚点

跳转(字节偏移): 必须真正生效(阻塞取锁)
  清全部旧 deadline → play_from = 目标块 → 重建尾窗预约 + 新位置头窗 → tick
  (不清旧预约 = 远端残留块抢带宽, 已缓存区反而卡顿)

开播门控: 头部连续前缀 ≥ min(1MB, 文件大小) 且 尾入口就绪 → 放行; 等待有上界, 到点可归因文案
失败重试: 播放器打开失败 → rearm(无条件重刷尾窗+播放位置窗口, 有界) + 指数退避重排

停止预览:
  hold 档: 清 deadline + 优先级全 0 + 撤 auto_managed + pause
  convert 档(release_only): 只清 deadline + 还原锚点(不清优先级/不 pause),
    是否转正由宿主决策
```

### 2.2 实现位置（读码核实）

| 侧 | 位置 |
|---|---|
| 桌面 | `core/scheduler.py:21-36`（全部参数）、`:39-59`（window_pieces）、`:62-78`（tail_window_bytes）、`:81-97`（tail_entry_pieces）、`:100-112`（tail_piece_window）、`:268-297`（_request_tail 入口提前）、`:440-487`（begin）、`:489-538`（request_range，**字节口径夹取 `:528-531`**）、`:540-584`（seek_to_byte 先清后建 `:570`）、`:668-720`（tick，slack `:708`）、`:722-755`（rearm）、`:770-804`（stop release_only）；开播门控在 UI 侧：`ui/stream_host.py:243-281`（判据 `:256-258`，超时 60s `:167`）、`:319-356`（on_stream_failed→rearm）、`:358-380`（retry_stream 续播位置）；纯判据 `core/preview.py:332-378`（gate_snapshot 单快照） |
| 安卓 | `core/sched/PreviewPlan.kt:12-36`（参数）、`:45-49`（windowPieces）、`:57-61`（tailWindowBytes）、`:68-87`（tailEntryPieces/tailPieceWindow）、`:96-107`（tailPlan）、`:127-141`（rangePlan，**夹取用 LOOKAHEAD_PIECES 常量 `:138`**）；`engine/PreviewScheduler.kt:149-181`（begin）、`:198-225`（switchTarget）、`:234-253`（pause=hold 句柄动作）、`:262-293`（resume）、`:304-336`（stop releaseOnly）、`:354-361`（requestTail）、`:370-387`（requestRange）、`:398-424`（seekToByte）、`:430-457`（tick，slack `:446`）；门控 `engine/PreviewGate.kt:13`（30s）、`ui/magnet/MagnetViewModel.kt:1408-1417`（判据 `:1413`） |

### 2.3 行为比对

- **一致**：全部数值参数逐值相同（16MB/[4,60]/400ms/2MB-16MB-0.25%/128/尾家族六项）；
  尾窗入口提前策略一致（`tailPlan` vs `_request_tail`）；tick 滚动算法与 C2 防抖
  `max(1,min(16,n/2))` 一致；seek 先清后建一致；begin/stop 句柄动作序列一致；
  hold/convert 两档语义一致（安卓 `released` 标志保留句柄供可用性查询属有意保留，注释 `:27-37`）。
- **差异**：
  - **C21（高）**：安卓 `rangePlan` 点播夹取用**块数口径** `LOOKAHEAD_PIECES=60`
    （`PreviewPlan.kt:138`），桌面 plan/07 阶段 1 已改为**字节预算口径** `window_pieces`
    （`scheduler.py:528-531`，注释明确「两处口径必须同源，否则等于把已修好的问题从窗口侧
    搬到点播侧」）。4MB 块种子下安卓一次拖动点播 = 240MB 全 ASAP 洪泛（正是桌面实证过的
    「连续前缀凑不出、缓冲恒 0.0%」成因）。**处置：修复**——`rangePlan` 夹取改用
    `windowPieces(pieceLength)`（`rangePlan` 已持有 pieceLength，改动一行）。
  - **C23（低）**：安卓无 `rearm()`（桌面 `scheduler.py:722-755`，播放器失败时「催数据」
    立即重刷尾窗+头窗）；安卓门控超时后仅置 `previewFailed` 等用户点重试
    （`MagnetViewModel.kt:1418-1430`），无等价重刷。慢链路上安卓重试首轮更易再失败。
    **处置：修复（可选）**——在重试入口补一次 `requestTail + 头窗重刷`。
  - **C22（低）**：开播门控阈值：桌面 `contig ≥ min(1MB, size)`（`stream_host.py:257`），
    安卓仅 `bufferedBytes() > 0`（`MagnetViewModel.kt:1413`）；超时桌面 60s
    （`stream_host.py:167`）vs 安卓 30s（`PreviewGate.kt:13`，其注释「与桌面 main_window
    的等待口径一致」已过时）。安卓更早放行→播放器探测失败率升高，但配合流服务渐进等待
    不致命。**处置：修复**（对齐 1MB 阈值与 60s，或改注释显式豁免）。
  - **C24（信息，豁免）**：安卓 `requestRange` 用 50ms 限时等锁
    （`PreviewScheduler.kt:371-375`），桌面 `request_range` 非阻塞拿不到即跳
    （`scheduler.py:489`）。安卓注释论证充分（点播是用户可感知动作），维持。

---

## 3. Range 流服务语义

### 3.1 契约伪代码

```
绑定: 仅 127.0.0.1 随机端口; URL 带每会话随机 token(?t=) + Host 白名单
      {127.0.0.1, localhost, ::1}; token 常量时间比较; 不符 → 403
路径: 解码 → 净化(safe_rel_path) → 根目录包含性(canonical, 防同前缀兄弟目录) → 逐根解析;
      非文件 → 404
可用性: 只暴露已下载分块(pieces_cb → PieceMap); 回调异常/判不出 → 按「不可用」503,
      **绝不降级为整文件可用**(稀疏零数据喂播放器是历史 P0)
Range 解析: 只取第一个区间; bytes=-N 相对逻辑大小; 非法 → 按无 Range; start≥logical → 416
应答:
  206: Content-Range 总长恒为**逻辑大小**; Content-Length 恒等于实发字节(绝不谎报);
       Cache-Control: no-store; X-Content-Type-Options: nosniff; Accept-Ranges: bytes
  416: Content-Range: bytes */<logical> + X-Why: range + Content-Length: 0
  503: Retry-After: 1 + X-Why ∈ {no-data, unavailable, overloaded} + X-Ready-Bytes
  无 Range: 立即以当前连续前缀应答(前缀<逻辑大小 → 206, 全量 → 200)——不等待
  区间未就绪: 向调度器点播该区间 → 等待(见下) → 就绪多少发多少(渐进 206)
等待: 轮询 50ms; 预算 20s(必须 < 播放器读超时); 每 2s 重新点播(幂等续期);
  就绪 ≥ 256KB(PROGRESSIVE_MIN) 即返回; 前缀连续 10 轮不增长(≈0.5s)提前交出(停滞检测);
  一个字节都没就绪 → 503(桌面细分: 文件整体无数据 503 / 有前缀但起点无数据 416)
并发: 上限超限 → 立即 503 overloaded, 绝不无界排队
僵尸连接回收: 读请求阶段 15s 超时(防「连上不发请求」占线程);
  写 body 阶段取消读超时(防播放器暂停截断传输), 但**写侧必须有空闲回收**(防停读连接
  永久占线程/额度)
优雅停机: 置停止事件 → 等待中的请求立即交出已就绪前缀返回(不耗满 20s)
诊断: 记录最近一次应答(码/区间/就绪字节/等待毫秒/原因), 失败路径同样记录(C4)
```

### 3.2 实现位置（读码核实）

| 侧 | 位置 |
|---|---|
| 桌面 | `core/stream_server.py:47-48`（token/HOSTS）、`:107-122`（鉴权）、`:51-62`（包含性）、`:126-161`（_availability 不可判定不降级 `:129-160`）、`:210-236`（503/416 头，X-Why/X-Ready-Bytes `:218-222`/`:233-234`）、`:238-272`（206/200，Content-Range 逻辑大小 `:254`）、`:274-309`（_send_file 写阶段 settimeout(None) `:277-283`）、`:311-354`（两类额度 wait/serving `:315-338`/`:340-354`）、`:356-438`（_serve_inner：404 `:364-365`、Range `:376-392`、无 Range 不等待 `:394-407`、渐进 `:421-432`、503/416 细分 `:433-438`）、`:467-517`（_wait_ready：PROGRESSIVE_MIN `:498`、STALL `:500-505`、续期 `:514-516`、stop_evt `:507-511`）、`:84`（timeout=15s）、`:540-567`（max_concurrency=16 `:542`、wait_limiter ×4 `:557-558`）、`:613-630`（shutdown 置位顺序）、`:173-197`（last_note C4）；keep-alive：`:70-84`（HTTP/1.1 + 15s 论证） |
| 安卓 | `stream/StreamServer.kt:40-62`（头注释语义）、`:89`（127.0.0.1）、`:521-526`（鉴权）+`:662-666`（常量时间比较）、`:532-569`（resolvePath 逐根 S7）、`:282-319`（serveFile：416 `:285-298`、开区间夹 1MB `:301`、渐进 `:303-316`）、`:325-344`（serveOpening **等待** `:333-343`）、`:371-414`（waitForReady：渐进 `:388`、停滞 `:389-395`、续期 `:399-406`）、`:418-446`（头，Connection close `:423`、503+X-Why `:437-446`）、`:462-484`（206/200）、`:493-517`（sendFileBody 绝不短写）、`:104-112`（固定池 8+队列 16）、`:195-206`（过载 503）、`:228-245`（读 15s→写 soTimeout=0 `:232`/`:242`）、`:116-158`（**写空闲看门狗 60s**）、`:588-627`（参数：20s/50ms/2s/256KB/STALL=10/READ 15s/WRITE 60s）；Range 纯函数 `core/http/HttpRange.kt:24-50` |

### 3.3 行为比对

- **一致**：token+Host 鉴权、路径双重净化、不可判定不降级（安卓找不到任务→null PieceMap→
  等待→503，`StreamServer.kt:346-354` + `BtEngine.kt:2790-2798`）、Content-Range 逻辑大小、
  503/416 诊断头（X-Why/X-Ready-Bytes/Retry-After）、渐进 256KB + STALL 10 + 续期 2s +
  轮询 50ms + 预算 20s 全部逐值一致、过载明确 503 不无界排队、优雅停机（安卓靠
  `pool.shutdownNow()` 中断 + `waitForReady` 捕获 InterruptedException `:409-411`，
  等效桌面 stop_evt）。
- **审计 #16 已修复**：安卓写阶段 `soTimeout=0` 现配套**写空闲看门狗**（60s 无写出即
  close，`StreamServer.kt:116-158`、`:242-245`），僵尸停读连接不再永久占线程池。
- **差异**：
  - **C31（中）**：无 Range 请求语义相反——桌面**不等待**、立即以当前连续前缀应答 206
    （`stream_server.py:394-407`）；安卓 `serveOpening` **等待**至 1MB 前缀就绪或 20s 超时
    （`StreamServer.kt:325-344`，`openingMinBytes=1MB`）。探测阶段的响应时延与失败面不同。
    **处置：需产品确认**——安卓注释称 ExoPlayer 探测依赖开播前缀，若属实则**豁免**并回写
    桌面文档；否则修复对齐。
  - **C32（中）**：并发额度口径——桌面分**等待/服务两类额度**（serving 16 + waiting 64，
    等待不占服务额度，`stream_server.py:315-354`，S2 专项修复「等待者占满额度」）；
    安卓固定池 8 线程**等待即占线程**，8 个慢等待者让后续请求进队列、队列 16 满即 503
    （`StreamServer.kt:104-112`）。多路探测+seek 并发时安卓更易 overloaded。
    **处置：修复**——安卓等待阶段与写 body 阶段分账（或至少等待用可超时的队列而非占线程）。
  - **C33（低）**：起点无数据时的细分——桌面区分「整文件无前缀→503」与「有前缀但请求
    起点未就绪→416」（`stream_server.py:433-438`）；安卓 `ready <= start` 一律 503
    （`StreamServer.kt:304-306`）。对 FFmpeg/ExoPlayer 重试行为影响小。**处置：修复（低优先）**。
  - **C36（低）**：开区间 `bytes=a-` 安卓自定 1MB 上界（`openEndedCapBytes`，
    `StreamServer.kt:73`、`:301`，论证「无上界永远等不到全落盘」）；桌面无界等待但靠
    渐进+停滞提前返回兜底。结果收敛一致，安卓实现更直白。**处置：豁免**。
  - **C34（低）**：连接复用——桌面 HTTP/1.1 keep-alive（`stream_server.py:70-84`，论证
    高频开合被安全软件断连）；安卓每个响应 `Connection: close`（`StreamServer.kt:423`，
    未见反向论证注释）。**处置：豁免+登记**（安卓 ExoPlayer 行为不同，需真机回归再定）。
  - **C35（信息）**：越界路径安卓 403（`StreamServer.kt:262-264`）vs 桌面 404
    （`stream_server.py:364-365`）。语义都拒绝，豁免。
  - **C37（信息，观察）**：桌面写 body 阶段完全无写超时/看门狗（`stream_server.py:277-283`
    置 None 后不恢复直到写完）——停读对端可长期占住 serving 额度（daemon 线程有界 16）。
    安卓反而更强（60s 看门狗）。**处置：登记桌面侧观察项**（不在 A5c 范围）。

---

## 4. 任务持久化 / fastresume

### 4.1 契约伪代码

```
清单载体: <cache>/.tasks.json; 结构化 JSON(version=1), 键=ih(40/64 hex 小写)
原子写: 同目录唯一临时名(pid/tid/nanotime+序号) + fsync + 原子替换 + 失败清残骸;
  同目标路径互斥; Windows 目标被读句柄占用时替换有界重试
损坏容错(B3): 缺失/非 JSON/截断/顶层错/版本不符 → 空表(绝不抛、绝不阻断启动);
  单条非法 → 只丢该条并 warn 上报; 磁盘类型绝不信任(逐字段钳制)
写时机: 任务增删/状态切换/退出; 快照必须在锁内取、序列化落盘在锁外
fastresume: <cache>/.resume/<ih>.fastresume(bencode); 文件名强制 40/64 hex 防注入;
  同样原子写; 请求→告警→落盘是异步链
请求条件: 仅 download 且 result 就绪的任务; 标志 = flush_disk_cache | save_info_dict
  (不带 info 段 → 重启退回重拉元数据, P-1b)
落盘归属: 告警句柄取不到合法 ih → **跳过**(绝不回退焦点哈希, 防跨任务污染)
落盘时机(桌面): 暂停/继续/完成/转正/退出 + **60s 周期脏写**(仅 DOWNLOADING) +
  退出 drain(有界等待全部下载任务落盘, 超时只告警)
启动恢复(逐任务, 任何失败只置该任务 FAILED 不阻断):
  ih 非法 → FAILED 留可见状态
  save_path 消毒: 逃出 cache_dir/下载根 → 回落默认目录; 目录创建失败 → FAILED
  resume 缺失 = 正常态(重拉元数据); resume 损坏 = 静默降级全新加入
  无 resume 且无 info → 回退 .meta 元数据缓存(P-1c, 免重启全体重拉雪崩)
  来源: magnet: → 磁力参数; .torrent 且文件在 → 种子文件; 否则 FAILED「来源不可用」
  磁盘态 ∈ {PAUSED, STOPPED, COMPLETED} → 加完即 pause + 撤 auto_managed
  否则 → 续传; 元数据已在且态 ∈ {META_FETCH,QUEUED,VALIDATE} → 提升 DOWNLOADING 并激活
退出: 落盘清单 + **全部**下载任务请求 fastresume(不只焦点) + drain 有界等待 +
  remove 全部句柄(保留磁盘文件) + 复位
崩溃遗留: 启动时清扫固定三处(cache 根/.resume/.meta)的 *.tmp 孤儿(>60s 才删, 旁路不抛)
```

### 4.2 实现位置（读码核实）

| 侧 | 位置 |
|---|---|
| 桌面 | `core/taskstore.py:35-37`（文件名/版本）、`:44-58`（normalize_info_hash）、`:113-160`（normalize_task 钳制）、`:165-189`（分片写锁 64，注释 `:179-186` 明确对比安卓按路径字典的无界问题）、`:192-213`（_replace_with_retry）、`:215-269`（atomic_write_bytes）、`:293-325`（load_tasks 容错）、`:285-290`（save_tasks）；`core/resume.py:24-38`（路径约定）、`:99-105`（write_resume）；`core/persist.py:164-206`（persist_tasks 锁内快照）、`:217-234`（request_resume：download+result 守卫 `:226-228`、save_info_dict `:230-232`）、`:236-255`（write_resume_from_alert 坏 ih 只告警 `:247-255`）、`:257-309`（drain_resume_alerts，timeout=3.0 `:257`）、`:311-453`（restore_task：消毒 `:329-338`、损坏降级 `:343-353`、.meta 回退 `:399-402`、暂停档 `:414-420`、激活 `:446-450`）；`core/session.py:39`（RESUME_SWEEP_INTERVAL=60）、`:816-827`（resume_sweep）、`:540-593`（shutdown：全任务请求 `:553-570`、drain `:580`、remove options=0 `:583-591`）；`core/cache_guard.py:225-261`（sweep_tmp_files） |
| 安卓 | `data/TaskStore.kt:35-44`（文件名/版本/路径）、`:52-60`（normalizeInfoHash/isResumeKey）、`:144-165`（decodeTask 钳制，`download` 字段 `:163`）、`:192-233`（atomicWriteBytes+writeLocked，**按路径字典互斥 `:193-197`**）、`:249-265`（sweepTmpFiles）、`:268-282`（saveTasks/Quietly）、`:286-328`（loadTasks 容错）；`data/ResumeStore.kt:27-55`（path/read/write/writeQuietly）；`engine/BtEngine.kt:1198-1222`（requestResumeData 焦点版，SAVE_INFO_DICT `:1217`）、`:2191-2202`（requestResumeDataFor，**无 download/result 守卫**）、`:1228-1268`（writeResumeFromAlert，无 ih 跳过 `:1246-1250`）、`:554-601`（drainResumeBeforeShutdown，timeout=1.5s `:554`、跳过已有文件 `:568`）、`:1364-1590`（restoreTasks）、`:1313-1350`（buildRestoreParams，**无 .meta 回退**）；`data/RestorePlan.kt:89-95`（stateAfterRestore）、`:104-161`（planRestore）；落盘触发 `ui/magnet/MagnetViewModel.kt:393-395`（合并队列单写者）、`engine/BtEngine.kt:1899/1953/2072/2279/2348/2383/2409`（事件点） |

### 4.3 行为比对

- **一致**：`.tasks.json`/`.resume/<ih>.fastresume` 布局与字段同名（安卓多一个显式
  `download` 字段——桌面 review 任务根本不进清单，安卓把「仅解析」也持久化并靠该字段
  恢复为 upload_mode，`BtEngine.kt:1482-1498`，属**有意扩展**，豁免）；原子写三件套
  （唯一临时名+fsync+原子替换+清残骸）一致；损坏容错 B3 一致；save_info_dict 一致
  （安卓论证更严：libtorrent4j 无 url 访问器，缺 info-dict 恢复即死，`:1211-1217`）；
  恢复九组场景对齐（`RestorePlan.kt:22-30` 头注释逐条列桌面语义）；tmp 孤儿清扫一致
  （`TaskStore.kt:249-265` vs `cache_guard.py:225-261`，同为三处固定位置+60s 新鲜度）。
- **审计 #12 已修复**：`drainResumeBeforeShutdown` 现遍历**全部下载型任务**逐个请求
  （`BtEngine.kt:554-577`，注释 `:549-552` 记载修复缘由）。
- **审计 #13 已修复**：`writeResumeFromAlert` 取不到哈希**直接跳过**，不回退
  `currentInfoHash`（`BtEngine.kt:1241-1250`，注释记载跨任务污染缘由）。
- **差异**：
  - **C41（中）**：安卓**无 60s 周期 fastresume 脏写**（桌面 `session.py:816-827`
    `resume_sweep`，libtorrent 建议 ≥1min）；安卓只在事件点写（pause/resume/preview/
    promote/VM 元数据就绪/App onStop/退出）。长时间连续下载中进程被杀 → 恢复点回退到
    最后一个事件（可达数小时进度丢失）。**处置：修复**——`tick()`（1s 心跳，
    `BtEngine.kt:2709-2717`）内加 60s 节流的全任务脏写即可。
  - **C42（中）**：安卓退出 drain **跳过已有 fastresume 的任务**
    （`BtEngine.kt:568` `if (target.isFile && target.length() > 0) continue`）——只补
    「从未落过盘」的任务，已有旧文件的不刷新；桌面退出对**全部**下载任务重新请求
    （`session.py:553-570`）。配合 C41，安卓正常退出时最新进度同样可能不落盘。
    **处置：修复**——去掉该跳过，全部请求 + 统一等待。
  - **C43（中）**：安卓启动恢复**不回退 `.meta` 元数据缓存**（`buildRestoreParams`
    `BtEngine.kt:1313-1350` 只用 resume/磁力链；`readCachedTorrentInfo` 仅加种路径
    `:722` 调用）；桌面 P-1c 在「无 resume 或无 info 段」时注入缓存的 torrent_info
    （`persist.py:399-402`）。安卓冷资源重启必重拉元数据（90s 超时风险）。
    **处置：修复**——恢复参数无 ti 时查 MetaCache 注入。
  - **C44（低）**：安卓 `requestResumeDataFor` 无 download/result 守卫
    （`BtEngine.kt:2191-2202`），对仅解析任务暂停/继续也会写 fastresume（无续传价值、
    徒增 .resume 杂物）；桌面 `request_resume` 明确跳过（`persist.py:226-228`）。
    **处置：修复**（一行守卫）。
  - **C46（低）**：drain 预算安卓 1.5s vs 桌面 3.0s（`BtEngine.kt:554` vs
    `persist.py:257`）。安卓多任务均摊更紧。**处置：豁免**（移动设备上退出预算更短合理，
    修 C42 后建议同步放宽到 3s）。
  - **C45（低）**：安卓 `writeLocks` 按路径无界字典（`TaskStore.kt:193-197`），`.meta`
    写也走此路径（`MetaCache.kt:107`）→ 锁对象随「看过的种子数」无界增长；桌面正是为此
    改 64 分片（`taskstore.py:179-189` 注释点名安卓做法）。**处置：修复**（改分片）。

---

## 5. 缓存配额清理

### 5.1 契约伪代码

```
范围: 配额清理**只动 .preview/ 一级子目录**; downloads/.tasks.json/.resume/.meta 等
  属保留名单管辖, 配额模块无权涉足
LRU: 活跃时间 = 目录内全部文件最大 mtime(空目录退化目录 mtime); 最旧优先,
  同刻按路径定序(确定性); 删到总占用 ≤ 上限即停; 上限 ≤ 0 = 只统计不删
口径分离: 配额判定 = 目录逻辑大小(保守, 稀疏预分配); 显示「已下载」= file_progress
  求和; 两口径不可互换
保护名单: 活跃任务落盘目录绝不删; 接受集合或回调; 回调形态下**每个候选删除前重取复核**
  (TOCTOU: 取名单→删除窗口内新登记的 review/转正任务必须现形);
  回调抛异常 = 名单故障 → 本轮保守不删(误删比漏删严重)
假成功防护: 删除后以「目录是否还在」复核, 失败不计入 freed(否则配额提前达标实际仍超限)
绝不抛: 单目录失败跳过留待下次, 只经 warn 留痕
缓存目录守卫(桌面独有, 安卓豁免): 拒绝盘符根/用户数据目录作缓存; 受管标记文件必须存在
  才允许整体清空; 清空走保留名单(CLEANUP_KEEP = downloads/.tasks.json/.resume/.meta/
  .dht_state/标记), 活任务目录整体/部分跳过(父目录含受保子目录 → 只删无保兄弟)
```

### 5.2 实现位置（读码核实）

| 侧 | 位置 |
|---|---|
| 桌面 | `core/cache_quota.py:34-46`（dir_size_bytes）、`:49-72`（downloaded_bytes 显示口径）、`:75-90`（_last_active）、`:93-105`（scan_preview_dirs）、`:108-128`（_norm_keep 回调/故障 None）、`:131-198`（enforce_preview_limit：入口快照 `:146`、逐项复核 `:175-183`、假成功复核 `:191-195`）；`core/cache_guard.py:22-30`（CACHE_MARKER/CLEANUP_KEEP）、`:53-59`（is_risky_dir）、`:80-86`（守卫）、`:89-207`（clear_cache_contents：逐条复核 `:176-188`、部分清理 `:192-197`、名单故障上抛 `:144-146`）；调用方 `core/fetcher.py:430-439`（薄委托）、`ui/cache_host.py:29-40`（唯一清理入口）、`:90-106`（立即清理=先停预览再清） |
| 安卓 | `data/CacheQuota.kt:32-35`（子目录名）、`:42-47`（normPath）、`:50-58`（dirSizeBytes）、`:81-85`（downloadedBytes）、`:88-106`（lastActiveSeconds）、`:112-129`（scanPreviewDirs）、`:165-174`（normalizeKeep/callKeep）、`:195-304`（enforcePreviewLimit：pendingRestore 兜底 `:235-239`、逐项复核 `:264-275`、假成功 `:285-289`）、`:319-399`（purgePreviewDirs 立即清理）、`:401-419`（cacheUsage）；调用方 `ui/magnet/MagnetViewModel.kt:1109-1142`（enforceCacheLimit，keepProvider=`engine.activeTaskDirs()`）、`:1145-1173`（clearCacheNow）、`:1346`（解析后触发）；`engine/BtEngine.kt:1666-1668`（activeTaskDirs） |

### 5.3 行为比对

- **一致**：只动 `.preview`、LRU 口径（最大文件 mtime + 路径定序）、字节口径分离、
  保护名单回调 + 逐项复核、名单故障保守不删、假成功不计 freed、绝不抛——逐条对齐
  （安卓 `QuotaResult` 结构化返回是形态改进，语义同）。
- **差异**：
  - **C51（信息，豁免）**：安卓独有 `pendingRestore` 兜底（`CacheQuota.kt:235-239`，
    B-01：启动恢复未完成且名单为空 → 跳过删除）；桌面恢复在会话启动内同步完成、无此窗口。
  - **C52（信息，豁免）**：「立即清理」范围——桌面清**整个缓存目录非保留内容**
    （`cache_host.py:29-40` 走 `clear_cache_contents`，含散落杂项文件），安卓只清
    `.preview` 子目录（`purgePreviewDirs`）。私有目录内差异可忽略。
  - **C53（信息，豁免）**：安卓无 cache_guard 等价物（受管标记/高风险目录拒绝/保留名单
    整体清空）——安卓缓存固定在应用私有目录，路径不可配，风险面不存在。桌面守卫语义
    在安卓**无需移植**，登记为平台性豁免。

---

## 6. 主题令牌映射

### 6.1 契约伪代码

```
模式: light(默认) / dark / system(跟随系统深浅, 取不到回落 light); 非法值回退 light, 绝不抛
注册表(桌面 Wave 3): 主题名→调色板; 键集必须完整; 重复名首注册者胜
浅色板(LIGHT)两端逐值对齐的 18 个共享语义色:
  bg #f4f5f7 · panel #ffffff · bg_input #ffffff · bg_hover #eef0f3 · bg_pressed #e6e9ee
  bg_selected #e7effd · border #e1e4e9 · border_strong #c9cfd8 · text #1f2328
  text_muted #5b6472 · text_dim #8a929f · accent #2563eb · accent_hover #3b78f0
  accent_pressed #1d4fd7 · ok #1a7f47 · warn #9a6700 · danger #c0392b · alt_row #f3f5f8
深色板: 应有**独立深色值**(非浅色值换角色), 且容器色(okSoft/dangerSoft)与文字色
  对比度充足
间距: 4/8/12/16/24 五档(8px 网格语义)
固定值(不随主题): 强调上白字 / 遮罩 / 视频区恒黑
平台扩展(允许, 但须登记): 桌面渲染扩展键(segment/image_bg/bg_compartment);
  安卓移动扩展键(surfaceSunken/tonalPressed/dangerSoft/dangerPressed/okSoft/videoPlaceholder)
```

### 6.2 实现位置（读码核实）

| 侧 | 位置 |
|---|---|
| 桌面 | `ui/theme.py:37-59`（LIGHT 21 键）、`:60-82`（DARK 独立深色板）、`:89-124`（THEMES 注册表 + register_theme 键集校验，Wave 3 新增）、`:125-126`（THEME_MODES/DEFAULT）、`:141-153`（固定值/间距/圆角 6-8-10/字号 13-14-16-20）、`:474-489`（system_mode）、`:492-506`（resolve_mode 回退）、`:537-557`（apply_theme 热切换） |
| 安卓 | `theme/Color.kt:11-44`（Pal 浅色 24 键，头注释声明与桌面 LIGHT 逐值对齐——**18 个共享键实测逐值一致**）、`:54-68`（DarkPal 仅 7 个独立深色值）、`theme/Theme.kt:25-112`（Palette.Light/Dark；**Dark 主体是浅色值按角色重映射 `:84-110`**）、`:115`（LocalPal）、`:121-128`（圆角 16/12/全圆）、`:177-187`（MagnetViewerTheme）；`theme/Type.kt:19-28`（正文 15sp 等）、`theme/Spacing.kt:21-27`（4/8/12/16/24 一致）；`data/SettingsStore.kt:47-52`（三档值域）、`:116-120`（normalizeThemeMode 回退 light）、`:142-147`（isDarkTheme system 跟随） |

### 6.3 行为比对

- **一致**：三档模式值域、非法回退 light、system 跟随语义、间距五档数值、浅色共享键
  逐值对齐（18/18 核实）、强调上白字、视频区恒黑。
- **差异**：
  - **C61（中）**：**深色板数值系统性不同**——桌面 DARK 是独立深色值
    （bg `#14161a`、panel `#1b1e24`、text `#e6e9ef`、accent `#5b9dff`、ok `#4ec27a`、
    warn `#e0a83c`、danger `#ef6b62`、alt_row `#232833`，`theme.py:60-82`）；安卓
    `Palette.Dark` 主体把**浅色值按角色重映射**（bg=Pal.text `#1F2328`、panel=Pal.textMuted
    `#5B6472`、text=Pal.bg `#F4F5F7`、accent=Pal.accentHover `#3B78F0`、warn 直接沿用
    浅色的 `#9A6700`，`Theme.kt:84-110`）。仅 ok/danger/altRow 等 7 键用了独立深色
    （`DarkPal`，审计 P0 三连的局部修复）。后果：安卓深色下 panel 为中灰 `#5B6472`、
    warn 深字在暗底对比度不足、accent 与桌面不同蓝。两端「深色主题」观感明显分叉。
    **处置：修复或显式豁免二选一**——若以桌面为基准，安卓应引入桌面 DARK 的 21 键数值；
    若安卓移动设计有意独立（Theme.kt:20-23 注释称「不新造色值」是历史决策），则回写
    契约把深色板定为「各端自定、只约束语义角色与对比度」。**建议 A5c 按「修复」处理**
    （契约基线取桌面 DARK 值），并在修复时保留 DarkPal 已修正的对比度成果。
  - **C62（信息，豁免）**：令牌集合互有缺项（桌面 segment/image_bg/bg_compartment 无
    安卓对应；安卓 6 个移动扩展键无桌面对应）——平台渲染形态差异，登记不修。
  - **C63（信息，豁免）**：圆角/字号刻度不同（桌面控件 8/卡片 10、正文 14px；安卓
    按钮 12/卡 16、正文 15sp——QA 移动可读性决策，`Type.kt:10-13` 注释记载）。
  - **C64（信息）**：桌面有主题注册表扩展口（register_theme，Wave 3），安卓无对应
    扩展机制（两档硬编码）。安卓未来加第三主题时需对齐，现阶段登记即可。

---

## 差异清单总表

编号规则：`C<流程号><序>`（如 C21 = 流程 2 第 1 号差异）。严重度依据 §「术语约定」。核实证据均为 2026-10-10 当前源码行号。

| 编号 | 流程 | 桌面行为 | 安卓行为 | 严重度 | 建议处置 | 核实证据 |
|---|---|---|---|---|---|---|
| C21 | 2 调度 | 点播夹取=字节预算 window_pieces（4MB 块→4 块） | 点播夹取=60 块常量（4MB 块→240MB ASAP 洪泛） | **高** | **修复** | `scheduler.py:528-531` vs `PreviewPlan.kt:138`（`LOOKAHEAD_PIECES` 用法未随桌面 plan/07 阶段 1 同源化） |
| C41 | 4 持久化 | 60s 周期 fastresume 脏写（仅 DOWNLOADING） | 无周期脏写，仅事件点落盘 | 中 | **修复** | `session.py:39,816-827`；安卓 `BtEngine.kt` 全文无 sweep（grep 核实），心跳 `:2709-2717` 不含 |
| C42 | 4 持久化 | 退出对全部下载任务重新请求 save_resume_data | drain 跳过已有 fastresume 的任务（不刷新） | 中 | **修复** | `session.py:553-570` vs `BtEngine.kt:568` |
| C43 | 4 持久化 | 恢复无 resume/无 info 时回退 .meta 缓存（P-1c） | 恢复路径不查 MetaCache（仅加种查） | 中 | **修复** | `persist.py:399-402` vs `BtEngine.kt:1313-1350`（buildRestoreParams 无缓存注入）、`:722`（仅 addMagnet 用） |
| C11 | 1 状态机 | 元数据超时→pause 句柄 + FAILED | 超时仅置 FAILED，不动句柄 | 中 | **修复** | `session.py:795-802` vs `BtEngine.kt:2725-2748`（无 pause 调用） |
| C12 | 1 状态机 | 完成→D3 自动停止（pause + 撤 auto_managed）+ 落盘 + resume 请求 | 完成仅轮询判 COMPLETED 改状态，句柄不动（继续做种/announce） | 中 | **修复** | `resolver.py:355-383` vs `BtEngine.kt:1827-1843`（无句柄动作） |
| C31 | 3 流服务 | 无 Range 不等待，立即回当前连续前缀 206 | 无 Range 等待至 1MB 前缀或 20s 超时 | 中 | 需确认后修复/豁免 | `stream_server.py:394-407` vs `StreamServer.kt:325-344` |
| C32 | 3 流服务 | 等待/服务两类额度分账（16+64，等待不占服务额度，S2） | 固定池 8，等待即占线程；8 慢等待者饿死服务请求 | 中 | **修复** | `stream_server.py:315-354` vs `StreamServer.kt:104-112` |
| C61 | 6 主题 | DARK 独立深色板（bg #14161a / panel #1b1e24 / accent #5b9dff / warn #e0a83c…） | Dark 主体为浅色值按角色重映射（panel=#5B6472 中灰、warn 沿用浅色 #9A6700） | 中 | **修复**（以桌面 DARK 为契约基线，保留 DarkPal 对比度成果） | `theme.py:60-82` vs `Theme.kt:84-110`、`Color.kt:54-68` |
| C22 | 2 调度 | 开播门控 contig ≥ min(1MB, size)，超时 60s | 门控仅 contig > 0，超时 30s（注释声称与桌面一致，已过时） | 低 | 修复（对齐阈值与超时，或改注释显式豁免） | `stream_host.py:167,256-258` vs `MagnetViewModel.kt:1413`、`PreviewGate.kt:13` |
| C23 | 2 调度 | 播放器失败→rearm 催数据 + 指数退避自动重试 + 断点续播 | 无 rearm；门控超时置 previewFailed 等用户手动重试 | 低 | 修复（可选） | `scheduler.py:722-755`、`stream_host.py:319-356` vs `MagnetViewModel.kt:1418-1430`（grep 安卓无 rearm） |
| C33 | 3 流服务 | 起点无数据细分：整文件无前缀 503 / 有前缀 416 | ready ≤ start 一律 503 | 低 | 修复（低优先） | `stream_server.py:433-438` vs `StreamServer.kt:304-306` |
| C34 | 3 流服务 | HTTP/1.1 keep-alive（防高频开合被安全软件断连） | 每响应 Connection: close | 低 | 豁免+登记（需真机回归 ExoPlayer 行为） | `stream_server.py:70-84` vs `StreamServer.kt:423` |
| C44 | 4 持久化 | request_resume 仅 download+result 就绪任务 | requestResumeDataFor 无 download/result 守卫（仅解析任务也写） | 低 | **修复**（一行守卫） | `persist.py:226-228` vs `BtEngine.kt:2191-2202` |
| C45 | 4 持久化 | 原子写互斥=64 分片锁（内存有界，注释点名安卓做法） | 原子写互斥=按路径 ConcurrentHashMap（随种子数无界增长，.meta 同走此路径） | 低 | **修复**（改分片） | `taskstore.py:165-189` vs `TaskStore.kt:193-197`、`MetaCache.kt:107` |
| C46 | 4 持久化 | drain 预算 3.0s | drain 预算 1.5s | 低 | 豁免（修 C42 后建议放宽至 3s） | `persist.py:257` vs `BtEngine.kt:554` |
| C13 | 1 状态机 | activate_download 置 auto_managed（受队列管理） | 恢复/续传路径一律撤 AUTO_MANAGED + 手动 resume | 低 | 豁免（安卓整体回避队列管理器的设计选择，`BtEngine.kt:1103,1513-1514`；如需转正对齐再议） | `taskops.py:318` vs `BtEngine.kt:1103` |
| C14 | 1 状态机 | per-record timeout 覆盖会话级超时 | 仅全局 metadataTimeoutSeconds | 低 | 豁免（安卓 UI 无 per-task 超时入口） | `session.py:763-764`、`registry.py:59` vs `BtEngine.kt:315,2726` |
| C15 | 1 状态机 | review 就绪态 = READY（不持久化） | 仅解析就绪态 = PAUSED + download=false（持久化） | 低 | 豁免（与安卓 `download` 字段扩展一体，见 §4.3） | `resolver.py:452-453`、`states.py:28-29` vs `BtEngine.kt:1118-1122`、`TaskStore.kt:82-83` |
| C16 | 1 状态机 | 支持本地 .torrent 文件**新添加**解析入口 | 无新添加口（仅磁力链）；但恢复路径支持 .torrent 来源（`RestorePlan.kt:125`、`BtEngine.kt:1321`） | 信息 | 豁免（功能集差异，路线图未要求） | `resolver.py:132-146,216-263` vs 安卓 grep 无 addTorrentFile/openDocument |
| C17 | 1 状态机 | connect_peer 手动直连（回环验证/DHT 受阻网络） | 无 connectPeer | 信息 | 豁免（调试/验证特性） | `resolver.py:316-351` vs 安卓 grep 无 |
| C18 | 1 状态机 | 元数据失败仅靠 90s 看门狗 | 处理 metadata_failed 告警，即时失败反馈 | 信息 | 豁免（安卓增强，桌面可反向借鉴） | `session.py:663-671`（无该告警分支）vs `BtEngine.kt:947-956` |
| C24 | 2 调度 | request_range 非阻塞拿不到即跳 | requestRange 限时 50ms 等锁（点播即时性优先） | 信息 | 豁免（论证充分） | `scheduler.py:489` vs `PreviewScheduler.kt:371-375` |
| C35 | 3 流服务 | 越界路径 → 404 | 越界路径 → 403 | 信息 | 豁免 | `stream_server.py:364-365` vs `StreamServer.kt:262-264` |
| C36 | 3 流服务 | 开区间无自定上界（靠渐进+停滞兜底） | 开区间自定 1MB 上界 | 信息 | 豁免（结果收敛一致） | `stream_server.py:421-432` vs `StreamServer.kt:73,301` |
| C37 | 3 流服务 | **写 body 阶段无任何写侧超时/看门狗**（停读对端可长占额度） | 60s 写空闲看门狗 | 信息 | **观察（桌面侧）** | `stream_server.py:277-283` vs `StreamServer.kt:116-158,242-245` |
| C51 | 5 配额 | 无启动竞态兜底（恢复同步完成，无窗口） | pendingRestore 兜底（恢复未完成+空名单→不删） | 信息 | 豁免（安卓独有防御） | `cache_quota.py:131-198` vs `CacheQuota.kt:235-239` |
| C52 | 5 配额 | 立即清理=清整个缓存目录非保留内容 | 立即清理=只清 .preview 子目录 | 信息 | 豁免 | `cache_host.py:29-40,90-106` vs `MagnetViewModel.kt:1145-1173` |
| C53 | 5 配额 | cache_guard（高风险目录拒绝/受管标记/保留名单） | 无对应（缓存目录固定私有、不可配） | 信息 | 豁免（平台性） | `cache_guard.py:22-86` vs 安卓无对应模块 |
| C62 | 6 主题 | 扩展键 segment/image_bg/bg_compartment | 无对应；另有 6 个安卓扩展键 | 信息 | 豁免（平台渲染形态） | `theme.py:33-36` vs `Color.kt:31-43` |
| C63 | 6 主题 | 圆角 8/10、正文 14px | 圆角 12/16、正文 15sp | 信息 | 豁免（移动设计决策） | `theme.py:148-151` vs `Theme.kt:121-125`、`Type.kt:19-28` |
| C64 | 6 主题 | THEMES 注册表扩展口（register_theme） | 两档硬编码，无注册表 | 信息 | 豁免（未来第三主题时对齐） | `theme.py:89-124` vs `Theme.kt:53-111` |

**A5c 修复优先级建议**：C21（高，一行）→ C41/C42/C43（中，fastresume 三件套）→
C11/C12（中，状态机动作）→ C32（中，并发分账）→ C31（中，先确认 ExoPlayer 依赖）→
C61（中，先定深色基准归属）→ C22/C23/C33/C44/C45（低）。

---

## 审计假设核实结论（full_audit_report_2026-09-29 的 4 项安卓 P2）

| 审计编号 | 内容 | 当前真实状态 | 证据 |
|---|---|---|---|
| #12 | drainResumeBeforeShutdown 只给焦点任务落 fastresume | **已修复** | `BtEngine.kt:545-577`：注释记载修复，代码遍历 `taskMap` 全部 download 任务逐个 `requestResumeDataFor` |
| #13 | writeResumeFromAlert 取不到哈希回退 currentInfoHash（跨任务污染） | **已修复** | `BtEngine.kt:1241-1250`：`ih.isEmpty()` 直接跳过落盘，注释明确「不允许回退焦点哈希」 |
| #14 | 复用分支 onMetadataReady 只传 3 参、download 默认 true | **已修复** | `BtEngine.kt:697-700`：第 4 参显式传 `existing.download`，注释记载 J-01 残留缘由 |
| #16 | StreamServer 写 body 阶段 soTimeout=0 无超时（停读占满线程池） | **已修复** | `StreamServer.kt:116-158`（写空闲看门狗 60s + FeedDogOutputStream）、`:238-245`（注释记载审计修复）、`:622`（阈值常量） |

四项与 Wave 3 的 5.2/5.3 情形相同：审计点位均已由后续提交修复。本契约文档以当前源码为准，
差异清单中**不含**上述四项。

---

## 遗留不确定项

1. **C31（无 Range 等待语义）**：安卓 `serveOpening` 等待 1MB 是否 ExoPlayer 探测的硬依赖，
   源码内无实测记录（头注释只讲「绝不短写」动机）。A5c 动手前需真机/单测确认，否则可能
   把安卓修成探测失败。
2. **C13（auto_managed 方向）**：安卓在恢复/续传路径撤 auto_managed 是「回避 active_downloads
   队列饿死预览」的延续（桌面 `scheduler.begin` 同样撤，见 `scheduler.py:459-461`），但
   桌面**任务级**激活走 `activate_download` 置位。安卓 `activateTask`（转正路径
   `BtEngine.kt:2088`）也置位——即安卓只在「元数据到达续传/恢复」路径撤、转正路径置，
   与桌面恰好相反。是否构成队列饿死/自动 resume 暂停态的实际缺陷，需行为级回归确认。
3. **C34（keep-alive）**：安卓 `Connection: close` 无注释论证；桌面 keep-alive 的「安全软件
   断连」论证是 Windows 现场经验，安卓（本机回环 + ExoPlayer）未必适用。建议豁免但留真机
   回归记录要求。
4. **C61（深色板基准）**：修复方向（安卓对齐桌面 DARK）与豁免方向（各端自定）都成立，
   取决于产品对「双端观感一致」的权重，需父代理/用户裁决后 A5c 再动。
5. 桌面 drain 重请求只带 flush_disk_cache（`persist.py:294-295`，无 save_info_dict）——
   首次请求带 info 段、重请求不带，若首次失败则重请求落盘的 resume 缺 info 段。桌面侧
   小瑕疵，不在本契约双端比对范围，登记备查。

---

## Wave 4 / A5c 修复与豁免登记（2026-10-10）

> 本节为 A5c（android/契约闭环子代理）的处置记录；上方分析原文不删改。
> 修复方向唯一：桌面为契约基线（ADR-0004），安卓向桌面靠；每条修复在代码注释标注
> 「对齐桌面 core/xxx.py:yyy」。验收：`gradlew testDebugUnitTest` 364 用例全绿
> （原 355 + 新增 9）；桌面 persist_test 95 项 / contract_check 208 项 / smoke_test /
> regression_run 全量 27 套件全绿。

### 已修复

| 编号 | 严重度 | 安卓改动（文件:行） | 桌面参照 |
|---|---|---|---|
| C21 | 高 | `core/sched/PreviewPlan.kt` rangePlan 夹取改用 `windowPieces(pieceLength)`（字节口径同源，不再用 LOOKAHEAD_PIECES 常量） | `scheduler.py:528-531` |
| C41 | 中 | `engine/BtEngine.kt` tick() 挂 `sweepResumeDirty()`（60s 节流，`RESUME_SWEEP_INTERVAL_MS`；筛选纯函数 `TaskProgress.kt:resumeSweepNeeded`） | `session.py:39,816-827` |
| C42 | 中 | `engine/BtEngine.kt` drainResumeBeforeShutdown 去掉「已有文件跳过」，全部下载任务（download+result 就绪）重新请求；等待循环改 **mtime 新鲜度** 判定（请求前记 baseline，变新才算落盘）；预算 1.5s→3s（C46 建议随修放宽） | `session.py:550-570`、`persist.py:257` |
| C43 | 中 | `engine/BtEngine.kt` buildRestoreParams 增 cacheDir 参数：无 resume（缺失/损坏）时查 MetaCache 注入 ti（RestoreParams.metaInjected 标记）；加种失败且 metaInjected → 摘缓存退基线重试（对齐 addMagnet 同款兜底） | `persist.py:399-402`（P-1c） |
| C11 | 中 | `engine/BtEngine.kt` checkMetadataTimeout 超时任务先 `handle.pause()` 再上报 FAILED | `session.py:795-802` |
| C12 | 中 | `engine/BtEngine.kt` taskSnapshots 判完成处补 D3 自动停止：`pause + unsetFlags(AUTO_MANAGED)` + `requestResumeDataFor`（安卓无 seed_after_complete 入口，桌面默认 False=恒停止，两端基线一致；SEEDING 分支安卓缺席属功能集差异，登记见下） | `resolver.py:355-383` |
| C32 | 中 | `stream/StreamServer.kt` 等待/服务两类额度分账：`needsWait` 判定 → 需等待的请求移交 `waitPool`（上限 maxConcurrency×4，同桌面 ×4 派生比例），服务线程立即归还；`servingPermits` 信号量（=maxConcurrency）守写 body 阶段（`enterServing`，对齐 `_enter_serving`）；等待池饱和/服务额度满 → 503 overloaded；close() 同步 shutdownNow 等待池（等效 stop_evt）。与桌面差距：双池 vs 信号量记账，准入语义一致（注释已写明） | `stream_server.py:315-354,542,557-558` |
| C22 | 低 | `engine/PreviewGate.kt` `PREVIEW_GATE_TIMEOUT_MS` 30s→60s + 新增 `PREVIEW_GATE_CONTIG_BYTES=1MB`；`ui/magnet/MagnetViewModel.kt` 门控判据 `bufferedBytes() >= min(1MB, file.size)`；删除 VM companion 内遮蔽用的旧 30s 常量 | `stream_host.py:167,256-258` |
| C23 | 低 | `engine/PreviewScheduler.kt` 新增 `rearm()`（阻塞取锁、无条件重刷尾窗+播放位置窗口，不 clear）；`BtEngine.rearmPreview()`；`MagnetViewModel.retryPreview()` 重试前调用 | `scheduler.py:722-755`、`stream_host.py:319-356` |
| C33 | 低 | `stream/StreamServer.kt` serveFile Bounded 分支 `ready<=start` 细分：整文件无前缀→503 no-data / 有前缀但起点未就绪→416 `X-Why: range` | `stream_server.py:433-438` |
| C44 | 低 | `engine/BtEngine.kt` requestResumeDataFor 加 `if (!t.download || t.result == null) return` 守卫 | `persist.py:225-227` |
| C45 | 低 | `data/TaskStore.kt` writeLocks 按路径 ConcurrentHashMap → 固定 64 分片数组（`writeLockFor`，同路径恒同片；`.meta` 同走此路径的锁泄漏面消除） | `taskstore.py:165-189` |
| 4.3 | — | `shared/task-schema.json` 新建（draft-07：顶层 version/schema_version/tasks + 任务记录 15 字段 = 桌面 normalize 14 字段 + 安卓扩展 download；file 子对象 6 字段）；桌面 `taskstore.py` save 写 `schema_version=1`（SHARED_SCHEMA_VERSION），load 缺字段容忍=版本 0 静默兼容、version 拒读语义不变；安卓 `TaskStore.kt` SHARED_SCHEMA_VERSION 同值读写；两端各补校验用例（persist_test §B +4、TaskStoreTest +3） | — |

### 豁免登记（有意差异 / 待真机确认）

| 编号 | 处置 | 理由 |
|---|---|---|
| C61 | **豁免（用户裁决：跨平台双轨）** | 深色板定为「各端自定、只约束语义角色与对比度」；安卓移动设计独立（Theme.kt 历史决策），不改安卓数值。 |
| C31 | **豁免+登记待办** | `serveOpening` 等 1MB 是 ExoPlayer 开播前缀依赖的假设未经真机证实；贸然改成「不等待立即回前缀」可能把安卓修成探测失败。登记：真机回归确认 ExoPlayer 行为后再定修复/豁免回写。 |
| C13 | **豁免+登记待办** | auto_managed 方向相反是安卓整体回避队列管理器的设计选择（`BtEngine.kt:1103,1513-1514`）；是否构成实际缺陷需行为级回归（真机多任务+队列饿死场景）确认。 |
| C34 | **豁免+登记待办** | 安卓 `Connection: close` vs 桌面 keep-alive：桌面论证是 Windows 现场经验，安卓回环+ExoPlayer 未必适用；需真机回归 ExoPlayer 重连行为再定。 |
| C46 | **随 C42 修复吸收** | drain 预算已同步放宽 1.5s→3s（对齐桌面 `persist.py:257`），不再单列豁免。 |
| C12-SEEDING | **登记（功能集差异）** | 桌面 seed=True 分支（SEEDING+resume）安卓无对应（无 seed_after_complete 设置入口）；本次只对齐默认 D3 停止语义。安卓做种属新特性，另行裁决。 |
| C37 | **登记（桌面侧观察）** | 桌面写 body 阶段无写侧看门狗（安卓反而更强）；不在 A5c 范围，维持原处置。 |
| 其余「信息/豁免」项（C15–C18、C24、C35、C36、C51–C53、C62–C64） | 维持原表处置 | 均为有意设计差异/平台性差异，本轮不动。 |

### C21 修复的连带说明

`LOOKAHEAD_PIECES=60` 常量保留（contract_check 冻结项 + `windowPieces` 上限仍引用），
仅 `rangePlan` 的**用法**改为经 `windowPieces` 换算——与桌面 plan/07 阶段 1 完全同形
（`scheduler.py:500-506` 注释「常量原样保留，只改用法」）。
