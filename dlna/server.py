"""DLNA 设备服务端：描述文档、SCPD、SOAP 控制、事件订阅与状态同步"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Optional

from aiohttp import web

from core.buffer import BufferManager
from core.config import Config
from core.media import MediaService
from core.player import Player, QueueItem
from core.const import (
    AVTRANSPORT_URN,
    CONNECTION_MANAGER_URN,
    MI_STATUS_PLAYING,
    RENDERING_CONTROL_URN,
    SUPPORTED_PROTOCOLS,
    TRANSPORT_STATE_PLAYING,
    TRANSPORT_STATE_STOPPED,
    UPNP_ERROR_ACTION_FAILED,
    UPNP_ERROR_INVALID_ACTION,
)
from .renderer import DLNARenderer
from .ssdp import SSDPServer
from .templates import (
    AVTRANSPORT_SCPD,
    CONNECTION_MANAGER_SCPD,
    RENDERING_CONTROL_SCPD,
    device_description_xml,
    soap_fault,
    soap_response,
)

log = logging.getLogger("mibox")

SCPDS = {
    "AVTransport": AVTRANSPORT_SCPD,
    "RenderingControl": RENDERING_CONTROL_SCPD,
    "ConnectionManager": CONNECTION_MANAGER_SCPD,
}


def _parse_soap(body: bytes) -> tuple[str, dict]:
    """解析 SOAP 请求，返回 (action, params)"""
    params: dict[str, str] = {}
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return "", params

    ns = "{http://schemas.xmlsoap.org/soap/envelope/}"
    body_el = root.find(f"{ns}Body")
    if body_el is None or len(body_el) == 0:
        return "", params

    action_el = body_el[0]
    action = action_el.tag.split("}")[-1]
    for child in action_el:
        key = child.tag.split("}")[-1]
        params[key] = child.text or ""
    return action, params


class DLNAServer:
    """为每台音箱提供一个虚拟 DLNA 渲染器"""

    def __init__(
        self,
        config: Config,
        players: dict[str, Player],
        media: MediaService,
        buffers: BufferManager,
    ):
        self.config = config
        self.players = players
        self.media = media
        self.buffers = buffers

        self.renderers: dict[str, DLNARenderer] = {}
        self.ssdp = SSDPServer(config.hostname, config.dlna_port)
        self._runner: Optional[web.AppRunner] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._subscriptions: dict[str, tuple[str, int]] = {}  # sid -> (callback, timeout)

    # ---------------- 生命周期 ----------------
    def register(self, did: str, player: Player):
        sp = player.speaker
        udn = sp.ensure_udn()
        r = DLNARenderer(udn, sp.get_dlna_name(), player)
        r.on_set_uri = self._on_set_uri
        r.on_play = self._on_play
        r.on_pause = self._on_pause
        r.on_stop = self._on_stop
        r.on_seek = self._on_seek
        r.on_volume = self._on_volume
        self.renderers[udn] = r
        self.ssdp.register(udn, r.name)
        log.info(f"DLNA 渲染器就绪: {r.name} (uuid:{udn})")

    async def start(self):
        app = web.Application()
        app.router.add_get("/device/{udn}/description.xml", self.handle_description)
        app.router.add_get("/device/{udn}/{service}.xml", self.handle_scpd)
        app.router.add_post("/device/{udn}/{service}/control", self.handle_control)
        app.router.add_route("*", "/device/{udn}/{service}/event", self.handle_event)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.config.dlna_port)
        await site.start()
        log.info(f"DLNA 服务已监听 0.0.0.0:{self.config.dlna_port}")

        await self.ssdp.start()
        self._poll_task = asyncio.create_task(self._poll_states())

    async def stop(self):
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.ssdp.stop()
        if self._runner:
            await self._runner.cleanup()
        log.info("DLNA 服务已停止")

    # ---------------- HTTP handler ----------------
    async def handle_description(self, request: web.Request) -> web.Response:
        udn = request.match_info["udn"]
        r = self.renderers.get(udn)
        if r is None:
            return web.Response(status=404, text="not found")
        return web.Response(
            text=device_description_xml(udn, r.name),
            content_type="text/xml",
            charset="utf-8",
        )

    async def handle_scpd(self, request: web.Request) -> web.Response:
        service = request.match_info["service"]
        xml = SCPDS.get(service)
        if xml is None:
            return web.Response(status=404, text="not found")
        return web.Response(text=xml, content_type="text/xml", charset="utf-8")

    async def handle_control(self, request: web.Request) -> web.Response:
        udn = request.match_info["udn"]
        service = request.match_info["service"]
        r = self.renderers.get(udn)
        if r is None:
            return web.Response(status=404, text="not found")

        soapaction = request.headers.get("SOAPACTION", "")
        action = soapaction.split("#")[-1].strip('"')
        body = await request.read()
        parsed_action, params = _parse_soap(body)
        if not action:
            action = parsed_action

        urn = {
            "AVTransport": AVTRANSPORT_URN,
            "RenderingControl": RENDERING_CONTROL_URN,
            "ConnectionManager": CONNECTION_MANAGER_URN,
        }[service]

        try:
            if service == "AVTransport":
                result = await self._avt_action(r, action, params)
            elif service == "RenderingControl":
                result = await self._rc_action(r, action, params)
            else:
                result = await self._cm_action(action)
        except Exception as e:
            log.error(f"SOAP {action} 异常: {e}")
            return web.Response(
                status=500,
                text=soap_fault(UPNP_ERROR_ACTION_FAILED, str(e)),
                content_type="text/xml",
                charset="utf-8",
            )

        if result is None:
            return web.Response(
                status=500,
                text=soap_fault(UPNP_ERROR_INVALID_ACTION, f"unsupported action {action}"),
                content_type="text/xml",
                charset="utf-8",
            )
        return web.Response(
            text=soap_response(urn, action, result),
            content_type="text/xml",
            charset="utf-8",
        )

    async def handle_event(self, request: web.Request) -> web.Response:
        """极简事件订阅：记录回调地址，状态变化时尽力 NOTIFY"""
        if request.method == "SUBSCRIBE":
            callback = request.headers.get("CALLBACK", "").strip("<>")
            sid = f"uuid:{uuid.uuid4()}"
            timeout = 1800
            if callback:
                self._subscriptions[sid] = (callback, time.time() + timeout)
            return web.Response(
                status=200,
                headers={
                    "SID": sid,
                    "TIMEOUT": f"Second-{timeout}",
                    "CONTENT-TYPE": "text/xml",
                },
            )
        if request.method == "UNSUBSCRIBE":
            sid = request.headers.get("SID", "")
            self._subscriptions.pop(sid, None)
            return web.Response(status=200)
        return web.Response(status=405)

    # ---------------- AVTransport ----------------
    async def _avt_action(self, r: DLNARenderer, action: str, p: dict):
        if action == "SetAVTransportURI":
            await r.set_uri(p.get("CurrentURI", ""), p.get("CurrentURIMetaData", ""))
            return {}
        if action == "SetNextAVTransportURI":
            r.next_uri = p.get("NextURI", "")
            r.next_uri_metadata = p.get("NextURIMetaData", "")
            return {}
        if action == "Play":
            await r.play(p.get("Speed", "1"))
            return {}
        if action == "Pause":
            await r.pause()
            return {}
        if action == "Stop":
            await r.stop()
            return {}
        if action == "Seek":
            unit = p.get("Unit", "REL_TIME")
            ok = await r.seek(unit, p.get("Target", "0"))
            if not ok:
                raise RuntimeError("seek failed")
            return {}
        if action == "Next":
            await r.player.next()
            return {}
        if action == "Previous":
            await r.player.prev()
            return {}
        if action == "SetPlayMode":
            await r.set_play_mode(p.get("NewPlayMode", "NORMAL"))
            return {}
        if action == "GetTransportInfo":
            return r.transport_info()
        if action == "GetPositionInfo":
            return r.position_info()
        if action == "GetMediaInfo":
            return r.media_info()
        if action == "GetTransportSettings":
            return {"PlayMode": r.play_mode, "RecQualityMode": "NOT_IMPLEMENTED"}
        if action == "GetDeviceCapabilities":
            return {
                "PlayMedia": "NETWORK",
                "RecMedia": "NOT_IMPLEMENTED",
                "RecQualityModes": "NOT_IMPLEMENTED",
            }
        if action == "GetCurrentTransportActions":
            return {"Actions": "Play,Pause,Stop,Seek,Next,Previous"}
        return None

    async def _rc_action(self, r: DLNARenderer, action: str, p: dict):
        if action == "GetVolume":
            return {"CurrentVolume": str(r.volume)}
        if action == "SetVolume":
            try:
                await r.set_volume(int(p.get("DesiredVolume", r.volume)))
            except ValueError:
                pass
            return {}
        if action == "GetMute":
            return {"CurrentMute": "1" if r.mute else "0"}
        if action == "SetMute":
            r.mute = p.get("DesiredMute", "0") in ("1", "true", "True")
            return {}
        if action == "ListPresets":
            return {"CurrentPresetNameList": "FactoryDefaults"}
        if action == "SelectPreset":
            return {}
        return None

    async def _cm_action(self, action: str):
        if action == "GetProtocolInfo":
            return {"Source": "", "Sink": SUPPORTED_PROTOCOLS}
        if action == "GetCurrentConnectionIDs":
            return {"ConnectionIDs": "0"}
        if action == "GetCurrentConnectionInfo":
            return {
                "RcsID": "-1",
                "AVTransportID": "-1",
                "ProtocolInfo": "",
                "PeerConnectionManager": "",
                "PeerConnectionID": "-1",
                "Direction": "Input",
                "Status": "OK",
            }
        return None

    # ---------------- 回调：串联播放引擎 ----------------
    async def _on_set_uri(self, r: DLNARenderer, uri: str, meta: str):
        """收到 URI 后立刻开始后台缓冲，缩短 Play 时的等待

        新的 URI 会让上一个缓冲失效，先释放旧资源。
        """
        if not uri:
            return
        self._release_buffer(r)
        token = uuid.uuid4().hex
        r.uri_token = token
        buf = self.buffers.create(token, uri)
        asyncio.create_task(buf.start_download())

    async def _on_play(self, r: DLNARenderer) -> bool:
        token = getattr(r, "uri_token", "")
        buf = self.buffers.get(token) if token else None
        if buf is None:
            # 没有缓冲（例如 URI 就是局域网地址），直接投递
            return await self._deliver(r, r.uri, 0)

        await buf.wait_for_completion()
        if not buf.ok():
            log.error(f"[{r.name}] 缓冲失败，放弃播放")
            return False

        path = self.media.save_buffer(token, bytes(buf.data))
        needs = r.player.speaker.needs_conversion()
        if needs:
            path = await self.media.convert_buffer(path, buf.content_type)
        url = f"{self.config.base_url()}/cache/{os.path.basename(path)}"
        duration = self.media.get_duration(path)
        return await self._deliver(r, url, duration)

    async def _deliver(self, r: DLNARenderer, url: str, duration: int) -> bool:
        r.duration = duration or 0
        item = QueueItem(
            name=r.name or "DLNA",
            url=url,
            duration=duration or 0,
            source="dlna",
        )
        await r.player.play_items([item])
        return r.player.state == "playing"

    async def _on_pause(self, r: DLNARenderer):
        await r.player.pause()

    async def _on_stop(self, r: DLNARenderer):
        await r.player.stop()
        self._release_buffer(r)

    # ---------------- 资源回收 ----------------
    def _release_buffer(self, r: DLNARenderer):
        """播放结束/停止/切换音源时释放缓冲与磁盘临时文件"""
        token = getattr(r, "uri_token", "")
        if not token:
            return
        self.buffers.release(token)
        r.uri_token = ""
        self._delete_token_files(token)

    def _delete_token_files(self, token: str):
        """删除与该 token 相关的所有缓存文件（含转码与 seek 产物）"""
        root = self.config.cache_dir
        try:
            for name in os.listdir(root):
                if token in name:
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(root, name))
        except OSError:
            pass

    async def _on_seek(self, r: DLNARenderer, offset: int) -> bool:
        """seek：从偏移位置重新编码一段再投递"""
        token = getattr(r, "uri_token", "")
        buf = self.buffers.get(token) if token else None
        if buf is None:
            return False
        src = self.media.save_buffer(token, bytes(buf.data))
        out = await self._ffmpeg_seek(src, offset)
        url = f"{self.config.base_url()}/cache/{os.path.basename(out)}"
        duration = self.media.get_duration(out)
        return await self._deliver(r, url, duration)

    async def _ffmpeg_seek(self, src: str, offset: int) -> str:
        out = src.rsplit(".", 1)[0] + f"_seek{offset}.mp3"
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-ss", str(offset), "-i", src,
            "-vn", "-codec:a", "libmp3lame", "-b:a", "192k", out,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            log.error(f"seek 转码失败: {err.decode(errors='ignore')[:200]}")
            return src
        return out

    async def _on_volume(self, r: DLNARenderer, volume: int):
        await r.player.set_volume(volume)

    # ---------------- 状态同步 ----------------
    async def _poll_states(self):
        """定期核对音箱真实状态，修正 DLNA 状态漂移"""
        try:
            while True:
                await asyncio.sleep(5)
                for r in self.renderers.values():
                    try:
                        if r.transport_state != TRANSPORT_STATE_PLAYING:
                            continue
                        st = await r.player.controller.get_status()
                        if st.get("status") != MI_STATUS_PLAYING:
                            log.info(f"[{r.name}] 音箱已停止，同步 DLNA 状态并释放缓冲")
                            r.transport_state = TRANSPORT_STATE_STOPPED
                            self._release_buffer(r)
                    except Exception:
                        continue
                self._purge_subscriptions()
        except asyncio.CancelledError:
            pass

    def _purge_subscriptions(self):
        """清理过期的事件订阅，避免长期累积"""
        now = time.time()
        for sid, (_, expire) in list(self._subscriptions.items()):
            if expire < now:
                self._subscriptions.pop(sid, None)
