# ts3-bark

![Docker Pulls](https://img.shields.io/docker/pulls/sdparklab/ts3-bark)
![Docker Image Size](https://img.shields.io/docker/image-size/sdparklab/ts3-bark)
![GitHub stars](https://img.shields.io/github/stars/sdparklab/ts3-bark?style=flat)
![License](https://img.shields.io/badge/license-MIT-blue)

把 **TeamSpeak 3 的进服 / 离服事件**实时推送到 **Bark**（iPhone 推送）。自建 TS3、想第一时间知道谁进来了，装它就行。

- 📦 镜像地址：<https://hub.docker.com/r/sdparklab/ts3-bark> → `docker pull sdparklab/ts3-bark`
- 📖 源码仓库：<https://github.com/sdparklab/ts3-bark>

<!-- 提示：把你的 Bark 通知截图存成 screenshot.png 放到仓库根目录，下面这行才会正常显示 -->
![通知效果](screenshot.png)

> 单文件、纯 Python 标准库、**零第三方依赖**，镜像仅约 55MB。全部通过环境变量配置，不需要改代码。

---

## 为什么会有这个东西

很多 TS3 播报机器人都有个通病：**冷的时候会"睡着"** —— 服务器半天没人，它就不报了；你在群里问一句"现在有人吗"，它答完又能正常播报一会儿。

原因是 TS3 的 ServerQuery 事件订阅（`servernotifyregister`）是**连接级**的：连接一旦断开或空闲超时，订阅就静默失效，之后所有事件全部丢失。本项目的全部设计都在解决这一件事。

## 特性

- **四层保障让事件不丢**：常驻长连接 + 定时保活 + 断线重连并重新订阅 + 定时对账兜底
- **断线会主动告诉你**：连不上 TS3 时推一条「⛔ 离线」，恢复后推一条「✅ 恢复」（状态变化才推，不刷屏）
- **重连带指数退避**（5→10→20→40→60 秒），不会疯狂冲击服务端触发 Query 封禁
- **昵称冲突自愈**：撞上 `error id=513` 会自动换一个带随机后缀的昵称
- **零依赖、单文件**，镜像小、启动快，支持 `linux/amd64` 与 `linux/arm64`
- 可选能力：只播报指定的人（`WATCH_UIDS`）、静音指定频道（`MUTE_CHANNELS`）、不播报离开（`REPORT_LEAVE=0`）

## 它是怎么工作的

```
TeamSpeak3 服务器 (UDP 9987)
      │  ServerQuery 事件流 (TCP 10011)
      ▼
  本容器（长连接常驻）
      │  HTTP POST
      ▼
  Bark（自建 bark-server 或官方 api.day.app）→ iPhone
```

四层保障的具体含义：

1. **保活**：每 `IDLE_SEC` 秒（默认 90）发一次 `version` 命令，避免 Query 空闲被服务端踢掉
2. **重连 + 重新订阅**：断线后自动 `login → use → servernotifyregister` 重建订阅（这一步是很多机器人漏掉的关键）
3. **对账兜底**：每 90 秒拉一次 `clientlist` 和本地状态比对，万一事件真漏了，最多 90 秒后补报
4. **退避**：连续失败时逐渐拉长重试间隔，避免把服务端惹毛

## 快速开始

```bash
docker run -d --name ts3-bark --restart=unless-stopped \
  -e TS3_HOST=192.168.1.10 \
  -e TS3_USER=你的ServerQuery账号 \
  -e TS3_PASS=你的密码 \
  -e BARK_SERVER=https://api.day.app \
  -e BARK_KEY=你的Bark设备key \
  sdparklab/ts3-bark:latest
```

看日志确认：

```bash
docker logs -f ts3-bark
```

正常时你会看到：

```
基线建立：当前在线 0 人，频道 5 个
事件订阅成功（昵称 BarkNotify），等待进服事件…
进入: Joko -> 大厅
[bark] 200 {"code":200,"message":"success","timestamp":...}
```

### 先干跑验证（推荐）

`--probe` 模式**只打印、不推送**，用来确认权限和事件都正常，不会打扰手机：

```bash
docker run --rm -it \
  -e TS3_HOST=192.168.1.10 \
  -e TS3_USER=你的ServerQuery账号 -e TS3_PASS=你的密码 \
  sdparklab/ts3-bark:latest --probe
```

### 诊断模式

看服务器上都有谁（包含 ServerQuery / 僵尸连接）：

```bash
docker run --rm -it \
  -e TS3_HOST=192.168.1.10 \
  -e TS3_USER=你的ServerQuery账号 -e TS3_PASS=你的密码 \
  sdparklab/ts3-bark:latest --who
```

### docker compose

```yaml
services:
  ts3-bark:
    image: sdparklab/ts3-bark:latest
    container_name: ts3-bark
    restart: unless-stopped
    environment:
      TZ: Asia/Shanghai
      TS3_HOST: 192.168.1.10
      TS3_USER: 你的ServerQuery账号
      TS3_PASS: 你的密码
      BARK_SERVER: https://api.day.app
      BARK_KEY: 你的Bark设备key
```

### Unraid

Docker 页签 → **ADD CONTAINER**：

| 字段 | 填什么 |
|---|---|
| Name | `ts3-bark` |
| Repository | `sdparklab/ts3-bark:latest` |
| Network Type | `Bridge` |
| Autostart | `On` |

然后点 **Add another Path, Port, Variable…** → 类型选 **Variable**，把上表里的环境变量一条条加上去（`TZ` / `TS3_HOST` / `TS3_USER` / `TS3_PASS` / `BARK_SERVER` / `BARK_KEY`）。不需要加任何端口或路径。

## 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `TS3_HOST` | *(必填)* | TS3 主机地址（Query 端口映射到的那台机器） |
| `TS3_QPORT` | `10011` | ServerQuery 端口（RAW） |
| `TS3_PORT` | `9987` | 语音端口，仅用于 `use` 选服 |
| `TS3_USER` | *(必填)* | Query 账号 |
| `TS3_PASS` | *(必填)* | Query 密码 |
| `BOT_NICK` | `BarkNotify` | 机器人昵称；撞名（`error id=513`）会自动加随机后缀 |
| `BARK_SERVER` | *(必填)* | Bark 服务地址（`https://api.day.app` 或你的自建地址） |
| `BARK_KEY` | *(必填)* | Bark 设备 key（每台设备一个） |
| `BARK_GROUP` | `TeamSpeak` | 通知分组 |
| `BARK_SOUND` | `calypso` | 通知铃声 |
| `TS3_LINK` | *(空)* | 通知里的跳转链接（如 `ts3server://host?port=9987`）；留空则不带链接 |
| `REPORT_LEAVE` | `1` | 是否播报「离开」 |
| `WATCH_UIDS` | *(空)* | 只播报这些 UID，逗号分隔；留空 = 所有人 |
| `MUTE_CHANNELS` | *(空)* | 不播报的频道 cid，逗号分隔（如挂机频道） |
| `IDLE_SEC` | `90` | 保活 + 对账间隔（秒） |
| `DEDUP_SEC` | `60` | 同一人同动作去重窗口（秒），防掉线重连刷屏 |
| `ALERT_ON_ERROR` | `1` | 连不上 TS3 时推 Bark 告警 |
| `ALERT_AFTER_FAILS` | `3` | 连续失败几次才告警 |

> **全部通过环境变量配置，不需要改代码** —— 环境变量优先级永远高于脚本里的默认值。
> 有必填项没传时，启动日志会直接打印一条可以照抄的命令，而不是抛一堆看不懂的异常。
> `--probe` / `--who` 两个诊断模式只需要 TS3 的三个参数，不需要 Bark 的。

## 推送样式

| 场景 | 标题 | 正文 |
|---|---|---|
| 进服 | 🎧 TeamSpeak 进入 | `Joko 进入「大厅」` |
| 离服 | 🚪 TeamSpeak 离开 | `Joko 离开「大厅」` |
| 连不上 | ⛔ TeamSpeak 离线 | `连续 3 次连接失败` |
| 恢复 | ✅ TeamSpeak 恢复 | `连接已恢复，断开约 87 秒` |

## 准备 TS3 的 Query 账号

1. 用 TS3 客户端以管理员身份连上服务器
2. 「工具 → ServerQuery 登录」生成一组账号密码（或让管理员执行 `serverquerylogin client_name=你的名字`）
3. 把账号密码填给 `TS3_USER` / `TS3_PASS`

> ⚠️ **注意**：TS3 会为每个 ServerQuery 登录账号在服务端保存一个默认昵称。**如果同一个账号被两个程序同时使用**（比如你还装了别的 TS3 机器人），后连上来的那个设置昵称时会报 `error id=513 nickname is already in use`。规避办法：给每个程序设不同的 `BOT_NICK`，或者各自用独立账号。
>
> 另外，TS3 默认**不会**把 Query 连接写进 `ts3server_*.log`，所以别指望从服务端日志里排查谁连接过。

## 常见问题

**订阅事件时报 `error id=2568` / 权限不足**
该 Query 账号没有订阅服务器级事件的权限。把它加进一个有权限的 Server Group，或改用管理员账号。

**收不到推送**
先用 `--probe` 确认事件是否到齐；若 `--probe` 正常但没有 `[bark] 200`，那是 Bark 那边的问题（地址不通、key 不对）：

```bash
curl -X POST 'http://你的Bark地址/你的key' -H 'Content-Type: application/json' \
  -d '{"title":"测试","body":"hello","group":"TeamSpeak"}'
```

**日志里出现 `↳ 已过滤`**
说明收到的是 ServerQuery / 机器人自身的连接事件（`client_type=1`），已被正确跳过，属正常现象。

**日志里出现 `连接异常，X 秒后重连`**
偶尔一次是网络抖动，会自动恢复；反复出现请检查 TS3 是否在重启、是否有别的程序在用同一个 Query 账号。

## 自己构建 / 参与开发

```bash
git clone https://github.com/sdparklab/ts3-bark.git
cd ts3-bark
docker build -t ts3-bark:dev .
docker run --rm -it \
  -e TS3_HOST=192.168.1.10 \
  -e TS3_USER=你的账号 -e TS3_PASS=你的密码 \
  ts3-bark:dev --probe
```

- 主程序只有一个文件：`ts3_bark.py`（纯标准库）
- 打 tag 后 GitHub Actions 会自动构建 `linux/amd64` + `linux/arm64` 并推送到 `ghcr.io`

## License

MIT
