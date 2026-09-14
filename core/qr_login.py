"""小米账号扫码登录

流程（与 xiaomusic 一致）：
  1. serviceLogin?sid=mijia 拿到登录入口参数
  2. longPolling/loginUrl 换出二维码与长轮询地址
  3. 米家 App 扫码后，长轮询返回 userId / passToken
  4. 把 userId + passToken 交给 AuthManager，走 passToken 换取 micoapi serviceToken

扫码的意义：绕开账号密码登录常见的短信验证与风控。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import random
import string
import time
from urllib import parse

import aiohttp
from qrcode import QRCode

log = logging.getLogger("mibox")

SERVICE_LOGIN = "https://account.xiaomi.com/pass/serviceLogin"
LOGIN_URL_API = "https://account.xiaomi.com/longPolling/loginUrl"

# 二维码有效期由小米服务端控制，这里只作为兜底
QR_TIMEOUT = 180


def _random_hex(n: int) -> str:
    return "".join(random.choices("0123456789ABCDEF", k=n))


def _android_ua(pass_o: str, locale: str = "zh_CN") -> str:
    """构造米家 Android 客户端 UA（格式参照 xiaomusic）"""
    country = locale.split("_")[-1] if "_" in locale else "CN"
    a, b, c, d = _random_hex(40), _random_hex(32), _random_hex(32), _random_hex(40)
    return (
        f"Android-15-11.0.701-Xiaomi-23046RP50C-OS2.0.212.0.VMYCNXM-"
        f"{a}-{country}-{c}-{b}-SmartHome-MI_APP_STORE-{a}|{d}|{pass_o}-64"
    )


def _loads(text: str) -> dict:
    """小米部分接口返回带 &&&START&&& 前缀"""
    if text.startswith("&&&START&&&"):
        text = text[11:]
    return json.loads(text)


class QRLogin:
    """一次扫码登录会话"""

    def __init__(self):
        self.device_id = "".join(
            random.choices(string.ascii_letters + string.digits, k=16)
        )
        self.pass_o = "".join(random.choices("0123456789abcdef", k=16))
        self.ua = _android_ua(self.pass_o)

        self.status = "idle"        # idle|waiting|success|expired|error
        self.message = ""
        self.user_id = ""
        self.pass_token = ""
        self.qr_data_url = ""       # base64 PNG
        self.login_url = ""

        self._task: asyncio.Task | None = None

    # ---------------- 发起 ----------------
    async def start(self) -> dict:
        """生成二维码，并开始后台等待扫码结果"""
        await self.cancel()

        headers = {
            "User-Agent": self.ua,
            "Accept-Encoding": "gzip",
            "Content-Type": "application/x-www-form-urlencoded",
            "Connection": "keep-alive",
            "Cookie": f"deviceId={self.device_id};pass_o={self.pass_o};uLocale=zh_CN",
        }

        self.status = "waiting"
        self.message = "请用米家 App 扫描二维码"

        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                # Step 1：登录入口
                url = f"{SERVICE_LOGIN}?_json=true&sid=mijia&_locale=zh_CN&deviceId={self.device_id}"
                async with s.get(url, headers=headers) as r:
                    data = _loads(await r.text())

                if data.get("code") == 0:
                    # 已有有效凭据，直接访问 location 即可完成
                    self.status = "error"
                    self.message = "本机已存在有效登录，无需扫码"
                    return self.state()

                location = data.get("location", "")
                if not location:
                    self.status = "error"
                    self.message = f"无法获取登录入口: {data.get('desc', '')}"
                    return self.state()

                # Step 2：换二维码
                qs = parse.parse_qs(parse.urlparse(location).query)
                params = {k: v[0] for k, v in qs.items()}
                params.update(
                    {
                        "theme": "",
                        "bizDeviceType": "",
                        "_hasLogo": "false",
                        "_qrsize": "240",
                        "_dc": str(int(time.time() * 1000)),
                    }
                )
                async with s.get(
                    LOGIN_URL_API + "?" + parse.urlencode(params), headers=headers
                ) as r:
                    login_data = _loads(await r.text())
        except Exception as e:
            self.status = "error"
            self.message = f"获取二维码失败: {e}"
            log.warning(self.message)
            return self.state()

        self.login_url = login_data.get("loginUrl", "")
        lp = login_data.get("lp", "")
        if not self.login_url or not lp:
            self.status = "error"
            self.message = "二维码接口返回异常"
            return self.state()

        self.qr_data_url = self._make_png(self.login_url)
        self._task = asyncio.create_task(self._wait_for_scan(lp))
        log.info("二维码已生成，等待米家 App 扫码")
        return self.state()

    @staticmethod
    def _make_png(content: str) -> str:
        qr = QRCode(border=1, box_size=8)
        qr.add_data(content)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/png;base64,{b64}"

    # ---------------- 等待结果 ----------------
    async def _wait_for_scan(self, lp: str):
        headers = {
            "User-Agent": self.ua,
            "Accept-Encoding": "gzip",
            "Content-Type": "application/x-www-form-urlencoded",
            "Connection": "keep-alive",
        }
        try:
            timeout = aiohttp.ClientTimeout(total=QR_TIMEOUT)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(lp, headers=headers) as r:
                    text = await r.text()
                data = _loads(text)

                if not (data.get("userId") and data.get("passToken")):
                    self.status = "expired"
                    self.message = "二维码已过期或未完成确认，请重新获取"
                    return

                # 访问回调以落地 serviceToken cookie
                callback = data.get("location", "")
                if callback:
                    try:
                        async with s.get(callback, headers=headers) as r2:
                            await r2.read()
                    except Exception:
                        pass

                self.user_id = str(data["userId"])
                self.pass_token = data["passToken"]
                self.status = "success"
                self.message = "扫码成功"
                log.info(f"扫码登录成功，userId={self.user_id}")
        except asyncio.TimeoutError:
            self.status = "expired"
            self.message = "二维码已过期，请重新获取"
        except asyncio.CancelledError:
            self.status = "idle"
            self.message = "已取消"
        except Exception as e:
            self.status = "error"
            self.message = f"等待扫码异常: {e}"
            log.warning(self.message)

    # ---------------- 状态与清理 ----------------
    def state(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "qr": self.qr_data_url,
            "login_url": self.login_url,
            "user_id": self.user_id,
        }

    async def cancel(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None
