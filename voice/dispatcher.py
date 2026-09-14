"""语音指令分发

先匹配内置音乐指令，未命中再交给 HA 桥接（若启用）。
"""

from __future__ import annotations

import logging

from ..core.const import PLAY_MODE_NORMAL
from ..core.library import MusicLibrary
from ..core.player import Player
from .music_cmds import match_music_command

log = logging.getLogger("mibox")


class CommandDispatcher:
    def __init__(self, config, players: dict[str, Player], library: MusicLibrary,
                 ha_bridge=None):
        self.config = config
        self.players = players
        self.library = library
        self.ha_bridge = ha_bridge

    def _player_for(self, did: str) -> Player | None:
        p = self.players.get(did)
        if p is not None:
            return p
        return next(iter(self.players.values()), None)

    async def handle(self, did: str, query: str):
        query = query.strip()
        if not query:
            return

        player = self._player_for(did)
        if player is None:
            log.warning("没有可用音箱，忽略语音指令")
            return

        hit = match_music_command(query)
        if hit:
            action, params = hit
            log.info(f"[{player.speaker.name}] 语音: '{query}' -> {action} {params}")
            await self._run_music(player, action, params)
            return

        if self.ha_bridge is not None:
            ok = await self.ha_bridge.handle(query)
            if ok:
                log.info(f"[{player.speaker.name}] 语音: '{query}' -> HA 规则命中")
                return

        log.debug(f"未匹配任何指令: {query}")

    # ---------------- 音乐指令 ----------------
    async def _run_music(self, player: Player, action: str, params: dict):
        lib = self.library

        if action == "play_song":
            kw = params.get("keyword", "")
            songs = lib.search(kw, limit=20)
            if not songs:
                log.warning(f"未在本地音乐库找到: {kw}")
                return
            await player.play_songs(songs)
            return

        if action == "play_playlist":
            kw = params.get("keyword", "")
            songs = lib.by_playlist(kw)
            if not songs:
                log.warning(f"未找到歌单: {kw}")
                return
            await player.play_songs(songs)
            return

        if action == "play_favorites":
            songs = lib.favorite_songs()
            if not songs:
                log.warning("收藏列表为空")
                return
            await player.play_songs(songs)
            return

        if action == "play_index":
            idx = params.get("num", 1) - 1
            if 0 <= idx < len(player.queue):
                await player.play_items(player.queue, idx)
            return

        if action == "next":
            await player.next()
            return

        if action == "prev":
            await player.prev()
            return

        if action == "pause":
            await player.pause()
            return

        if action == "resume":
            await player.resume()
            return

        if action == "stop":
            await player.stop()
            return

        if action == "mode":
            player.set_mode(params.get("mode", PLAY_MODE_NORMAL))
            log.info(f"播放模式切换为 {player.mode}")
            return

        if action == "volume":
            await player.set_volume(params.get("num", self.config.default_volume))
            return

        if action == "fav_add":
            if player.cur_item:
                lib.add_favorite(player.cur_item.name)
                log.info(f"已收藏: {player.cur_item.name}")
            return

        if action == "fav_del":
            if player.cur_item:
                lib.remove_favorite(player.cur_item.name)
                log.info(f"已取消收藏: {player.cur_item.name}")
            return
