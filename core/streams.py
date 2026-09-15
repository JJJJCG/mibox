"""可控音频流：支持随时掐断

音箱对 MiNA 的 pause/stop 指令经常不响应，尤其是播外部 URL 的时候。
与其反复发指令，不如直接把音箱正在拉的这条 HTTP 流掐断：没有数据了，
它自然就停下来。这是 MiAir 等同类项目验证过的做法。

两个动作配合：
- kill：标记某个路径为"已中断"，正在传输的分块流会在下一个分块处退出，
  连接随之关闭；之后的短暂时间内该路径直接拒绝，防止音箱重连续拉。
- release：下次投递同一文件前解除标记，顺便关掉该路径上残留的旧连接
  （暂停不再断流之后，音箱手里可能还晾着一条连接，留着只会占资源）。

单独需要"只关旧连接、不拦新连接"时用 drop（release 会调用它）。
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import time

log = logging.getLogger("mibox")

CHUNK_SIZE = 64 * 1024


def parse_range(header: str, size: int) -> tuple[int, int | None]:
    """解析单段 Range: bytes=a-b / bytes=a-，无法解析则从头播"""
    if not header or size <= 0:
        return 0, None
    try:
        units, _, rng = header.partition("=")
        if units.strip() != "bytes":
            return 0, None
        start_s, _, end_s = rng.strip().split("-", 1)
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else size - 1
        end = min(end, size - 1)
        if start > end or start < 0:
            return 0, None
        return start, end
    except Exception:
        return 0, None


class StreamManager:
    """按路径维度管理"可中断"的音频流"""

    def __init__(self, block_ttl: float = 20.0):
        self._blocked: dict[str, float] = {}
        self.block_ttl = block_ttl
        # path -> 该路径上正在传输的流（分块生成器）
        self._streams: dict[str, set[int]] = {}
        self._seq = 0

    # ---------------- 路径归一 ----------------
    @staticmethod
    def path_of(url_or_path: str) -> str:
        """把完整 URL 或路径统一成 /music/xxx、/cache/xxx 形式"""
        s = url_or_path or ""
        for marker in ("/music/", "/cache/"):
            i = s.find(marker)
            if i >= 0:
                return s[i:]
        return s

    # ---------------- 中断 / 恢复 ----------------
    def kill(self, url_or_path: str):
        """掐断某个音频流，并在 block_ttl 内拒绝音箱重连"""
        key = self.path_of(url_or_path)
        if not key:
            return
        self._blocked[key] = time.time() + self.block_ttl
        log.info(f"断流: {key}")

    def release(self, url_or_path: str):
        """解除中断标记，并关掉该路径上残留的旧连接

        调用时机是"马上要重新投递同一个文件"，此刻这条路径上还活着的连接
        一定是上一次投递留下的（音箱已经在播新的了，或者被暂停后晾着），
        留着只会占着文件句柄和 socket 缓冲，直接让它退出。
        """
        key = self.path_of(url_or_path)
        if not key:
            return
        self._blocked.pop(key, None)
        self.drop(key)

    def drop(self, url_or_path: str) -> int:
        """关掉某路径上正在传输的连接（不影响之后新来的连接）

        与 kill 的区别：kill 会在一段时间内连新连接一起拒绝，drop 只清旧的。
        返回被关掉的连接数。
        """
        key = self.path_of(url_or_path)
        if not key:
            return 0
        count = len(self._streams.pop(key, ()) or ())
        if count:
            log.info(f"关闭残留流: {key}（{count} 条）")
        return count

    def release_all(self):
        self._blocked.clear()
        self._streams.clear()

    def is_blocked(self, url_or_path: str) -> bool:
        key = self.path_of(url_or_path)
        expire = self._blocked.get(key)
        if expire is None:
            return False
        if time.time() > expire:
            self._blocked.pop(key, None)
            return False
        return True

    # ---------------- 分块输出 ----------------
    async def iter_file(self, full: str, path: str, start: int = 0,
                        end: int | None = None):
        """边读边发，每一块之前检查是否已被掐断或被顶掉"""
        key = self.path_of(path)
        self._seq += 1
        token = self._seq
        self._streams.setdefault(key, set()).add(token)
        try:
            with open(full, "rb") as f:
                if start:
                    f.seek(start)
                remaining = None if end is None else (end - start + 1)
                while True:
                    if self.is_blocked(path) or token not in self._streams.get(key, ()):
                        log.info(f"流已中断，关闭连接: {path}")
                        return
                    size = CHUNK_SIZE if remaining is None else min(CHUNK_SIZE, remaining)
                    data = f.read(size)
                    if not data:
                        return
                    if remaining is not None:
                        remaining -= len(data)
                    yield data
                    # 让出事件循环，保证 kill / drop 能被及时检查到
                    await asyncio.sleep(0)
        except OSError as e:
            log.debug(f"读取音频中断 {full}: {e}")
        finally:
            conns = self._streams.get(key)
            if conns is not None:
                conns.discard(token)
                if not conns:
                    self._streams.pop(key, None)


def guess_content_type(path: str) -> str:
    ctype, _ = mimetypes.guess_type(path)
    if ctype:
        return ctype
    ext = os.path.splitext(path)[1].lower()
    return {
        ".flac": "audio/flac", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".ogg": "audio/ogg", ".aac": "audio/aac", ".ape": "audio/ape",
    }.get(ext, "audio/mpeg")
