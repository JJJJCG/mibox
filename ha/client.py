"""Home Assistant REST 客户端"""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("mibox")


class HAClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._session: aiohttp.ClientSession | None = None

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
        return self._session

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)

    async def call_service(self, domain: str, service: str, data: dict | None = None) -> bool:
        """POST /api/services/{domain}/{service}"""
        if not self.configured:
            log.warning("HA 未配置，跳过服务调用")
            return False
        url = f"{self.base_url}/api/services/{domain}/{service}"
        session = await self._ensure_session()
        try:
            async with session.post(url, headers=self._headers(), json=data or {}) as r:
                if r.status >= 400:
                    text = await r.text()
                    hint = {
                        401: "令牌无效或过期",
                        403: "令牌权限不足",
                        404: "HA 地址或服务路径不存在",
                        400: "参数或实体问题（常见：entity_id 拼错/实体不存在）",
                    }.get(r.status, "")
                    log.error(
                        f"HA 调用失败: POST /api/services/{domain}/{service} "
                        f"data={data} -> HTTP {r.status} {hint} | {text[:300]}"
                    )
                    return False
                log.info(f"HA 调用成功: {domain}.{service} {data or ''}")
                return True
        except Exception as e:
            log.error(f"HA 调用异常: {e}")
            return False

    async def get_state(self, entity_id: str) -> dict | None:
        if not self.configured:
            return None
        session = await self._ensure_session()
        try:
            async with session.get(
                f"{self.base_url}/api/states/{entity_id}", headers=self._headers()
            ) as r:
                if r.status >= 400:
                    return None
                return await r.json()
        except Exception as e:
            log.debug(f"获取 HA 状态失败: {e}")
            return None

    async def test(self) -> tuple[bool, str]:
        """测试连通性"""
        if not self.configured:
            return False, "未配置 HA 地址或 Token"
        session = await self._ensure_session()
        try:
            async with session.get(f"{self.base_url}/api/", headers=self._headers()) as r:
                if r.status == 200:
                    data = await r.json()
                    return True, data.get("message", "ok")
                return False, f"HTTP {r.status}"
        except Exception as e:
            return False, str(e)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
