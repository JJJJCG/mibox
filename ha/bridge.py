"""语音 -> 外部 AI 接口 -> HA 播报

小爱听到的话先过一遍关键词，命中就把这句话转发给外部接口
（pi HTTP Bridge 的 `POST /v1/chat`），拿到回答后用 HA 的
`notify.send_message` 交给音箱播报。

原来的「正则匹配 -> 调用 HA 服务」规则引擎已移除，只保留关键词转发。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import aiohttp

from core.config import Config
from .client import HAClient

log = logging.getLogger("mibox")

# 关键词分隔符：换行 / 逗号 / 顿号 / 分号 / 空格
_KW_SPLIT = re.compile(r"[\s,，、;；]+")

# 剥关键词时顺手清掉的标点/空白
_TRIM = " \t\r\n,，、。.!！?？;；:：\"'“”‘’"

# 播报前的清洗：代码块、表格、链接、Markdown 标记
_CODE_BLOCK = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_TABLE_LINE = re.compile(r"^[ \t]*\|.*\|[ \t]*$", re.M)
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MD_MARK = re.compile(r"^[ \t]*#{1,6}[ \t]*|^[ \t]*[-*+][ \t]+|[*_~]{1,3}", re.M)
_BLANK_LINE = re.compile(r"\n{2,}")
_SPACES = re.compile(r"[ \t]{2,}")

# 播报内容上限（超出按句读截断）；接口带 voice 短稿时通常远达不到
_MAX_SPEECH = 400

# 接口没给出回答时，兜底播这一句
_NO_REPLY_SPEECH = "喵喵……它好像睡着了"

# 接口错误码 -> 人话（详见 http-bridge 文档）
_HTTP_HINT = {
    400: "请求体不合法",
    401: "token 不对",
    409: "注入被拒或排队异常",
    503: "接口会话正在退出（/new、/resume），稍后重试",
    504: "超时没等到回复，agent 可能还在跑",
}


def keywords_of(raw) -> list[str]:
    """把配置里的关键词（字符串或列表）拆成列表"""
    if isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = _KW_SPLIT.split(str(raw or ""))
    return [k.strip() for k in items if k.strip()]


def clean_speech(text: str) -> str:
    """清掉 Markdown / 代码 / 链接，只留能读出来的话"""
    s = _CODE_BLOCK.sub(" ", text or "")
    s = _INLINE_CODE.sub(r"\1", s)
    s = _TABLE_LINE.sub(" ", s)
    s = _LINK.sub(r"\1", s)
    s = _URL.sub(" ", s)
    s = _MD_MARK.sub("", s)
    s = _SPACES.sub(" ", s)
    s = _BLANK_LINE.sub("\n", s).strip()

    if len(s) > _MAX_SPEECH:
        head = s[:_MAX_SPEECH]
        cut = max(head.rfind(ch) for ch in "。！？!?；;\n")
        s = (head[: cut + 1] if cut > 0 else head).strip() + "…"
    return s


def _strip_leading(q: str, kws: list[str]) -> str:
    """反复剥掉开头的触发词："问问今天天气" -> "今天天气"

    只动开头：句中出现的关键词原样留着（关键词若本身是"天气"这类实词，
    从中间剔掉会把问题改坏）。
    """
    text = q
    changed = True
    while changed and text:
        changed = False
        text = text.strip(_TRIM)
        for kw in kws:
            if kw and text.lower().startswith(kw.lower()):
                text = text[len(kw):]
                changed = True
                break
    return text.strip(_TRIM)


class AIBridge:
    """关键词 -> 接口 -> 播报"""

    def __init__(self, config: Config, ha_client: HAClient | None):
        self.config = config
        self.ha = ha_client
        self._session: aiohttp.ClientSession | None = None

    # ---------------- 配置判断 ----------------
    @property
    def keywords(self) -> list[str]:
        return keywords_of(self.config.ai_keywords)

    @property
    def configured(self) -> bool:
        """接口能否调用（关键词 + 地址）"""
        return bool(self.config.ai_enabled and self.config.ai_url and self.keywords)

    def status(self) -> dict:
        return {
            "enabled": self.config.ai_enabled,
            "keywords": self.keywords,
            "url": self.config.ai_url,
            "has_token": bool(self.config.ai_token),
            "notify_entity": self.config.ai_notify_entity,
            "can_speak": bool(
                self.ha and self.ha.configured and self.config.ai_notify_entity
            ),
        }

    # ---------------- 关键词 ----------------
    def match(self, query: str, keywords: list[str] | None = None) -> tuple[str, str] | None:
        """命中返回 (关键词, 要转发的文本)；未命中返回 None

        开头的关键词会被剥掉（"问问今天天气" -> "今天天气"），
        剥完不剩东西时（整句就是关键词）原样转发，避免发空请求。
        """
        q = (query or "").strip()
        if not q:
            return None
        kws = self.keywords if keywords is None else keywords
        hit = next((k for k in kws if k and k.lower() in q.lower()), None)
        if hit is None:
            return None
        return hit, (_strip_leading(q, kws) or q)

    # ---------------- 主流程 ----------------
    async def handle(self, query: str) -> bool:
        """返回是否由本桥接接手（未命中关键词即 False）"""
        if not self.config.ai_enabled:
            return False

        hit = self.match(query)
        if hit is None:
            return False
        if not self.config.ai_url:
            log.warning("关键词命中，但未配置接口地址，无法转发")
            return True

        kw, text = hit
        log.info(f"关键词「{kw}」命中，转发给接口: {text}")
        reply = await self._ask_with_ack(text)
        if not reply:
            await self.speak(_NO_REPLY_SPEECH)
            return True
        await self.speak(reply)
        return True

    async def _ask_with_ack(self, text: str) -> str:
        """把请求发出去后随即播一句提示语（默认「好的」），再等回答

        先把请求 create_task 出去，提示语的播报不占用接口的等待时间。
        """
        ask = asyncio.create_task(self.ask(text))
        ack = (self.config.ai_ack or "").strip()
        if ack:
            await self.speak(ack)
        return await ask

    # ---------------- 接口 ----------------
    async def ask(self, text: str, *, url: str = "", token: str = "",
                  timeout: int = 0) -> str:
        """POST {base}/v1/chat，返回已清洗的回答（失败返回空串）

        url / token / timeout 可传参覆盖，供界面「测试接口」在保存前直接试。
        """
        base = (url or self.config.ai_url or "").strip().rstrip("/")
        tk = token or self.config.ai_token
        wait = int(timeout or self.config.ai_timeout or 300)
        if not base:
            log.error("未配置 AI 接口地址")
            return ""

        payload: dict = {"text": text, "timeout": wait}
        if self.config.ai_voice_hint:
            payload["hint"] = "voice"
        headers = {
            "Authorization": f"Bearer {tk}",
            "Content-Type": "application/json",
        }

        # 客户端超时必须比接口自身的 timeout 长，否则先被自己掐断（文档建议 +30s）
        t0 = time.time()
        try:
            session = await self._ensure_session()
            async with session.post(
                f"{base}/v1/chat", json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=wait + 30),
            ) as r:
                raw = await r.text()
                if r.status >= 400:
                    hint = _HTTP_HINT.get(r.status, "")
                    log.error(f"AI 接口失败: HTTP {r.status} {hint} | {raw[:300]}")
                    return ""
                data = json.loads(raw)
        except asyncio.TimeoutError:
            log.error("AI 接口超时（agent 可能仍在运行，可查 /v1/status 的 busy）")
            return ""
        except Exception as e:
            log.error(f"AI 接口异常: {e}")
            return ""

        reply = str(data.get("reply") or "").strip()
        log.info(
            f"AI 接口返回 {len(reply)} 字，用时 {time.time() - t0:.1f}s，"
            f"工具调用 {data.get('tools', 0)} 次"
        )
        return clean_speech(reply)

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # 单次请求的超时由 post(..., timeout=) 覆盖，这里只兜底
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60)
            )
        return self._session

    # ---------------- 播报 ----------------
    async def speak(self, message: str) -> bool:
        """通过 HA 的 notify.send_message 让音箱播报"""
        msg = clean_speech(message)
        if not msg:
            return False
        if self.ha is None or not self.ha.configured:
            log.warning("HA 未配置（地址 + 长期令牌），无法播报")
            return False
        entity = (self.config.ai_notify_entity or "").strip()
        if not entity:
            log.warning("未配置播报实体（notify.*），无法播报")
            return False

        ok = await self.ha.call_service(
            "notify", "send_message", {"message": msg, "entity_id": entity}
        )
        if ok:
            log.info(f"已播报: {msg[:60]}{'…' if len(msg) > 60 else ''}")
        return ok

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
