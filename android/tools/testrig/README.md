# 安卓端到端测试靶场（本地确定性，不依赖公网）

> 目的：验证安卓 App 的「磁力链 → 元数据 → 文件清单 → 边下边播」时，**失败必须能归因**。
> 如果只靠公网 DHT 找 peer，测试会变成薛定谔状态——通了不知道是不是运气，不通不知道是 App 还是网络。

## 原理

BEP-9 的磁力链参数 **`x.pe=<ip>:<port>`**（peer 提示）可以直接把 peer 地址写进磁力链。
Android 模拟器里 **`10.0.2.2` 等价于宿主机的 `127.0.0.1`**，于是在宿主机上做种、
把 `x.pe=10.0.2.2:<端口>` 拼进磁力链，模拟器里的 App 就能直连宿主机做种端 —— 全程不出本机。

## 文件

| 文件 | 作用 |
|---|---|
| `mkfixture.py` | 用 ffmpeg 生成两个小视频 + 建种子 + 打印带 `x.pe` 的磁力链 |
| `seed.py` | 宿主机做种端（默认端口 **16881**），要一直活着 |
| `selftest.py` | 单进程自证：做种 + 磁力链取元数据 + 实下载 + sha256 校验（证明"夹具+协议栈"这条 seam 本身是通的） |
| `payload/` | 夹具数据：`fixture.mp4`（faststart，moov 在前）、`fixture-tail.mp4`（moov 在尾部，用于验证尾部窗口调度） |
| `fixture.torrent` | 16KB 分块，10 块，2 个真实文件 + 2 个 pad；**故意不放公网 tracker** |
| `magnet.txt` | 当前磁力链（infohash 会随夹具重建变化） |

## 用法

```bash
cd /c/Users/Administrator/Desktop/magnet-viewer

# 1) 生成/刷新夹具（已存在则跳过视频，仅重建种子与磁力链）
./.venv/Scripts/python android/tools/testrig/mkfixture.py

# 2) 自证 seam（必须先绿，再谈 App 的问题）
./.venv/Scripts/python android/tools/testrig/selftest.py      # 期望：结论：seam 可用 ✓

# 3) 起做种端（后台常驻；每次改完夹具要重启）
./.venv/Scripts/python android/tools/testrig/seed.py          # 期望：[seed] ... state=seeding

# 4) 从模拟器内验证端口可达（应输出 nc exit=0）
"$ANDROID_HOME/platform-tools/adb.exe" shell 'nc -w 4 10.0.2.2 16881 </dev/null; echo nc exit=$?'

# 5) 在模拟器 App 里粘贴 magnet.txt 的内容
```

## 四个坑（都实测踩过，别再踩）

1. **`torrent_info.info_hash()` 不是 v1 哈希**。libtorrent 2.1 里它对混合种子返回 **v2 截断哈希**，
   磁力链的 `btih` 用的是 **`info_hashes().v1`**。拿 `info_hash()` 与磁力链对比会误判"hash 不匹配"。
2. **做种端的 `save_path` 必须是种子内容根目录的父目录**。种子里文件路径是 `<种子名>/xxx`，
   种子名就是 `payload`，所以 `save_path` 要用 `android/tools/testrig/`，不是 `.../payload/`。
   写错的表现是：状态一直 `downloading`、需要下载自己已有的数据。
3. **`listen_interfaces` 不要用 `:0`（随机端口）**：libtorrent 会按网卡各绑一个**不同的**随机端口，
   `listen_port()` 只返回其中一个，对端会拨错端口（实测连接被拒 10061）。**一律写死端口**。
4. **Windows 上把 uTP 绑到回环会失败**（`sock_bind ... 访问权限不允许` / WSAEACCES）；
   绑 `0.0.0.0` 正常。另外 `enable_utp` 在 libtorrent 2.1 里**不是合法设置名**（会 KeyError）。

## 当前夹具事实（2026-09-14）

- infohash(v1)：`444b8b5c9a69b388a5013d85a42caef060f03225`
- 磁力链：`magnet:?xt=urn:btih:444b8b5c9a69b388a5013d85a42caef060f03225&dn=fixture&x.pe=10.0.2.2:16881`
- 种子：`payload/fixture.mp4` + `payload/fixture-tail.mp4`（各 80832 字节，16KB 分块 ×10，含 pad）
- 自证结果：元数据 0.6s 到位；两个 mp4 sha256 与源文件逐字节一致
- 模拟器侧：`ping 10.0.2.2` 通（1ms）、`nc 10.0.2.2 16881` 返回 0（TCP 可达）

## 大夹具（灭屏 / 后台保活测试）

小夹具 88KB 一眨眼就下完，测不出“灭屏后是否还在下载”。所以另配一份大夹具：

- 内容：`payload_big/big.mp4`（**视频**，31MB 量级、256KB 分块、faststart）。必须是视频：UI 只对视频扩展名开放预览，随机字节 `.bin` 点不开预览，灭屏测试就走不到“下载中”。合成测试图案压缩率极高，所以编码时加了噪点把码率抬到 2~4 Mbps。
- 磁力链：`magnet_big.txt`（infohash 每次重建会变，**以文件内容为准**，同样带 `x.pe=10.0.2.2:16881`）
- 用做种端**上传限速**把传输拉长到分钟级：

```bash
./.venv/Scripts/python android/tools/testrig/seed.py \
    --torrent fixture_big.torrent --payload payload_big --limit-kbps 300
```

**同一端口同一时刻只能跑一个做种端**（两条磁力链都指向 16881）：切大夹具前先杀掉在做种的那个 python 进程，切回来同理。

灭屏测试的验收口径：**灭屏 60 秒内下载字节仍在增长**（两次采样对比）+ `adb shell dumpsys activity services com.bitseed.magnetviewer | grep -i foreground` 里 FGS 存活。

## 图片夹具（画廊 / 按需下载测试）

第 3 套夹具，**与其他两套互不影响**（独立目录、独立种子、独立磁力链、独立端口）：

| 文件 | 内容 |
|---|---|
| `payload_img/photo_big.png` | 1920×1080 噪点 PNG（**3.8 MB**，64KB 分块 ≈ 61 块）——用来观察画廊里的**下载进度**逐格上涨 |
| `payload_img/photo_mid.jpg` | 1280×720 JPEG（≈383 KB）——翻页用，几秒下完 |
| `payload_img/photo_small.jpg` | 320×240 JPEG（≈8 KB）——秒开，验证「已下载直接出图」 |
| `fixture_img.torrent` | 64KB 分块，68 块，3 个真实文件 + 3 个 pad |
| `magnet_img.txt` | 图片夹具磁力链（**infohash 以文件内容为准**，重建会变） |

为什么必须有一张 2MB 以上的图：小图一片分块就下完了，画廊的「未下载 → 占位 + 进度」这段
状态根本来不及看见；大图才观察得到进度文本与「下载完成自动显示」的切换。

**端口固定为 16920**（与另两套的 16881 分开）→ 三套夹具可以同时做种，不用来回杀进程。

```bash
cd /c/Users/Administrator/Desktop/magnet-viewer

# 生成图片夹具（并刷新 fixture_img.torrent / magnet_img.txt）
./.venv/Scripts/python android/tools/testrig/mkfixture.py

# 做种（端口 16920；磁盘紧的话去掉 --limit-kbps）
./.venv/Scripts/python android/tools/testrig/seed.py \
    --torrent fixture_img.torrent --payload payload_img --port 16920 --limit-kbps 300

# 模拟器侧端口可达（应输出 nc exit=0）
"$ANDROID_HOME/platform-tools/adb.exe" shell 'nc -w 4 10.0.2.2 16920 </dev/null; echo nc exit=$?'

# App 里粘贴 magnet_img.txt 的内容 → 解析 → 文件清单里点图片行（「图片 · 画廊」）→ 画廊
```

**当前图片夹具事实（2026-09-14 重建后，以 `magnet_img.txt` 为准）**

- infohash(v1)：`16f1cc4744edcb71296d782103bd620848e7b868`
- 磁力链：`magnet:?xt=urn:btih:16f1cc4744edcb71296d782103bd620848e7b868&dn=payload_img&x.pe=10.0.2.2:16920`
- 种子：总 4,456,448 字节 / 68 块 / 块长 65536

### 观察「下载进度」的小技巧

本机回环传输很快（3.8MB 几秒就完），进度文本一闪而过。想稳定看到「下载中 xx%」：

1. 做种端加 `--limit-kbps 300`（或更小）；
2. 更可靠的是**在 App 设置页把「下载限速」调到 64 KB/s**（保存即生效，作用于 App 会话）——
   3.8MB 的大图会以 ~60s 匀速下完，画廊里能稳定截到「下载中 75%」这类中间态；
   看完记得把限速改回 0。

### 已知限制（与另两套相同）

- 做种端 ↔ 模拟器偶发「连上即掉线」：元数据可能几十秒才到，甚至超时。重试法见下文
  「已知限制与坑」§1；**关键补充**：超时或失败后**重新粘贴磁力链不会重新加种**
  （App 会复用任务列表里的那条，peer 提示已被消费掉），必须先「移除(保留数据/删数据)」
  或 `pm clear` 再粘贴，否则会一直在等一个永远不会连上的 peer。
- `adb shell input text` 打长磁力链会**偶发吞掉末尾字符**（实测丢过 `20`，变成
  `x.pe=10.0.2.2:169`）→ 打完必须回读输入框内容比对，不一致就清空重打。

## 已知限制与坑（我实测踩过的，别重复浪费时间）

0. **⚠️ 端口上「服务的是哪份夹具」必须核对，不然所有症状都像网络 flaky**（2026-09-14 血的教训）：
   多个代理/多轮实验会在同一台机器上留下**多份残留做种端**，端口被"另一份夹具"占着时，磁力链 infohash 与做种内容**对不上**，
   表现是「能加种、但永远拿不到元数据」——和传输 flaky 一模一样。**开测前先做两件事**：
   ```bash
   # ① 列出所有做种端及其夹具，确认没有陌生的占用者
   powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { \$_.CommandLine -like '*seed.py*' } | ForEach-Object { \$_.ProcessId.ToString() + ' :: ' + (\$_.CommandLine -replace '.*testrig/','') }"
   # ② 用宿主机探针核对「该端口服务的 infohash == 你要用的磁力链」——这一步是判据，别跳
   ./.venv/Scripts/python - <<'PY'
   import libtorrent as lt, time, pathlib, warnings; warnings.filterwarnings("ignore")
   m = pathlib.Path("android/tools/testrig/magnet.txt").read_text().strip().replace("10.0.2.2","127.0.0.1")
   p = pathlib.Path(".hermes/leech_probe"); p.mkdir(exist_ok=True)
   ses = lt.session({"listen_interfaces":"0.0.0.0:0","enable_dht":False,"enable_lsd":False})
   atp = lt.parse_magnet_uri(m); atp.save_path = str(p); h = ses.add_torrent(atp); t0=time.time()
   while time.time()-t0 < 20:
       if h.status().has_metadata:
           print("✓ 端口服务的就是这份夹具"); break
       time.sleep(0.3)
   else: print("✗ 拿不到元数据 → 先查端口上到底是谁在做种（别去查网络）")
   PY
   ```
   收工前把不该留的做种端 taskkill 干净。

1. **做种端 ↔ 模拟器的传输会偶发"连上即掉线"**：能拿到元数据（约 10KB 上传），但载荷传输停在十几 KB、做种端显示 `peers=0 rate=0`。表现随机，重试可恢复。
   - 重试方法：**重新加种**（peer 提示在首次连接失败后基本被消费掉）——即 `pm clear`/重开 App 后重新粘贴磁力链，或重启做种端后重开 App；
   - 同一夹具在宿主机 client 之间互传从不出问题，只在"模拟器↔宿主机"这条路上出现，属环境侧。
2. **开关屏一定要用确定性的键码**：`input keyevent 26` 是 **电源键 toggle**，你不知道当前是开还是关，会出现"我以为关屏了其实开着、点击全落空"；用 `224`（WAKEUP）/`223`（SLEEP）并在每次后 `dumpsys power | grep mWakefulness` 确认。
3. **当前 App 还没做持久化**：`am force-stop`、Activity 重建（含反复开关屏）之后**清单会丢**，回到"无任务"态。所以**灭屏类测试必须一次性连贯跑完**（解析→预览→关屏→采样），中途别开关屏。持久化落地后此条应失效。
4. `du -k` 对大文件要数**已分配块**（稀疏文件），`ls -l` 的表面大小会一直等于文件全长，不能用来看进度。
5. 切换夹具必须重启做种端（同一端口只能跑一个），并确认它打印的 `infohash(v1)` 与磁力链一致（用 `info_hashes().v1`，别用 `info_hash()`）。

- 夹具重建后 **infohash 会变**（只要视频文件重新生成），`magnet.txt` 要跟着刷新。
- 改完 `seed.py` 必须重启做种端才生效（后台常驻进程不会自动重载）。
- `.hermes/` 被 gitignore，本靶场不入库；如需长期保留可整体挪到 `android/tools/`。
