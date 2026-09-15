"""mibox 主入口

单进程 asyncio：所有模块共享同一个 event loop 与同一个小米账号会话。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response, StreamingResponse

from core.auth import AuthManager
from core.buffer import BufferManager
from core.config import Config, Speaker
from core.const import LOG_NAME
from core.library import MusicLibrary
from core.media import MediaService
from core.player import Player
from core.qr_login import QRLogin
from core.speaker import SpeakerController
from core.streams import StreamManager, guess_content_type, parse_range
from dlna.server import DLNAServer
from ha.bridge import AIBridge, keywords_of
from ha.client import HAClient
from ha.media import HASpeaker
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
        self.streams = StreamManager()           # 可中断的音频流管理
        self.players: dict[str, Player] = {}     # did -> Player
        self.dlna: DLNAServer | None = None
        self.ha_client: HAClient | None = None
        self.ai_bridge: AIBridge | None = None
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

            # HA 客户端要先建好：播放器的暂停/继续/停止与状态读取都挂在它上面
            self._build_ha()
            self._build_players()

            if cfg.enable_dlna:
                self.dlna = DLNAServer(cfg, self.players, self.media, self.buffers)
                for player in self.players.values():
                    self.dlna.register(player.speaker.did, player)
                await self.dlna.start()

            if cfg.enable_voice:
                speakers = [p.speaker for p in self.players.values()]
                self.dispatcher = CommandDispatcher(
                    cfg, self.players, self.library, self.ai_bridge
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
            ha_spk = self._build_ha_speaker(sp)
            controller = SpeakerController(
                sp, self.auth, self.media, self.streams, ha=ha_spk
            )
            player = Player(sp, controller, self.config, self.media)
            player.start_monitor()
            self.players[sp.did] = player
            log.info(
                f"播放器就绪: {sp.get_dlna_name()}"
                f"（控制与状态: {'HA ' + sp.ha_entity if ha_spk else '小米 API'}）"
            )
        # 音量同步到默认音量
        for p in self.players.values():
            p.volume = self.config.default_volume

    def _build_ha_speaker(self, sp: Speaker) -> HASpeaker | None:
        """按音箱配置构造 HA 媒体实体代理；返回 None 表示这台继续走小米 API"""
        cfg = self.config
        if not cfg.ha_control:
            return None
        if self.ha_client is None or not self.ha_client.configured:
            # 配置缺失的告警在 _build_ha 里已经打过一次，这里逐台静默降级
            log.debug(f"[{sp.name or sp.did}] HA 不可用，回落小米 API")
            return None
        entity = (sp.ha_entity or "").strip()
        if not entity:
            log.info(f"[{sp.name or sp.did}] 未指定 HA 媒体实体，继续走小米 API")
            return None
        return HASpeaker(self.ha_client, entity)

    def _build_ha(self):
        cfg = self.config
        # 播报（enable_ha）与接管播放控制（ha_control）都需要客户端
        if (cfg.enable_ha or cfg.ha_control) and cfg.ha_url and cfg.ha_token:
            self.ha_client = HAClient(cfg.ha_url, cfg.ha_token)
        elif cfg.ha_control:
            log.warning("已开启 HA 接管播放控制，但未配置 HA 地址或长期令牌")

        # AI 桥接的播报走 HA，所以没有 HA 客户端时桥接只能"问得到、说不出"
        if cfg.ai_enabled:
            if self.ha_client is None:
                log.warning("AI 桥接已开启但 HA 未配置（需地址 + 长期令牌），回答将无法播报")
            self.ai_bridge = AIBridge(cfg, self.ha_client)

    async def reinit(self) -> bool:
        """配置变更后重新初始化"""
        await self.shutdown()
        self.config = Config.load()
        self.auth = AuthManager(self.config)
        self.media = MediaService(self.config)
        self.library = MusicLibrary(self.config, self.media)
        self.buffers = BufferManager()
        self.streams = StreamManager()
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
        if self.ai_bridge:
            await self.ai_bridge.close()
            self.ai_bridge = None
        if self.ha_client:
            await self.ha_client.close()
            self.ha_client = None
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
    # uvicorn 此刻已完成 logging 配置，替换其访问日志 handler 并挂上过滤器
    _install_access_log_filter()
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


class _HeartbeatFilter(logging.Filter):
    """静音前端轮询 /api/status 的成功访问日志（非 200 照常记录）

    注意：uvicorn 的 message 以状态码结尾（如 `... HTTP/1.1" 200`），
    没有 " 200 OK" 里的那个尾随空格，判断必须用 endswith。
    """

    _QUIET = ('"GET /api/status',)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage().rstrip()
        except Exception:
            return True
        if msg.endswith(" 200") and any(q in msg for q in self._QUIET):
            return False
        return True


def _install_access_log_filter():
    """用自带过滤器的 handler 替换 uvicorn 的访问日志 handler

    此前尝试给 uvicorn 配置好的 handler 追加 filter，实测真实请求不生效
    （模拟日志可被拦，真实请求绕过了追加的 filter）。改为直接移除
    uvicorn 的 handler、装上自己的，lifespan 之后 uvicorn 不会再配置
    logging，因此不存在被覆盖的问题。
    """
    lg = logging.getLogger("uvicorn.access")
    for h in list(lg.handlers):
        lg.removeHandler(h)
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    h.addFilter(_HeartbeatFilter())
    lg.addHandler(h)
    lg.propagate = False


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
        "enable_ai": state.config.ai_enabled,
        "ha_control": state.config.ha_control,
        "ai_ready": bool(state.ai_bridge and state.ai_bridge.configured),
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
            # 这台音箱在 HA 里对应的 media_player 实体（用于接管控制与状态）
            "ha_entity": state.config.speaker_ha_entity(d.get("miotDID", "")),
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
        "ha_control": cfg.ha_control,
        "ha_poll_sec": cfg.ha_poll_sec,
        "pull_ask_sec": cfg.pull_ask_sec,
        # AI 桥接
        "ai_enabled": cfg.ai_enabled,
        "ai_keywords": cfg.ai_keywords,
        "ai_url": cfg.ai_url,
        "has_ai_token": bool(cfg.ai_token),
        "ai_timeout": cfg.ai_timeout,
        "ai_voice_hint": cfg.ai_voice_hint,
        "ai_notify_entity": cfg.ai_notify_entity,
        "ai_ack": cfg.ai_ack,
    }


@app.post("/api/config")
async def api_save_config(request: Request):
    data = await request.json()
    cfg = state.config
    # 敏感字段（前端不回显）：空值一律视为"未修改"，绝不能用空串
    # 覆盖已保存的凭据（否则一次普通保存就会把扫码登录清掉）
    sensitive = ("password", "cookie", "ha_token", "ai_token")
    for key in (
        "account", "password", "cookie", "mi_did", "hostname",
        "web_port", "dlna_port", "music_path", "default_volume",
        "enable_dlna", "enable_voice", "enable_ha", "ha_url",
        "ha_token", "ha_control", "ha_poll_sec", "pull_ask_sec",
        "ai_enabled", "ai_keywords", "ai_url", "ai_token", "ai_timeout",
        "ai_voice_hint", "ai_notify_entity", "ai_ack",
    ):
        if key not in data or data[key] is None:
            continue
        if key in sensitive and not str(data[key]).strip():
            continue
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
    # scan 现在是同步函数，放线程池执行避免卡住事件循环
    n = await asyncio.to_thread(state.library.scan)
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


@app.get("/api/ha/media_players")
async def api_ha_media_players():
    """列出 HA 里的 media_player 实体，供界面给每台音箱挑一个"""
    if state.ha_client is None or not state.ha_client.configured:
        raise HTTPException(status_code=400, detail="HA 未配置（需要地址与长期令牌）")
    return await state.ha_client.list_media_players()


@app.get("/api/ha/state")
async def api_ha_state(entity_id: str):
    """读一个实体的状态，并给出映射后的播放状态（自测用）

    验证「播本地音乐时 HA 的 state 会不会变成 playing」就靠它。
    """
    if state.ha_client is None or not state.ha_client.configured:
        raise HTTPException(status_code=400, detail="HA 未配置（需要地址与长期令牌）")
    if not entity_id:
        raise HTTPException(status_code=400, detail="请提供 entity_id")
    raw = await state.ha_client.get_state(entity_id)
    if raw is None:
        raise HTTPException(status_code=404, detail=f"读不到实体 {entity_id}")
    probe = HASpeaker(state.ha_client, entity_id, ttl=0)
    return {
        "entity_id": entity_id,
        "state": raw.get("state", ""),
        "attributes": raw.get("attributes", {}),
        "mapped": await probe.status(force=True),
    }


@app.post("/api/speakers")
async def api_save_speaker(request: Request):
    """保存单台音箱的 HA 媒体实体（body: {did, ha_entity}）"""
    body = await request.json()
    did = (body.get("did") or "").strip()
    if not did:
        raise HTTPException(status_code=400, detail="缺少 did")
    sp = state.config.get_speaker(did)
    sp.ha_entity = (body.get("ha_entity") or "").strip()
    state.config.save()
    log.info(f"音箱 {sp.name or did} 的 HA 媒体实体设为 {sp.ha_entity or '（无）'}")
    return {"ok": True, "did": did, "ha_entity": sp.ha_entity}


# ==================== AI 桥接 ====================
@contextlib.asynccontextmanager
async def _ai_bridge():
    """测试用：优先复用运行中的桥接；未启用时按当前配置临时建一个

    临时实例只为读配置、发一次请求，用完即关，不会参与语音分发。
    """
    if state.ai_bridge is not None:
        yield state.ai_bridge
        return
    tmp = AIBridge(state.config, state.ha_client)
    try:
        yield tmp
    finally:
        await tmp.close()


@app.get("/api/ai/status")
async def api_ai_status():
    async with _ai_bridge() as bridge:
        return bridge.status()


@app.post("/api/ai/match")
async def api_ai_match(request: Request):
    """只试关键词匹配，不调用接口"""
    body = await request.json()
    query = (body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="请输入要测试的语句")
    kws = keywords_of(body["keywords"]) if body.get("keywords") is not None else None
    async with _ai_bridge() as bridge:
        hit = bridge.match(query, kws)
    if hit is None:
        return {"matched": False, "keyword": "", "text": ""}
    return {"matched": True, "keyword": hit[0], "text": hit[1]}


@app.post("/api/ai/test")
async def api_ai_test(request: Request):
    """真的调一次接口，拿回复（但不播报）"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="请输入要发送的内容")

    url = (body.get("url") or state.config.ai_url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="请先填写接口地址")

    t0 = time.time()
    async with _ai_bridge() as bridge:
        reply = await bridge.ask(text, url=url, token=(body.get("token") or "").strip())
    return {
        "ok": bool(reply),
        "reply": reply,
        "ms": int((time.time() - t0) * 1000),
        "error": "" if reply else "接口没有返回内容，详见服务日志",
    }


@app.post("/api/ai/speak")
async def api_ai_speak(request: Request):
    """用当前配置播报一句，验证 notify 实体是否可用"""
    body = await request.json()
    msg = (body.get("message") or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="请输入要播报的文字")
    if state.ha_client is None or not state.ha_client.configured:
        raise HTTPException(status_code=400, detail="HA 未配置（需要地址与长期令牌）")
    if not (state.config.ai_notify_entity or "").strip():
        raise HTTPException(status_code=400, detail="未填写播报实体（notify.*）")

    async with _ai_bridge() as bridge:
        ok = await bridge.speak(msg)
    if not ok:
        raise HTTPException(status_code=400, detail="播报失败，详见服务日志")
    return {"ok": True}


# ==================== 媒体文件 ====================
@app.get("/music/{path:path}")
async def api_music_file(path: str, request: Request):
    full = os.path.join(state.config.music_path, path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="文件不存在")
    return _audio_stream(full, f"/music/{path}", request)


@app.get("/cache/{name}")
async def api_cache_file(name: str, request: Request):
    full = os.path.join(state.config.cache_dir, name)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="缓存不存在")
    return _audio_stream(full, f"/cache/{name}", request)


def _audio_stream(full: str, key: str, request: Request):
    """可控的音频流响应：暂停/停止时可被随时掐断"""
    if state.streams.is_blocked(key):
        return Response(status_code=403, text="playback stopped")

    size = os.path.getsize(full)
    rng = request.headers.get("Range")
    start, end = parse_range(rng, size)
    last = size - 1 if end is None else end
    length = max(1, last - start + 1)

    headers = {
        "Content-Type": guess_content_type(full),
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
    }
    status = 200
    if rng:
        headers["Content-Range"] = f"bytes {start}-{last}/{size}"
        status = 206
    return StreamingResponse(
        state.streams.iter_file(full, key, start, end),
        status_code=status,
        headers=headers,
    )


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(
        level=logging.DEBUG if state.config.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run(app, host="0.0.0.0", port=state.config.web_port)
