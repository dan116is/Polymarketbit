"""Run the POLYSIGNAL engine.

Usage:
    python scripts/run_engine.py                 # PAPER mode (default)
    python scripts/run_engine.py --mode LIVE     # requires GATE1+GATE2 green
    python scripts/run_engine.py --minutes 60    # bounded run (default: forever)

LIVE here still means *signals only* — the code never trades. LIVE display is
refused by RISK unless both gates are green in the store.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys

sys.path.insert(0, ".")

from polysignal.engine import Engine
from polysignal.store import Store


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="PAPER", choices=["PAPER", "LIVE"])
    ap.add_argument("--minutes", type=float, default=0, help="0 = run forever")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--with-delivery", action="store_true",
                    help="start Telegram + PWA server (M3)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = json.load(open(args.config))
    store = Store(cfg["runtime"]["db_path"])
    if args.mode == "LIVE" and not (store.gate_green("GATE1") and store.gate_green("GATE2")):
        print("LIVE מסורב: שני השערים חייבים להיות ירוקים במסד הנתונים. "
              "אין override. ראה reports/GATE1_NO_GO.md")
        store.close()
        sys.exit(2)
    engine = Engine(cfg, store, mode=args.mode)
    if cfg.get("m6", {}).get("shadow_enabled", False):
        from polysignal.executor import LiveExecutor, ShadowExecutor
        engine.executor = ShadowExecutor(store, cfg)
        engine.live_executor = LiveExecutor(store, engine.risk, cfg)
        asyncio.get_running_loop().create_task(engine.executor.warmup())

    if args.with_delivery:
        from polysignal.delivery.telegram import TelegramNotifier
        from polysignal.delivery.web import WebServer
        tg = TelegramNotifier.from_env()
        if tg:
            engine.signal_hooks.append(tg.on_signal)
            engine.alert_hooks.append(tg.send)
            asyncio.get_running_loop().create_task(
                tg.command_loop(engine.risk, engine))
        web = WebServer(engine)
        await web.start()
        engine.status_hooks.append(web.broadcast)

    # graceful shutdown: Task Scheduler stop / Ctrl+C closes the DB cleanly
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, sig_name):
            with contextlib.suppress(NotImplementedError):  # Windows event loop
                loop.add_signal_handler(getattr(signal, sig_name), engine.stop)

    store.log_event("ENGINE_START", json.dumps({"mode": args.mode}))
    stopper_task = None
    if args.minutes > 0:
        async def stopper():
            await asyncio.sleep(args.minutes * 60)
            engine.stop()
        stopper_task = asyncio.get_running_loop().create_task(stopper())
    try:
        await engine.run()
    finally:
        if stopper_task:
            stopper_task.cancel()
        store.log_event("ENGINE_STOP", "")
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
