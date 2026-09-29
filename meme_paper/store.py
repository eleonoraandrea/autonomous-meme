"""Append-only observations and a persistent virtual-cash ledger."""

from __future__ import annotations

import sqlite3
import math
from contextlib import contextmanager
from pathlib import Path

from .market import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, mint TEXT NOT NULL, pair TEXT NOT NULL,
 received_at INTEGER NOT NULL, created_at INTEGER, price REAL NOT NULL,
 liquidity REAL NOT NULL, volume_5m REAL NOT NULL, buys_5m INTEGER NOT NULL,
 sells_5m INTEGER NOT NULL, raw TEXT NOT NULL,
 UNIQUE(mint, pair, received_at)
);
CREATE INDEX IF NOT EXISTS observations_time ON observations(received_at, id);
CREATE INDEX IF NOT EXISTS observations_mint_time ON observations(mint, received_at DESC);
CREATE TABLE IF NOT EXISTS positions (
 mint TEXT PRIMARY KEY, pair TEXT NOT NULL, units REAL NOT NULL,
 entry_at INTEGER NOT NULL, entry_price REAL NOT NULL, cost REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
 id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL UNIQUE,
 action TEXT NOT NULL, reason TEXT NOT NULL, strategy_version TEXT NOT NULL,
 FOREIGN KEY(observation_id) REFERENCES observations(id)
);
CREATE TABLE IF NOT EXISTS fills (
 id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL, mint TEXT NOT NULL,
 side TEXT NOT NULL, units REAL NOT NULL, price REAL NOT NULL,
 cash_delta REAL NOT NULL, fee REAL NOT NULL,
 FOREIGN KEY(observation_id) REFERENCES observations(id)
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rl_values (
 state TEXT PRIMARY KEY, q REAL NOT NULL, visits INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rl_episodes (
 id INTEGER PRIMARY KEY, mint TEXT NOT NULL, state TEXT NOT NULL,
 entry_observation_id INTEGER NOT NULL, exit_observation_id INTEGER,
 entry_cost REAL NOT NULL, exit_proceeds REAL, reward REAL,
 status TEXT NOT NULL CHECK(status IN ('open','closed')),
 FOREIGN KEY(entry_observation_id) REFERENCES observations(id),
 FOREIGN KEY(exit_observation_id) REFERENCES observations(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_rl_episode ON rl_episodes(mint) WHERE status='open';
CREATE TABLE IF NOT EXISTS discovery_events (
 id INTEGER PRIMARY KEY, signature TEXT NOT NULL UNIQUE, mint TEXT NOT NULL,
 received_at INTEGER NOT NULL, source TEXT NOT NULL, raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS discovery_recent ON discovery_events(received_at DESC, mint);
CREATE TABLE IF NOT EXISTS feed_health (
 source TEXT PRIMARY KEY, status TEXT NOT NULL, changed_at INTEGER NOT NULL,
 last_event_at INTEGER, last_error TEXT
);
"""


class Store:
    def __init__(self, path: str, initial_cash: float = 1000):
        if not math.isfinite(initial_cash) or initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout = 5000")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        if "params" not in {row["name"] for row in self.db.execute("PRAGMA table_info(decisions)")}:
            self.db.execute("ALTER TABLE decisions ADD COLUMN params TEXT")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO state VALUES ('cash', ?)", (initial_cash,))
            self.db.execute("INSERT OR IGNORE INTO state VALUES ('initial_cash', ?)", (initial_cash,))
            self.db.execute("INSERT OR IGNORE INTO state VALUES ('peak_equity', ?)", (initial_cash,))
            self.db.execute("INSERT OR IGNORE INTO state VALUES ('day_start_equity', ?)", (initial_cash,))
            self.db.execute("INSERT OR IGNORE INTO state VALUES ('day_number', -1)")

    @contextmanager
    def transaction(self):
        with self.db:
            yield

    def value(self, key: str) -> float:
        return float(self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()[0])

    def set_value(self, key: str, value: float):
        self.db.execute("UPDATE state SET value=? WHERE key=?", (value, key))

    def insert_snapshot(self, snap: Snapshot) -> int | None:
        cursor = self.db.execute("""INSERT OR IGNORE INTO observations
            (mint,pair,received_at,created_at,price,liquidity,volume_5m,buys_5m,sells_5m,raw)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (snap.mint, snap.pair, snap.received_at, snap.created_at, snap.price,
             snap.liquidity, snap.volume_5m, snap.buys_5m, snap.sells_5m, snap.raw))
        return cursor.lastrowid if cursor.rowcount else None

    def previous(self, mint: str, pair: str, before: int) -> sqlite3.Row | None:
        return self.db.execute("""SELECT * FROM observations WHERE mint=? AND pair=?
            AND received_at<? ORDER BY received_at DESC, id DESC LIMIT 1""", (mint, pair, before)).fetchone()

    def position(self, mint: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM positions WHERE mint=?", (mint,)).fetchone()

    def positions(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM positions").fetchall()

    def last_sell_at(self, mint: str) -> int | None:
        return self.db.execute("""SELECT MAX(o.received_at) FROM fills f
            JOIN observations o ON o.id=f.observation_id WHERE f.mint=? AND f.side='SELL'""",
            (mint,)).fetchone()[0]

    def equity(self, at: int, haircut: float = 0.04) -> float:
        # Never mark to a future observation. Conservative haircut is not a sell guarantee.
        total = self.value("cash")
        for position in self.positions():
            row = self.db.execute("""SELECT price FROM observations WHERE mint=? AND pair=?
                AND received_at<=? ORDER BY received_at DESC, id DESC LIMIT 1""",
                (position["mint"], position["pair"], at)).fetchone()
            total += position["units"] * (row["price"] if row else position["entry_price"]) * (1 - haircut)
        return total

    def portfolio_risk(self, at: int, max_quote_age_ms: int = 120_000) -> dict:
        """Conservative entry exposure; stale/unpriced holdings freeze new entries."""
        positions = self.positions()
        exposure = 0.0
        stale = []
        for position in positions:
            row = self.db.execute("""SELECT price,received_at FROM observations
                WHERE mint=? AND pair=? AND received_at<=?
                ORDER BY received_at DESC,id DESC LIMIT 1""",
                (position['mint'], position['pair'], at)).fetchone()
            if row is None or at - row['received_at'] > max_quote_age_ms:
                stale.append(position['mint'])
            # Price appreciation consumes headroom; price drops cannot release committed cost.
            mark = position['units'] * row['price'] if row else 0
            exposure += max(position['cost'], mark)
        return {'open_positions': len(positions), 'exposure_usd': exposure,
                'stale_positions': stale, 'max_quote_age_s': max_quote_age_ms / 1000}

    def observations(self):
        return self.db.execute("SELECT * FROM observations ORDER BY received_at, id").fetchall()

    def record_discovery(self, signature: str, mint: str, received_at: int, raw: str) -> bool:
        with self.transaction():
            result = self.db.execute("""INSERT OR IGNORE INTO discovery_events
                (signature,mint,received_at,source,raw) VALUES (?,?,?,'PumpPortal',?)""",
                (signature, mint, received_at, raw))
            if result.rowcount:
                self.db.execute("""UPDATE feed_health SET last_event_at=? WHERE source='PumpPortal'""", (received_at,))
            return bool(result.rowcount)

    def feed_status(self, status: str, at: int, error: str | None = None) -> None:
        with self.transaction():
            self.db.execute("""INSERT INTO feed_health(source,status,changed_at,last_event_at,last_error)
                VALUES('PumpPortal',?,?,NULL,?) ON CONFLICT(source) DO UPDATE SET
                status=excluded.status,changed_at=excluded.changed_at,last_error=excluded.last_error""",
                (status, at, error[:180] if error else None))

    def discovery_candidates(self, now: int, limit: int = 15) -> list[str]:
        """Keep observed discoveries in view for a second price sample, then try new mints."""
        if limit < 1 or limit > 30:
            raise ValueError("discovery limit must be 1..30")
        # Reserve a slice for unseen mints even when priced candidates are plentiful.
        active = min(10, limit)
        priced = self.db.execute("""SELECT e.mint,MAX(o.received_at) AS last_price
            FROM discovery_events e JOIN observations o ON o.mint=e.mint
            AND o.received_at>=e.received_at
            WHERE e.received_at BETWEEN ? AND ?
            GROUP BY e.mint ORDER BY last_price DESC LIMIT ?""",
            (now - 1_200_000, now, active)).fetchall()
        unseen = self.db.execute("""SELECT e.mint,MAX(e.received_at) AS last_seen
            FROM discovery_events e WHERE e.received_at BETWEEN ? AND ?
            AND NOT EXISTS (SELECT 1 FROM observations o WHERE o.mint=e.mint
                            AND o.received_at>=e.received_at)
            GROUP BY e.mint ORDER BY last_seen DESC LIMIT ?""",
            (now - 1_200_000, now, limit - len(priced))).fetchall()
        return [row['mint'] for row in priced] + [row['mint'] for row in unseen]

    def report(self) -> dict:
        last = self.db.execute("SELECT MAX(received_at) FROM observations").fetchone()[0]
        return {
            "cash": round(self.value("cash"), 4),
            "estimated_equity": round(self.equity(last), 4) if last else self.value("cash"),
            "open_positions": len(self.positions()),
            "observations": self.db.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
            "decisions": dict(self.db.execute("SELECT action, COUNT(*) FROM decisions GROUP BY action").fetchall()),
            "fills": self.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
            "rl_closed_episodes": self.db.execute("SELECT COUNT(*) FROM rl_episodes WHERE status='closed'").fetchone()[0],
            "rl_trained_states": self.db.execute("SELECT COUNT(*) FROM rl_values").fetchone()[0],
            "note": "Equity is an estimate, not an executable liquidation value.",
        }
