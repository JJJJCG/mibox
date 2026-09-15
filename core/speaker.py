"""小爱音箱控制层：投递走小米 API，暂停/继续/停止与状态优先走 HA 实体

两件事必须分开看：

- **投递**（把一个 HTTP URL 交给音箱）只能用 miservice 的 `play_by_url` /
  `play_by_music_url`。HA 那边的 media_player 实体没有 PLAY_MEDIA 能力位，
  `media_player.play_media` 调了也不会生效，所以这条链路保持不动。
- **控制与状态**则正好相反：HA 实体是官方米家集成常驻维护的，指令直发设备，
  状态也是它自己订阅上来的，比每几秒去问一次小米云可靠得多。因此
  pause / resume / stop / get_status 在配了 HA 实体时优先走 HA。

断流（`StreamManager.kill`）依然是"真的让它停下来"的最终手段：播外部 URL
时音箱对云端暂停指令经常不响应。但**只在 HA 指令确认未生效时才断流**——
断过流音箱就没法"接着播"，HA 的 media_play 也就续不上了。
"""

from __future__ import annotations

import asyncio
import json
import logging

from ha.media import HA_STATE_PAUSED, HA_STATE_PLAYING, HASpeaker

from .auth import AuthManager
from .config import Speaker
from .const import DEFAULT_AUDIO_ID, MI_STATUS_STOPPED

log = logging.getLogger("mibox")


class SpeakerController:
    """单台音箱的控制接口"""

    def __init__(self, speaker: Speaker, auth: AuthManager, media=None, streams=None,
                 ha: HASpeaker | None = None):
        self.speaker = speaker
        self.auth = auth
        self.media = media
        self.streams = streams
        self.ha = ha
        self._last_volume = 50
        # 这条流是否被掐断过：断过就不可能"接着播"，只能重投
        self._stream_killed = False

    @property
    def device_id(self) -> str:
        return self.speaker.device_id

    @property
    def did(self) -> str:
        return self.speaker.did

    # ---------------- HA 可用性 ----------------
    @property
    def ha_active(self) -> bool:
        """这台音箱是否由 HA 实体接管控制与状态"""
        return self.ha is not None and self.ha.configured

    @property
    def status_trusted(self) -> bool:
        """当前拿到的状态能否当真

        播外部 URL 时音箱可能不上报 playing-state，HA 于是一直显示 idle。
        这种"从没见过它播"的 idle 不能当作"已停止"，否则队列会被误判打断。
        """
        return (not self.ha_active) or self.ha.seen_playing

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
            if self.ha_active:
                # 刚投递，HA 缓存里的旧状态不作数，让状态对齐读到新的
                self.ha.invalidate()
            return ret is not None
        except Exception as e:
            log.error(f"play_url 失败 ({self.speaker.name}): {e}")
            return False

    async def pause(self, url: str = "") -> bool:
        """暂停：先让 HA 实体自己暂停，确认没生效才断流兜底

        不断流是刻意的：只有流还活着，音箱才可能接着播（HA 的 media_play
        才续得上）。确认没停下来时再掐流，保证"按了暂停就真的没声"。
        """
        if self.ha_active:
            try:
                before = await self.ha.state()
                if await self.ha.pause():
                    # 只有明确知道它刚才在播（HA 看得见这条流）才去验证；
                    # 状态未知就别较真，免得把本可续播的流掐了
                    if before != HA_STATE_PLAYING or await self.ha.wait_state(
                        HA_STATE_PAUSED
                    ):
                        log.info(f"[{self.speaker.name}] 已通过 HA 暂停（未断流）")
                        return True
                    log.info(f"[{self.speaker.name}] HA 暂停未生效，断流兜底")
                else:
                    log.warning(f"[{self.speaker.name}] HA 暂停调用失败，回落小米 API")
            except Exception as e:
                log.error(f"HA 暂停异常，回落小米 API: {e}")

        # 小米 API 路径（原行为：断流为主，云端指令为辅）
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

    async def resume(self, url: str = "") -> bool:
        """继续播放：让 HA 实体自己续上，不重新投递

        返回 False 表示"这条流已经没了 / HA 续不上"，由 Player 回落成重投整首。
        """
        if self._stream_killed:
            # 流被掐过，音箱手里没有数据可续
            return False
        if not self.ha_active:
            return False
        try:
            before = await self.ha.state()
            if not await self.ha.play():
                log.warning(f"[{self.speaker.name}] HA 继续调用失败，回落重投")
                return False
            if before == HA_STATE_PAUSED and not await self.ha.wait_state(
                HA_STATE_PLAYING
            ):
                log.info(f"[{self.speaker.name}] HA 继续未生效，回落重投")
                return False
            log.info(f"[{self.speaker.name}] 已通过 HA 继续播放")
            return True
        except Exception as e:
            log.error(f"HA 继续异常，回落重投: {e}")
            return False

    async def stop(self, url: str = "") -> bool:
        """停止：断流（唯一能保证真停的手段）+ HA 侧发一条暂停

        该实体没有 STOP 能力位，`media_stop` 在 HA 里是空转，只能发 pause，
        队列清空由 Player 负责。
        """
        self.kill(url)
        if self.ha_active:
            try:
                if await self.ha.stop():
                    return True
                log.warning(f"[{self.speaker.name}] HA 停止调用失败，回落小米 API")
            except Exception as e:
                log.error(f"HA 停止异常，回落小米 API: {e}")

        try:
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
            self._stream_killed = True

    def release(self, url: str):
        """解除中断标记（重新投递同一文件前调用）"""
        if self.streams and url:
            self.streams.release(url)
        self._stream_killed = False

    async def set_volume(self, volume: int) -> bool:
        """音量：HA 优先（media_player.volume_set），写完回读校验

        HA 的 volume_level 是 0~1，这里的 0~100 要换算。官方米家集成在不同
        型号上的换算方式未必一致，所以**写完必须回读**：读回来的值和目标
        对不上就说明量纲不对（或者实体压根没执行），立刻回落小米 API，
        免得把音量设成静音或爆音。
        """
        volume = max(0, min(100, volume))
        if self.ha_active:
            try:
                if await self._set_volume_by_ha(volume):
                    return True
                log.warning(
                    f"[{self.speaker.name}] HA 音量写入未通过回读校验，回落小米 API"
                )
            except Exception as e:
                log.error(f"HA 音量异常，回落小米 API: {e}")

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

    async def _set_volume_by_ha(self, volume: int) -> bool:
        """写 HA 音量并回读确认；返回 False 表示不可信/没生效"""
        if not await self.ha.set_volume(volume):
            return False
        st = await self.ha.status(force=True)
        if st is None:
            return False
        got = int(st.get("volume") or 0)
        if volume > 0 and got <= 0:
            # 静音态下 volume_level 常常读不出来，没法判断就先不采信
            log.debug(f"[{self.speaker.name}] HA 音量回读为 0，无法校验")
            return False
        if abs(got - volume) > max(5, int(volume * 0.1)):
            log.info(
                f"[{self.speaker.name}] HA 音量回读 {got} != 目标 {volume}，"
                f"疑似量纲不一致"
            )
            return False
        if st.get("muted"):
            log.warning(
                f"[{self.speaker.name}] 音箱处于静音态（is_volume_muted），"
                f"音量已设为 {volume} 但可能仍无声"
            )
        self._last_volume = volume
        log.info(f"[{self.speaker.name}] 音量已通过 HA 设为 {volume}")
        return True

    async def get_volume(self) -> int:
        try:
            if self.ha_active:
                st = await self.ha.status()
                if st is not None and st["volume"] > 0:
                    self._last_volume = st["volume"]
                    return st["volume"]
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
        """返回 {status, volume, duration, position}；status: 0停止 1播放 2暂停

        HA 接管时直接读 HA 实体（读它本地维护的状态，不打小米云）；读不到再
        回落小米 API。HA 不提供播放进度，position/duration 恒为 0。
        """
        if self.ha_active:
            st = await self.ha.status()
            if st is not None:
                if st["volume"] > 0:
                    self._last_volume = st["volume"]
                return st
            log.debug(f"[{self.speaker.name}] HA 状态读取失败，回落小米 API")
        return await self._status_by_mina()

    async def _status_by_mina(self) -> dict:
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
