"""配置管理：单一 JSON + MIBOX_* 环境变量覆盖"""

from __future__ import annotations

import inspect
import json
import logging
import os
import socket
import threading
import uuid
from dataclasses import asdict, dataclass, field

log = logging.getLogger("mibox")

ENV_PREFIX = "MIBOX_"


def _env(name: str, default=None):
    return os.getenv(ENV_PREFIX + name.upper(), default)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class Speaker:
    """单台小爱音箱的持久化配置"""

    did: str = ""
    device_id: str = ""
    hardware: str = ""
    name: str = ""
    dlna_name: str = ""
    udn: str = ""
    # 投递模式：auto / url / music_api
    play_mode: str = "auto"
    # 强制转码（覆盖型号自动判断）
    force_convert: bool = False
    enabled: bool = True

    def ensure_udn(self) -> str:
        if not self.udn:
            self.udn = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"mibox-{self.did}"))
        return self.udn

    def get_dlna_name(self) -> str:
        return self.dlna_name or self.name or f"XiaoAI-{self.did}"

    def use_music_api(self) -> bool:
        """是否需要走 play_by_music_url"""
        from .const import NEED_USE_PLAY_MUSIC_API

        if self.play_mode == "music_api":
            return True
        if self.play_mode == "url":
            return False
        return any(model in self.hardware for model in NEED_USE_PLAY_MUSIC_API)

    def needs_conversion(self, fmt: str = "") -> bool:
        """是否需要 ffmpeg 转码"""
        from .const import NON_LOSSLESS_HARDWARE

        if self.force_convert:
            return True
        if self.hardware not in NON_LOSSLESS_HARDWARE:
            return False
        return fmt.lower() not in ("mp3", "mpeg", "wav", "m4a", "aac")


@dataclass
class Config:
    """全局配置"""

    # 小米账号
    account: str = ""
    password: str = ""
    cookie: str = ""
    mi_did: str = ""

    # 网络
    hostname: str = ""
    web_port: int = 8080
    dlna_port: int = 8200

    # 路径
    conf_path: str = "/app/conf"
    music_path: str = "/app/music"

    # 播放
    default_volume: int = 38
    delay_sec: int = 0
    continue_play: bool = True

    # 语音
    enable_voice: bool = True
    pull_ask_sec: float = 1.5
    fuzzy_match_cutoff: float = 0.6

    # 模块开关
    enable_dlna: bool = True
    enable_ha: bool = False

    # Home Assistant
    ha_url: str = ""
    ha_token: str = ""
    ha_rules: list = field(default_factory=list)

    verbose: bool = False

    speakers: dict = field(default_factory=dict)

    _save_lock = threading.Lock()

    def __post_init__(self):
        if not self.account:
            self.account = _env("ACCOUNT", "")
        if not self.password:
            self.password = _env("PASSWORD", "")
        if not self.cookie:
            self.cookie = _env("COOKIE", "")
        if not self.mi_did:
            self.mi_did = _env("MI_DID", "")
        if not self.hostname:
            self.hostname = _env("HOSTNAME", "")
        self.web_port = _env_int("WEB_PORT", self.web_port)
        self.dlna_port = _env_int("DLNA_PORT", self.dlna_port)
        self.conf_path = _env("CONF_PATH", self.conf_path)
        self.music_path = _env("MUSIC_PATH", self.music_path)
        self.default_volume = _env_int("DEFAULT_VOLUME", self.default_volume)
        self.pull_ask_sec = max(0.5, _env_float("PULL_ASK_SEC", self.pull_ask_sec))
        self.enable_voice = _env_bool("ENABLE_VOICE", self.enable_voice)
        self.enable_dlna = _env_bool("ENABLE_DLNA", self.enable_dlna)
        self.enable_ha = _env_bool("ENABLE_HA", self.enable_ha)
        if not self.ha_url:
            self.ha_url = _env("HA_URL", "")
        if not self.ha_token:
            self.ha_token = _env("HA_TOKEN", "")
        self.verbose = _env_bool("VERBOSE", self.verbose)

        if not self.hostname:
            self.hostname = self._detect_local_ip()

    @staticmethod
    def _detect_local_ip() -> str:
        """尽力探测局域网 IP。host 网络下通常正确，但仍建议显式配置。"""
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            if s:
                s.close()

    # ---------- 路径 ----------
    @property
    def config_file(self) -> str:
        return os.path.join(self.conf_path, "config.json")

    @property
    def token_file(self) -> str:
        return os.path.join(self.conf_path, ".mi.token")

    @property
    def cache_dir(self) -> str:
        return os.path.join(self.conf_path, "cache")

    @property
    def log_file(self) -> str:
        return os.path.join(self.conf_path, "mibox.log")

    # ---------- 对外 URL ----------
    def base_url(self) -> str:
        return f"http://{self.hostname}:{self.web_port}"

    # ---------- 音箱 ----------
    def get_did_list(self) -> list[str]:
        if not self.mi_did:
            return []
        return [d.strip() for d in self.mi_did.split(",") if d.strip()]

    def get_speaker(self, did: str) -> Speaker:
        sp = self.speakers.get(did)
        if sp is None or isinstance(sp, dict):
            sp = Speaker(**sp) if isinstance(sp, dict) else Speaker(did=did)
            self.speakers[did] = sp
        sp.did = did
        sp.ensure_udn()
        return sp

    def get_enabled_speakers(self) -> list[Speaker]:
        return [self.get_speaker(d) for d in self.get_did_list()
                if self.get_speaker(d).enabled]

    # ---------- 持久化 ----------
    def save(self):
        with self._save_lock:
            os.makedirs(self.conf_path, exist_ok=True)
            data = asdict(self)
            data["speakers"] = {
                did: asdict(sp) if isinstance(sp, Speaker) else sp
                for did, sp in self.speakers.items()
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, conf_path: str | None = None) -> "Config":
        conf_path = conf_path or _env("CONF_PATH", "/app/conf")
        cfg = cls(conf_path=conf_path)
        path = cfg.config_file
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                log.warning(f"配置文件解析失败，使用默认配置: {e}")
                return cfg
            data.pop("conf_path", None)
            valid = set(inspect.signature(cls.__init__).parameters)
            filtered = {k: v for k, v in data.items() if k in valid}
            cfg = cls(**filtered)
            cfg.conf_path = conf_path
            # __post_init__ 会让 MIBOX_* 环境变量参与构造；这里用文件值再覆盖
            # 一遍，确立优先级：config.json > 环境变量 > 内置默认。否则在界面
            # 上勾选/修改的配置会在重启时被 compose 里的旧环境变量静默改回
            # （例如 MIBOX_ENABLE_HA=false 会让"HA 桥接"永远勾不上）。
            for k, v in filtered.items():
                setattr(cfg, k, v)
            return cfg
