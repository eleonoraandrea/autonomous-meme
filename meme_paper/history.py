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
