"""小爱音箱控制层：封装 miservice 的播放/暂停/音量/状态"""

from __future__ import annotations

import asyncio
import json
import logging

from .auth import AuthManager
from .config import Speaker
from .const import DEFAULT_AUDIO_ID, MI_STATUS_STOPPED

log = logging.getLogger("mibox")


class SpeakerController:
    """单台音箱的控制接口"""

    def __init__(self, speaker: Speaker, auth: AuthManager, media=None, streams=None):
        self.speaker = speaker
        self.auth = auth
        self.media = media
        self.streams = streams
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

    async def pause(self, url: str = "") -> bool:
        """暂停：先掐断本地流（主手段），再补一条云端指令（副手段）"""
        try:
            self.kill(url)
            await self.auth.ensure_login()
            mina = self.auth.get_mina()
            if mina is None:
                return True
            if self.speaker.use_music_api():
                await mina.player_stop(self.device_id)
            else:
                await mina.player_pause(self.device_id)
            return True
        except Exception as e:
            log.error(f"pause 失败: {e}")
            return False

    async def stop(self, url: str = "") -> bool:
        """停止：同样以掐断本地流为主"""
        try:
            self.kill(url)
            await self.auth.ensure_login()
            mina = self.auth.get_mina()
            if mina is None:
                return True
            await mina.player_pause(self.device_id)
            await asyncio.sleep(0.3)
            await mina.player_stop(self.device_id)
            return True
        except Exception as e:
            log.error(f"stop 失败: {e}")
            return False

    def kill(self, url: str):
        """掐断音箱正在拉的音频流"""
        if self.streams and url:
            self.streams.kill(url)

    def release(self, url: str):
        """解除中断标记（重新投递同一文件前调用）"""
        if self.streams and url:
            self.streams.release(url)

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
