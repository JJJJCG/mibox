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
from .const import PLAY_MODE_NORMAL, PLAY_MODE_REPEAT_ALL, PLAY_MODE_REPEAT_ONE, PLAY_MODE_SHUFFLE
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
        self._schedule_next()
        log.info(f"[{self.speaker.name}] 播放: {item.name} ({item.duration}s)")
        return True

    def _schedule_next(self):
        self._cancel_timer()
        self._seq += 1
        seq = self._seq
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
        await self.controller.pause()
        self.state = "paused"

    async def resume(self):
        if self.state != "paused":
            return
        if self.cur_item:
            await self._play_current()

    async def stop(self):
        self._cancel_timer()
        self._seq += 1
        await self.controller.stop()
        self.state = "idle"

    async def set_volume(self, volume: int):
        self.volume = max(0, min(100, volume))
        await self.controller.set_volume(self.volume)

    def set_mode(self, mode: str):
        self.mode = mode

    def elapsed(self) -> int:
        if self.state == "playing" and self.started_at:
            return int(time.time() - self.started_at)
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

    async def close(self):
        self._cancel_timer()
