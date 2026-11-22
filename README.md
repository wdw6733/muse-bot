# 3 号机器人：NAS + Docker + ESXi 管理（v2.5.0：内存 TOP5 + 操作授权确认 + TrueNAS 系统服务）

> v2.5.0（2026-11-22，未部署，先审后装）：打字就能操作，不再只靠按钮菜单。
> v2.4.0（2026-11-21，未部署）：精简菜单 + 美化状态/TOP + 容器更新可忽略/升级。
> v2.3.1（2026-11-19）：容器/VM 列表按钮文字等宽对齐，同一行 🟢/🔴 圆点横向对齐。

## v2.5.0 新功能

- **内存 TOP5**：发“内存 TOP5”或 `/topmem`，按内存占用排前 5 名（只读，立即回，不用确认）。
- **打字操作 + 授权确认**：发“重启 plex”“停止 cifs”“启动 ssh”，机器人先回“确定要重启 XX 吗”，你点 ✅ 才执行，❌ 取消；5 分钟过期，只认主人。目标先对实时列表校验：容器对 Docker 列表，系统服务对 TrueNAS 服务列表，找不到的直接说找不到，不猜、不执行。
- **TrueNAS 系统服务**：发“系统服务”或 `/services` 看实时列表。走 NAS 主机 SSH 的 `midclt call service.*`（优先），或 REST API（备用，TrueNAS 26 已移除 REST，26 及以后必须用 SSH）。`middlewared`、`docker` 等关键服务永不重启。
- **没有任意命令执行**：只认固定几种操作（查状态/内存/进程/服务列表、重启/启停指定容器或服务），注入字符（`;`、`$()` 等）直接拒绝。
- 明确敲的 `/docker restart <容器>`、`/esxi on|off|kill <VM>` 和按钮确认流程不变：命令本身就是你明确下的，仍直接执行。

部署在 NAS（ESXi 上的 VM）里的 Docker 容器。经 OpenBox SOCKS5 访问
Telegram Bot API，长轮询收消息，不需要 webhook，不需要开 inbound 端口，
不用动 Lucky 反代。

- 快捷按钮：发 `/start` 打开主菜单，点按钮直接查 NAS、管容器（本机秒回）。
  原来的命令也一样能用。
- 主动告警：容器意外停了、磁盘/负载/内存超阈值，机器人主动在 TG 里提醒，
  可在告警页开关（默认开）。
- 自我更新：主菜单点“检查更新”，从 GitHub 仓库拉新版 bot.py，验证后安装
  重启；也可以直接把新版 bot.py 文件发给机器人。旧版自动备份。
- 镜像更新检测：每 6 小时自动检查全部容器的镜像，有新镜像就通知，
  点按钮确认后才更新；同一个新版本只通知一次，没更新就不打扰。
- CPU、内存、磁盘用量都用 🟥（已用）/🟩（未用）彩条显示；容器管理改成 3 列网格，每页 9 个。
- Docker：直接读 `/var/run/docker.sock` 调 Engine API，不依赖 docker CLI。
- NAS 主机状态：读只读挂载的 `/host/proc`、`/host/sys` 和 `df /host`；
  磁盘默认看 `/mnt/PT` 和 `/mnt/Strongge`（compose 里已显式挂载，
  `.env` 的 `HOST_DATA_PATHS` 填了就按填的来）。
- ESXi：SSH 执行 `vim-cmd` / `esxcli`。
- 只认主人一个人；没有任意命令执行功能。危险操作要点两次确认。

## 准备工作

1. 在 Telegram 找 @BotFather 建 3 号机器人，拿到 token。token 只写进
   `.env`，不要发给任何人、不要提交到仓库、不要提交到 Muse。
2. ESXi 开 SSH：浏览器打开 ESXi Host Client → Manage → Services →
   TSM-SSH → Start；Policy 选 Start and stop with host（跟随主机启动）。
   记下 ESXi 管理 IP。
3. 确认 OpenBox 的 TG 入口（192.168.124.250:57080）在 NAS 上能连通。
   如果入口设了账号密码，`.env` 里的 `SOCKS_PROXY` 要带上账号密码。
4. （可选）想用“重启 NAS”按钮：在 `.env` 里填 `NAS_VM_NAME`（NAS 这个
   VM 在 ESXi 里的名字）或 `NAS_VM_ID`。留空则该按钮会提示未配置。
5. （可选，v2.5.0）想用“系统服务”：二选一——
   - **SSH（推荐，TrueNAS 26 也能用）**：TrueNAS 网页 → System Settings → Services → SSH → 开启（默认 22 端口）。
     密钥方式：把私钥放 `./truenas_key`（不要提交），`.env` 里填 `TRUENAS_SSH_KEY_FILE=/keys/truenas_key`；
     密码方式：`.env` 里填 `TRUENAS_SSH_PASSWORD`（不推荐，密钥优先）。
   - **API（备用，TrueNAS 26 已移除 REST）**：TrueNAS 网页 → Credentials → API Keys → 新建，
     `.env` 里填 `TRUENAS_API_URL=https://192.168.124.251/api/v2.0` 和 `TRUENAS_API_KEY`。
   都不填时，“系统服务”会提示未配置，容器操作不受影响。
5. （可选）想用“检查更新”从 GitHub 拉新版：在 GitHub 建一个仓库（公开
   或私有都行），根目录放 `bot.py`；`.env` 里填 `UPDATE_REPO`
   （例如 `wdw6733/tg-bot3`），私有仓库再填只读的 `UPDATE_TOKEN`。
   不填也能更新：直接把新版 `bot.py` 文件发给机器人。

## 部署

```sh
# 1. 把这份文件夹传到 NAS，例如 /opt/tg-bot3/
# 2. 填配置
cp .env.example .env
# 编辑 .env：BOT_TOKEN、SOCKS_PROXY、ESXI_HOST/ESXI_USER/ESXI_PASSWORD、
# HOST_DATA_PATHS（可选）、NAS_VM_NAME（可选）、UPDATE_REPO（可选）
# 3. 构建并启动
docker compose up -d --build
# 4. 看日志，出现“启动：@xxx”即成功
docker compose logs -f
```

TG 里给机器人发 `/start` 测试。

升级（已部署过 v2/v2.1）：把新版的 `bot.py`、`docker-compose.yml`、
`.env.example`、`README.md` 覆盖过去，**不要覆盖你自己的 `.env`**，
然后 `docker compose up -d --build`。注意新版 compose 多了一个
`./bot.py:/app/bot.py` 挂载（可写，自我更新靠它把新代码写回宿主机），
不要改成 `:ro`。如需“重启 NAS”，再按上面第 4 步往 `.env` 里补
`NAS_VM_NAME` 后重新 `up -d` 一次。

## 快捷按钮（/start 打开）

```text
系统状态 / 公网 IP（直连 IPv4/IPv6 + 代理出口 IPv4）
容器列表 / 容器资源（CPU、内存，按 CPU 排序）
磁盘空间 / 温度（VM 内通常读不到传感器，会直接提示）
进程 TOP / 网络流量（1 秒采样算速率）
容器管理（3 列网格，每页 9 个，点容器 → 启动/停止/重启/看日志/搜日志/更新容器，分页显示）
ESXi VM（点 VM → 开机/优雅关机/强制断电）
重启 NAS（需配 NAS_VM_NAME，两次确认，走 ESXi 重启 NAS 这个 VM）
检查更新 / 告警（检查更新：从 GitHub 拉新版；告警：看状态、开关）
检查容器更新（检查全部容器的镜像更新，有新镜像就列出来让你确认）
给 Muse 下指令（跳到 2 号机器人，3 号本身不接 Muse）
```

每个详情页都有“刷新”和“主菜单”按钮。

## 命令

```text
/status NAS 状态 + 容器 + VM 汇总
/docker 列出容器
/docker logs <容器> [行数] [关键词] 看/搜日志（默认 50 行，最多 200 行）
/docker start|stop|restart <容器>
/docker prune 清理悬空镜像（只清 dangling，不删在用的）
/docker update <容器> 拉新镜像并重建（要点确认）
/esxi 列出 VM 和电源状态
/esxi on|off|kill <VM 名或 ID> 开机/优雅关机/强制断电
/disk 磁盘用量
/update 检查自我更新
/check_updates 检查全部容器镜像更新
/alerts 告警状态（/alerts on|off 开关）
/menu 打开快捷按钮菜单
/help 帮助
```

容器和 VM 名支持唯一前缀/子串匹配，匹配到多个时会报错让你写全名。
直接打字说“系统状态”“磁盘空间”等，只读查询会直接在本机执行；说到
重启、停止、启动等操作时，机器人会让你点按钮确认，不会直接猜着执行，
也不会把服务器维护推给 2 号。只有生图、生视频、查资料这些才去 2 号。
容器详情页能看到创建时间、重启策略、网络、挂载，还能启停、重启、
看日志、搜日志、更新容器。

## 主动告警

每 5 分钟检查一次（`ALERT_INTERVAL_SEC` 可改）：

- 容器：上次检查还在跑、这次停了，且不是本机器人停的，就提醒一次；
  机器人自己停的 15 分钟内不提醒；容器恢复后再停会再提醒。
- 磁盘：用量超过 `ALERT_DISK_PCT`（默认 90%）提醒一次，降到阈值以下
  5% 后重新布防。
- 负载：1 分钟负载超过 线程数 × `ALERT_LOAD_MULT`（默认 2.0）提醒，
  同一问题 `ALERT_COOLDOWN_SEC`（默认 3600 秒）内只提醒一次。
- 内存：占用超过 `ALERT_MEM_PCT`（默认 92%）同上。

开关在告警页（`/alerts`），存在 `data/alerts.json` 里，重启不丢。
告警只读检查，不会自动重启容器、删东西。

## 自我更新

两种来源，同一个安装流程：

1. 检查更新：`.env` 配好 `UPDATE_REPO` 后，点按钮或发 `/update`，
   机器人从 `https://raw.githubusercontent.com/<仓库>/<分支>/bot.py`
   拉新版（先直连，失败走 SOCKS 代理）。
2. 发文件：直接把新版 `bot.py` 发给机器人（TG 文件）。

安装前必做：文件大小检查、找 `BOT_VERSION`、语法检查、子进程冒烟
导入（检查版本与关键函数）。任何一项失败都拒绝安装，旧版不动。
安装时：旧版备份到 `data/bot.py.bak`，新版原地写回挂载的 `bot.py`，
然后重启自己；重启成功后会发“更新完成”消息，版本没变会发警告。
限制：`requirements.txt` 变了必须重建镜像，自我更新会拒绝并提示手动
部署；compose、Dockerfile、`.env` 的变化也要手动部署。
回滚：`cp data/bot.py.bak bot.py && docker compose restart`。
信任说明：只认主人发的确认，只从你自己配的仓库拉代码；不要把来路
不明的 bot.py 发给机器人。

## 容器更新（更新容器按钮）

流程：拉取新镜像 → 镜像已是最新就不重建 → 按 inspect 的配置创建
临时名新容器（环境变量、挂载、端口、网络、重启策略原样保留，匿名卷
显式绑回）→ 停旧容器 → 起新容器并观察最多 30 秒 → 新容器跑起来后
删旧容器、改回原名；新容器起不来就删新容器、起旧容器回滚。
只适合常驻服务容器；compose 管理的栈建议用 `docker compose` 更新；
3 号自己不能这样更新，请用“检查更新”。

## 镜像更新检测（自动检查全部容器）

启动 60 秒后先查一次，之后每 6 小时自动查一次
（`IMAGE_CHECK_INTERVAL_SEC` 可改，`IMAGE_CHECK_ENABLED=0` 关闭自动
检查，手动按钮/命令仍可用）。检查方法是拉取每个容器的镜像，比对
镜像 Id：没变化就不下载数据，只多一次网络请求。

有新镜像才发通知，通知里每个容器一个“更新”按钮，点按钮后还要再
确认一次才会重建，不点就不动。同一个新版本只通知一次；容器更新完
或删掉后记录自动清除。3 号自己的容器不参与（走 GitHub 自我更新），
digest 固定的镜像跳过。手动检查：主菜单“检查容器更新”按钮、
`/check_updates` 命令，或直接打字说“检查更新”。

## 排障

- 日志里 getMe 失败：检查 `SOCKS_PROXY` 和 `BOT_TOKEN`。在 NAS 上先测：
  `curl -x socks5h://192.168.124.250:57080 https://api.telegram.org/bot<token>/getMe`
- `/docker` 报错：确认 `/var/run/docker.sock` 挂载了、容器是 root 运行。
- `/esxi` 报错：ESXi SSH 开了没、IP/密码对不对；在 NAS 上
  `ssh root@<esxi-ip>` 先试试能不能登录。
- 磁盘只显示根分区、没有 /mnt/PT 或 /mnt/Strongge：看启动日志里的
  “磁盘路径”和“磁盘路径不存在”提示；确认宿主机上这两个目录真实存在，
  且 `docker-compose.yml` 里有对应的只读挂载，改完重新 `up -d --build`。
- 温度显示读不到：正常。NAS 是 ESXi 上的 VM，传感器一般不直通进 VM，
  温度请看 ESXi 主机或 TrueNAS 界面。
- “重启 NAS”提示未配置：`.env` 里填 `NAS_VM_NAME` 后重新 `up -d`。
- 告警没来：看告警页开关和上次检查时间；容器基线在启动时建立，
  启动前就停着的容器不会告警。
- “检查更新”说找不到自己：`SELF_CONTAINER_NAME` 要和 compose 里的
  `container_name` 一致（默认 `tg-bot3`）。
- 更新后版本没变：`bot.py` 挂载没生效，检查 compose 里有没有
  `./bot.py:/app/bot.py` 这一行（不要 `:ro`）。
- 更新后机器人不回消息：看 `docker compose logs`；用
  `cp data/bot.py.bak bot.py && docker compose restart` 回滚。
- 容器更新失败回滚了：新镜像可能有问题，先看该容器的日志。
- 按钮点了没反应：看 `docker compose logs`；旧消息的按钮在容器重启后
  可能失效，重新发 `/start` 打开新菜单。
- 机器人不回消息：看 `docker compose logs`；确认 `OWNER_CHAT_ID` 是你
  本人的（给 @userinfobot 发消息能查到自己的 id）。

## 安全说明

- 白名单：只有 `OWNER_CHAT_ID` 的消息和按钮点击会被处理，其他人的一律忽略。
- 没有 `/exec` 之类的任意命令执行；ESXi 只能开关 VM、看列表。
- 容器挂载了 `/var/run/docker.sock`（管理容器必需）和只读的 `/`、
  `/proc`、`/sys`（读主机状态）。容器只在内网运行，不要把它暴露到公网。
- `bot.py` 挂载是可写的（自我更新必需），只有容器内的机器人进程会写
  它；不要把项目目录暴露给其他不可信的容器。
- 自我更新只装验证通过的文件，旧版自动备份；更新来源只认主人配置
  的仓库和主人发的文件。
- 容器更新会原样保留环境变量（重建必需），但详情页和日志里永远不
  显示环境变量。
- ESXi 密码只存在 `.env` 里；也可以改用密钥（`ESXI_KEY_FILE`，把私钥
  挂载到 `/data` 并填路径）。
- SSH 接受任意 ESXi 主机密钥（内网环境简化处理）；如在意可改为校验
  known_hosts。
