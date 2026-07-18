"""PWA server — serves the single-file app and pushes engine status over WS."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from aiohttp import WSMsgType, web

log = logging.getLogger("polysignal.web")

PWA_DIR = Path(__file__).resolve().parent.parent / "pwa"


class WebServer:
    def __init__(self, engine, host: str | None = None, port: int | None = None):
        self.engine = engine
        self.host = host or os.environ.get("POLYSIGNAL_HOST", "127.0.0.1")
        self.port = int(port or os.environ.get("POLYSIGNAL_PORT", "8787"))
        self.clients: set[web.WebSocketResponse] = set()
        self.app = web.Application()
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/manifest.json", self.manifest)
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/ws", self.ws)
        self.app.router.add_post("/acted", self.acted)
        self.runner: web.AppRunner | None = None

    async def index(self, _req):
        return web.FileResponse(PWA_DIR / "index.html")

    async def manifest(self, _req):
        return web.FileResponse(PWA_DIR / "manifest.json")

    async def health(self, _req):
        return web.json_response(self.engine.status_payload())

    async def acted(self, req):
        """Daniel marks that he actually clicked this window's recommendation.
        Feeds LIVE risk accounting and measures the real hand latency."""
        import json as _json
        import time as _time
        body = await req.json()
        win = int(body["window_ts"])
        self.engine.store.upsert_window(win, self.engine.mode, acted=1)
        st = self.engine.window
        detail = {"window_ts": win, "acted_ts": _time.time()}
        if st and st.window_ts == win:
            if st.signal_ts:
                detail["hand_latency_s"] = round(_time.time() - st.signal_ts, 2)
            if st.signal and st.signal.side != "PASS":
                side_book = st.book.get(st.signal.side.lower())
                detail["ask_at_act"] = side_book[1] if side_book else None
        self.engine.store.log_event("ACTED", _json.dumps(detail))
        return web.json_response({"ok": True, "window_ts": win})

    async def ws(self, req):
        w = web.WebSocketResponse(heartbeat=15)
        await w.prepare(req)
        self.clients.add(w)
        try:
            await w.send_json(self.engine.status_payload())
            async for msg in w:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            self.clients.discard(w)
        return w

    async def broadcast(self, payload: dict) -> None:
        dead = []
        for c in self.clients:
            try:
                await c.send_json(payload)
            except Exception:
                dead.append(c)
        for c in dead:
            self.clients.discard(c)

    async def start(self):
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.host, self.port)
        await site.start()
        log.info("PWA at http://%s:%d", self.host, self.port)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()
