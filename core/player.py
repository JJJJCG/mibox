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
    HA_PAUSE_GRACE_SEC,
    MI_STATUS_PAUSED,
    MI_STATUS_PLAYING,
    MI_STATUS_STOPPED,
    PLAY_MODE_NORMAL,
    PLAY_MODE_REPEAT_ALL,
    PLAY_MODE_REPEAT_ONE,
    PLAY_MODE_SHUFFLE,
    STATUS_POLL_SEC,
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
        d = {"name": self.name, "duration": self.duration, "source": self.source}
        # url 供本机播放直接喂给 <audio>；rel 供界面回定位曲目
        if self.song is not None:
            d["rel"] = getattr(self.song, "rel", "")
        if self.url:
            d["url"] = self.url
        return d


class Player:
    """单台音箱的播放队列状态机"""

    # 是否是"本机播放"（浏览器）通道；真实音箱为 False
    is_local = False

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
        self._order: list[QueueItem] = []   # 队列的原始顺序，切回"顺序播放"时还原
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
        # 起播序号：每次真正开始播放都 +1。界面据此判断"要重新开始播"，
        # 否则单曲循环时 URL 不变，播放端（<audio>）不会重播同一首。
        self.play_id = 0

    # ---------------- 内部 ----------------
    def _cancel_timer(self):
        """取消待触发的切歌定时器

        坑：`next()` 既可能被外部（语音/接口）调用，也可能由定时器自己触发
        （`_runner` -> `next(auto=True)`）。后者是"在当前任务里 cancel 当前
        任务"，会把正在跑的 `next()` 自己取消掉——它会在第一个 await（真实
        的投递网络调用）处抛 CancelledError 静默退出，于是歌永远切不到下
        一首，音箱就靠自己的单曲循环反复重放同一首。所以这里必须跳过
        "当前正在运行的任务"。
        """
        timer = self._timer
        self._timer = None
        if (
            timer is not None
            and not timer.done()
            and timer is not asyncio.current_task()
        ):
            timer.cancel()

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

        # 重新投递前解除中断标记，否则音箱会被自己上一次的"断流"挡住
        self.controller.release(item.url)
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
    async def play_items(
        self, items: list[QueueItem], start: int = 0, remember: bool = True
    ):
        """替换队列并从 start 开始播放

        remember=False 用于"跳到队列里某一首"（items 就是当前队列本身），
        此时不能覆盖 _order，否则随机模式下的原始顺序会被打乱顺序顶掉，
        切回"顺序播放"就没得还原了。
        """
        self._cancel_timer()
        self.queue = list(items)
        if remember:
            self._order = list(items)
        self.index = max(0, min(start, len(self.queue) - 1)) if self.queue else -1
        if not self.queue:
            self.state = "idle"
            self.cur_item = None
            return
        self.cur_item = self.queue[self.index]
        await self._play_current()

    async def play_songs(self, songs, start: int = 0):
        items = [QueueItem(name=s.name, song=s) for s in songs]
        # 先记下列表原始顺序（供切回顺序播放时还原），再按模式决定是否整表打乱
        self._order = list(items)
        if self.mode == PLAY_MODE_SHUFFLE:
            random.shuffle(items)
        await self.play_items(items, start, remember=False)

    async def enqueue(self, item: QueueItem):
        self.queue.append(item)
        # 原始顺序快照跟着一起追加，否则切回顺序播放时这首歌会凭空消失
        if self._order:
            self._order.append(item)

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
            # 只有「全部循环」才回头接着放；顺序 / 随机播放到列表尾就停。
            # 这里以前写的是 `if mode == REPEAT_ALL or auto`——自动续播时无条件
            # 回到第一首，于是"顺序播放"也变成了永远循环，界面上就找不到
            # "列表放完即停"这个选项了。
            if self.mode == PLAY_MODE_REPEAT_ALL:
                nxt = 0
            else:
                await self._finish()
                return

        self.index = nxt
        self.cur_item = self.queue[nxt]
        await self._play_current()

    async def _finish(self):
        """队列播放完毕：真正停掉音箱并切断音频流

        只把 state 改成 idle 是不够的。本地文件走的是 mibox 自己提供的 HTTP
        流，音箱拉完 EOF 之后**不会自己上报停止**：它手里还晾着这条连接，HA
        那边就一直显示 playing——表现就是"没声音，但音箱显示在播放"。
        所以必须把流掐断（controller.stop 内部会 kill），音箱才会真的回到
        停止态，HA 的实体状态也跟着更新。

        另外音箱自己的播放器循环模式还开着，掐流也能顺带阻止它重拉最后一首。
        """
        url = self._current_url()
        log.info(f"[{self.speaker.name}] 队列播放完毕（{self.mode}），停止并断流")
        try:
            await self.controller.stop(url)
        except Exception as e:
            log.error(f"[{self.speaker.name}] 停止音箱失败: {e}")

        self.state = "idle"
        self.cur_item = None
        self.started_at = 0.0
        self._paused_pos = 0.0
        self._user_paused = False
        # index 留在最后一首，不再像以前那样重置成 0（重置会让界面显示 1/N
        # 却什么都没有，而且下次"下一首"会从第二首开始）

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
        # 记下暂停位置供界面显示。走 HA 时不断流（流留着才能续播），
        # 只有 HA 指令确认没生效，Controller 才会掐流兜底。
        self._paused_pos = float(self.elapsed())
        self._user_paused = True
        self._pause_at = time.time()
        await self.controller.pause(self._current_url())
        self.state = "paused"
        self.started_at = 0.0
        log.info(f"[{self.speaker.name}] 暂停于 {int(self._paused_pos)}s")

    async def resume(self):
        # 被外部（语音/其他设备）停止时 state 是 idle，此时也允许"继续"，
        # 否则按钮点了毫无反应
        if self.state == "idle" and self.cur_item:
            await self._play_current()
            return
        if self.state != "paused":
            return

        # 优先让 HA 实体自己接着播：流没被掐断时音箱手里还有数据，
        # 这才是真续播（不回到歌曲开头）
        if self.cur_item and await self.controller.resume(self._current_url()):
            self.state = "playing"
            self.started_at = time.time() - self._paused_pos
            self._deliver_at = 0.0        # 不是新投递，不占投递宽限期
            self._user_paused = False
            remaining = max(1, (self.cur_item.duration or 0) - self._paused_pos)
            self._schedule_next(wait=remaining)
            # 接了 HA 是 HA 续播；本机播放则是浏览器自己接着播
            via = "HA " if self.controller.ha_active else ""
            log.info(
                f"[{self.speaker.name}] 继续播放（{via}续播，剩余 {int(remaining)}s）"
            )
            return

        # 断过流 / HA 续不上：只能重新投递整首
        self._paused_pos = 0.0
        self._user_paused = False
        if self.cur_item:
            await self._play_current()

    def _current_url(self) -> str:
        return self.cur_item.url if self.cur_item else ""

    async def stop(self):
        self._cancel_timer()
        self._seq += 1
        await self.controller.stop(self._current_url())
        self.state = "idle"
        self.cur_item = None
        self.started_at = 0.0
        self._paused_pos = 0.0
        self._user_paused = False

    async def set_volume(self, volume: int):
        self.volume = max(0, min(100, volume))
        await self.controller.set_volume(self.volume)

    def _reshuffle_tail(self):
        """打乱"当前曲目之后"的部分，正在播的这首留在原位

        用尾部而不是整表，是因为整表打乱会让正在播的歌被换掉位置的语义变得
        混乱；尾部打乱等价于"剩下的按随机顺序接着放"。
        """
        if not self.queue:
            return
        start = self.index + 1 if self.index >= 0 else 0
        tail = self.queue[start:]
        if len(tail) > 1:
            random.shuffle(tail)
            self.queue[start:] = tail

    def _restore_order(self):
        """从随机切回顺序：还原列表的原始顺序，正在播的那首继续当当前项"""
        if not self._order:
            return
        cur = self.cur_item
        self.queue = list(self._order)
        if cur is not None:
            for i, it in enumerate(self.queue):
                if it is cur:
                    self.index = i
                    break

    def set_mode(self, mode: str):
        """切换播放模式

        之前这里只写 `self.mode = mode`，模式纯粹是个标签——正在播的列表
        仍按原顺序往下走，于是"随机播放"看起来完全没生效（只有恰好"先切
        随机、再点播放"才碰得上 play_songs 里那次整表打乱）。现在切到随机
        会立刻打乱当前队列，切回顺序/循环则还原原顺序。
        """
        if mode == self.mode:
            return
        if self.queue:
            if mode == PLAY_MODE_SHUFFLE:
                self._reshuffle_tail()
            elif self.mode == PLAY_MODE_SHUFFLE:
                self._restore_order()
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
            "play_id": self.play_id,
            "is_local": self.is_local,
            "queue": [i.to_dict() for i in self.queue[:50]],
            # 控制与状态由谁提供：配了 HA 实体就是 home_assistant
            "status_source": "home_assistant" if self.controller.ha_active else "mina",
            "ha_entity": self.speaker.ha_entity,
        }

    # ---------------- 真实状态对齐 ----------------
    def _poll_interval(self) -> float:
        """状态核对间隔：HA 接管时读的是它本地维护的状态，可以更勤"""
        return self.config.ha_poll_sec if self.controller.ha_active else STATUS_POLL_SEC

    def _pause_grace(self) -> float:
        """刚按下暂停时的宽限期：HA 指令延迟小，不必等 20 秒"""
        return HA_PAUSE_GRACE_SEC if self.controller.ha_active else USER_PAUSE_GRACE_SEC

    def start_monitor(self):
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor())

    async def _monitor(self):
        """对齐音箱真实状态，解决"外部停止/暂停后本地计时器还在跑"的问题

        场景：用户对小爱说"停止"（小爱原生处理，mibox 不感知）、手机上
        暂停、其他设备控制音箱——本地到点仍会自动切下一首。
        这里定期核对真实状态并取消/恢复计时器。

        接 HA 之后核对的是 HA 实体上的状态（从 HA 本地读，不碰小米云），
        间隔更短；但播外部 URL 时音箱未必上报 playing-state，所以只有
        "见过它在播"（controller.status_trusted）的 idle 才算真的停了。
        """
        try:
            while True:
                await asyncio.sleep(self._poll_interval())
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
                    if not self.controller.status_trusted:
                        log.debug(
                            f"[{self.speaker.name}] HA 未见过这条流在播，"
                            f"忽略它的 idle"
                        )
                        continue
                    log.info(f"[{self.speaker.name}] 音箱已被外部停止，取消自动切歌")
                    self._cancel_timer()
                    self._seq += 1
                    self.state = "idle"
                elif status == MI_STATUS_PAUSED and self.state == "playing":
                    log.info(f"[{self.speaker.name}] 音箱已被外部暂停，暂停队列计时")
                    self._cancel_timer()
                    self._seq += 1
                    # 用音箱上报的位置作为续播点（HA 不给进度，退回本地计时）
                    self._paused_pos = float(
                        st.get("position", 0) or self.elapsed()
                    )
                    self._user_paused = False
                    self.started_at = 0.0
                    self.state = "paused"
                elif status == MI_STATUS_PLAYING and self.state == "paused":
                    # mibox 自己刚按下暂停时，指令还在路上，此时音箱上报
                    # PLAYING 属于延迟，不能当成"外部恢复播放"
                    if self._user_paused and (
                        time.time() - self._pause_at < self._pause_grace()
                    ):
                        continue
                    # HA 不提供进度，用它拿不到就退回按下暂停时记的位置
                    pos = int(st.get("position", 0) or self._paused_pos)
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
