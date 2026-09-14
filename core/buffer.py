"""DLNA 推送内容的下载缓冲

控制点（手机 App）推过来的往往是外网 URL，音箱不一定能直连，
所以先由本服务下载到本地，再以局域网 URL 交给音箱。
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from .const import BUFFER_MAX_COUNT, BUFFER_MAX_SIZE

log = logging.getLogger("mibox")


class MediaBuffer:
    """单个远端音频的下载缓冲（内存态）"""

    def __init__(self, url: str):
        self.url = url
        self.data = bytearray()
        self.content_type = ""
        self.complete = False
        self.created_at = time.time()
        self._event = asyncio.Event()
        self._error: Exception | None = None

    async def start_download(self, timeout: int = 300):
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout, connect=10, sock_read=30)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.get(self.url) as resp:
                    if resp.status >= 400:
                        raise RuntimeError(f"远端返回 {resp.status}")
                    self.content_type = resp.headers.get("Content-Type", "")
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        if len(self.data) + len(chunk) > BUFFER_MAX_SIZE:
                            log.warning("缓冲超过上限，停止下载")
                            break
                        self.data.extend(chunk)
            self.complete = True
            log.info(f"缓冲完成: {self.url[:80]}... ({len(self.data)} bytes, {self.content_type})")
        except Exception as e:
            self._error = e
            log.error(f"缓冲失败 {self.url[:80]}: {e}")
        finally:
            self._event.set()

    async def wait_for_completion(self, timeout: int = 300):
        if self.complete:
            return
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("等待缓冲超时")

    def ok(self) -> bool:
        return self.complete and len(self.data) > 0 and self._error is None


class BufferManager:
    """管理所有缓冲

    三重回收，避免内存长期占用：
      1. 数量上限，超过则淘汰最旧的
      2. TTL 过期淘汰（默认 30 分钟）
      3. 播放结束/被替换时由调用方显式 release
    """

    def __init__(self, ttl: int = 1800):
        self._buffers: dict[str, MediaBuffer] = {}
        self._ttl = ttl

    def create(self, token: str, url: str) -> MediaBuffer:
        self._purge_expired()
        self._evict_if_needed()
        buf = MediaBuffer(url)
        self._buffers[token] = buf
        return buf

    def get(self, token: str) -> MediaBuffer | None:
        return self._buffers.get(token)

    def release(self, token: str):
        """显式释放（播放结束、切换音源、停止时调用）"""
        if self._buffers.pop(token, None) is not None:
            log.debug(f"释放缓冲: {token}")

    def _purge_expired(self):
        now = time.time()
        expired = [
            t for t, b in self._buffers.items() if now - b.created_at > self._ttl
        ]
        for t in expired:
            self._buffers.pop(t, None)
        if expired:
            log.info(f"清理 {len(expired)} 个过期缓冲")

    def _evict_if_needed(self):
        while len(self._buffers) >= BUFFER_MAX_COUNT:
            oldest = min(self._buffers.items(), key=lambda kv: kv[1].created_at)
            log.info(f"淘汰最旧缓冲: {oldest[0]}")
            self._buffers.pop(oldest[0], None)

    def clear(self):
        self._buffers.clear()
