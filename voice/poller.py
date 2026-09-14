"""语音对话轮询

全进程只有这一处轮询小米对话 API —— 音乐指令与 HA 指令共用同一份记录。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import ClientSession, ClientTimeout

from ..core.auth import AuthManager
from ..core.config import Config
from ..core.const import GET_ASK_BY_MINA, LATEST_ASK_API

log = logging.getLogger("mibox")


class ConversationPoller:
    def __init__(self, config: Config, auth: AuthManager, speakers: list, dispatcher):
        """
        speakers: Speaker 列表（含 device_id / hardware）
        dispatcher: CommandDispatcher
        """
        self.config = config
        self.auth = auth
        self.speakers = speakers
        self.dispatcher = dispatcher

        self._last_ts: dict[str, int] = {}
        self._task: asyncio.Task | None = None

    async def start(self):
        self._task = asyncio.create_task(self._loop())
        log.info(f"语音轮询已启动（间隔 {self.config.pull_ask_sec}s）")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("语音轮询已停止")

    async def _loop(self):
        timeout = ClientTimeout(total=10)
        async with ClientSession(timeout=timeout) as session:
            try:
                while True:
                    if not self.config.enable_voice:
                        await asyncio.sleep(5)
                        continue
                    try:
                        await self._poll_once(session)
                    except Exception as e:
                        log.debug(f"轮询异常: {e}")
                    await asyncio.sleep(self.config.pull_ask_sec)
            except asyncio.CancelledError:
                pass

    async def _poll_once(self, session: ClientSession):
        tasks = []
        for sp in self.speakers:
            if not sp.enabled or not sp.device_id or not sp.hardware:
                continue
            if sp.device_id not in self._last_ts:
                self._last_ts[sp.device_id] = int(time.time() * 1000)
            if sp.hardware in GET_ASK_BY_MINA:
                tasks.append(self._ask_by_mina(sp))
            else:
                tasks.append(self._ask_by_http(session, sp))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---------------- 两种拉取方式 ----------------
    def _cookies(self, device_id: str) -> dict | None:
        account = self.auth.account
        if not account or not account.token:
            return None
        token = account.token
        uid = token.get("userId")
        micoapi = token.get("micoapi")
        if not uid or not micoapi:
            return None
        service_token = micoapi[1] if isinstance(micoapi, (list, tuple)) else micoapi
        return {
            "userId": str(uid),
            "serviceToken": service_token,
            "deviceId": device_id,
        }

    async def _ask_by_http(self, session: ClientSession, sp):
        cookies = self._cookies(sp.device_id)
        if not cookies:
            return
        url = LATEST_ASK_API.format(
            hardware=sp.hardware, timestamp=str(int(time.time() * 1000))
        )
        try:
            async with session.get(url, cookies=cookies) as r:
                if r.status != 200:
                    if r.status == 401:
                        log.warning("对话 API 401，凭据可能已过期")
                    return
                data = await r.json(content_type=None)
        except Exception as e:
            log.debug(f"拉取对话失败: {e}")
            return

        payload = data.get("data")
        if not payload:
            return
        try:
            records = json.loads(payload) if isinstance(payload, str) else payload
            records = records.get("records") or []
        except (json.JSONDecodeError, AttributeError):
            return
        if not records:
            return
        rec = records[0]
        self._check(sp, rec)

    async def _ask_by_mina(self, sp):
        mina = self.auth.get_mina()
        if mina is None:
            return
        try:
            messages = await mina.get_latest_ask(sp.device_id)
        except Exception as e:
            log.debug(f"mina 拉取对话失败: {e}")
            return
        for msg in messages or []:
            try:
                ans = msg.response.answer[0]
                self._check(
                    sp,
                    {
                        "time": msg.timestamp_ms,
                        "query": ans.question,
                        "answer": ans.content,
                    },
                )
            except (AttributeError, IndexError):
                continue

    # ---------------- 去重与分发 ----------------
    def _check(self, sp, record: dict):
        ts = record.get("time")
        query = (record.get("query") or "").strip()
        if not ts or not query:
            return
        last = self._last_ts.get(sp.device_id, 0)
        if ts <= last:
            return
        self._last_ts[sp.device_id] = ts
        latency = time.time() - ts / 1000
        log.info(f"[{sp.name}] 收到语音: {query}（延迟 {latency:.1f}s）")
        asyncio.create_task(self.dispatcher.handle(sp.did, query))
