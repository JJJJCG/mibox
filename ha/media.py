"""HA 媒体实体代理：把小爱音箱的播放控制与状态读取收敛到 HA 实体上

音箱经官方米家集成接入 HA 后，HA 里会有对应的 media_player 实体。本模块
是它在 mibox 内的代理，只认 HA 的 REST 接口，不碰小米 API。

已知的能力位限制（以 OH2 的 supported_features=17469 为例）：
    PLAY(16384) + VOLUME_STEP(1024) + NEXT_TRACK(32) + PREVIOUS_TRACK(16)
    + VOLUME_MUTE(8) + VOLUME_SET(4) + PAUSE(1)
也就是有播放与暂停，但**没有 STOP(4096)**——HA 的 media_stop 在未声明该
能力位的实体上会直接 return，既不报错也不执行。所以停止必须由调用方
「断流 + media_pause」组合完成，本模块的 stop() 也只发 pause。

状态读取带 TTL 缓存：播放器的状态对齐（每几秒一次）、DLNA 状态同步、
前端状态接口都在读同一个实体，不缓存会把 HA 打爆。
"""

from __future__ import annotations

import asyncio
import logging
import time

from .client import HAClient

log = logging.getLogger("mibox")

# HA 的 media_player 状态取值
HA_STATE_PLAYING = "playing"
HA_STATE_PAUSED = "paused"
HA_STATE_IDLE = "idle"

# HA state -> 小米风格状态码（与 core.const 的 MI_STATUS_* 对齐）
_STATE_MAP = {
    "playing": 1,
    "paused": 2,
    "idle": 0,
    "off": 0,
    "standby": 0,
    "unavailable": 0,
    "unknown": 0,
}

# 等实体状态变过来时的轮询步长（HA 侧状态靠音箱上报，需要给一点时间）
_WAIT_STEP = 0.25

# HA 拉取失败时，多旧的缓存就不再采信（超过就返回 None，交给调用方回落小米 API）
_STALE_LIMIT = 10.0


class HASpeaker:
    """单个 media_player 实体的读写代理"""

    def __init__(self, client: HAClient, entity_id: str, ttl: float = 1.0):
        self.client = client
        self.entity_id = (entity_id or "").strip()
        self.ttl = max(0.0, ttl)
        self._cache: dict | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()
        # HA 是否"亲眼见过这台在播"：只在见过之后才采信它的 idle（见 should_trust_stop）
        self._seen_playing = False

    @property
    def configured(self) -> bool:
        return bool(self.entity_id and self.client and self.client.configured)

    @property
    def seen_playing(self) -> bool:
        """本进程是否见过该实体上报 playing

        播外部 URL 时音箱有可能不上报 playing-state，此时 HA 会一直停在 idle。
        那种情况下它的 idle 不能当作"已停止"，否则队列会被误判打断。
        """
        return self._seen_playing

    # ---------------- 读 ----------------
    async def raw(self, force: bool = False) -> dict | None:
        """实体原始 JSON（带 TTL 缓存；HA 抖动时短暂沿用旧值）"""
        if not self.configured:
            return None
        now = time.monotonic()
        if not force and self._fresh(now):
            return self._cache
        async with self._lock:
            now = time.monotonic()
            if not force and self._fresh(now):
                return self._cache
            data = await self.client.get_state(self.entity_id)
            if data is not None:
                self._cache = data
                self._fetched_at = now
                return self._cache
            # 拉取失败：短时间内沿用旧值（容忍偶发抖动），
            # 超过 _STALE_LIMIT 就交回 None，让调用方回落小米 API
            if self._cache is not None and (now - self._fetched_at) <= _STALE_LIMIT:
                log.debug(f"HA 状态读取失败，短暂沿用缓存: {self.entity_id}")
                return self._cache
            log.debug(f"HA 状态读取失败且无可用缓存: {self.entity_id}")
            return None

    def _fresh(self, now: float) -> bool:
        return self._cache is not None and (now - self._fetched_at) < self.ttl

    async def state(self, force: bool = False) -> str:
        """实体状态字符串（小写）；读不到返回空串"""
        data = await self.raw(force)
        state = str((data or {}).get("state") or "").lower()
        if state == HA_STATE_PLAYING:
            self._seen_playing = True
        return state

    async def status(self, force: bool = False) -> dict | None:
        """HA 状态 -> {status, volume, state, muted, ...}；读不到返回 None

        HA 的 media_player 不提供播放进度（该实体没有 SEEK 能力位），
        duration/position 恒为 0，进度仍由播放器本地计时负责。
        """
        data = await self.raw(force)
        if not data:
            return None
        state = str(data.get("state") or "").lower()
        if state == HA_STATE_PLAYING:
            self._seen_playing = True
        attrs = data.get("attributes") or {}
        try:
            level = attrs.get("volume_level")
            volume = int(round(float(level) * 100)) if level is not None else 0
        except (TypeError, ValueError):
            volume = 0
        return {
            "status": _STATE_MAP.get(state, 0),
            "state": state,
            "volume": volume,
            "muted": bool(attrs.get("is_volume_muted")),
            "duration": 0,
            "position": 0,
            "media_title": attrs.get("media_title") or "",
        }

    # ---------------- 写 ----------------
    async def _service(self, service: str, extra: dict | None = None) -> bool:
        if not self.configured:
            return False
        data = {"entity_id": self.entity_id}
        if extra:
            data.update(extra)
        ok = await self.client.call_service("media_player", service, data)
        if ok:
            # 指令刚下发，缓存里的旧状态已经不作数了
            self.invalidate()
        return ok

    async def pause(self) -> bool:
        return await self._service("media_pause")

    async def play(self) -> bool:
        return await self._service("media_play")

    async def play_pause(self) -> bool:
        return await self._service("media_play_pause")

    async def stop(self) -> bool:
        """该实体没有 STOP 能力位，media_stop 等于空转，只能发 pause

        真正的"停止"由 SpeakerController 配合断流完成。
        """
        return await self._service("media_pause")

    async def set_volume(self, percent: int) -> bool:
        """音量走 HA 的 volume_set（0~1）

        音量读回放在调用方做（SpeakerController 写完会回读校验）：
        静音（is_volume_muted）时实体上报的 volume_level 不可信。
        """
        pct = max(0, min(100, int(percent)))
        return await self._service("volume_set", {"volume_level": pct / 100})

    # ---------------- 生效确认 ----------------
    def invalidate(self):
        """让下一次读取强制回源"""
        self._fetched_at = 0.0

    async def wait_state(self, expect: str, timeout: float = 2.0) -> bool:
        """等实体状态变成 expect，超时返回 False

        用于确认指令是否真的生效：没生效时调用方可以退到断流兜底。
        """
        if not self.configured:
            return False
        deadline = time.monotonic() + max(0.2, timeout)
        while True:
            if await self.state(force=True) == expect:
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(_WAIT_STEP)
