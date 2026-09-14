"""媒体服务：URL 生成、时长探测、按需转码与缓存

音箱播放的都是 HTTP URL（由音箱自己拉取），因此本模块负责把本地文件
变成"能被音箱直接吃的 URL"，并在必要时先转码。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import time
from urllib.parse import quote

from .config import Config, Speaker
from .const import DIRECT_PLAY_FORMATS

log = logging.getLogger("mibox")


class MediaService:
    def __init__(self, config: Config):
        self.config = config
        os.makedirs(config.cache_dir, exist_ok=True)

    # ---------------- URL ----------------
    def music_url(self, rel_path: str) -> str:
        """本地音乐文件的对外 URL（rel_path 相对 music_path）"""
        return f"{self.config.base_url()}/music/{quote(rel_path)}"

    def proxy_url(self, token: str) -> str:
        """DLNA 推送内容的缓冲代理 URL"""
        return f"{self.config.base_url()}/proxy/{token}"

    # ---------------- 时长 ----------------
    def get_duration(self, abs_path: str) -> int:
        """返回秒，失败返回 0"""
        try:
            from mutagen import File

            f = File(abs_path)
            if f is not None and f.info is not None:
                return int(f.info.length)
        except Exception as e:
            log.debug(f"读取时长失败 {abs_path}: {e}")
        return 0

    # ---------------- 转码 ----------------
    def needs_conversion(self, speaker: Speaker, abs_path: str) -> bool:
        ext = os.path.splitext(abs_path)[1].lstrip(".").lower()
        if ext in DIRECT_PLAY_FORMATS:
            return False
        return speaker.needs_conversion(ext)

    async def ensure_playable(self, speaker: Speaker, abs_path: str) -> str:
        """返回可直接投递给音箱的 URL，必要时先转码。

        转码产物按 "路径 + 型号" 哈希缓存，同名文件不重复转。
        """
        if not self.needs_conversion(speaker, abs_path):
            rel = os.path.relpath(abs_path, self.config.music_path)
            return self.music_url(rel)

        cache = await self._transcode(abs_path)
        if cache is None:
            # 转码失败就退回原文件，让音箱自己决定能不能播
            rel = os.path.relpath(abs_path, self.config.music_path)
            return self.music_url(rel)
        return f"{self.config.base_url()}/cache/{os.path.basename(cache)}"

    def _cache_path(self, abs_path: str) -> str:
        key = f"{abs_path}|{os.path.getmtime(abs_path)}|{os.path.getsize(abs_path)}"
        digest = hashlib.md5(key.encode()).hexdigest()
        return os.path.join(self.config.cache_dir, f"{digest}.mp3")

    async def _transcode(self, abs_path: str) -> str | None:
        out = self._cache_path(abs_path)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out

        tmp = out + ".part"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", abs_path,
            "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", tmp,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                log.error(f"转码失败 {abs_path}: {stderr.decode(errors='ignore')[:300]}")
                return None
            os.replace(tmp, out)
            log.info(f"转码完成: {os.path.basename(abs_path)} -> {os.path.basename(out)}")
            return out
        except FileNotFoundError:
            log.error("未找到 ffmpeg，无法转码")
            return None
        except Exception as e:
            log.error(f"转码异常: {e}")
            return None
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass

    # ---------------- 缓冲代理（DLNA 用） ----------------
    def save_buffer(self, token: str, data: bytes) -> str:
        """把已下载完的音频写入缓存并返回本地路径"""
        path = os.path.join(self.config.cache_dir, f"buf_{token}.mp3")
        with open(path, "wb") as f:
            f.write(data)
        return path

    async def convert_buffer(self, src: str, fmt_hint: str = "") -> str:
        """把缓冲文件转成 mp3（音箱型号不支持原始格式时）"""
        out = src.rsplit(".", 1)[0] + "_c.mp3"
        if os.path.exists(out):
            return out
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", src,
               "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", out]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
            )
            _, err = await proc.communicate()
            if proc.returncode != 0:
                log.error(f"缓冲转码失败: {err.decode(errors='ignore')[:200]}")
                return src
            return out
        except Exception as e:
            log.error(f"缓冲转码异常: {e}")
            return src

    # ---------------- 缓存清理 ----------------
    def cleanup_cache(self, max_age_hours: int = 24, max_files: int = 500) -> int:
        """清理 DLNA 推送产生的临时音频

        只清理 buf_*/_seek*/_c.mp3 这类一次性产物；
        本地音乐的转码结果（按内容哈希命名）保留复用，不受影响。
        """
        root = self.config.cache_dir
        if not os.path.isdir(root):
            return 0

        now = time.time()
        markers = ("buf_", "_seek", "_c.mp3")
        temps: list[tuple[float, str]] = []

        for name in os.listdir(root):
            p = os.path.join(root, name)
            # 转码中断残留
            if name.endswith(".part"):
                try:
                    if now - os.path.getmtime(p) > 3600:
                        os.remove(p)
                except OSError:
                    pass
                continue
            if not name.endswith(".mp3"):
                continue
            if not any(m in name for m in markers):
                continue
            try:
                temps.append((os.path.getmtime(p), p))
            except OSError:
                continue

        removed = 0
        for mtime, p in temps:
            if now - mtime > max_age_hours * 3600:
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass

        temps = [t for t in temps if os.path.exists(t[1])]
        if len(temps) > max_files:
            temps.sort()
            for _, p in temps[: len(temps) - max_files]:
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass

        if removed:
            log.info(f"已清理临时缓存 {removed} 个文件")
        return removed
