# mibox

一个镜像搞定：**小爱音箱播放本地音乐** + **DLNA 渲染器** + **语音问答（关键词转发给外部 AI 接口，回答用 HA 播报）**。

底层与 xiaomusic / MiAir 相同：通过 `miservice` 把一个 HTTP 音频 URL 交给音箱，由音箱自行拉取播放。
区别是本项目把三个能力收敛进**同一个进程**，共享同一份登录态与同一个播放队列。

## 与三个参考项目的关系

| 能力 | 来源 | 说明 |
|---|---|---|
| 本地音乐 + 语音点歌 | xiaomusic | 保留音乐库扫描、模糊匹配、投递三模式；去掉 yt-dlp 与插件系统 |
| DLNA 渲染器 | MiAir | 保留 SSDP/SOAP/缓冲代理；去掉 AirPlay |
| 语音 → AI → HA | 本项目 | 关键词命中即转发给外部 AI 接口，回答用 HA 播报（规则式命令匹配已移除） |
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

未命中音乐指令时，会继续尝试匹配 AI 桥接的关键词（见下）。

## AI 桥接（语音 → 接口 → HA 播报）

关键词命中就把这句话转发给外部 HTTP 接口，拿到回答后用 HA 的
`notify.send_message` 交给音箱播报。**原来的「正则匹配 → 调用 HA 服务」规则引擎已移除。**

### 配置（Web 界面「AI 桥接」页）

| 字段 | 说明 |
|---|---|
| 关键词 | 每行一个或用逗号分隔，命中即转发；句首的关键词会被自动剥掉（可连剥多个，如「问问小爱同学明天冷不冷」→「明天冷不冷」），剥完为空时原样转发。句中出现的关键词不剥，免得把问题本体改坏 |
| 接口地址 | 基址，服务端会请求 `{地址}/v1/chat`，如 `http://192.168.31.145:9901` |
| Bearer Token | 接口的 `Authorization` 令牌 |
| 等待回复上限 | 秒，客户端超时自动 +30s（接口自身 `timeout` 若更大以接口为准） |
| 请求口语短稿 | 带 `hint:"voice"`，让接口出口语稿（pi 会限制在 150 字内）；强烈建议开 |
| 播报实体 | HA 的 `notify.*` 实体，如 `notify.xiaomi_cn_2118828500_oh2_play_text_a_5_3` |
| 转发后提示语 | 命中转发后**立刻**播一句（默认「好的」），留空则不播 |

开关在「配置」页的**模块开关**里：`AI 语音桥接`（转发是否生效）与
`接入 Home Assistant（播报）`（播报需要 HA 地址 + 长期令牌）。

### 完整例子：接 pi HTTP Bridge

```bash
# 1) 拿到 pi 的 token（在跑 pi 的机器上）
python3 -c 'import json;print(json.load(open("/root/.pi/agent/http-bridge.json"))["token"])'
```

2) Web 界面填：接口地址 `http://192.168.31.145:9901`，Token 粘上面的值，
   播报实体 `notify.xiaomi_cn_2118828500_oh2_play_text_a_5_3`，关键词写 `问问`

3) 点「测试匹配」说一句「问问今天有哪些日程」→ 应显示命中并把 `今天有哪些日程` 作为转发内容；
   点「测试接口」可先验证接口本身；点「试播一句」验证 notify 实体。

4) 保存 → 应用并重启服务，然后对小爱说：

```
小爱同学，问问今天有哪些日程
```

音箱先说一声「好的」，接口返回后接着把回答播出来（接口慢时中间是静默的）。
回答里的 Markdown、代码块、表格、链接会被清掉再播；超过 400 字按句读截断。

### 相关 HTTP 接口（自测用）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/ai/status` | 关键词、地址、是否配了 token / 播报实体 |
| `POST` | `/api/ai/match` | `{"query":"问问今天天气","keywords":"问问"}` → 只试关键词匹配 |
| `POST` | `/api/ai/test` | `{"text":"你好","url":"...","token":"..."}` → 真调一次接口，返回 reply |
| `POST` | `/api/ai/speak` | `{"message":"测试播报"}` → 用当前配置播报一句 |

## HA 接管播放控制（暂停 / 继续 / 停止 + 状态）

音箱用**官方米家集成（Xiaomi Home）**接入 HA 后，HA 里会有对应的
`media_player` 实体。打开「配置 → 模块开关 → 用 HA 接管播放控制」，
再到「设备」页给每台音箱挑一个 HA 媒体实体，这台音箱的暂停/继续/停止
与状态读取就交给 HA，不再打小米云。

### 三条通道各自的分工

| 通道 | 走谁 | 说明 |
|---|---|---|
| 投递 URL | 小米 API | `play_by_url` / `play_by_music_url`。HA 实体没有 `PLAY_MEDIA` 能力位，`play_media` 调了不生效 |
| 暂停 / 继续 / 停止 | HA 实体 | `media_pause` / `media_play` |
| 状态读取 | HA 实体 | 读 `GET /api/states/<entity>`，替代每 5 秒一次的 `mina.player_get_status` |
| 音量 | HA 实体 | `volume_set`，**写完回读校验**，对不上就回落小米 API |
| DLNA 状态漂移修正 | HA 实体 | 与播放器共用同一份状态缓存，间隔从 10s 收到 `ha_poll_sec` |

未配置实体的音箱、或关掉开关时，全部照旧走小米 API（零回归）。

### 三个必须知道的实情

1. **「停止」没有对应的 HA 服务。** 官方集成给小爱的 media_player 只有
   `supported_features = 17469`（PLAY|PAUSE|PREV|NEXT|VOLUME_SET|VOLUME_MUTE|
   VOLUME_STEP），**没有 STOP(4096)**，所以 `media_stop` 在 HA 里是空转。
   停止只能用「断流 + `media_pause`」组合实现（音箱本来也不认云端停止指令）。
2. **暂停默认不断流。** 断过流音箱就没法"接着播"，HA 的 `media_play` 也就续不上。
   所以暂停只在「HA 指令确认没生效」时才掐流兜底；继续优先走 HA 原生续播，
   续不上才回落成重新投递整首。DLNA 的「暂停后播放」同理：同一个 URI 会话内
   续播，换过曲（token 变了）才重投。
3. **播外部 URL 时音箱未必上报 `playing-state`。** 那样 HA 会一直显示 `idle`。
   因此代码只在「HA 见过这台在播」之后才采信它的 `idle`（否则队列会被误判打断）。
   同理，`media_player` 不提供播放进度，界面上的进度仍由本地计时负责。

### 音量为什么要回读

HA 的 `volume_level` 是 0~1，界面用的是 0~100，换算方向没问题，但不同型号的
换算基准未必一致（静音态下读出来的值也不可信）。所以 `set_volume` 写完立刻
回读一次：`|回读值 - 目标值| > max(5, 10%)` 就判定不可信，自动回落小米 API，
并在日志里写明"疑似量纲不一致"。这样最坏情况是"没生效"，而不是"被设成静音或爆音"。

### DLNA 这边的两个"轮询"

- `_poll_states`：核对音箱是否还在播、修正 DLNA 传输状态。**它已经是读 HA 状态**
  （没接 HA 时读小米 API），间隔也改成跟着状态源走：HA 2s（`ha_poll_sec`）、
  小米 10s。两处读取共用同一份 TTL 缓存，不会重复打 HA。
- SSDP 的 `alive` 广播（30s）：把自己公告到局域网，属于设备发现，不是状态轮询，
  与 HA 无关，保持原样。

### 相关接口（自测用）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/ha/media_players` | 列出 HA 里所有 media_player 实体（含当前状态） |
| `GET` | `/api/ha/state?entity_id=media_player.x` | 读单个实体，并给出映射后的播放状态 |
| `POST` | `/api/speakers` | `{"did":"...","ha_entity":"media_player..."}` 保存每台的实体 |

验证「播本地音乐时 HA 的 state 会不会变成 playing」，投一首歌后连着查：
`/api/ha/state?entity_id=media_player.xiaomi_cn_xxx_oh2`。

## 目录结构

```
app.py                  FastAPI 入口 + 生命周期编排
core/    auth  config  speaker  player  library  media  buffer  const
voice/   poller  dispatcher  music_cmds
dlna/    ssdp  renderer  server  templates
ha/      client  media  bridge
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
| `MIBOX_HA_CONTROL` / `MIBOX_HA_POLL_SEC` | 用 HA 实体接管播放控制 / HA 状态读取间隔（默认 2 秒） |
| `MIBOX_AI_ENABLED` | 开启 AI 桥接 |
| `MIBOX_AI_KEYWORDS` | 触发关键词，逗号分隔 |
| `MIBOX_AI_URL` / `MIBOX_AI_TOKEN` | 外部接口基址与 Bearer 令牌 |
| `MIBOX_AI_NOTIFY_ENTITY` | 播报用的 `notify.*` 实体 |
| `MIBOX_AI_TIMEOUT` / `MIBOX_AI_VOICE_HINT` | 等待上限（秒）/ 请求口语短稿 |
| `MIBOX_AI_ACK` | 转发后立刻播的提示语，默认「好的」，设为空串则不播 |
| `MIBOX_PULL_ASK_SEC` | 语音轮询间隔（秒，支持 0.5 步进），默认 1.5 |
| `MIBOX_VERBOSE` | 调试日志 |

## 已知限制

- 仅 `linux/amd64`。
- 一期不做：AirPlay、yt-dlp 下载、MQTT。
- 播报统一走一个 `notify.*` 实体（多音箱场景下回答都从这台出来）；需要按音箱分发时再说。
- AI 接口故障/超时时会播一句「喵喵……它好像睡着了」，不会静默。
- 语音指令依赖小米对话记录接口，接口变动会导致语音失效（播放与 DLNA 不受影响）。
