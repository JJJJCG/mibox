"""音乐库：扫描、歌单（按目录）、模糊匹配、收藏

扫描结果与时长会持久化到 conf/library.json。
重启时只对新文件或内容有变动的文件重新读取时长，
避免每次启动都把整个曲库的音频元数据重读一遍。
"""

from __future__ import annotations

import difflib
import json
import logging
import os
from dataclasses import dataclass
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
    mtime: int = 0  # 用于判断缓存是否失效
    size: int = 0

    def to_dict(self):
        return {
            "name": self.name,
            "rel": self.rel,
            "folder": self.folder,
            "duration": self.duration,
        }


class MusicLibrary:
    def __init__(self, config: Config, media=None):
        self.config = config
        self.media = media
        self.songs: list[Song] = []
        self._name_index: dict[str, Song] = {}
        self.favorites: list[str] = []
        self._fav_file = os.path.join(config.conf_path, "favorites.json")
        self._cache_file = os.path.join(config.conf_path, "library.json")
        self._load_favorites()

    # ---------------- 扫描 ----------------
    def _load_cache(self) -> dict:
        """读取上次的扫描结果：rel -> {duration, mtime, size}"""
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
            s.rel: {"duration": s.duration, "mtime": s.mtime, "size": s.size}
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

                mtime, size = int(st.st_mtime), st.st_size
                hit = cache.get(rel)
                if (
                    hit
                    and hit.get("mtime") == mtime
                    and hit.get("size") == size
                    and hit.get("duration")
                ):
                    duration = int(hit["duration"])
                    reused += 1
                else:
                    duration = 0  # 交给 attach_durations 后台补读

                songs.append(
                    Song(
                        name=os.path.splitext(fn)[0],
                        path=full,
                        rel=rel,
                        folder=folder,
                        duration=duration,
                        mtime=mtime,
                        size=size,
                    )
                )

        self.songs = songs
        self._name_index = {s.name: s for s in songs}
        log.info(
            f"音乐库扫描完成：{len(songs)} 首"
            f"（复用缓存 {reused} 首，需补读时长 {len(songs) - reused} 首）"
        )
        return len(songs)

    def attach_durations(self):
        """只给时长缺失的曲目补读，读完后立刻落盘缓存"""
        if self.media is None:
            return
        pending = [s for s in self.songs if s.duration <= 0]
        if not pending:
            return
        for s in pending:
            s.duration = self.media.get_duration(s.path)
        self._save_cache()
        log.info(f"已补读 {len(pending)} 首曲目的时长并写入缓存")

    # ---------------- 查询 ----------------
    def all(self) -> list[Song]:
        return self.songs

    def playlists(self) -> dict[str, int]:
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
