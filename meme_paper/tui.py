"""Read-only terminal dashboard for the virtual ledger."""

from __future__ import annotations

import curses
import json
import re
import sqlite3
import time
from pathlib import Path

from .analytics import (WINDOW_MS, analyze_samples, cohort_summary,
                        decision_funnel)
from .engine import Limits, STRATEGY_VERSION, SUPPORTED_QUOTES


def load_token_analysis(path: str, mint: str, now_ms: int | None = None) -> dict:
    """15-minute observed window for one mint, even after it leaves the latest-20 page."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", mint):
        raise ValueError("invalid mint")
    if not Path(path).is_file():
        return {"missing": True, "found": False, "mint": mint}
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")  # One consistent snapshot, same contract as load_dashboard.
        try:
            row = db.execute("""SELECT * FROM observations WHERE mint=? AND received_at<=?
                ORDER BY received_at DESC,id DESC LIMIT 1""", (mint, now_ms)).fetchone()
            if row is None:
                return {"missing": False, "found": False, "mint": mint}
            window = [dict(r) for r in reversed(db.execute("""SELECT received_at,price,liquidity
                FROM observations WHERE mint=? AND pair=? AND received_at BETWEEN ? AND ?
                ORDER BY received_at DESC,id DESC LIMIT 120""",
                (mint, row["pair"], now_ms - WINDOW_MS, now_ms)).fetchall())]
            raw = json.loads(row["raw"])
            quote = (raw.get("quoteToken") or {}).get("address")
            decision = db.execute("SELECT action FROM decisions WHERE observation_id=?", (row["id"],)).fetchone()
            return {"missing": False, "found": True, "mint": mint, "pair": row["pair"],
                    "symbol": str(((raw.get("baseToken") or {}).get("symbol")) or mint[:12])[:32],
                    "received_at": row["received_at"],
                    "decision": decision["action"] if decision else None,
                    "analysis": analyze_samples(window, now_ms, row["buys_5m"], row["sells_5m"],
                                                row["created_at"], quote in SUPPORTED_QUOTES)}
        finally:
            db.rollback()
    finally:
        db.close()


def load_dashboard(path: str, now_ms: int | None = None) -> dict:
    """Read a consistent snapshot; never create a database or modify the ledger."""
    if not Path(path).is_file():
        return {"missing": True, "path": path}
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")  # One consistent read snapshot across all queries.
        try:
            state = {row["key"]: row["value"] for row in db.execute("SELECT key,value FROM state")}
            positions = [dict(row) for row in db.execute("""SELECT p.mint,p.pair,p.units,p.entry_price,p.cost,
                (SELECT o.price FROM observations o WHERE o.mint=p.mint AND o.pair=p.pair
                 AND o.received_at<=? ORDER BY o.received_at DESC,o.id DESC LIMIT 1) AS price,
                (SELECT o.received_at FROM observations o WHERE o.mint=p.mint AND o.pair=p.pair
                 AND o.received_at<=? ORDER BY o.received_at DESC,o.id DESC LIMIT 1) AS seen
                FROM positions p ORDER BY p.entry_at DESC""", (now_ms, now_ms))]
            recent = [dict(row) for row in db.execute("""SELECT o.received_at,o.mint,d.action,d.reason,d.params
                FROM decisions d JOIN observations o ON o.id=d.observation_id
                WHERE o.received_at<=? ORDER BY o.received_at DESC,d.id DESC LIMIT 12""", (now_ms,))]
            for item in recent:
                item["params"] = json.loads(item["params"]) if item["params"] else None
            latest = db.execute("SELECT MAX(received_at) FROM observations WHERE received_at<=?", (now_ms,)).fetchone()[0]
            latest_pools = db.execute("""SELECT * FROM (
                SELECT o.*, ROW_NUMBER() OVER (PARTITION BY o.mint ORDER BY o.received_at DESC,o.id DESC) AS rank
                FROM observations o WHERE o.received_at<=?
            ) WHERE rank=1 ORDER BY received_at DESC, id DESC LIMIT 20""", (now_ms,)).fetchall()
            markets = []
            for pool in latest_pools:
                raw = json.loads(pool["raw"])
                history = list(reversed(db.execute("""SELECT price,received_at FROM observations
                    WHERE mint=? AND pair=? AND received_at<=? ORDER BY received_at DESC,id DESC LIMIT 16""",
                    (pool["mint"], pool["pair"], now_ms)).fetchall()))
                window = [dict(row) for row in reversed(db.execute("""SELECT received_at,price,liquidity
                    FROM observations WHERE mint=? AND pair=? AND received_at BETWEEN ? AND ?
                    ORDER BY received_at DESC,id DESC LIMIT 120""",
                    (pool["mint"], pool["pair"], now_ms - WINDOW_MS, now_ms)).fetchall())]
                previous = history[-2] if len(history) >= 2 else None
                token = raw.get("baseToken") or {}
                quote = raw.get("quoteToken") or {}
                analysis = analyze_samples(window, now_ms, pool["buys_5m"], pool["sells_5m"],
                                           pool["created_at"], quote.get("address") in SUPPORTED_QUOTES)
                decision = db.execute("SELECT action,reason,params FROM decisions WHERE observation_id=?", (pool["id"],)).fetchone()
                markets.append({
                    "mint": pool["mint"], "pair": pool["pair"],
                    "symbol": str(token.get("symbol") or "—")[:32],
                    "name": str(token.get("name") or "Unknown token")[:80],
                    "dex": str(raw.get("dexId") or "unknown")[:32],
                    "quote": str(quote.get("symbol") or "—")[:20],
                    "price": pool["price"], "liquidity": pool["liquidity"],
                    "volume_5m": pool["volume_5m"], "buys_5m": pool["buys_5m"],
                    "sells_5m": pool["sells_5m"], "received_at": pool["received_at"],
                    "created_at": pool["created_at"],
                    "sample_change_pct": (pool["price"] / previous["price"] - 1) * 100 if previous else None,
                    "sample_interval_ms": pool["received_at"] - previous["received_at"] if previous else None,
                    "history": [p["price"] for p in history],
                    "analysis": analysis,
                    "decision": decision["action"] if decision else "—",
                    "reason": decision["reason"] if decision else "No decision",
                    "params": json.loads(decision["params"]) if decision and decision["params"] else None,
                })
            count = db.execute("SELECT COUNT(*) FROM observations WHERE received_at<=?", (now_ms,)).fetchone()[0]
            total_unique = db.execute("SELECT COUNT(DISTINCT mint) FROM observations WHERE received_at<=?", (now_ms,)).fetchone()[0]
            fills = db.execute("SELECT COUNT(*) FROM fills f JOIN observations o ON o.id=f.observation_id WHERE o.received_at<=?", (now_ms,)).fetchone()[0]
            closed = db.execute("SELECT COUNT(*) FROM rl_episodes WHERE status='closed'").fetchone()[0]
            open_episodes = db.execute("SELECT COUNT(*) FROM rl_episodes WHERE status='open'").fetchone()[0]
            trained_states = db.execute("SELECT COUNT(*) FROM rl_values").fetchone()[0]
            evaluated = db.execute("""SELECT COUNT(*) FROM decisions d
                JOIN observations o ON o.id=d.observation_id
                WHERE json_extract(d.params,'$.rl.mode')='experimental' AND o.received_at<=?""", (now_ms,)).fetchone()[0]
            all_actions = dict(db.execute("""SELECT d.action,COUNT(*) FROM decisions d
                JOIN observations o ON o.id=d.observation_id WHERE o.received_at<=?
                GROUP BY d.action""", (now_ms,)).fetchall())
            rl_actions = dict(db.execute("""SELECT json_extract(d.params,'$.rl.action'),COUNT(*) FROM decisions d
                JOIN observations o ON o.id=d.observation_id
                WHERE o.received_at<=? AND json_extract(d.params,'$.rl.mode')='experimental'
                GROUP BY d.action""", (now_ms,)).fetchall())
            last_policy = db.execute("""SELECT o.mint,o.received_at,d.action,d.reason,d.params
                FROM decisions d JOIN observations o ON o.id=d.observation_id
                WHERE json_extract(d.params,'$.rl.mode')='experimental' AND o.received_at<=?
                ORDER BY d.id DESC LIMIT 1""", (now_ms,)).fetchone()
            last_policy = dict(last_policy) if last_policy else None
            if last_policy:
                last_policy["params"] = json.loads(last_policy["params"])
            feed = db.execute("SELECT status,changed_at,last_event_at,last_error FROM feed_health WHERE source='PumpPortal'").fetchone()
            since = now_ms - 86_400_000
            discoveries_total = db.execute("SELECT COUNT(*) FROM discovery_events WHERE received_at BETWEEN ? AND ?", (since, now_ms)).fetchone()[0]
            matched = db.execute("""SELECT COUNT(DISTINCT e.mint) FROM discovery_events e
                WHERE e.received_at BETWEEN ? AND ? AND EXISTS
                (SELECT 1 FROM observations o WHERE o.mint=e.mint AND o.received_at>=e.received_at)""", (since, now_ms)).fetchone()[0]
            discovered_mints = db.execute("SELECT COUNT(DISTINCT mint) FROM discovery_events WHERE received_at BETWEEN ? AND ?", (since, now_ms)).fetchone()[0]
            discoveries = []
            for event in db.execute("""SELECT e.mint,e.received_at,e.raw,
                (SELECT MIN(o.received_at) FROM observations o WHERE o.mint=e.mint
                 AND o.received_at>=e.received_at) AS first_priced
                FROM discovery_events e WHERE e.received_at<=? ORDER BY e.id DESC LIMIT 12""", (now_ms,)):
                raw = json.loads(event['raw'])
                discoveries.append({'mint': event['mint'], 'received_at': event['received_at'],
                                    'symbol': str(raw.get('symbol') or event['mint'][:12])[:32],
                                    'first_priced': event['first_priced']})
            funnel_since = now_ms - 86_400_000
            funnel_obs = db.execute("""SELECT COUNT(*) FROM observations
                WHERE received_at BETWEEN ? AND ?""", (funnel_since, now_ms)).fetchone()[0]
            funnel_rows = db.execute("""SELECT d.action,d.reason,COUNT(*) FROM decisions d
                JOIN observations o ON o.id=d.observation_id
                WHERE d.strategy_version=? AND o.received_at BETWEEN ? AND ?
                GROUP BY d.action,d.reason""", (STRATEGY_VERSION, funnel_since, now_ms)).fetchall()
            funnel = decision_funnel([tuple(row) for row in funnel_rows], funnel_obs)
            funnel.update({"window_hours": 24, "strategy_version": STRATEGY_VERSION})
        finally:
            db.rollback()
    finally:
        db.close()
    cash = state.get("cash", 0)
    estimated = cash + sum(p["units"] * (p["price"] or p["entry_price"]) * .96 for p in positions)
    limits = Limits()
    exposure = sum(max(p['cost'], p['units'] * (p['price'] or p['entry_price'])) for p in positions)
    stale_positions = sum(p['seen'] is None or now_ms - p['seen'] > limits.max_position_quote_age_ms
                          for p in positions)
    cap = estimated * limits.max_total_exposure_fraction
    reserve = estimated * limits.min_cash_reserve_fraction
    reasons = []
    if stale_positions:
        reasons.append('quotazioni stale su posizioni aperte')
    if len(positions) >= limits.max_positions:
        reasons.append('numero massimo posizioni')
    if cap - exposure <= 1:
        reasons.append('limite esposizione totale')
    if cash - reserve <= 1:
        reasons.append('riserva di cassa')
    if estimated <= state.get('day_start_equity', estimated) * (1 - limits.max_daily_loss_fraction):
        reasons.append('limite perdita giornaliera')
    if estimated <= state.get('peak_equity', estimated) * (1 - limits.max_drawdown_fraction):
        reasons.append('limite drawdown')
    return {"missing": False, "cash": cash, "estimated": estimated, "positions": positions,
            "recent": recent, "latest": latest, "observations": count, "fills": fills,
            "markets": markets, "now": now_ms, "path": path,
            "cohort": cohort_summary(markets, now_ms, total_unique),
            "decision_summary": {"total": sum(all_actions.values()), "actions": all_actions,
                                  "rl_actions": rl_actions},
            "funnel": funnel,
            "capital_risk": {"exposure_usd": exposure, "exposure_cap_usd": cap,
                             "headroom_usd": max(0, cap - exposure), "cash_reserve_usd": reserve,
                             "stale_positions": stale_positions, "max_positions": limits.max_positions,
                             "entry_paused": bool(reasons), "reasons": reasons},
            "rl_summary": {"closed_episodes": closed, "open_episodes": open_episodes,
                           "trained_states": trained_states, "evaluated_candidates": evaluated,
                           "policy_version": "episodic-q-1", "last_policy_decision": last_policy},
            "source": "DexScreener token profiles / pairs",
            "discovery": {"source": "PumpPortal (creation events only)",
                          "status": feed['status'] if feed else 'not_started',
                          "last_event_at": feed['last_event_at'] if feed else None,
                          "last_error": feed['last_error'] if feed else None,
                          "events": discoveries_total, "unique_mints": discovered_mints,
                          "priced_mints": matched, "recent": discoveries}}


def dashboard_lines(data: dict) -> list[str]:
    """Pure formatter, independently testable without an interactive terminal."""
    lines = ["MEME PAPER  |  SOLANA SIMULATION ONLY  |  NO SIGNER / NO TRANSACTIONS",
             "q: quit   r: refresh     Updates automatically every 2s", ""]
    if data["missing"]:
        return lines + ["No paper database yet: " + data["path"],
                        "Start collection separately: python -m meme_paper run"]
    age = "no observations" if data["latest"] is None else f"{max(0, (data['now'] - data['latest']) // 1000)}s ago"
    lines += [f"Virtual cash: ${data['cash']:,.2f}  Estimated equity*: ${data['estimated']:,.2f}",
              f"Observations: {data['observations']}  Simulated fills: {data['fills']}  Feed last seen: {age}",
              "*Estimate with 4% haircut; not an executable liquidation quote.", "", "OPEN POSITIONS"]
    if not data["positions"]:
        lines.append("  (none)")
    for p in data["positions"]:
        age = "missing" if p["seen"] is None else f"{max(0, (data['now'] - p['seen']) // 1000)}s"
        mark = p["units"] * (p["price"] or p["entry_price"]) * .96
        lines.append(f"  {p['mint'][:12]}  cost ${p['cost']:.2f}  est ${mark:.2f}  quote age {age}")
    lines += ["", "RECENT DECISIONS"]
    if not data["recent"]:
        lines.append("  (none)")
    for row in data["recent"]:
        stamp = time.strftime("%H:%M:%S", time.gmtime(row["received_at"] / 1000))
        lines.append(f"  {stamp} UTC  {row['mint'][:12]:12}  {row['action']:8}  {row['reason']}")
    return lines


def run_tui(path: str) -> None:
    def draw(screen):
        curses.curs_set(0)
        screen.nodelay(True)
        screen.timeout(2000)
        while True:
            try:
                lines = dashboard_lines(load_dashboard(path))
            except sqlite3.Error as error:
                lines = ["MEME PAPER | database temporarily unavailable", str(error), "q: quit  r: retry"]
            screen.erase()
            height, width = screen.getmaxyx()
            for y, text in enumerate(lines[:height]):
                if width > 1:
                    screen.addnstr(y, 0, text, width - 1)
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                break

    curses.wrapper(draw)
