"""语音指令分发

先匹配内置音乐指令，未命中再交给 AI 桥接（关键词命中就转发给外部接口）。
"""

from __future__ import annotations

import logging

from core.const import PLAY_MODE_NORMAL
from core.library import MusicLibrary
from core.player import Player
from .cn_num import cn_to_arabic
from .music_cmds import match_music_command

log = logging.getLogger("mibox")

# 口播里可能混进搜索词的噪音前缀，搜索前剥掉
_NOISE_PREFIX = ("本地音乐", "本地歌曲", "本地", "歌曲", "音乐")


def _clean_keyword(kw: str) -> str:
    kw = (kw or "").strip()
    changed = True
    while changed and kw:
        changed = False
        for n in _NOISE_PREFIX:
            if kw.startswith(n):
                kw = kw[len(n):].strip()
                changed = True
    return kw


class CommandDispatcher:
    def __init__(self, config, players: dict[str, Player], library: MusicLibrary,
                 ai_bridge=None):
        self.config = config
        self.players = players
        self.library = library
        self.ai_bridge = ai_bridge

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

        # 原句未命中时，用中文数字转换后的句子再试一轮
        # （ASR 常把"二十六度"识别成中文数字，而规则里的 \d 只认阿拉伯数字）
        candidates = [query]
        cn = cn_to_arabic(query)
        if cn != query:
            candidates.append(cn)

        for cand in candidates:
            hit = match_music_command(cand)
            if hit:
                action, params = hit
                log.info(f"[{player.speaker.name}] 语音: '{query}' -> {action} {params}")
                await self._run_music(player, action, params)
                return

        # 音乐指令未命中 -> AI 桥接（原句匹配关键词，不做中文数字转换，
        # 免得把要问的话本身改写掉）
        if self.ai_bridge is not None:
            ok = await self.ai_bridge.handle(query)
            if ok:
                log.info(f"[{player.speaker.name}] 语音: '{query}' -> AI 桥接")
                return

        log.debug(f"未匹配任何指令: {query}")

    # ---------------- 音乐指令 ----------------
    async def _play(self, player: Player, songs, params: dict):
        """点歌统一入口：本次随机还是顺序，由指令里的"随机"字样决定

        `shuffle` 为 None 表示指令没提模式（走到这里的语音指令一般都会带上），
        那就沿用当前模式。
        """
        await player.play_songs(songs, shuffle=params.get("shuffle"))
        log.info(
            f"[{player.speaker.name}] 本次点歌 {len(songs)} 首，播放模式 {player.mode}"
        )

    async def _run_music(self, player: Player, action: str, params: dict):
        lib = self.library

        if action == "play_song":
            kw = _clean_keyword(params.get("keyword", ""))
            if not kw:
                log.warning("语音里没有可搜索的歌名")
                return
            songs = lib.search(kw, limit=20)
            if not songs:
                log.warning(f"未在本地音乐库找到: {kw}")
                return
            await self._play(player, songs, params)
            return

        if action == "play_folder":
            kw = (params.get("keyword", "") or "").strip()
            songs = lib.by_folder(kw)
            if not songs:
                log.warning(f"未找到文件夹: {kw}")
                return
            await self._play(player, songs, params)
            return

        if action == "play_artist":
            kw = (params.get("keyword", "") or "").strip()
            songs = lib.by_artist(kw)
            if not songs:
                log.warning(f"未找到歌手: {kw}")
                return
            await self._play(player, songs, params)
            return

        if action == "play_playlist":
            kw = params.get("keyword", "")
            songs = lib.by_playlist(kw)
            if not songs:
                log.warning(f"未找到歌单: {kw}")
                return
            await self._play(player, songs, params)
            return

        if action == "play_favorites":
            songs = lib.favorite_songs()
            if not songs:
                log.warning("收藏列表为空")
                return
            await self._play(player, songs, params)
            return

        if action == "play_index":
            idx = params.get("num", 1) - 1
            if 0 <= idx < len(player.queue):
                # remember=False：跳到队列内某一首，别覆盖原始顺序
                await player.play_items(player.queue, idx, remember=False)
            return

        if action == "next":
            await player.next()
            return

        if action == "prev":
            await player.prev()
            return

        if action == "mode":
            mode = params.get("mode", PLAY_MODE_NORMAL)
            player.set_mode(mode)
            # 切到随机是立刻打乱当前队列剩下的部分，切回顺序则还原原顺序
            log.info(f"播放模式切换为 {player.mode}")
            return

        if action == "volume":
            await player.set_volume(params.get("num", self.config.default_volume))
            return

        if action == "playlist_add":
            name = (params.get("keyword", "") or "").strip()
            if not name:
                log.warning("语音里没有歌单名")
                return
            if not player.cur_item or not player.cur_item.song:
                log.warning("当前没有在播的本地歌曲，无法加入歌单")
                return
            song = player.cur_item.song
            try:
                real, added = lib.add_to_playlist(name, song.rel)
            except ValueError as e:
                log.warning(f"加入歌单失败: {e}")
                return
            log.info(
                f"{'已加入' if added else '已在'}歌单「{real}」: {song.name}"
            )
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
