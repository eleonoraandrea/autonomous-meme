"""CLI for collection, deterministic replay, and virtual account reporting."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from .engine import PaperEngine, limits_from_env
from .market import DexScreener, Snapshot
from .store import Store
from .config import load_env


def main() -> int:
    load_env()
    parser = argparse.ArgumentParser(description="Paper-only token tracker; no Solana transaction code")
    parser.add_argument("command", choices=["once", "run", "discover", "report", "replay", "tui", "web"])
    parser.add_argument("--db", default=os.getenv("PAPER_DB", "data/paper.sqlite3"))
    parser.add_argument("--output", help="New DB for replay; required with replay")
    parser.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL_SECONDS", "60")), help="Polling seconds, at least 10")
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8080")))
    args = parser.parse_args()
    if os.getenv("MODE", "paper") != "paper":
        parser.error("This application only supports MODE=paper")
    if args.interval < 10:
        parser.error("--interval must be at least 10 seconds")
    try:
        initial_cash = float(os.getenv("INITIAL_CASH_USD", "1000"))
    except ValueError:
        parser.error("INITIAL_CASH_USD must be numeric")
    if args.command == "web":
        if not 1 <= args.port <= 65535:
            parser.error("--port must be between 1 and 65535")
        from .web import serve
        serve(args.db, args.host, args.port)
        return 0
    if args.command == "tui":
        from .tui import run_tui
        run_tui(args.db)
        return 0
    if args.command == "replay":
        if not args.output or os.path.exists(args.output) or os.path.abspath(args.db) == os.path.abspath(args.output):
            parser.error("--output must be a new DB path different from --db")
        source, target = Store(args.db, initial_cash), Store(args.output, initial_cash)
        engine = PaperEngine(target, limits_from_env())
        for row in source.observations():
            snap = Snapshot(*(row[key] for key in ("mint", "pair", "received_at", "created_at", "price",
                                                 "liquidity", "volume_5m", "buys_5m", "sells_5m", "raw")))
            engine.on_snapshot(snap)
        print(json.dumps(target.report(), indent=2))
        return 0
    store = Store(args.db, initial_cash)
    if args.command == "report":
        print(json.dumps(store.report(), indent=2))
        return 0
    if args.command == "discover":
        from .discovery import run_discovery
        try:
            asyncio.run(run_discovery(store))
        except KeyboardInterrupt:
            pass
        return 0
    try:
        limits = limits_from_env()
    except ValueError as exc:
        parser.error(str(exc))
    client, engine = DexScreener(), PaperEngine(store, limits)
    while True:
        try:
            tracked = {row["mint"]: row["pair"] for row in store.positions()}
            discovered = store.discovery_candidates(int(time.time() * 1000))
            snapshots = client.snapshots(tracked_pairs=tracked, discovered_mints=discovered)
            missing = set(tracked) - {snap.mint for snap in snapshots}
            if missing:
                print(f"WARNING: no market snapshot for open positions: {sorted(missing)}",
                      file=sys.stderr, flush=True)
            now = int(time.time() * 1000)
            # Never process data before its receipt time; prefer held positions on ties.
            for snap in sorted(snapshots, key=lambda item: (item.received_at, item.mint not in tracked, item.mint)):
                action, reason = engine.on_snapshot(snap, now=now)
                print(json.dumps({"mint": snap.mint, "pair": snap.pair, "time": snap.received_at,
                                  "action": action, "reason": reason}), flush=True)
            print(json.dumps({"candidates": len(snapshots), "report": store.report()}), flush=True)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Collection failed (no new trades): {exc}", file=sys.stderr, flush=True)
            if args.command == "once":
                return 1
        if args.command == "once":
            break
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
