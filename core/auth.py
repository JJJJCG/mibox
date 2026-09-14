"""统一小米账号认证

全进程唯一入口：所有模块共用同一个 MiAccount / MiNAService 实例，
token 持久化在单一文件，从根本上避免多服务互相踢下线。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from urllib import parse

import aiohttp
from miservice import MiAccount, MiIOService, MiNAService

from .config import Config
from .const import APP_UA

log = logging.getLogger("mibox")

# 重新登录冷却时间（秒）。登录失败后不立即重试，避免刷新 token 造成风暴。
RELOGIN_COOLDOWN = 300


def parse_cookie_string(cookie_str: str) -> dict:
    """从 cookie 串中提取 userId / passToken"""
    result = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in ("userId", "passToken", "serviceToken", "deviceId"):
                result[k] = v
    return result


class AuthManager:
    """管理小米账号认证与设备服务"""

    def __init__(self, config: Config):
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.account: MiAccount | None = None
        self.mina: MiNAService | None = None
        self.miio: MiIOService | None = None
        self._logged_in = False
        self._lock = asyncio.Lock()
        self._last_relogin = 0.0
        self._device_id = hashlib.md5(b"mibox").hexdigest()[:16].upper()

    # ---------------- 登录 ----------------
    async def login(self, force: bool = False) -> bool:
        """登录小米账号。返回是否成功。

        force=False 时受冷却时间保护，失败不会立刻重试。
        """
        async with self._lock:
            if self._logged_in and not force:
                return True
            if not force and time.time() - self._last_relogin < RELOGIN_COOLDOWN:
                return self._logged_in

            os.makedirs(self.config.conf_path, exist_ok=True)
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
                )

            token_store = self.config.token_file

            if self.config.cookie:
                ok = await self._login_by_cookie(token_store)
            elif self.config.account and self.config.password:
                ok = await self._login_by_password(token_store)
            else:
                log.warning("未配置小米账号/密码/cookie，跳过登录")
                ok = False

            self.mina = MiNAService(self.account) if self.account else None
            self.miio = MiIOService(self.account) if self.account else None
            self._logged_in = ok
            if not ok:
                self._last_relogin = time.time()
            return ok

    async def _login_by_cookie(self, token_store: str) -> bool:
        """用 userId + passToken 换取 serviceToken（无需账号密码）"""
        data = parse_cookie_string(self.config.cookie)
        uid, ptk = data.get("userId"), data.get("passToken")
        if not (uid and ptk):
            log.error("cookie 中缺少 userId 或 passToken")
            return False

        self.account = MiAccount(self.session, "", "", token_store=token_store)
        self.account.now_ua = APP_UA

        cached = self.account.token_store.load_token() if self.account.token_store else None
        if (cached and str(cached.get("userId")) == str(uid)
                and cached.get("passToken") == ptk and "micoapi" in cached):
            self.account.token = cached
            log.info("复用本地缓存的 micoapi serviceToken")
            return True

        dev_id = hashlib.md5(f"mibox_{uid}".encode()).hexdigest()[:16].upper()
        self._device_id = dev_id
        headers = {"User-Agent": APP_UA}
        cookies = {
            "sdkVersion": "3.9",
            "deviceId": dev_id,
            "userId": str(uid),
            "passToken": str(ptk),
        }
        url = "https://account.xiaomi.com/pass/serviceLogin?sid=micoapi&_json=true"
        try:
            async with self.session.get(url, cookies=cookies, headers=headers) as r:
                text = (await r.read()).decode("utf-8", errors="ignore")
                if text.startswith("&&&START&&&"):
                    text = text[11:]
                resp = json.loads(text)

            if resp.get("code") != 0:
                log.error(f"passToken 换取 serviceToken 失败: {resp.get('description')}")
                return False

            location, nonce = resp["location"], resp["nonce"]
            ssecurity = resp["ssecurity"]
            client_sign = base64.b64encode(
                hashlib.sha1(f"nonce={nonce}&{ssecurity}".encode()).digest()
            ).decode()
            async with self.session.get(
                location + "&clientSign=" + parse.quote(client_sign)
            ) as r2:
                st = r2.cookies.get("serviceToken")
                if not st:
                    log.error("鉴权响应中未找到 serviceToken")
                    return False

            self.account.token = {
                "userId": str(uid),
                "passToken": str(ptk),
                "deviceId": dev_id,
                "ssecurity": ssecurity,
                "serviceToken": st.value,
                "micoapi": (ssecurity, st.value),
            }
            if self.account.token_store:
                self.account.token_store.save_token(self.account.token)
            log.info("cookie 登录成功")
            return True
        except Exception as e:
            log.error(f"cookie 登录异常: {e}")
            return False

    async def _login_by_password(self, token_store: str) -> bool:
        self.account = MiAccount(
            self.session,
            self.config.account,
            self.config.password,
            token_store=token_store,
        )
        self.account.now_ua = APP_UA
        try:
            await self.account.login("micoapi")
            log.info("小米账号登录成功")
            return True
        except Exception as e:
            msg = str(e)
            code = self._extract_code(msg)
            if code in ("87001",) or "captcha" in msg.lower():
                log.error("登录需要验证码，建议改用 cookie 方式登录")
            elif code == "70016":
                log.error("登录验证失败：密码错误、二次验证或需人机验证，建议改用 cookie 登录")
            else:
                log.error(f"登录失败: {e}")
            return False

    @staticmethod
    def _extract_code(msg: str) -> str:
        m = re.search(r"\b(\d{4,6})\b", msg)
        return m.group(1) if m else ""

    async def ensure_login(self) -> bool:
        if self._logged_in and self.mina is not None:
            return True
        return await self.login()

    def is_logged_in(self) -> bool:
        return self._logged_in and self.mina is not None

    def get_mina(self) -> MiNAService | None:
        return self.mina

    # ---------------- 设备 ----------------
    async def device_list(self) -> list[dict]:
        """获取账号下设备列表，失败时按冷却策略重试一次"""
        await self.ensure_login()
        if not self.mina:
            return []
        try:
            if self.account:
                self.account.now_ua = APP_UA
            devices = await self.mina.device_list()
            return devices or []
        except Exception as e:
            log.warning(f"获取设备列表失败: {e}")
            if time.time() - self._last_relogin < RELOGIN_COOLDOWN:
                return []
            self._logged_in = False
            if not await self.login(force=True):
                return []
            try:
                return await self.mina.device_list() or []
            except Exception as e2:
                log.error(f"重新登录后仍失败: {e2}")
                return []

    async def update_speakers_info(self):
        """把云端设备信息回填到每台 Speaker（device_id / hardware / name）"""
        devices = await self.device_list()
        did_list = self.config.get_did_list()
        for dev in devices:
            miot_did = dev.get("miotDID", "")
            if miot_did not in did_list:
                continue
            sp = self.config.get_speaker(miot_did)
            sp.device_id = dev.get("deviceID", "")
            sp.hardware = dev.get("hardware", "")
            if not sp.name:
                sp.name = dev.get("name", "")
            sp.ensure_udn()
            log.info(f"设备就绪: {sp.name} (hardware={sp.hardware}, device_id={sp.device_id})")

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
        self.account = None
        self.mina = None
        self.miio = None
        self._logged_in = False
