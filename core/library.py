"""音乐库：扫描、歌单（按目录）、模糊匹配、收藏"""

from __future__ import annotations

import difflib
import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Optional

from .config import Config
from .const import MUSIC_EXTENSIONS

log = logging.getLogger("mibox")


@dataclass
class Song:
    name: str      # 显示名（文件名去扩展名）
    path: str      # 绝对路径
    rel: str       # 相对 music_path
    folder: str    # 所属子目录，作为歌单名
    duration: int = 0

    def to_dict(self):
        return asdict(self)


class MusicLibrary:
    def __init__(self, config: Config, media=None):
        self.config = config
        self.media = media
        self.songs: list[Song] = []
        self._name_index: dict[str, Song] = {}
        self.favorites: list[str] = []
        self._fav_file = os.path.join(config.conf_path, "favorites.json")
        self._load_favorites()

    # ---------------- 扫描 ----------------
    async def scan(self) -> int:
        root = self.config.music_path
        if not os.path.isdir(root):
            log.warning(f"音乐目录不存在: {root}")
            return 0

        songs: list[Song] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in sorted(filenames):
                ext = os.path.splitext(fn)[1].lower()
                if ext not in MUSIC_EXTENSIONS:
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, root)
                folder = os.path.dirname(rel) or "全部"
                songs.append(
                    Song(
                        name=os.path.splitext(fn)[0],
                        path=full,
                        rel=rel.replace("\\", "/"),
                        folder=folder,
                    )
                )

        self.songs = songs
        self._name_index = {s.name: s for s in songs}
        log.info(f"音乐库扫描完成，共 {len(songs)} 首")
        return len(songs)

    def attach_durations(self):
        """批量补时长（ mutagen 读文件，数量大时较慢，仅按需调用）"""
        if self.media is None:
            return
        for s in self.songs:
            if s.duration <= 0:
                s.duration = self.media.get_duration(s.path)

    # ---------------- 查询 ----------------
    def all(self) -> list[Song]:
        return self.songs

    def playlists(self) -> dict[str, int]:
        """歌单名 -> 歌曲数"""
        result: dict[str, int] = {}
        for s in self.songs:
            result[s.folder] = result.get(s.folder, 0) + 1
        return result

    def by_playlist(self, folder: str) -> list[Song]:
        if folder in ("全部", "all", ""):
            return self.songs
        return [s for s in self.songs if s.folder == folder or s.folder.startswith(folder)]

    def get(self, name: str) -> Optional[Song]:
        return self._name_index.get(name)

    def search(self, keyword: str, limit: int = 5) -> list[Song]:
        """先精确包含，再模糊匹配"""
        kw = keyword.strip()
        if not kw:
            return []
        exact = [s for s in self.songs if kw in s.name]
        if exact:
            return exact[:limit]
        names = list(self._name_index.keys())
        close = difflib.get_close_matches(
            kw, names, n=limit, cutoff=self.config.fuzzy_match_cutoff
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
        return [self._name_index[n] for n in self.favorites if n in self._name_index]
