"""HA 语音规则引擎：正则匹配 -> 调用 HA 服务（无 AI 兜底）"""

from __future__ import annotations

import asyncio
import logging
import re

from .client import HAClient

log = logging.getLogger("mibox")


def _fill(template, groups: tuple) -> object:
    """把模板里的 {0} {1} 替换成正则捕获组"""
    if isinstance(template, str):
        out = template
        for i, g in enumerate(groups):
            out = out.replace(f"{{{i}}}", str(g))
        return out
    return template


class RuleEngine:
    def __init__(self, client: HAClient, rules: list[dict] | None = None):
        self.client = client
        self.rules = rules or []

    def set_rules(self, rules: list[dict]):
        self.rules = rules

    async def handle(self, query: str) -> bool:
        """返回是否命中"""
        q = query.strip()
        if not q or not self.rules:
            return False

        for rule in self.rules:
            pattern = rule.get("pattern", "")
            if not pattern:
                continue
            try:
                m = re.search(pattern, q)
            except re.error:
                log.warning(f"规则正则非法: {pattern}")
                continue
            if not m:
                continue

            domain = rule.get("domain", "")
            service = rule.get("service", "")
            if not domain or not service:
                continue

            groups = m.groups()
            data: dict = {}
            entity_id = _fill(rule.get("entity_id", ""), groups)
            if entity_id:
                data["entity_id"] = entity_id

            for k, v in (rule.get("data") or {}).items():
                data[str(_fill(k, groups))] = _fill(v, groups)

            # 数值字段转成数字发送；HA 的 number.set_value 要求 value 为数字，
            # 字符串 "26" / "26.5" 在部分版本会被拒绝
            for key in ("brightness_pct", "temperature", "percentage", "position", "value"):
                if key in data:
                    try:
                        fv = float(data[key])
                        data[key] = int(fv) if fv == int(fv) else fv
                    except (ValueError, TypeError):
                        pass

            delay = int(rule.get("delay_minutes", 0) or 0)
            if delay > 0:
                log.info(f"规则命中（{delay} 分钟后执行）: {q} -> {domain}.{service}")
                asyncio.create_task(self._delayed(delay, domain, service, data))
            else:
                await self.client.call_service(domain, service, data)

            reply = _fill(rule.get("reply", ""), groups)
            if reply:
                # 一期不做 TTS，仅在日志与 Web 界面记录
                log.info(f"规则回复（未播报）: {reply}")
            return True

        return False

    async def _delayed(self, minutes: int, domain: str, service: str, data: dict):
        try:
            await asyncio.sleep(minutes * 60)
            await self.client.call_service(domain, service, data)
        except asyncio.CancelledError:
            pass
