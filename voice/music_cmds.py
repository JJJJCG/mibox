"""内置音乐语音指令

纯正则匹配，不做任何 AI 兜底。长指令排前面，避免"停止播放"只命中"播放"。

**随机还是顺序，由这一次的指令说了算。** 播放类指令里提到"随机 / 随便 /
打乱"就随机播，没提就顺序播——模式跟着点歌走，不沿用上一次切换的结果。
否则说过一次"随机播放"之后，后面每句"播放周杰伦"都会莫名其妙地随机。
"""

from __future__ import annotations

import re

from core.const import PLAY_MODE_NORMAL, PLAY_MODE_SHUFFLE

# (正则, 动作, 说明)
# 动作：
#   play_song     需要 <keyword>  搜索并播放
#   play_folder   需要 <keyword>  播放文件夹（音乐目录下的子目录）
#   play_artist   需要 <keyword>  播放某歌手的全部曲目
#   play_playlist 需要 <keyword>  播放歌单（用户自定义，找不到时回落文件夹）
#   play_favorites              播放收藏
#   play_index    需要 <num>     播放当前列表第 N 首
#   playlist_add  需要 <keyword> 把当前播放的歌加入某歌单
#   next / prev / pause / resume / stop
#   mode          需要 <mode>
#   volume        需要 <num>
#   fav_add / fav_del
#
# 播放类动作最终会由 match_music_command 补一个 `shuffle` 参数：
# 指令里提了"随机/随便/打乱"是 True，否则 False（顺序播放）。
#
# 注意顺序：带前缀的指令（文件夹/歌手/歌单）必须排在通用 "^播放(.+)$" 之前，
# 否则会被它整句吃掉当成歌名。
MUSIC_PATTERNS: list[tuple[str, str, dict]] = [
    # "播放本地音乐XX / 播放本地XX / 播放音乐XX" 先于通用 "播放XX" 匹配，
    # 避免"本地(音乐)"混进搜索词
    (r"^播放本地音乐(?P<keyword>.+)$", "play_song", {}),
    (r"^播放本地歌曲(?P<keyword>.+)$", "play_song", {}),
    (r"^播放本地(?P<keyword>.+)$", "play_song", {}),
    (r"^播放音乐(?P<keyword>.+)$", "play_song", {}),
    (r"^播放歌曲(?P<keyword>.+)$", "play_song", {}),
    # 三种归类各一条：文件夹 / 歌手 / 歌单
    (r"^播放文件夹(?:的)?(?P<keyword>.+)$", "play_folder", {}),
    (r"^播放目录(?:的)?(?P<keyword>.+)$", "play_folder", {}),
    (r"^播放歌手(?:的)?(?P<keyword>.+)$", "play_artist", {}),
    (r"^播放歌单(?:的)?(?P<keyword>.+)$", "play_playlist", {}),
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
    # "随机播放 / 顺序播放" 不在这里：它们由 match_music_command 的模式词
    # 剥离统一处理（带不带点歌都能说），剥完是"播放"，走一个 mode 分支。
    # 把当前这首歌收进歌单（歌单不存在会自动新建）
    (r"^(?:把)?(?:这首歌|这首|当前的?歌|当前歌曲)?(?:加入|添加到?|放进|收进)歌单(?P<keyword>.+)$",
     "playlist_add", {}),
    (r"^加入收藏$", "fav_add", {}),
    (r"^取消收藏$", "fav_del", {}),
    (r"^音量(调到|设置为|调成)?(?P<num>\d+)$", "volume", {}),
    (r"^把?音量(调到|设置为|调成)?(?P<num>\d+)$", "volume", {}),
]

_COMPILED = [(re.compile(p), a, extra) for p, a, extra in MUSIC_PATTERNS]

# ---------------- 随机 / 顺序 ----------------
# 句首句尾出现这些词就先剥掉，剩下的句子照常匹配，剥出来的词决定这次怎么放。
# 只放最短的核心词在前：`随机播放音乐周杰伦` 剥完是 `播放音乐周杰伦`，正好命中
# 普通播放规则（要是连"播放"一起剥，就变成"音乐周杰伦"这种搜不到的词了）。
_SHUFFLE_WORDS = (
    "随机", "随便", "打乱", "乱序", "shuffle",
    # 句尾写法：`播放周杰伦随机播放`
    "随机播放", "随便播放", "打乱顺序",
)
_ORDER_WORDS = ("按顺序", "顺序", "依次")

# 句首的称呼/连接词：`帮我随机播放周杰伦`
_LEAD_FILLER = re.compile(r"^(?:请|帮我|给我|麻烦|那|就|现在|马上|立刻|再)+")
# 剥模式词时顺手丢掉的分隔符
_SEP = "，,。.、！!？? 　\t"

# "来一首 / 放两首 / 播放几首歌" 里的量词与尾巴，长的排前面
_FILLERS = (
    "一下|一点|一些|几首|几曲|听听|本地音乐|本地|歌儿|歌曲|音乐"
    "|些|点|下|首|曲|歌|一|两|几"
)
# 整句只剩下这些词 = 纯粹在切模式（`随机播放` 就是把当前队列打乱）
_BARE_PLAY_RE = re.compile(rf"^(?:再|请|帮我|给我|来)?(?:播放|放|播|听|来)?(?:{_FILLERS})*$")
# 随机兜底时把"放点 / 来一首 / 播放"这类空动词剥掉，剩下的才是搜索词
_PLAY_VERB_RE = re.compile(rf"^(?:播放|放|播|听|来)(?:{_FILLERS})*")


def _trim_words(text: str, words: tuple[str, ...]) -> tuple[str, bool]:
    """剥掉句首/句尾的模式词，返回 (剩下的句子, 有没有剥到)"""
    hit = False
    for _ in range(3):
        changed = False
        for w in words:
            if text.startswith(w):
                text = text[len(w):].strip(_SEP)
                changed = hit = True
                break
            if text.endswith(w):
                text = text[: -len(w)].strip(_SEP)
                changed = hit = True
                break
        if not changed:
            break
    return text, hit


def _strip_play_filler(text: str) -> str:
    """`放点周杰伦` -> `周杰伦`"""
    s = text.strip(_SEP)
    for _ in range(2):
        m = _PLAY_VERB_RE.match(s)
        if not m or m.end() == 0:
            break
        s = s[m.end():].strip(_SEP)
    return s


def _match_one(q: str) -> tuple[str, dict] | None:
    """在单句上跑一遍规则表"""
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


def match_music_command(query: str) -> tuple[str, dict] | None:
    """返回 (动作, 参数)；未命中返回 None

    播放类动作会带上 `shuffle`：指令里说了"随机"就是 True，否则 False
    （明确要求"顺序播放"也是 False）。动作是 `mode` 时表示只切当前队列
    的模式，不动队列内容。
    """
    q = query.strip()
    if not q:
        return None

    core = _LEAD_FILLER.sub("", q).strip(_SEP) or q
    core, shuffled = _trim_words(core, _SHUFFLE_WORDS)
    core, ordered = _trim_words(core, _ORDER_WORDS)
    # 两个都提到时以"随机"为准
    intent = True if shuffled else (False if ordered else None)

    if intent is not None and _BARE_PLAY_RE.match(core):
        # `随机播放` / `随机来一首`：只把当前队列切个模式
        return "mode", {
            "mode": PLAY_MODE_SHUFFLE if intent else PLAY_MODE_NORMAL
        }

    hit = _match_one(core)
    if hit:
        action, params = hit
        if action.startswith("play_"):
            # 提了"随机"就随机，没提就是顺序——本次点歌说了算
            params["shuffle"] = bool(intent)
        return action, params

    if intent is None:
        return None

    # 没命中任何规则：`随机放点周杰伦` / `按顺序放点周杰伦` 这种确实在点歌的
    # 说法，把空动词剥掉后剩下的当歌名搜。句中不含点歌动作的（`随便说点什么`）
    # 直接放行走 AI 桥接，别把问句当成歌名吃掉。
    if _PLAY_VERB_RE.match(core):
        kw = _strip_play_filler(core)
        if kw:
            return "play_song", {"keyword": kw, "shuffle": bool(intent)}
    return None
