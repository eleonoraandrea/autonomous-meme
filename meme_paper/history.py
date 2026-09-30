"""Read-only, snapshot-pinned pages of every stored paper decision."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

PAGE_SIZE = 25


def load_history(path: str, kind: str = "all", page: int = 1, anchor: int | None = None) -> dict:
    if kind not in ("all", "rl") or page < 1 or page > 100_000 or anchor is not None and anchor < 0:
        raise ValueError("invalid history parameters")
    if not Path(path).is_file():
        return {"kind": kind, "page": page, "page_size": PAGE_SIZE, "anchor": 0, "total": 0, "items": []}
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        try:
            if anchor is None:
                anchor = db.execute("SELECT COALESCE(MAX(id),0) FROM decisions").fetchone()[0]
            where = "d.id<=?" + (" AND json_extract(d.params,'$.rl.mode')='experimental'" if kind == "rl" else "")
            total = db.execute(f"SELECT COUNT(*) FROM decisions d WHERE {where}", (anchor,)).fetchone()[0]
            rows = db.execute(f"""SELECT d.id,o.received_at,o.mint,
                json_extract(o.raw,'$.baseToken.symbol') AS symbol,
                d.action,d.reason,d.strategy_version,d.params
                FROM decisions d JOIN observations o ON o.id=d.observation_id
                WHERE {where} ORDER BY d.id DESC LIMIT ? OFFSET ?""",
                (anchor, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
            items = [dict(row) for row in rows]
            for item in items:
                item["symbol"] = str(item["symbol"] or item["mint"][:12])[:32]
                item["params"] = json.loads(item["params"]) if item["params"] else None
        finally:
            db.rollback()
    finally:
        db.close()
    return {"kind": kind, "page": page, "page_size": PAGE_SIZE, "anchor": anchor,
            "total": total, "items": items}


def load_operations(path: str, page: int = 1, anchor: int | None = None) -> dict:
    """Every persisted fill, each joined to the RL episode (if any) it opened or closed."""
    if page < 1 or page > 100_000 or anchor is not None and anchor < 0:
        raise ValueError("invalid operations parameters")
    empty_summary = {"buys": 0, "sells": 0, "fees_usd": 0.0, "closed_pnl_usd": 0.0,
                     "open_positions": 0, "unfilled_exits": 0}
    if not Path(path).is_file():
        return {"page": page, "page_size": PAGE_SIZE, "anchor": 0, "total": 0,
                "items": [], "summary": empty_summary}
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        try:
            if anchor is None:
                anchor = db.execute("SELECT COALESCE(MAX(id),0) FROM fills").fetchone()[0]
            total = db.execute("""SELECT COUNT(*) FROM fills f JOIN observations o ON o.id=f.observation_id
                WHERE f.id<=?""", (anchor,)).fetchone()[0]
            rows = db.execute("""SELECT f.id,o.received_at,o.mint,
                json_extract(o.raw,'$.baseToken.symbol') AS symbol,
                f.side,f.units,f.price,f.cash_delta,f.fee,
                e.status AS episode_status,e.state AS episode_state,e.reward AS episode_reward,
                e.entry_cost,e.exit_proceeds
                FROM fills f JOIN observations o ON o.id=f.observation_id
                LEFT JOIN rl_episodes e
                  ON (e.entry_observation_id=f.observation_id OR e.exit_observation_id=f.observation_id)
                WHERE f.id<=? ORDER BY f.id DESC LIMIT ? OFFSET ?""",
                (anchor, PAGE_SIZE, (page - 1) * PAGE_SIZE)).fetchall()
            items = [dict(row) for row in rows]
            for item in items:
                item["symbol"] = str(item["symbol"] or item["mint"][:12])[:32]
            summary = db.execute("""SELECT COALESCE(SUM(side='BUY'),0) buys,
                COALESCE(SUM(side='SELL'),0) sells, COALESCE(SUM(fee),0) fees_usd
                FROM fills""").fetchone()
            closed = db.execute("""SELECT COALESCE(SUM(exit_proceeds-entry_cost),0),COUNT(*)
                FROM rl_episodes WHERE status='closed'""").fetchone()
            open_positions = db.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
            unfilled = db.execute("SELECT COUNT(*) FROM decisions WHERE action='UNFILLED'").fetchone()[0]
        finally:
            db.rollback()
    finally:
        db.close()
    return {"page": page, "page_size": PAGE_SIZE, "anchor": anchor, "total": total, "items": items,
            "summary": {"buys": summary["buys"], "sells": summary["sells"],
                        "fees_usd": round(summary["fees_usd"], 4),
                        "closed_pnl_usd": round(closed[0], 4), "closed_count": closed[1],
                        "open_positions": open_positions, "unfilled_exits": unfilled}}


def read_rl_events(path: str, after_id: int, limit: int = 100) -> tuple[list[dict], int]:
    """Return committed RL evaluations after cursor and a consistent high watermark."""
    if after_id < 0 or not 1 <= limit <= 100:
        raise ValueError("invalid event cursor")
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        try:
            high = db.execute("SELECT COALESCE(MAX(id),0) FROM decisions").fetchone()[0]
            rows = db.execute("""SELECT d.id,o.received_at,o.mint,
                json_extract(o.raw,'$.baseToken.symbol') AS symbol,
                d.action,d.reason,d.strategy_version,d.params
                FROM decisions d JOIN observations o ON o.id=d.observation_id
                WHERE d.id>? AND d.id<=? AND json_extract(d.params,'$.rl.mode')='experimental'
                ORDER BY d.id ASC LIMIT ?""", (after_id, high, limit)).fetchall()
            items = [dict(row) for row in rows]
            for item in items:
                item["symbol"] = str(item["symbol"] or item["mint"][:12])[:32]
                item["params"] = json.loads(item["params"])
        finally:
            db.rollback()
    finally:
        db.close()
    # Don't skip events if a backlog exceeds the bounded batch size.
    return items, items[-1]["id"] if len(items) == limit else high
