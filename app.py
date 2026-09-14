"""mibox 主入口

单进程 asyncio：所有模块共享同一个 event loop 与同一个小米账号会话。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.auth import AuthManager
from core.buffer import BufferManager
from core.config import Config
from core.const import LOG_NAME
from core.library import MusicLibrary
from core.media import MediaService
from core.player import Player
from core.qr_login import QRLogin
from core.speaker import SpeakerController
from dlna.server import DLNAServer
from ha.client import HAClient
from ha.rules import RuleEngine
from voice.dispatcher import CommandDispatcher
from voice.poller import ConversationPoller

log = logging.getLogger(LOG_NAME)

WEB_DIR = Path(__file__).parent / "web"


class AppState:
    """全局状态容器"""

    def __init__(self):
        self.config: Config = Config.load()
        self.auth = AuthManager(self.config)
        self.media = MediaService(self.config)
        self.library = MusicLibrary(self.config, self.media)
        self.buffers = BufferManager()
        self.players: dict[str, Player] = {}     # did -> Player
        self.dlna: DLNAServer | None = None
        self.ha_client: HAClient | None = None
        self.ha_engine: RuleEngine | None = None
        self.poller: ConversationPoller | None = None
        self.dispatcher: CommandDispatcher | None = None
        self.ready = False
        self.last_error = ""
        self._cleanup_task: asyncio.Task | None = None
        self.boot_task: asyncio.Task | None = None
        self.qr: QRLogin | None = None
        self._qr_applied = False

    async def _cleanup_loop(self):
        """每小时清理一次 DLNA 推送产生的临时音频"""
        try:
            while True:
                await asyncio.sleep(3600)
                try:
                    self.media.cleanup_cache()
                except Exception as e:
                    log.debug(f"缓存清理失败: {e}")
        except asyncio.CancelledError:
            pass

    # ---------------- 初始化 ----------------
    async def bootstrap(self) -> bool:
        """登录 -> 拉取设备 -> 建播放器 -> 启动各模块"""
        cfg = self.config
        try:
            if self._cleanup_task is None or self._cleanup_task.done():
                self._cleanup_task = asyncio.create_task(self._cleanup_loop())

            # 曲库是本地资源，不依赖小米账号，无论是否登录都先扫出来。
            # 之前放在账号检查之后，导致未配置账号/未选音箱时曲库永远为空。
            await asyncio.to_thread(self.library.scan)
            asyncio.create_task(asyncio.to_thread(self.library.attach_durations))

            if not (cfg.cookie or (cfg.account and cfg.password)):
                self.last_error = "尚未配置小米账号（或 cookie）"
                log.warning(self.last_error)
                return False

            if not await self.auth.login():
                self.last_error = "小米账号登录失败"
                return False

            dids = cfg.get_did_list()
            if not dids:
                # 未指定 DID 时自动列出全部音箱，交给用户在界面上勾选
                self.last_error = "未配置音箱 DID，请在设备列表中选择"
                log.warning(self.last_error)
                return False

            await self.auth.update_speakers_info()

            self._build_players()

            if cfg.enable_dlna:
                self.dlna = DLNAServer(cfg, self.players, self.media, self.buffers)
                for player in self.players.values():
                    self.dlna.register(player.speaker.did, player)
                await self.dlna.start()

            self._build_ha()

            if cfg.enable_voice:
                speakers = [p.speaker for p in self.players.values()]
                self.dispatcher = CommandDispatcher(
                    cfg, self.players, self.library, self.ha_engine
                )
                self.poller = ConversationPoller(cfg, self.auth, speakers, self.dispatcher)
                await self.poller.start()

            self.ready = True
            self.last_error = ""
            return True
        except Exception as e:
            self.last_error = f"初始化失败: {e}"
            log.exception(self.last_error)
            return False

    def _build_players(self):
        self.players = {}
        for sp in self.config.get_enabled_speakers():
            if not sp.device_id:
                log.warning(f"音箱 {sp.name or sp.did} 缺少 device_id，跳过")
                continue
            controller = SpeakerController(sp, self.auth)
            player = Player(sp, controller, self.config, self.media)
            self.players[sp.did] = player
            log.info(f"播放器就绪: {sp.get_dlna_name()}")
        # 音量同步到默认音量
        for p in self.players.values():
            p.volume = self.config.default_volume

    def _build_ha(self):
        cfg = self.config
        if not cfg.enable_ha or not cfg.ha_url or not cfg.ha_token:
            return
        self.ha_client = HAClient(cfg.ha_url, cfg.ha_token)
        self.ha_engine = RuleEngine(self.ha_client, cfg.ha_rules)

    async def reinit(self) -> bool:
        """配置变更后重新初始化"""
        await self.shutdown()
        self.config = Config.load()
        self.auth = AuthManager(self.config)
        self.media = MediaService(self.config)
        self.library = MusicLibrary(self.config, self.media)
        self.buffers = BufferManager()
        return await self.bootstrap()

    async def shutdown(self):
        if self.qr:
            await self.qr.cancel()
            self.qr = None
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.poller:
            await self.poller.stop()
        if self.dlna:
            await self.dlna.stop()
        for p in self.players.values():
            await p.close()
        if self.ha_client:
            await self.ha_client.close()
        await self.auth.close()
        self.ready = False

    # ---------------- 查询辅助 ----------------
    def player(self, did: str) -> Player:
        p = self.players.get(did) or next(iter(self.players.values()), None)
        if p is None:
            raise HTTPException(status_code=404, detail="没有可用音箱")
        return p


state = AppState()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.DEBUG if state.config.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    os.makedirs(state.config.conf_path, exist_ok=True)
    os.makedirs(state.config.music_path, exist_ok=True)
    # 初始化（小米登录、拉设备、扫曲库）涉及网络与磁盘 IO，可能耗时数十秒；
    # 放到后台执行，让 Web 立即可访问，避免浏览器刷新时一直转圈。
    state.boot_task = asyncio.create_task(state.bootstrap())
    yield
    try:
        await asyncio.wait_for(state.boot_task, timeout=10)
    except (asyncio.TimeoutError, Exception):
        pass
    await state.shutdown()


app = FastAPI(title="mibox", version="0.1.0", lifespan=lifespan)


# 注：曾尝试用 logging.Filter 静音前端轮询 /api/status 的访问日志，但 uvicorn
# 会在 import 应用之后用 dictConfig 重建 logger 的 handler，过滤器安装时机难以
# 稳定覆盖，故不再处理。前端已将轮询降到 5s 并在页面隐藏时暂停，日志量已足够少。


# ==================== 前端 ====================
@app.get("/", response_class=HTMLResponse)
async def index():
    html = WEB_DIR / "index.html"
    if not html.exists():
        return HTMLResponse("<h1>mibox</h1><p>前端文件缺失</p>")
    return HTMLResponse(html.read_text(encoding="utf-8"))


# ==================== 状态 ====================
@app.get("/api/status")
async def api_status():
    booting = state.boot_task is not None and not state.boot_task.done()
    return {
        "ready": state.ready,
        "booting": booting,
        "error": state.last_error,
        "hostname": state.config.hostname,
        "web_port": state.config.web_port,
        "dlna_port": state.config.dlna_port,
        "logged_in": state.auth.is_logged_in(),
        "library_count": len(state.library.all()),
        "enable_dlna": state.config.enable_dlna,
        "enable_voice": state.config.enable_voice,
        "enable_ha": state.config.enable_ha,
        "players": [p.status() for p in state.players.values()],
    }


@app.get("/api/devices")
async def api_devices():
    """列出账号下所有设备，供用户挑选音箱"""
    devices = await state.auth.device_list()
    selected = state.config.get_did_list()
    return [
        {
            "did": d.get("miotDID", ""),
            "device_id": d.get("deviceID", ""),
            "name": d.get("name", ""),
            "hardware": d.get("hardware", ""),
            "selected": d.get("miotDID", "") in selected,
        }
        for d in devices
    ]


# ==================== 扫码登录 ====================
@app.post("/api/qr/start")
async def api_qr_start():
    """生成米家扫码登录二维码，并在后台等待扫码结果"""
    if state.qr is None:
        state.qr = QRLogin()
    state._qr_applied = False
    return await state.qr.start()


@app.get("/api/qr/status")
async def api_qr_status():
    if state.qr is None:
        return {"status": "idle", "message": "", "qr": "", "user_id": ""}

    st = state.qr.state()

    # 扫码成功：把 userId/passToken 落盘，然后用它重新走登录流程
    if st["status"] == "success" and st["user_id"] and not state._qr_applied:
        state._qr_applied = True
        state.config.cookie = f"userId={st['user_id']};passToken={state.qr.pass_token}"
        state.config.account = ""
        state.config.password = ""
        state.config.save()
        log.info("扫码成功，已保存凭据并重新初始化")
        asyncio.create_task(state.reinit())
        st["message"] = "扫码成功，正在重新初始化…"

    return st


@app.post("/api/qr/cancel")
async def api_qr_cancel():
    if state.qr:
        await state.qr.cancel()
        state.qr = None
    return {"ok": True}


@app.get("/api/config")
async def api_get_config():
    cfg = state.config
    return {
        "account": cfg.account,
        "has_password": bool(cfg.password),
        "has_cookie": bool(cfg.cookie),
        "mi_did": cfg.mi_did,
        "hostname": cfg.hostname,
        "web_port": cfg.web_port,
        "dlna_port": cfg.dlna_port,
        "music_path": cfg.music_path,
        "default_volume": cfg.default_volume,
        "enable_dlna": cfg.enable_dlna,
        "enable_voice": cfg.enable_voice,
        "enable_ha": cfg.enable_ha,
        "ha_url": cfg.ha_url,
        "has_ha_token": bool(cfg.ha_token),
        "pull_ask_sec": cfg.pull_ask_sec,
    }


@app.post("/api/config")
async def api_save_config(request: Request):
    data = await request.json()
    cfg = state.config
    for key in (
        "account", "password", "cookie", "mi_did", "hostname",
        "web_port", "dlna_port", "music_path", "default_volume",
        "enable_dlna", "enable_voice", "enable_ha", "ha_url",
        "ha_token", "pull_ask_sec",
    ):
        if key in data and data[key] is not None:
            setattr(cfg, key, data[key])
    cfg.save()
    return {"ok": True, "need_restart": True}


@app.post("/api/restart")
async def api_restart():
    ok = await state.reinit()
    return {"ok": ok, "error": state.last_error}


# ==================== 音乐库 ====================
@app.get("/api/library")
async def api_library(q: str = "", limit: int = 100, offset: int = 0):
    songs = state.library.search(q, limit=limit) if q else state.library.all()
    return {
        "total": len(state.library.all()),
        "songs": [s.to_dict() for s in songs[offset:offset + limit]],
    }


@app.get("/api/playlists")
async def api_playlists():
    return state.library.playlists()


@app.post("/api/library/scan")
async def api_scan():
    n = await state.library.scan()
    state.library.attach_durations()
    return {"ok": True, "count": n}


@app.get("/api/favorites")
async def api_favorites():
    return [s.to_dict() for s in state.library.favorite_songs()]


# ==================== 播放控制 ====================
@app.post("/api/player/{did}/play")
async def api_play(did: str, request: Request):
    """body: {keyword?, song?, playlist?, favorites?}"""
    body = await request.json()
    player = state.player(did)
    lib = state.library

    # 跳转到队列指定位置
    if "index" in body and body["index"] is not None:
        idx = int(body["index"])
        if 0 <= idx < len(player.queue):
            await player.play_items(player.queue, idx)
            return {"ok": True, "index": idx}
        raise HTTPException(status_code=400, detail="索引越界")

    if body.get("favorites"):
        songs = lib.favorite_songs()
    elif body.get("playlist"):
        songs = lib.by_playlist(body["playlist"])
    elif body.get("song"):
        songs = [lib.get(body["song"])] if lib.get(body["song"]) else []
    elif body.get("keyword"):
        songs = lib.search(body["keyword"], limit=30)
    else:
        songs = lib.all()

    if not songs:
        raise HTTPException(status_code=404, detail="没有匹配的歌曲")
    await player.play_songs(songs)
    return {"ok": True, "count": len(songs)}


@app.post("/api/player/{did}/control")
async def api_control(did: str, request: Request):
    body = await request.json()
    action = body.get("action", "")
    player = state.player(did)
    if action == "next":
        await player.next()
    elif action == "prev":
        await player.prev()
    elif action == "pause":
        await player.pause()
    elif action == "resume":
        await player.resume()
    elif action == "stop":
        await player.stop()
    else:
        raise HTTPException(status_code=400, detail=f"未知动作 {action}")
    return {"ok": True}


@app.post("/api/player/{did}/volume")
async def api_volume(did: str, request: Request):
    body = await request.json()
    await state.player(did).set_volume(int(body.get("volume", 50)))
    return {"ok": True}


@app.post("/api/player/{did}/mode")
async def api_mode(did: str, request: Request):
    body = await request.json()
    state.player(did).set_mode(body.get("mode", "NORMAL"))
    return {"ok": True}


@app.get("/api/player/{did}/status")
async def api_player_status(did: str):
    return state.player(did).status()


# ==================== HA ====================
@app.get("/api/ha/rules")
async def api_ha_rules():
    return state.config.ha_rules


@app.post("/api/ha/rules")
async def api_ha_save_rules(request: Request):
    data = await request.json()
    state.config.ha_rules = data if isinstance(data, list) else []
    state.config.save()
    if state.ha_engine:
        state.ha_engine.set_rules(state.config.ha_rules)
    return {"ok": True, "count": len(state.config.ha_rules)}


@app.post("/api/ha/test")
async def api_ha_test(request: Request):
    """用请求里带的配置（或当前配置）即时测试

    不依赖已初始化的 HA 客户端，填完地址与令牌即可直接测，无需先重启服务。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    url = (body.get("url") or state.config.ha_url or "").strip()
    token = (body.get("token") or state.config.ha_token or "").strip()
    if not url or not token:
        raise HTTPException(status_code=400, detail="请先填写 HA 地址与令牌")

    client = HAClient(url, token)
    try:
        ok, msg = await client.test()
    finally:
        await client.close()
    return {"ok": ok, "message": msg}


@app.post("/api/ha/test-rule")
async def api_ha_test_rule(request: Request):
    """用一句话试匹配规则，但不真正执行"""
    body = await request.json()
    query = body.get("query", "")
    if not state.ha_engine:
        raise HTTPException(status_code=400, detail="HA 未启用")
    import re

    for rule in state.ha_engine.rules:
        try:
            if re.search(rule.get("pattern", ""), query):
                return {"matched": True, "rule": rule}
        except re.error:
            continue
    return {"matched": False}


# ==================== 媒体文件 ====================
@app.get("/music/{path:path}")
async def api_music_file(path: str):
    full = os.path.join(state.config.music_path, path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(full)


@app.get("/cache/{name}")
async def api_cache_file(name: str):
    full = os.path.join(state.config.cache_dir, name)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="缓存不存在")
    return FileResponse(full)


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.DEBUG if state.config.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(app, host="0.0.0.0", port=state.config.web_port)
