"""统一播放队列引擎

语音点歌和 DLNA 推送都进同一个队列，避免两个来源互相打断。
每台音箱一个 Player 实例。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from .config import Config, Speaker
from .const import (
    MI_STATUS_PAUSED,
    MI_STATUS_PLAYING,
    MI_STATUS_STOPPED,
    PLAY_MODE_NORMAL,
    PLAY_MODE_REPEAT_ALL,
    PLAY_MODE_REPEAT_ONE,
    PLAY_MODE_SHUFFLE,
    USER_PAUSE_GRACE_SEC,
)
from .media import MediaService
from .speaker import SpeakerController

log = logging.getLogger("mibox")


@dataclass
class QueueItem:
    name: str
    url: str = ""        # 最终投递给音箱的 URL
    duration: int = 0
    source: str = "local"   # local | dlna
    song: object = None     # 关联的 Song（本地歌曲）

    def to_dict(self):
        return {"name": self.name, "duration": self.duration, "source": self.source}


class Player:
    """单台音箱的播放队列状态机"""

    def __init__(
        self,
        speaker: Speaker,
        controller: SpeakerController,
        config: Config,
        media: MediaService,
    ):
        self.speaker = speaker
        self.controller = controller
        self.config = config
        self.media = media

        self.queue: list[QueueItem] = []
        self.index = -1
        self.mode = PLAY_MODE_NORMAL
        self.state = "idle"          # idle | playing | paused
        self.cur_item: Optional[QueueItem] = None
        self.started_at = 0.0
        self.volume = config.default_volume

        self._timer: Optional[asyncio.Task] = None
        self._seq = 0                 # 防止旧定时器误触发
        self._monitor_task: Optional[asyncio.Task] = None
        self._deliver_at = 0.0        # 最近一次投递时间（宽限期内不判停）

        # 暂停续播：小爱的 URL 播放没有 resume，只能记录位置后重新切片投递
        self._paused_pos = 0.0
        self._user_paused = False     # 是否由 mibox 主动暂停
        self._pause_at = 0.0

    # ---------------- 内部 ----------------
    def _cancel_timer(self):
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    async def _resolve(self, item: QueueItem):
        """把 QueueItem 解析成可投递的 URL 和时长"""
        song = item.song
        if song is not None:
            item.duration = song.duration or self.media.get_duration(song.path)
            item.url = await self.media.ensure_playable(self.speaker, song.path)
        if not item.duration:
            item.duration = 180  # 兜底：拿不到时长时 3 分钟后切下一首

    async def _play_current(self) -> bool:
        item = self.cur_item
        if item is None:
            return False
        await self._resolve(item)
        if not item.url:
            return False

        ok = await self.controller.play_url(item.url)
        if not ok:
            return False

        self.state = "playing"
        self.started_at = time.time()
        self._deliver_at = self.started_at
        self._user_paused = False
        self._paused_pos = 0.0
        self._schedule_next()
        log.info(f"[{self.speaker.name}] 播放: {item.name} ({item.duration}s)")
        return True

    def _schedule_next(self, wait: float | None = None):
        self._cancel_timer()
        self._seq += 1
        seq = self._seq
        if wait is None:
            wait = (self.cur_item.duration if self.cur_item else 0) + self.config.delay_sec

        async def _runner():
            try:
                await asyncio.sleep(max(1, wait))
                if seq == self._seq:
                    await self.next(auto=True)
            except asyncio.CancelledError:
                pass

        self._timer = asyncio.create_task(_runner())

    # ---------------- 对外：队列操作 ----------------
    async def play_items(self, items: list[QueueItem], start: int = 0):
        """替换队列并从 start 开始播放"""
        self._cancel_timer()
        self.queue = items
        self.index = max(0, min(start, len(items) - 1)) if items else -1
        if not items:
            self.state = "idle"
            self.cur_item = None
            return
        self.cur_item = self.queue[self.index]
        await self._play_current()

    async def play_songs(self, songs, start: int = 0):
        items = [QueueItem(name=s.name, song=s) for s in songs]
        if self.mode == PLAY_MODE_SHUFFLE:
            random.shuffle(items)
        await self.play_items(items, start)

    async def enqueue(self, item: QueueItem):
        self.queue.append(item)

    async def next(self, auto: bool = False):
        if not self.queue:
            return
        self._cancel_timer()
        self._seq += 1

        if self.cur_item and self.cur_item.source == "dlna" and auto:
            # DLNA 推送是单曲，播完自然结束
            self.state = "idle"
            self.cur_item = None
            return

        if self.mode == PLAY_MODE_REPEAT_ONE and auto:
            await self._play_current()
            return

        nxt = self.index + 1
        if nxt >= len(self.queue):
            if self.mode == PLAY_MODE_REPEAT_ALL or auto:
                nxt = 0
            else:
                self.state = "idle"
                self.cur_item = None
                self.index = 0
                return

        self.index = nxt
        self.cur_item = self.queue[nxt]
        await self._play_current()

    async def prev(self):
        if not self.queue:
            return
        self._cancel_timer()
        self.index = (self.index - 1) % len(self.queue)
        self.cur_item = self.queue[self.index]
        await self._play_current()

    # ---------------- 对外：控制 ----------------
    async def pause(self):
        if self.state != "playing":
            return
        self._cancel_timer()
        self._seq += 1
        # 先记下暂停位置，resume 时按这个偏移重新切片
        self._paused_pos = float(self.elapsed())
        self._user_paused = True
        self._pause_at = time.time()
        await self.controller.pause()
        self.state = "paused"
        self.started_at = 0.0
        log.info(f"[{self.speaker.name}] 暂停于 {int(self._paused_pos)}s")

    async def resume(self):
        if self.state != "paused":
            return
        offset = int(self._paused_pos)
        self._user_paused = False

        # 能定位到本地音频文件就按暂停位置切片，实现真正的续播
        if self.cur_item and offset > 2:
            url = await self._seek_url_at(offset)
            if url:
                ok = await self.controller.play_url(url)
                if ok:
                    self.state = "playing"
                    self.started_at = time.time() - offset
                    self._deliver_at = time.time()
                    remaining = max(1, (self.cur_item.duration or 0) - offset)
                    self._schedule_next(wait=remaining)
                    self._paused_pos = 0.0
                    log.info(f"[{self.speaker.name}] 从 {offset}s 续播，剩余 {remaining}s")
                    return
                log.warning(f"[{self.speaker.name}] 续播投递失败，退回从头播放")

        # 拿不到本地文件或切片失败：退回从头播放
        self._paused_pos = 0.0
        if self.cur_item:
            await self._play_current()

    async def _seek_url_at(self, offset: int) -> Optional[str]:
        """找到当前曲目的本地文件并切片出 offset 秒之后的音频"""
        try:
            src = ""
            song = getattr(self.cur_item, "song", None)
            if song is not None and getattr(song, "path", ""):
                src = song.path
            else:
                src = self.media.abs_path_of(self.cur_item.url)
            if not src:
                return None
            return await self.media.make_seek_url(src, offset)
        except Exception as e:
            log.warning(f"生成续播切片失败: {e}")
            return None

    async def stop(self):
        self._cancel_timer()
        self._seq += 1
        await self.controller.stop()
        self.state = "idle"
        self.cur_item = None
        self.started_at = 0.0
        self._paused_pos = 0.0
        self._user_paused = False

    async def set_volume(self, volume: int):
        self.volume = max(0, min(100, volume))
        await self.controller.set_volume(self.volume)

    def set_mode(self, mode: str):
        self.mode = mode

    def elapsed(self) -> int:
        if self.state == "playing" and self.started_at:
            return int(time.time() - self.started_at)
        if self.state == "paused":
            return int(self._paused_pos)
        return 0

    def status(self) -> dict:
        return {
            "did": self.speaker.did,
            "name": self.speaker.name,
            "hardware": self.speaker.hardware,
            "state": self.state,
            "mode": self.mode,
            "volume": self.volume,
            "index": self.index,
            "total": len(self.queue),
            "current": self.cur_item.to_dict() if self.cur_item else None,
            "elapsed": self.elapsed(),
            "queue": [i.to_dict() for i in self.queue[:50]],
        }

    # ---------------- 真实状态对齐 ----------------
    def start_monitor(self):
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor())

    async def _monitor(self):
        """对齐音箱真实状态，解决"外部停止/暂停后本地计时器还在跑"的问题

        场景：用户对小爱说"停止"（小爱原生处理，mibox 不感知）、手机上
        暂停、其他设备控制音箱——本地到点仍会自动切下一首。
        这里每 5 秒核对一次真实状态并取消/恢复计时器。
        """
        try:
            while True:
                await asyncio.sleep(5)
                if self.state == "idle" or self.cur_item is None:
                    continue
                # 刚投递完音箱可能还没真正开播，宽限期内不判停
                if time.time() - self._deliver_at < 10:
                    continue
                try:
                    st = await self.controller.get_status()
                except Exception:
                    continue
                status = st.get("status", MI_STATUS_STOPPED)

                if status == MI_STATUS_STOPPED and self.state == "playing":
                    log.info(f"[{self.speaker.name}] 音箱已被外部停止，取消自动切歌")
                    self._cancel_timer()
                    self._seq += 1
                    self.state = "idle"
                elif status == MI_STATUS_PAUSED and self.state == "playing":
                    log.info(f"[{self.speaker.name}] 音箱已被外部暂停，暂停队列计时")
                    self._cancel_timer()
                    self._seq += 1
                    # 用音箱上报的位置作为续播点（拿不到就退回本地计时）
                    self._paused_pos = float(st.get("position", 0) or self.elapsed())
                    self._user_paused = False
                    self.started_at = 0.0
                    self.state = "paused"
                elif status == MI_STATUS_PLAYING and self.state == "paused":
                    # mibox 自己刚按下暂停时，云端指令/静音兜底还在路上，
                    # 此时音箱上报 PLAYING 属于延迟，不能当成"外部恢复播放"
                    if self._user_paused and (
                        time.time() - self._pause_at < USER_PAUSE_GRACE_SEC
                    ):
                        continue
                    pos = st.get("position", 0)
                    remaining = max(1, (self.cur_item.duration or 0) - pos)
                    log.info(
                        f"[{self.speaker.name}] 音箱被外部恢复播放，"
                        f"按剩余 {remaining}s 续接队列"
                    )
                    self.state = "playing"
                    self.started_at = time.time() - pos
                    self._schedule_next(wait=remaining)
        except asyncio.CancelledError:
            pass

    async def close(self):
        self._cancel_timer()
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._monitor_task = None
