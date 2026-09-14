"""DLNA 渲染器状态机（每台音箱一个）

只维护 UPnP 层面的状态，真正干活的动作通过回调委托给 DLNAServer，
从而与播放队列引擎解耦。
"""

from __future__ import annotations

import logging
import time

from ..core.const import (
    PLAY_MODE_NORMAL,
    TRANSPORT_STATE_NO_MEDIA,
    TRANSPORT_STATE_PAUSED,
    TRANSPORT_STATE_PLAYING,
    TRANSPORT_STATE_STOPPED,
    TRANSPORT_STATE_TRANSITIONING,
    TRANSPORT_STATUS_OK,
)
from ..core.player import Player

log = logging.getLogger("mibox")


def fmt_time(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_time(value: str) -> int:
    """把 '00:01:30' 或 '90' 解析成秒"""
    if not value:
        return 0
    parts = value.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(float(parts[2]))
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(float(parts[1]))
        return int(float(value))
    except ValueError:
        return 0


class DLNARenderer:
    """单台音箱对应的 DLNA 渲染器"""

    def __init__(self, udn: str, name: str, player: Player):
        self.udn = udn
        self.name = name
        self.player = player

        self.transport_state = TRANSPORT_STATE_NO_MEDIA
        self.transport_status = TRANSPORT_STATUS_OK
        self.play_mode = PLAY_MODE_NORMAL

        self.uri = ""
        self.uri_metadata = ""
        self.next_uri = ""
        self.next_uri_metadata = ""

        self.duration = 0        # 秒
        self.track_started = 0.0  # 本轨开始播放的时间戳
        self.offset = 0          # seek 偏移（秒）

        self.volume = player.volume
        self.mute = False
        self.uri_token = ""     # 当前 URI 对应的缓冲 token

        # 由 DLNAServer 注入
        self.on_set_uri = None    # async (renderer, uri, meta) -> None
        self.on_play = None       # async (renderer) -> bool
        self.on_pause = None      # async (renderer) -> None
        self.on_stop = None       # async (renderer) -> None
        self.on_seek = None       # async (renderer, offset) -> bool
        self.on_volume = None     # async (renderer, vol) -> None

    # ---------------- 状态 ----------------
    def position(self) -> int:
        if self.transport_state == TRANSPORT_STATE_PLAYING and self.track_started:
            return int(time.time() - self.track_started) + self.offset
        return self.offset

    def is_playing(self) -> bool:
        return self.transport_state == TRANSPORT_STATE_PLAYING

    # ---------------- 动作 ----------------
    async def set_uri(self, uri: str, metadata: str = ""):
        self.uri = uri
        self.uri_metadata = metadata
        self.offset = 0
        self.duration = 0
        self.transport_state = TRANSPORT_STATE_STOPPED
        if self.on_set_uri:
            await self.on_set_uri(self, uri, metadata)

    async def play(self, speed: str = "1"):
        if not self.uri:
            log.warning(f"[{self.name}] 无 URI，忽略 Play")
            return False
        self.transport_state = TRANSPORT_STATE_TRANSITIONING
        ok = await self.on_play(self) if self.on_play else False
        if ok:
            self.transport_state = TRANSPORT_STATE_PLAYING
            self.track_started = time.time()
        else:
            self.transport_state = TRANSPORT_STATE_STOPPED
        return ok

    async def pause(self):
        if self.transport_state != TRANSPORT_STATE_PLAYING:
            return
        if self.on_pause:
            await self.on_pause(self)
        self.transport_state = TRANSPORT_STATE_PAUSED

    async def stop(self):
        if self.on_stop:
            await self.on_stop(self)
        self.transport_state = TRANSPORT_STATE_STOPPED
        self.offset = 0
        self.track_started = 0.0

    async def seek(self, unit: str, target: str) -> bool:
        if unit not in ("REL_TIME", "TRACK_NR"):
            return False
        offset = parse_time(target)
        if self.on_seek:
            ok = await self.on_seek(self, offset)
            if ok:
                self.offset = offset
                self.track_started = time.time()
                self.transport_state = TRANSPORT_STATE_PLAYING
            return ok
        return False

    async def set_volume(self, volume: int):
        volume = max(0, min(100, volume))
        self.volume = volume
        if self.on_volume:
            await self.on_volume(self, volume)

    async def set_play_mode(self, mode: str):
        self.play_mode = mode
        self.player.set_mode(mode)

    # ---------------- 查询 ----------------
    def transport_info(self) -> dict:
        return {
            "CurrentTransportState": self.transport_state,
            "CurrentTransportStatus": self.transport_status,
            "CurrentSpeed": "1",
        }

    def position_info(self) -> dict:
        meta = self.uri_metadata or self._default_metadata()
        return {
            "Track": "1",
            "TrackDuration": fmt_time(self.duration),
            "TrackMetaData": meta,
            "TrackURI": self.uri,
            "RelTime": fmt_time(self.position()),
            "AbsTime": fmt_time(self.position()),
            "RelCount": "2147483647",
            "AbsCount": "2147483647",
        }

    def media_info(self) -> dict:
        return {
            "NrTracks": "1",
            "MediaDuration": fmt_time(self.duration),
            "CurrentURI": self.uri,
            "CurrentURIMetaData": self.uri_metadata,
            "NextURI": self.next_uri,
            "NextURIMetaData": self.next_uri_metadata,
            "PlayMedium": "NETWORK",
            "RecordMedium": "NOT_IMPLEMENTED",
            "WriteStatus": "NOT_IMPLEMENTED",
        }

    def _default_metadata(self) -> str:
        return (
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="0" parentID="-1" restricted="1">'
            f"<dc:title>{self.name}</dc:title>"
            '<upnp:class>object.item.audioItem.musicTrack</upnp:class>'
            "</item></DIDL-Lite>"
        )
