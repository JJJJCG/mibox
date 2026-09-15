"""本机播放（浏览器）输出通道

与真实音箱并列的一种"输出设备"：队列、播放模式、歌单这些都完全复用
`Player`，区别只有两点：

1. **不往音箱投递 URL**，只把站内相对地址 `/music/xxx` 交给打开页面的
   浏览器，声音从"打开这个页面的设备"出来；
2. **不排自动切歌定时器**，等浏览器播完回报 `ended` 再推进队列。这样
   暂停、拖动进度、网络卡顿都不会让服务端提前切下一首。

于是同一套服务同时支撑两种用法：

- 对小爱说"播放周杰伦" —— 语音链路只认真实音箱，走小米 API 投递；
- 在浏览器里点播 —— 默认选中本机播放，声音从当前设备出来。

这才是"音乐服务器"该有的样子：语音入口和浏览器入口各走各的通道。
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

from .config import Config, Speaker
from .const import LOCAL_DID, LOCAL_NAME
from .media import MediaService
from .player import Player

log = logging.getLogger("mibox")


def make_local_speaker() -> Speaker:
    """本机播放的"伪音箱"，只为复用 Player 里的音箱字段"""
    sp = Speaker(did=LOCAL_DID, name=LOCAL_NAME, hardware="browser", enabled=True)
    sp.ensure_udn()
    return sp


class LocalController:
    """本机播放的控制层：真正的出声在浏览器，服务端这一层只记账

    所有控制调用一律"成功"，这样 Player 的暂停/继续/停止/音量流程可以原样
    复用，不用在播放器里到处写 `if is_local`。
    """

    def __init__(self, speaker: Speaker):
        self.speaker = speaker
        self.ha = None
        self._volume = 0

    @property
    def ha_active(self) -> bool:
        return False

    @property
    def status_trusted(self) -> bool:
        return True

    async def play_url(self, url: str, continue_play: bool = True) -> bool:
        return bool(url)

    async def pause(self, url: str = "") -> bool:
        return True

    async def resume(self, url: str = "") -> bool:
        return True

    async def stop(self, url: str = "") -> bool:
        return True

    def kill(self, url: str = ""):
        """本机播放绝不能掐流——浏览器还在拉这个文件"""

    def release(self, url: str = ""):
        pass

    async def set_volume(self, volume: int) -> bool:
        self._volume = volume
        return True

    async def get_volume(self) -> int:
        return self._volume

    async def get_status(self) -> dict:
        return {
            "status": 1,          # 浏览器自己管状态，这里给"在播"免得被判停
            "volume": self._volume,
            "duration": 0,
            "position": 0,
        }


class LocalPlayer(Player):
    """在当前设备上出声的播放器"""

    is_local = True

    def __init__(self, config: Config, media: MediaService):
        speaker = make_local_speaker()
        super().__init__(speaker, LocalController(speaker), config, media)

    # ---------------- 地址 ----------------
    async def _resolve(self, item):
        """解析成浏览器能直接播的站内相对地址

        用相对路径而不是 `http://hostname:port/...`：不管用户是从局域网 IP、
        localhost 还是反代域名打开页面，音频都从同一个来源拉，不会因为
        配置里的 hostname 和实际访问地址不一致而播不出来。
        """
        song = item.song
        if song is None:
            if not item.duration:
                item.duration = 180
            return
        if not item.duration:
            item.duration = song.duration or self.media.get_duration(song.path)
        item.url = await self._browser_url(song)
        if not item.duration:
            item.duration = 180  # 兜底：拿不到时长也能进队列

    async def _browser_url(self, song) -> str:
        rel = getattr(song, "rel", "") or ""
        if not rel:
            return ""
        if not self.media.browser_playable(song.path):
            # ape/wma 之类浏览器解不了，复用转码缓存
            cache = await self.media.ensure_browser_playable(song.path)
            if cache:
                return "/cache/" + quote(os.path.basename(cache))
        return "/music/" + quote(rel)

    # ---------------- 切歌由浏览器驱动 ----------------
    def _schedule_next(self, wait: float | None = None):
        """不排定时器：等浏览器的 ended 事件

        音箱那套"按时长定时切歌"在这里不成立——浏览器可能暂停、拖动进度、
        缓冲，服务端算出来的时间点全是错的，交还给它自己报。
        """
        self._cancel_timer()

    def start_monitor(self):
        """不需要核对真实状态：本机播放不存在"被外部停止"这回事"""

    async def notify_ended(self) -> bool:
        """浏览器报告当前曲目播完 -> 推进队列

        返回是否真的切了（界面可据此决定要不要刷新）。
        """
        if self.state != "playing":
            return False
        self._seq += 1
        await self.next(auto=True)
        return True

    async def _play_current(self) -> bool:
        self.play_id += 1
        return await super()._play_current()
