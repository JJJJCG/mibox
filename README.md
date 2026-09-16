# mibox

一个镜像搞定：**小爱音箱播放本地音乐** + **DLNA 渲染器** + **语音问答（关键词转发给外部 AI 接口，回答用 HA 播报）**。

底层与 xiaomusic / MiAir 相同：通过 `miservice` 把一个 HTTP 音频 URL 交给音箱，由音箱自行拉取播放。
区别是本项目把三个能力收敛进**同一个进程**，共享同一份登录态与同一个播放队列。

界面分两个页面：

| 页面 | 路径 | 内容 |
|---|---|---|
| 播放端 | `/` | 播放控制、队列、音乐库（所有歌曲 / 文件夹 / 歌手 / 歌单） |
| 后台 | `/admin` | 设备、配置、AI 桥接（需要口令） |

## 后台（/admin）

设备接入、全部配置项、AI 桥接设置都收在后台页里，`/` 只留日常要用的播放与音乐库。

进入后台需要输入口令，默认 **`123456`**，改的话直接改 `core/const.py` 里的
`ADMIN_PASSWORD`。

> ⚠️ **这只是一个形式，不是真实的访问控制。** 口令明文写在代码里，登录成功后
> 只种一个固定值的 cookie（`mibox_admin=ok`），而且**底下那些配置接口本身没有
> 任何鉴权**——不登录直接调 `/api/config` 一样能改配置。它是为了「日常用的界面
> 干净一点、别误触」而做的门面，别把它当安全边界。真要暴露到公网请自己在前面
> 加一层反代鉴权。

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

## 本地播放（浏览器）

设备下拉里除了真实音箱，还有一项「**本机播放（浏览器）**」，它是**默认输出通道**：

- 在浏览器里点播时，声音从**打开这个页面的设备**出来（手机、电脑都行）；
- 对小爱说话时，声音照旧从**小爱音箱**出来——语音链路只认真实音箱，
  不会串到浏览器上；
- 本机播放不依赖小米账号，没配账号也照样能听歌。

做法是服务端管队列、浏览器负责出声：播放地址是站内相对路径 `/music/xxx`
（不管你用局域网 IP、localhost 还是反代域名打开页面都能播），播完由浏览器
回报 `ended`，服务端再推进队列。因此暂停、拖动进度、缓冲卡顿都不会让服务端
提前切下一首。

**每台设备各自独立。** 本机播放按会话隔离：每个标签页启动时生成一个会话号
（存在 `sessionStorage`），请求都带 `X-Mibox-Session` 头，服务端据此维护一份
独立的队列与播放状态（`local:<会话号>`）。所以一台设备上的暂停 / 切歌 / 调音量
不会影响另一台，也不会串到别人的队列上。

会话上限 16 个，超出时回收最久未用的；闲置 6 小时也会被清掉。没带会话头的
调用（比如手工 curl）落在默认会话 `local` 上，方便自测。

浏览器解不了的容器（ape / wma 等）会自动转码成 mp3 再播，复用 `conf/cache`。
自动播放被浏览器拦下时，页面底部会出现「点击开始播放」按钮，点一下即可出声。

## 音乐库（四个标签）

「音乐库」页分四个标签，各自对应一套归类与一条语音指令：

| 标签 | 归类依据 | 语音 |
|---|---|---|
| 所有歌曲 | 整个曲库 | 播放歌曲XX（歌名或歌手都能搜） |
| 文件夹 | 音乐目录下的子目录（含层级） | 播放文件夹XX |
| 歌手 | 音频标签里的 artist | 播放歌手XX |
| 歌单 | 自己攒的（可增删） | 播放歌单XX |

- **歌手**取自音频标签（ID3 / MP4 / Vorbis / APEv2 / ASF 的键名都做了兼容）。
  没有标签时按文件名里的"歌手 - 歌名"猜一把，猜不出就算「未知歌手」。
  扫描时会顺手把歌手缓存进 `conf/library.json`，升级后第一次启动会补读一遍标签。
- **文件夹**支持层级：`播放文件夹流行` 会连 `流行/2024` 一起播。
- **歌单**是用户自定义的，页面上「所有歌曲 / 文件夹 / 歌手」三个标签里，
  每首歌行尾都有「加入歌单」，选中歌单即可（也可以当场新建）。
  歌单不存在时 `播放歌单XX` 会自动回落到同名文件夹，兼容老习惯
  （早期"歌单"就是子目录）。
- 持久化文件都在 `conf/` 下：`library.json`（扫描缓存）、`playlists.json`（歌单）、
  `favorites.json`（收藏）。删歌单只删歌单，不动音乐文件。

### 相关接口（自测用）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/library?q=&folder=&artist=&limit=` | 三选一过滤，都不给就是全部歌曲 |
| `GET` | `/api/folders` · `/api/artists` | 分组名 → 曲目数 |
| `GET` | `/api/playlists` | 歌单名 → 曲目数 |
| `POST` | `/api/playlists` | `{"name":"通勤"}` 新建歌单 |
| `DELETE` | `/api/playlists/{name}` | 删除歌单 |
| `GET` | `/api/playlists/{name}` | 歌单里的曲目 |
| `POST` | `/api/playlists/{name}/songs` | `{"song":"相对路径或歌名"}` 加入（歌单不存在则新建）|
| `POST` | `/api/playlists/{name}/remove` | `{"song":"..."}` 移出 |
| `POST` | `/api/player/{did}/play` | body 支持 `keyword` / `song` / `folder` / `artist` / `playlist` / `favorites` / `index`，可选 `shuffle`（true 随机 / false 顺序，不给则沿用当前模式）|

## 语音指令（内置，正则匹配）

```
播放歌曲周杰伦晴天    搜索本地音乐并播放（歌名与歌手一起搜）
播放文件夹流行        播放音乐目录下名为"流行"的子目录（含其子目录）
播放歌手周杰伦        播放该歌手的全部曲目
播放歌单通勤          播放自定义歌单（没有同名歌单时回落成文件夹）
把这首歌加入歌单通勤  把当前播放的本地歌曲收进歌单（歌单不存在则新建）
播放收藏              播放收藏列表
下一首 / 上一曲       切换
暂停 / 继续播放 / 停止播放
顺序播放              列表放完即停（默认）
全部循环              列表放完回到第一首接着放
单曲循环              当前这首反复播
随机播放              打乱顺序，同样放完即停
随机播放周杰伦        随机点歌：说了"随机"就随机，没说就顺序
随机播放歌单通勤      "随机"可以加在任何点播指令前面
播放周杰伦随机播放    说在句尾也算
音量50                设置音量
加入收藏 / 取消收藏
```

**随机还是顺序，由这一次的指令说了算。** 点播类指令里提到"随机 / 随便 /
打乱"（句首句尾都认：`随机播放周杰伦`、`随便放点周杰伦`、`播放周杰伦随机
播放`）就随机播；**没提就是顺序播**——模式跟着这次点歌走，不会因为上一次
说过"随机播放"就一路随机下去，也不会因为上次切过"全部循环"就接着循环。
只有"下一首 / 上一首 / 音量"这类不点歌的指令不动模式。

页面上那个下拉选的模式对**网页点播**仍然有效（点歌接口
`POST /api/player/{did}/play` 也支持显式传 `{"shuffle": true|false}`，
不传则沿用当前模式）。四种播放模式在「播放」页的下拉里也能切。`顺序播放`
与 `随机播放` 播到列表末尾就停下（界面显示「列表已播放完毕」，队列还留着，
点队列里的某首可以接着放），只有 `全部循环` 会回头重放——这是自动续播时
唯一会绕回开头的模式。

切换模式是**立刻生效**的：正在播歌时切到 `随机播放`，会马上打乱当前队列里
还没放的部分（正在播的那首留在原位），再切回 `顺序播放` 则还原原来的顺序。
所以"随机播放"可以单独当一句指令说，也可以跟点歌一起说。

放完即停时会**主动掐断音频流并给音箱发一条停止**（与按「停止」按钮同一条路径）。
本地文件走的是 mibox 自己提供的 HTTP 流，音箱拉完 EOF 之后不会自己上报停止，
不掐流的话 HA 里那台音箱会一直显示「正在播放」——即"没声音但显示在播放"。

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
