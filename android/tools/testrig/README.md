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
./.venv/Scripts/python .hermes/android-test/mkfixture.py

# 2) 自证 seam（必须先绿，再谈 App 的问题）
./.venv/Scripts/python .hermes/android-test/selftest.py      # 期望：结论：seam 可用 ✓

# 3) 起做种端（后台常驻；每次改完夹具要重启）
./.venv/Scripts/python .hermes/android-test/seed.py          # 期望：[seed] ... state=seeding

# 4) 从模拟器内验证端口可达（应输出 nc exit=0）
"$ANDROID_HOME/platform-tools/adb.exe" shell 'nc -w 4 10.0.2.2 16881 </dev/null; echo nc exit=$?'

# 5) 在模拟器 App 里粘贴 magnet.txt 的内容
```

## 四个坑（都实测踩过，别再踩）

1. **`torrent_info.info_hash()` 不是 v1 哈希**。libtorrent 2.1 里它对混合种子返回 **v2 截断哈希**，
   磁力链的 `btih` 用的是 **`info_hashes().v1`**。拿 `info_hash()` 与磁力链对比会误判"hash 不匹配"。
2. **做种端的 `save_path` 必须是种子内容根目录的父目录**。种子里文件路径是 `<种子名>/xxx`，
   种子名就是 `payload`，所以 `save_path` 要用 `.hermes/android-test/`，不是 `.../payload/`。
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

## 注意

- 夹具重建后 **infohash 会变**（只要视频文件重新生成），`magnet.txt` 要跟着刷新。
- 改完 `seed.py` 必须重启做种端才生效（后台常驻进程不会自动重载）。
- `.hermes/` 被 gitignore，本靶场不入库；如需长期保留可整体挪到 `android/tools/`。
