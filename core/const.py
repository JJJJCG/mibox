"""mibox 常量定义"""

VERSION = "0.1.0"

# ---------------- SSDP / UPnP ----------------
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_ALIVE_INTERVAL = 30

DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVTRANSPORT_URN = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL_URN = "urn:schemas-upnp-org:service:RenderingControl:1"
CONNECTION_MANAGER_URN = "urn:schemas-upnp-org:service:ConnectionManager:1"

SERVER_ID = f"mibox/{VERSION} UPnP/1.0"

TRANSPORT_STATE_NO_MEDIA = "NO_MEDIA_PRESENT"
TRANSPORT_STATE_STOPPED = "STOPPED"
TRANSPORT_STATE_PLAYING = "PLAYING"
TRANSPORT_STATE_PAUSED = "PAUSED_PLAYBACK"
TRANSPORT_STATE_TRANSITIONING = "TRANSITIONING"

TRANSPORT_STATUS_OK = "OK"
TRANSPORT_STATUS_ERROR = "ERROR_OCCURRED"

PLAY_MODE_NORMAL = "NORMAL"
PLAY_MODE_REPEAT_ONE = "REPEAT_ONE"
PLAY_MODE_REPEAT_ALL = "REPEAT_ALL"
PLAY_MODE_SHUFFLE = "SHUFFLE"

UPNP_ERROR_INVALID_ACTION = 401
UPNP_ERROR_INVALID_ARGS = 402
UPNP_ERROR_ACTION_FAILED = 501
UPNP_ERROR_TRANSITION_NOT_AVAILABLE = 701
UPNP_ERROR_SEEK_MODE_NOT_SUPPORTED = 710

# ---------------- 音箱型号兼容性 ----------------

# 这些型号必须使用 play_by_music_url 接口投递
NEED_USE_PLAY_MUSIC_API = [
    "X08C", "X08E", "X8F", "X4B", "LX05", "LX05A",
    "OH2", "OH2P", "X6A", "L15A", "L07A",
]

# 不支持无损格式的型号
NON_LOSSLESS_HARDWARE = {"L05B", "L05C", "LX06", "L16A"}

# 需要通过 Mina 服务获取对话记录的硬件型号
GET_ASK_BY_MINA = {"LX04", "L05B", "L05C", "S12", "S12A", "LX5A", "L15A", "L16A", "X6A"}

DEFAULT_AUDIO_ID = "448161862632079419"

# 播放器状态（小米 API 返回值）
MI_STATUS_STOPPED = 0
MI_STATUS_PLAYING = 1
MI_STATUS_PAUSED = 2

# 用户主动暂停后，这段时间内不把"音箱仍在播放"误判为外部恢复播放
# （云端停止指令有延迟，静音兜底也需要几秒才生效）
USER_PAUSE_GRACE_SEC = 20

# 用 HA 实体接管后，指令是直连 HA 的，状态也由 HA 自己维护，
# 不存在云端 player_* 那种排队延迟，所以宽限期可以收紧到几秒
HA_PAUSE_GRACE_SEC = 6

# 未接 HA 时核对音箱真实状态的间隔（秒）
STATUS_POLL_SEC = 5.0

# ---------------- 音乐库 ----------------
MUSIC_EXTENSIONS = {
    ".mp3", ".flac", ".wav", ".ape", ".ogg", ".m4a", ".aac", ".wma", ".opus",
}

# 无需转码、音箱可直接播放的格式
DIRECT_PLAY_FORMATS = {"mp3", "mpeg", "wav", "x-wav", "m4a", "aac"}

# 浏览器 <audio> 能直接解码的格式；这里的补集（ape/wma…）本地播放时先转 mp3
BROWSER_PLAY_FORMATS = {
    "mp3", "mpeg", "m4a", "aac", "wav", "x-wav", "ogg", "oga", "opus", "flac",
}

# 本地播放器（浏览器）的 DID：与真实音箱并列出现在设备列表里
LOCAL_DID = "local"
LOCAL_NAME = "本机播放（浏览器）"

SUPPORTED_PROTOCOLS = (
    "http-get:*:audio/mpeg:*,"
    "http-get:*:audio/mp3:*,"
    "http-get:*:audio/mp4:*,"
    "http-get:*:audio/ogg:*,"
    "http-get:*:audio/flac:*,"
    "http-get:*:audio/x-flac:*,"
    "http-get:*:audio/wav:*,"
    "http-get:*:audio/x-wav:*,"
    "http-get:*:audio/aac:*,"
    "http-get:*:audio/x-aac:*,"
    "http-get:*:audio/x-m4a:*,"
    "http-get:*:audio/x-ms-wma:*,"
    "http-get:*:audio/L16:*,"
    "http-get:*:audio/vnd.dlna.adts:*,"
    "http-get:*:audio/ape:*,"
    "http-get:*:audio/*:*"
)

# ---------------- 小米对话 API ----------------
LATEST_ASK_API = (
    "https://userprofile.mina.mi.com/device_profile/v2/conversation"
    "?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit=2"
)

APP_UA = "APP/com.xiaomi.mihome APPV/60209 iosPassportSDK/3.9.0 iOS/17.5.1"

# ---------------- 缓冲 ----------------
BUFFER_MAX_SIZE = 200 * 1024 * 1024   # 单个缓冲上限 200MB
BUFFER_MAX_COUNT = 10                  # 同时最多保留 10 个缓冲
BUFFER_MULTITHREAD_THRESHOLD = 5 * 1024 * 1024  # 超过 5MB 走多线程下载

LOG_NAME = "mibox"

# ---------------- 后台页面（/admin） ----------------
# 设备 / 配置 / AI 桥接收在后台里，进门要输口令。
# 口令明文写在代码里，**只是个形式，不是真实的访问控制**——接口本身没做鉴权，
# 想改口令直接改这里即可。
ADMIN_PASSWORD = "123456"
ADMIN_COOKIE = "mibox_admin"
ADMIN_TOKEN = "ok"
