"""小爱音箱控制层：封装 miservice 的播放/暂停/音量/状态"""

from __future__ import annotations

import asyncio
import json
import logging

from .auth import AuthManager
from .config import Speaker
from .const import DEFAULT_AUDIO_ID, MI_STATUS_PLAYING, MI_STATUS_STOPPED

log = logging.getLogger("mibox")

# 停止指令发出后，等待音箱状态落地的间隔与重试次数
STOP_CHECK_INTERVAL = 0.8
STOP_CHECK_RETRY = 2


class SpeakerController:
    """单台音箱的控制接口"""

    def __init__(self, speaker: Speaker, auth: AuthManager, media=None):
        self.speaker = speaker
        self.auth = auth
        self.media = media
        self._last_volume = 50

    @property
    def device_id(self) -> str:
        return self.speaker.device_id

    @property
    def did(self) -> str:
        return self.speaker.did

    async def play_url(self, url: str, continue_play: bool = True) -> bool:
        """让音箱播放指定 URL"""
        try:
            await self.auth.ensure_login()
            mina = self.auth.get_mina()
            if mina is None:
                return False
            if self.speaker.use_music_api():
                if continue_play:
                    ret = await mina.play_by_music_url(
                        self.device_id, url, _type=1, audio_id=DEFAULT_AUDIO_ID
                    )
                else:
                    ret = await mina.play_by_music_url(
                        self.device_id, url, audio_id=DEFAULT_AUDIO_ID
                    )
            else:
                ret = await mina.play_by_url(self.device_id, url)
            log.info(f"投递播放 {self.speaker.name}: {url[:80]}... ret={ret}")
            return ret is not None
        except Exception as e:
            log.error(f"play_url 失败 ({self.speaker.name}): {e}")
            return False

    async def pause(self) -> bool:
        """暂停

        云端指令对 URL 直投的内容经常无效，所以这里是"发指令 → 校验状态 →
        升级手段 → 静音覆盖"的阶梯式处理，任一环节真的停下来就结束。
        """
        try:
            await self.auth.ensure_login()
            mina = self.auth.get_mina()
            if mina is None:
                return False

            if self.speaker.use_music_api():
                await mina.player_stop(self.device_id)
            else:
                await mina.player_pause(self.device_id)

            for i in range(STOP_CHECK_RETRY):
                await asyncio.sleep(STOP_CHECK_INTERVAL)
                if not await self._is_playing():
                    return True
                log.info(f"pause 未生效({self.speaker.name})，改用 stop 指令重试 {i + 1}")
                await mina.player_stop(self.device_id)

            await asyncio.sleep(STOP_CHECK_INTERVAL)
            if not await self._is_playing():
                return True

            log.warning(f"[{self.speaker.name}] 云端暂停/停止均无效，用静音覆盖打断")
            return await self._cover_with_silence()
        except Exception as e:
            log.error(f"pause 失败: {e}")
            return False

    async def stop(self) -> bool:
        """停止，策略同 pause，但直接以 stop 指令为主"""
        try:
            await self.auth.ensure_login()
            mina = self.auth.get_mina()
            if mina is None:
                return False

            # 先 pause 再 stop：部分型号只认 pause，另一部分只认 stop，
            # 两条都发覆盖面最广（xiaomusic 的 force_stop 也是这个思路）
            await mina.player_pause(self.device_id)
            await asyncio.sleep(0.4)
            await mina.player_stop(self.device_id)

            for i in range(STOP_CHECK_RETRY):
                await asyncio.sleep(STOP_CHECK_INTERVAL)
                if not await self._is_playing():
                    return True
                log.info(f"stop 未生效({self.speaker.name})，重试 {i + 1}")
                await mina.player_stop(self.device_id)

            await asyncio.sleep(STOP_CHECK_INTERVAL)
            if not await self._is_playing():
                return True

            log.warning(f"[{self.speaker.name}] 云端停止无效，用静音覆盖打断")
            return await self._cover_with_silence()
        except Exception as e:
            log.error(f"stop 失败: {e}")
            return False

    async def _is_playing(self) -> bool:
        """音箱当前是否仍在播放（查询失败时保守返回 False，避免死循环重试）"""
        try:
            st = await self.get_status()
        except Exception:
            return False
        return st.get("status") == MI_STATUS_PLAYING

    async def _cover_with_silence(self) -> bool:
        """投递 1 秒静音覆盖当前播放，音箱播完自然停"""
        if self.media is None:
            return False
        try:
            url = await self.media.ensure_silence()
            if not url:
                return False
            return await self.play_url(url)
        except Exception as e:
            log.error(f"静音覆盖失败 ({self.speaker.name}): {e}")
            return False

    async def set_volume(self, volume: int) -> bool:
        volume = max(0, min(100, volume))
        try:
            await self.auth.ensure_login()
            mina = self.auth.get_mina()
            if mina is None:
                return False
            await mina.player_set_volume(self.device_id, volume)
            if volume > 0:
                self._last_volume = volume
            return True
        except Exception as e:
            log.error(f"set_volume 失败: {e}")
            return False

    async def get_volume(self) -> int:
        try:
            await self.auth.ensure_login()
            mina = self.auth.get_mina()
            if mina is None:
                return self._last_volume
            status = await mina.player_get_status(self.device_id)
            info = json.loads(status.get("data", {}).get("info", "{}"))
            vol = int(info.get("volume", 0))
            if vol > 0:
                self._last_volume = vol
            return vol
        except Exception as e:
            log.debug(f"get_volume 失败: {e}")
            return self._last_volume

    async def get_status(self) -> dict:
        """返回 {status, volume, duration, position}；status: 0停止 1播放 2暂停"""
        try:
            await self.auth.ensure_login()
            mina = self.auth.get_mina()
            if mina is None:
                return {"status": MI_STATUS_STOPPED, "volume": self._last_volume}
            info = await mina.player_get_status(self.device_id)
            if info.get("code") != 0:
                raise Exception(f"Mina API Error: {info}")
            data = json.loads(info.get("data", {}).get("info", "{}"))
            return {
                "status": data.get("status", MI_STATUS_STOPPED),
                "volume": int(data.get("volume", 0)),
                "duration": int(data.get("duration", 0)) or 0,
                "position": int(data.get("position", 0)) or 0,
            }
        except Exception as e:
            log.debug(f"get_status 失败: {e}")
            return {"status": MI_STATUS_STOPPED, "volume": self._last_volume}
