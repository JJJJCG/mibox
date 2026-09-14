"""内置音乐语音指令

纯正则匹配，不做任何 AI 兜底。长指令排前面，避免"停止播放"只命中"播放"。
"""

from __future__ import annotations

import re

# (正则, 动作, 说明)
# 动作：
#   play_song     需要 <keyword>  搜索并播放
#   play_playlist 需要 <keyword>  播放歌单（子目录）
#   play_favorites              播放收藏
#   play_index    需要 <num>     播放当前列表第 N 首
#   next / prev / pause / resume / stop
#   mode          需要 <mode>
#   volume        需要 <num>
#   fav_add / fav_del
MUSIC_PATTERNS: list[tuple[str, str, dict]] = [
    # "播放本地音乐XX / 播放本地XX / 播放音乐XX" 先于通用 "播放XX" 匹配，
    # 避免"本地(音乐)"混进搜索词
    (r"^播放本地音乐(?P<keyword>.+)$", "play_song", {}),
    (r"^播放本地歌曲(?P<keyword>.+)$", "play_song", {}),
    (r"^播放本地(?P<keyword>.+)$", "play_song", {}),
    (r"^播放音乐(?P<keyword>.+)$", "play_song", {}),
    (r"^播放歌曲(?P<keyword>.+)$", "play_song", {}),
    (r"^播放歌单(?P<keyword>.+)$", "play_playlist", {}),
    (r"^播放收藏$", "play_favorites", {}),
    (r"^播放第(?P<num>\d+)个?$", "play_index", {}),
    (r"^播放(?P<keyword>.+)$", "play_song", {}),
    (r"^下一首$", "next", {}),
    (r"^下一曲$", "next", {}),
    (r"^上一首$", "prev", {}),
    (r"^上一曲$", "prev", {}),
    (r"^单曲循环$", "mode", {"mode": "REPEAT_ONE"}),
    (r"^全部循环$", "mode", {"mode": "REPEAT_ALL"}),
    (r"^列表循环$", "mode", {"mode": "REPEAT_ALL"}),
    (r"^随机播放$", "mode", {"mode": "SHUFFLE"}),
    (r"^顺序播放$", "mode", {"mode": "NORMAL"}),
    (r"^加入收藏$", "fav_add", {}),
    (r"^取消收藏$", "fav_del", {}),
    (r"^音量(调到|设置为|调成)?(?P<num>\d+)$", "volume", {}),
    (r"^把?音量(调到|设置为|调成)?(?P<num>\d+)$", "volume", {}),
]

_COMPILED = [(re.compile(p), a, extra) for p, a, extra in MUSIC_PATTERNS]


def match_music_command(query: str) -> tuple[str, dict] | None:
    """返回 (动作, 参数)；未命中返回 None"""
    q = query.strip()
    if not q:
        return None
    for regex, action, extra in _COMPILED:
        m = regex.search(q)
        if not m:
            continue
        params = dict(extra)
        gd = m.groupdict()
        if "keyword" in gd and gd["keyword"]:
            params["keyword"] = gd["keyword"].strip()
        if "num" in gd and gd["num"]:
            try:
                params["num"] = int(gd["num"])
            except ValueError:
                pass
        return action, params
    return None
