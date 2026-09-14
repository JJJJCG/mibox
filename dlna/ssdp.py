"""SSDP 组播发现服务

注意：本服务必须绑定 UDP 1900 并加入 239.255.255.250 组播组，
因此容器必须运行在 host 网络下。
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct

from core.const import (
    AVTRANSPORT_URN,
    CONNECTION_MANAGER_URN,
    DEVICE_TYPE,
    RENDERING_CONTROL_URN,
    SERVER_ID,
    SSDP_ADDR,
    SSDP_ALIVE_INTERVAL,
    SSDP_PORT,
)

log = logging.getLogger("mibox")


class SSDPServer:
    def __init__(self, hostname: str, dlna_port: int):
        self.hostname = hostname
        self.dlna_port = dlna_port
        self.renderers: dict[str, str] = {}   # udn -> friendly_name
        self._transport = None
        self._alive_task = None
        self._sock = None

    def register(self, udn: str, friendly_name: str):
        self.renderers[udn] = friendly_name
        log.info(f"SSDP 注册渲染器: {friendly_name} (uuid:{udn})")

    def unregister(self, udn: str):
        self.renderers.pop(udn, None)

    def _location(self, udn: str) -> str:
        return f"http://{self.hostname}:{self.dlna_port}/device/{udn}/description.xml"

    def _targets(self, udn: str) -> list[tuple[str, str]]:
        u = f"uuid:{udn}"
        return [
            ("upnp:rootdevice", f"{u}::upnp:rootdevice"),
            (u, u),
            (DEVICE_TYPE, f"{u}::{DEVICE_TYPE}"),
            (AVTRANSPORT_URN, f"{u}::{AVTRANSPORT_URN}"),
            (RENDERING_CONTROL_URN, f"{u}::{RENDERING_CONTROL_URN}"),
            (CONNECTION_MANAGER_URN, f"{u}::{CONNECTION_MANAGER_URN}"),
        ]

    def _msearch_response(self, st: str, usn: str, udn: str) -> bytes:
        return (
            "HTTP/1.1 200 OK\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {self._location(udn)}\r\n"
            f"SERVER: {SERVER_ID}\r\n"
            f"ST: {st}\r\n"
            f"USN: {usn}\r\n"
            "EXT:\r\n\r\n"
        ).encode("utf-8")

    def _notify_alive(self, nt: str, usn: str, udn: str) -> bytes:
        return (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            "CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {self._location(udn)}\r\n"
            f"NT: {nt}\r\n"
            "NTS: ssdp:alive\r\n"
            f"SERVER: {SERVER_ID}\r\n"
            f"USN: {usn}\r\n\r\n"
        ).encode("utf-8")

    def _notify_byebye(self, nt: str, usn: str) -> bytes:
        return (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            f"NT: {nt}\r\n"
            "NTS: ssdp:byebye\r\n"
            f"USN: {usn}\r\n\r\n"
        ).encode("utf-8")

    def _multicast_if(self) -> str | None:
        try:
            socket.inet_aton(self.hostname)
        except OSError:
            return None
        if self.hostname in ("0.0.0.0", "127.0.0.1"):
            return None
        return self.hostname

    async def start(self):
        loop = asyncio.get_running_loop()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass
        self._sock.bind(("", SSDP_PORT))

        iface = self._multicast_if()
        if iface:
            self._sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface)
            )
        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(SSDP_ADDR),
            socket.inet_aton(iface or "0.0.0.0"),
        )
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self._sock.setblocking(False)

        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _SSDPProtocol(self), sock=self._sock
        )

        await self._send_alive()
        self._alive_task = asyncio.create_task(self._periodic_alive())
        log.info(f"SSDP 已启动（{SSDP_ADDR}:{SSDP_PORT}，接口 {iface or '自动'}）")

    async def stop(self):
        try:
            await asyncio.wait_for(self._send_byebye(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass
        if self._alive_task:
            self._alive_task.cancel()
            try:
                await asyncio.wait_for(self._alive_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self._transport:
            self._transport.close()
            self._transport = None
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        log.info("SSDP 已停止")

    async def _send_alive(self):
        if not self._transport:
            return
        for udn in self.renderers:
            for nt, usn in self._targets(udn):
                self._transport.sendto(self._notify_alive(nt, usn, udn), (SSDP_ADDR, SSDP_PORT))

    async def _send_byebye(self):
        if not self._transport:
            return
        for udn in self.renderers:
            for nt, usn in self._targets(udn):
                self._transport.sendto(self._notify_byebye(nt, usn), (SSDP_ADDR, SSDP_PORT))

    async def _periodic_alive(self):
        try:
            while True:
                await asyncio.sleep(SSDP_ALIVE_INTERVAL + random.uniform(-5, 5))
                await self._send_alive()
        except asyncio.CancelledError:
            pass

    def handle_msearch(self, data: bytes, addr: tuple):
        try:
            msg = data.decode("utf-8")
        except UnicodeDecodeError:
            return
        if "M-SEARCH" not in msg:
            return

        st, mx = "", 3
        for line in msg.split("\r\n"):
            low = line.lower()
            if low.startswith("st:"):
                st = line.split(":", 1)[1].strip()
            elif low.startswith("mx:"):
                try:
                    mx = int(line.split(":", 1)[1].strip())
                except ValueError:
                    mx = 3
        if not st:
            return

        for udn in self.renderers:
            for t_st, t_usn in self._targets(udn):
                if st in ("ssdp:all", t_st):
                    resp = self._msearch_response(t_st, t_usn, udn)
                    delay = random.uniform(0, min(mx, 3))
                    asyncio.get_running_loop().call_later(
                        delay, self._transport.sendto, resp, addr
                    )


class _SSDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, server: SSDPServer):
        self.server = server

    def datagram_received(self, data: bytes, addr: tuple):
        self.server.handle_msearch(data, addr)

    def error_received(self, exc):
        log.warning(f"SSDP 错误: {exc}")
