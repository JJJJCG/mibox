# mibox

一个镜像搞定：**小爱音箱播放本地音乐** + **DLNA 渲染器** + **接入 Home Assistant**。

底层与 xiaomusic / MiAir 相同：通过 `miservice` 把一个 HTTP 音频 URL 交给音箱，由音箱自行拉取播放。
区别是本项目把三个能力收敛进**同一个进程**，共享同一份登录态与同一个播放队列。

## 与三个参考项目的关系

| 能力 | 来源 | 说明 |
|---|---|---|
| 本地音乐 + 语音点歌 | xiaomusic | 保留音乐库扫描、模糊匹配、投递三模式；去掉 yt-dlp 与插件系统 |
| DLNA 渲染器 | MiAir | 保留 SSDP/SOAP/缓冲代理；去掉 AirPlay |
| 语音 → HA | xiaoai-ha-bridge | 保留正则规则引擎；去掉 AI 兜底 |
| 统一登录 | 本项目 | 单点 token，避免三个服务互踢 |
| 统一队列 | 本项目 | 语音点歌与 DLNA 推送进同一队列，互不打断 |

## 快速开始

### 方式一：用预构建镜像（推荐）

每次推送到 `main` 后，GitHub Actions 会自动构建并发布到 GHCR：

```bash
docker pull ghcr.io/jjjjcg/mibox:latest
```

```bash
mkdir -p conf music
# 编辑 docker-compose.yml，确认 MIBOX_HOSTNAME 是宿主机局域网 IP
docker compose up -d
```

可用标签：`latest`（main 分支）、`main`。

### 方式二：本地构建

把 `docker-compose.yml` 里的 `image:` 注释掉、打开 `build: .`，然后：

```bash
docker compose up -d --build
```

打开 `http://192.168.31.145:8080`：

1. 「配置」页点 **米家 App 扫码登录**，用米家 App 扫二维码（推荐，绕开短信验证）
2. 「设备」页勾选要接入的音箱，点保存 —— 服务自动重启生效

也可以手动填账号密码，或粘贴 cookie（`userId=xxx;passToken=xxx`）。

## 端口

| 端口 | 协议 | 用途 |
|---|---|---|
| 8080 | TCP | Web 界面、REST API、音乐文件、转码缓存 |
| 8200 | TCP | DLNA 设备描述 / SOAP 控制 |
| 1900 | UDP | SSDP 组播发现 |

## 注意事项

1. **必须 host 网络**。SSDP 要绑 UDP 1900 收组播，bridge 模式下 DLNA 发现会完全失效。
2. **`MIBOX_HOSTNAME` 必须正确**。音箱是自己去拉 URL 的，这个地址写错就会播不出声。
3. **部分型号不支持无损**：`L05B / L05C / LX06 / L16A` 遇到 flac 等格式会自动用 ffmpeg 转 mp3，
   转码结果缓存在 `conf/cache`。
4. **投递模式**按型号自动选择（`play_by_url` 或 `play_by_music_url`），可在配置里逐台覆盖。
5. **优先用扫码登录**。它走米家（`sid=mijia`）拿到 `userId` + `passToken`，自动填入 Cookie，
   再用 passToken 换取 `micoapi` 凭据——与 xiaomusic 的链路一致，可绕开账号密码常见的短信验证。
   账号密码与手动粘贴 cookie 仍然保留作为兜底。

## 语音指令（内置，正则匹配）

```
播放歌曲周杰伦晴天    搜索本地音乐并播放
播放歌单流行          按音乐目录下的子目录播放
播放收藏              播放收藏列表
下一首 / 上一曲       切换
暂停 / 继续播放 / 停止播放
单曲循环 / 全部循环 / 随机播放 / 顺序播放
音量50                设置音量
加入收藏 / 取消收藏
```

未命中音乐指令时，会继续尝试匹配 HA 规则。

## HA 规则

纯正则，无 AI 兜底。`pattern` 中用 `(\d+)` 捕获数字，在 `data` 里用 `{0}` 引用：

```json
[
  {
    "pattern": "(打开|开)(客厅灯)",
    "domain": "light",
    "service": "turn_on",
    "entity_id": "light.living_room",
    "reply": "客厅灯已打开"
  },
  {
    "pattern": "客厅灯调到(\\d+)[%％]",
    "domain": "light",
    "service": "turn_on",
    "entity_id": "light.living_room",
    "data": { "brightness_pct": "{0}" }
  },
  {
    "pattern": "客厅空调(\\d+)分钟后关闭",
    "domain": "climate",
    "service": "turn_off",
    "entity_id": "climate.living_room",
    "delay_minutes": 30
  }
]
```

`delay_minutes` 为固定分钟数；想用语音里的数字，可复制多条规则或直接在 `delay_minutes` 写死常用值。

## 目录结构

```
app.py                  FastAPI 入口 + 生命周期编排
core/    auth  config  speaker  player  library  media  buffer  const
voice/   poller  dispatcher  music_cmds
dlna/    ssdp  renderer  server  templates
ha/      client  rules
web/     index.html（零构建单页）
```

## 环境变量

所有 `MIBOX_*` 环境变量均可在 Web 界面配置（界面配置优先持久化在 `conf/config.json`）。

| 变量 | 说明 |
|---|---|
| `MIBOX_HOSTNAME` | 宿主机局域网 IP（必填） |
| `MIBOX_ACCOUNT` / `MIBOX_PASSWORD` | 小米账号密码 |
| `MIBOX_COOKIE` | `userId=xxx;passToken=xxx`，推荐 |
| `MIBOX_MI_DID` | 音箱 DID，逗号分隔多个 |
| `MIBOX_WEB_PORT` / `MIBOX_DLNA_PORT` | 端口 |
| `MIBOX_ENABLE_DLNA` / `MIBOX_ENABLE_VOICE` / `MIBOX_ENABLE_HA` | 模块开关 |
| `MIBOX_HA_URL` / `MIBOX_HA_TOKEN` | Home Assistant |
| `MIBOX_PULL_ASK_SEC` | 语音轮询间隔（秒，支持 0.5 步进），默认 1.5 |
| `MIBOX_VERBOSE` | 调试日志 |

## 已知限制

- 仅 `linux/amd64`。
- 一期不做：AirPlay、yt-dlp 下载、TTS 播报、MQTT、HA media_player 实体。
- 语音指令依赖小米对话记录接口，接口变动会导致语音失效（播放与 DLNA 不受影响）。
