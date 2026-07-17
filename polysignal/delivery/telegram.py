"""Telegram delivery — signal alerts + kill-switch command.

Token/chat id come from .env only. Round-trip latency is measured and logged
so the <=2s p95 DONE criterion is verifiable from the events table.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp

log = logging.getLogger("polysignal.telegram")

API = "https://api.telegram.org"


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self._session: aiohttp.ClientSession | None = None
        self.latencies_ms: list[float] = []

    @classmethod
    def from_env(cls) -> "TelegramNotifier | None":
        tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not tok or not chat:
            log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set — alerts disabled")
            return None
        return cls(tok, chat)

    async def _ensure(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5))
        return self._session

    async def send(self, text: str) -> float | None:
        """Send message; returns round-trip ms."""
        s = await self._ensure()
        t0 = time.time()
        try:
            async with s.post(f"{API}/bot{self.token}/sendMessage", json={
                "chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }) as r:
                ok = r.status == 200
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.error("telegram send failed: %r", e)
            return None
        ms = (time.time() - t0) * 1000
        if ok:
            self.latencies_ms.append(ms)
        return ms if ok else None

    async def on_signal(self, payload: dict) -> None:
        sig = payload.get("signal") or {}
        side = sig.get("side", "PASS")
        if side == "PASS":
            return
        emoji = "🟢⬆️" if side == "UP" else "🔴⬇️"
        txt = (f"{emoji} <b>{side}</b> ${sig.get('stake', 0):.0f}\n"
               f"פער {sig.get('edge', 0)*100:+.1f}¢ | P_fair {sig.get('p_fair', 0):.2f}\n"
               f"ask עלייה {payload.get('ask_up')} | ask ירידה {payload.get('ask_down')}\n"
               f"נותרו {payload.get('t_remaining', '?')} שניות | מצב {payload.get('mode')}")
        ms = await self.send(txt)
        log.info("signal alert sent in %.0fms", ms or -1)

    async def command_loop(self, risk, engine=None) -> None:
        """Long-poll for /kill /resume /status — the M5 kill-switch."""
        s = await self._ensure()
        offset = 0
        while True:
            try:
                async with s.get(f"{API}/bot{self.token}/getUpdates", params={
                    "timeout": 25, "offset": offset,
                    "allowed_updates": '["message"]',
                }, timeout=aiohttp.ClientTimeout(total=35)) as r:
                    updates = (await r.json()).get("result", [])
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(3)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                    continue  # ignore strangers
                text = (msg.get("text") or "").strip().lower()
                if text.startswith("/kill"):
                    risk.kill(True)
                    await self.send("🛑 מתג החירום הופעל — אין איתותים עד /resume")
                elif text.startswith("/resume"):
                    risk.kill(False)
                    await self.send("▶️ מתג החירום כובה — האיתותים חזרו")
                elif text.startswith("/status") and engine is not None:
                    p = engine.status_payload()
                    sig = p.get("signal") or {}
                    await self.send(
                        f"מצב {p['mode']} | חלון {p.get('window_ts')} "
                        f"(נותרו {p.get('t_remaining')} שניות)\n"
                        f"איתות {sig.get('side')} | פער {sig.get('edge', 0)*100:+.1f}¢\n"
                        f"פידים: binance {'תקין' if p['feeds']['binance'] else '⚠ מנותק'} | "
                        f"oracle {'תקין' if p['feeds']['oracle'] else '⚠ מנותק'}\n"
                        f"שערים: G1 {'✓' if p['gate1'] else '✗'} | G2 {'✓' if p['gate2'] else '✗'}")

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
