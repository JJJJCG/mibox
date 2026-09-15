"""音乐库：扫描、文件夹/歌手归类、歌单、模糊匹配、收藏

三种归类互不干扰，各自对应界面上的一个标签、也各有一条语音指令：

| 归类 | 来源 | 语音 |
|---|---|---|
| 文件夹 | 音乐目录下的子目录（层级）| 播放文件夹XX |
| 歌手 | 音频标签里的 artist，无标签时按"歌手 - 歌名"猜 | 播放歌手XX |
| 歌单 | 用户自定义（本文件维护）| 播放歌单XX |

持久化文件都在 conf/ 下：library.json（扫描缓存）、playlists.json（歌单）、
favorites.json（收藏）。重启时只对新文件或内容有变动的文件重新读取元数据。
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .const import MUSIC_EXTENSIONS

log = logging.getLogger("mibox")

# 没有歌手标签时的归类名（聚合与匹配都用它）
UNKNOWN_ARTIST = "未知歌手"
# "全部" 这类通配名，语音里说"播放文件夹全部"就播整个曲库
ALL_NAMES = ("全部", "所有", "all", "")

# 从文件名猜歌手的辅助正则："01. 周杰伦 - 晴天" -> 周杰伦
_LEADING_TRACK = re.compile(r"^\s*\d{1,3}\s*[.\-_、)）]\s*")
_NAME_SPLIT = re.compile(r"\s*[-—–－]\s*")

# 口播里常见的冗余后缀，"周杰伦的歌" -> "周杰伦"
_NOISE_SUFFIX = ("的歌曲", "的音乐", "的歌名", "的歌", "歌曲", "歌名")


def _strip_noise(kw: str) -> str:
    out = (kw or "").strip()
    for suf in _NOISE_SUFFIX:
        if out.endswith(suf) and len(out) > len(suf):
            out = out[: -len(suf)].strip()
            break
    return out


def guess_artist(stem: str) -> str:
    """无标签时从文件名猜歌手：形如 "歌手 - 歌名" 才认，否则返回空

    只是"聊胜于无"的兜底，猜错也只是归错组，不会影响播放——所以宁可
    保守：太长的、带括号的、没有分隔符的都不猜。
    """
    name = _LEADING_TRACK.sub("", (stem or "").strip())
    parts = _NAME_SPLIT.split(name, 1)
    if len(parts) != 2:
        return ""
    artist = parts[0].strip()
    title = parts[1].strip()
    if not artist or not title or len(artist) > 24 or "(" in artist or "（" in artist:
        return ""
    return artist


@dataclass
class Song:
    name: str      # 显示名（文件名去扩展名）
    path: str      # 绝对路径
    rel: str       # 相对 music_path
    folder: str    # 所属子目录，作为"文件夹"归类
    artist: str = ""   # 歌手标签（可能为猜测值，空表示未知）
    album: str = ""
    duration: int = 0
    mtime: int = 0  # 用于判断缓存是否失效
    size: int = 0

    def to_dict(self):
        return {
            "name": self.name,
            "rel": self.rel,
            "folder": self.folder,
            "artist": self.artist or UNKNOWN_ARTIST,
            "album": self.album,
            "duration": self.duration,
        }


class MusicLibrary:
    def __init__(self, config: Config, media=None):
        self.config = config
        self.media = media
        self.songs: list[Song] = []
        self._name_index: dict[str, Song] = {}
        self._rel_index: dict[str, Song] = {}
        self.favorites: list[str] = []
        # 歌单名 -> [rel, ...]；不用 playlists 命名（那是查询方法名）
        self._playlists: dict[str, list[str]] = {}
        self._fav_file = os.path.join(config.conf_path, "favorites.json")
        self._playlist_file = os.path.join(config.conf_path, "playlists.json")
        self._cache_file = os.path.join(config.conf_path, "library.json")
        self._load_favorites()
        self._load_playlists()

    # ---------------- 扫描 ----------------
    def _load_cache(self) -> dict:
        """读取上次的扫描结果：rel -> {duration, artist, album, mtime, size}"""
        if not os.path.exists(self._cache_file):
            return {}
        try:
            with open(self._cache_file, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            log.debug(f"曲库缓存读取失败，将全量扫描: {e}")
            return {}

    def _save_cache(self):
        os.makedirs(self.config.conf_path, exist_ok=True)
        data = {
            s.rel: {
                "duration": s.duration,
                "artist": s.artist,
                "album": s.album,
                "mtime": s.mtime,
                "size": s.size,
            }
            for s in self.songs
        }
        try:
            with open(self._cache_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except OSError as e:
            log.debug(f"曲库缓存写入失败: {e}")

    def scan(self) -> int:
        """扫描音乐目录（纯同步 IO，可放线程池执行，勿再加 async）"""
        root = self.config.music_path
        if not os.path.isdir(root):
            log.warning(f"音乐目录不存在: {root}")
            return 0

        cache = self._load_cache()
        songs: list[Song] = []
        reused = 0

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in sorted(filenames):
                ext = os.path.splitext(fn)[1].lower()
                if ext not in MUSIC_EXTENSIONS:
                    continue
                full = os.path.join(dirpath, fn)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, root).replace("\\", "/")
                folder = os.path.dirname(rel) or "全部"
                stem = os.path.splitext(fn)[0]

                mtime, size = int(st.st_mtime), st.st_size
                hit = cache.get(rel)
                if hit and hit.get("mtime") == mtime and hit.get("size") == size:
                    duration = int(hit.get("duration") or 0)
                    artist = str(hit.get("artist") or "")
                    album = str(hit.get("album") or "")
                    reused += 1
                else:
                    # 交给 attach_meta 后台补读；先按文件名猜一个歌手垫着
                    duration, artist, album = 0, guess_artist(stem), ""

                songs.append(
                    Song(
                        name=stem,
                        path=full,
                        rel=rel,
                        folder=folder,
                        artist=artist,
                        album=album,
                        duration=duration,
                        mtime=mtime,
                        size=size,
                    )
                )

        self.songs = songs
        self._name_index = {s.name: s for s in songs}
        self._rel_index = {s.rel: s for s in songs}
        log.info(
            f"音乐库扫描完成：{len(songs)} 首"
            f"（复用缓存 {reused} 首，待补读元数据 {len(songs) - reused} 首）"
        )
        return len(songs)

    def attach_meta(self):
        """给元数据缺失的曲目补读时长与歌手标签，读完后立刻落盘缓存

        缺"时长"或"歌手"都要补：老版本的 library.json 里没有 artist 字段，
        升级后第一次启动会借这里把标签补齐（时长仍走缓存，不重复读）。
        """
        if self.media is None:
            return
        pending = [s for s in self.songs if s.duration <= 0 or not s.artist]
        if not pending:
            return
        for s in pending:
            meta = self.media.get_meta(s.path)
            if s.duration <= 0:
                s.duration = int(meta.get("duration") or 0)
            # 标签里的歌手优先于文件名猜出来的
            if meta.get("artist"):
                s.artist = meta["artist"]
            if meta.get("album"):
                s.album = meta["album"]
            if not s.artist:
                s.artist = guess_artist(s.name)
        self._save_cache()
        log.info(f"已补读 {len(pending)} 首曲目的元数据并写入缓存")

    # 旧名保留：外部脚本/旧代码可能还在调
    def attach_durations(self):
        self.attach_meta()

    # ---------------- 查询：三种归类 ----------------
    def all(self) -> list[Song]:
        return self.songs

    def resolve(self, key: str) -> Optional[Song]:
        """按相对路径或显示名找一首歌（界面传 rel，语音传歌名）"""
        if not key:
            return None
        return self._rel_index.get(key) or self._name_index.get(key)

    def get(self, name: str) -> Optional[Song]:
        return self._name_index.get(name)

    # ---- 文件夹（子目录）----
    def folders(self) -> dict[str, int]:
        """子目录 -> 曲目数，按名称排序"""
        result: dict[str, int] = {}
        for s in self.songs:
            result[s.folder] = result.get(s.folder, 0) + 1
        return dict(sorted(result.items(), key=lambda kv: kv[0]))

    def by_folder(self, folder: str) -> list[Song]:
        """按文件夹取歌：同名目录 + 它的子目录一起给（选父目录=整棵树）

        这里按 "key/" 前缀判断层级，而不是裸 startswith——否则选「流行」
        会把「流行音乐」也带上。
        """
        key = (folder or "").strip()
        if key in ALL_NAMES:
            return list(self.songs)
        prefix = key + "/"
        return [
            s for s in self.songs
            if s.folder == key or s.folder.startswith(prefix)
        ]

    # ---- 歌手 ----
    def _artist_of(self, s: Song) -> str:
        return s.artist or UNKNOWN_ARTIST

    def artists(self) -> dict[str, int]:
        """歌手 -> 曲目数，按数量倒序（数量相同的按名字）"""
        result: dict[str, int] = {}
        for s in self.songs:
            a = self._artist_of(s)
            result[a] = result.get(a, 0) + 1
        return dict(sorted(result.items(), key=lambda kv: (-kv[1], kv[0])))

    def by_artist(self, artist: str) -> list[Song]:
        """精确 -> 互相包含 -> 模糊，逐级放宽"""
        key = (artist or "").strip()
        if key in ALL_NAMES:
            return list(self.songs)
        if not key:
            return []

        exact = [s for s in self.songs if self._artist_of(s) == key]
        if exact:
            return exact

        part = [
            s for s in self.songs
            if key in self._artist_of(s) or self._artist_of(s) in key
        ]
        if part:
            return part

        names = list(self.artists().keys())
        close = difflib.get_close_matches(
            key, names, n=1, cutoff=self.config.fuzzy_match_cutoff
        )
        if close:
            return [s for s in self.songs if self._artist_of(s) == close[0]]
        return []

    # ---------------- 歌单（用户自定义） ----------------
    def _load_playlists(self):
        if not os.path.exists(self._playlist_file):
            return
        try:
            with open(self._playlist_file, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            log.warning("歌单文件解析失败，按空歌单处理")
            return
        if not isinstance(data, dict):
            return
        cleaned: dict[str, list[str]] = {}
        for name, items in data.items():
            if not isinstance(items, list):
                continue
            cleaned[str(name)] = [str(i) for i in items if str(i).strip()]
        self._playlists = cleaned

    def _save_playlists(self):
        os.makedirs(self.config.conf_path, exist_ok=True)
        try:
            with open(self._playlist_file, "w", encoding="utf-8") as f:
                json.dump(self._playlists, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.warning(f"歌单写入失败: {e}")

    @staticmethod
    def normalize_playlist_name(name: str) -> str:
        """歌单名会出现在 URL 路径里，斜杠与控制字符一律拒绝"""
        n = (name or "").strip()
        if not n:
            raise ValueError("歌单名不能为空")
        if len(n) > 64:
            raise ValueError("歌单名过长（最多 64 字）")
        if "/" in n or "\\" in n or any(ord(c) < 32 for c in n):
            raise ValueError("歌单名不能包含斜杠等特殊字符")
        return n

    def playlist_names(self) -> list[str]:
        return list(self._playlists.keys())

    def playlists(self) -> dict[str, int]:
        """歌单名 -> 曲目数（只统计在曲库里真实存在的）"""
        return {name: len(self.playlist_songs(name)) for name in self._playlists}

    def has_playlist(self, name: str) -> bool:
        return name in self._playlists

    def create_playlist(self, name: str) -> str:
        n = self.normalize_playlist_name(name)
        self._playlists.setdefault(n, [])
        self._save_playlists()
        return n

    def delete_playlist(self, name: str) -> bool:
        if name in self._playlists:
            del self._playlists[name]
            self._save_playlists()
            return True
        return False

    def playlist_songs(self, name: str) -> list[Song]:
        """按顺序取出歌单里的歌；找不到的条目跳过（不删，方便外置盘临时离线）"""
        out: list[Song] = []
        for key in self._playlists.get(name, []):
            s = self.resolve(key)
            if s is not None:
                out.append(s)
        return out

    def add_to_playlist(self, name: str, song_key: str) -> tuple[str, bool]:
        """把歌加进歌单；歌单不存在就顺手建。返回 (歌单名, 是否新增了条目)"""
        n = self.normalize_playlist_name(name)
        song = self.resolve(song_key)
        if song is None:
            raise ValueError(f"曲库里没有这首歌: {song_key}")
        items = self._playlists.setdefault(n, [])
        if song.rel in items:
            return n, False
        items.append(song.rel)
        self._save_playlists()
        return n, True

    def remove_from_playlist(self, name: str, song_key: str) -> bool:
        """移除条目时 rel 与歌名都试一下，兼容手写的歌单文件"""
        items = self._playlists.get(name)
        if not items:
            return False
        song = self.resolve(song_key)
        candidates = {song_key}
        if song is not None:
            candidates.update((song.rel, song.name))
        for c in list(candidates):
            if c in items:
                items.remove(c)
                # 清空后歌单本身保留（用户可能只是想清空，改个名还能用）
                self._save_playlists()
                return True
        return False

    def by_playlist(self, name: str) -> list[Song]:
        """播放歌单：用户歌单优先，其次按歌单名模糊，最后回落文件夹

        回落是为了兼容老习惯——这个项目早期"歌单"就是子目录，
        现在仍然允许用"播放歌单流行"点到文件夹 流行。
        """
        key = (name or "").strip()
        if not key:
            return []
        if key in ("收藏", "favorites", "喜爱"):
            return self.favorite_songs()
        if key in self._playlists:
            return self.playlist_songs(key)

        part = [k for k in self._playlists if key in k or k in key]
        if part:
            return self.playlist_songs(part[0])

        close = difflib.get_close_matches(
            key, list(self._playlists.keys()), n=1,
            cutoff=self.config.fuzzy_match_cutoff,
        )
        if close:
            return self.playlist_songs(close[0])

        return self.by_folder(key)

    # ---------------- 搜索 ----------------
    def search(self, keyword: str, limit: int = 5) -> list[Song]:
        """先精确包含（歌名/歌手），再模糊匹配歌名

        说话常有冗余后缀（"播放周杰伦的歌"），先把 的/歌 这类尾巴削掉再试一轮，
        免得整句拿去比对谁都对不上。
        """
        candidates = [k for k in (keyword.strip(), _strip_noise(keyword)) if k]
        if not candidates:
            return []
        for kw in candidates:
            exact = [s for s in self.songs if kw in s.name or kw in self._artist_of(s)]
            if exact:
                return exact[:limit]
        names = list(self._name_index.keys())
        close = difflib.get_close_matches(
            candidates[-1], names, n=limit, cutoff=self.config.fuzzy_match_cutoff
        )
        return [self._name_index[n] for n in close]

    # ---------------- 收藏 ----------------
    def _load_favorites(self):
        if os.path.exists(self._fav_file):
            try:
                with open(self._fav_file, encoding="utf-8") as f:
                    self.favorites = json.load(f)
            except (json.JSONDecodeError, OSError):
                self.favorites = []

    def _save_favorites(self):
        os.makedirs(self.config.conf_path, exist_ok=True)
        with open(self._fav_file, "w", encoding="utf-8") as f:
            json.dump(self.favorites, f, ensure_ascii=False, indent=2)

    def add_favorite(self, name: str) -> bool:
        if name not in self.favorites:
            self.favorites.append(name)
            self._save_favorites()
            return True
        return False

    def remove_favorite(self, name: str) -> bool:
        if name in self.favorites:
            self.favorites.remove(name)
            self._save_favorites()
            return True
        return False

    def favorite_songs(self) -> list[Song]:
        out: list[Song] = []
        for key in self.favorites:
            s = self.resolve(key)
            if s is not None:
                out.append(s)
        return out
