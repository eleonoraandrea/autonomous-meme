"""Episodic tabular RL for paper-only entry decisions.

BUY reward is realized net return at a simulated exit. SKIP has no observed
counterfactual reward and remains at zero. This is a one-step episodic RL
problem, not PPO, and cannot establish an edge without many closed episodes.
"""

from __future__ import annotations

import hashlib
import math

from .store import Store

POLICY_VERSION = "episodic-q-1"


def state_key(growth: float, liquidity: float, volume: float, buys: int, sells: int) -> str:
    momentum = "m1" if growth < .05 else "m2" if growth < .10 else "m3"
    depth = "l1" if liquidity < 75_000 else "l2"
    activity = "v1" if volume < 20_000 else "v2"
    flow = "b1" if buys / max(1, sells) < 2.5 else "b2"
    return f"{momentum}:{depth}:{activity}:{flow}"


class EntryPolicy:
    def __init__(self, store: Store, seed: str = "paper-v1"):
        self.store = store
        self.seed = seed

    def evaluate(self, state: str, mint: str, received_at: int) -> dict:
        row = self.store.db.execute("SELECT q,visits FROM rl_values WHERE state=?", (state,)).fetchone()
        q_buy, visits = (float(row["q"]), int(row["visits"])) if row else (0.0, 0)
        total = self.store.db.execute("SELECT COUNT(*) FROM rl_episodes WHERE status='closed'").fetchone()[0]
        epsilon = max(.05, .25 / math.sqrt(1 + total / 20))
        # Stable exploration for deterministic replays, not a security primitive.
        def draw(label: str) -> float:
            digest = hashlib.sha256(f"{self.seed}|{mint}|{received_at}|{label}".encode()).digest()
            return int.from_bytes(digest[:8], "big") / 2**64
        sample = draw("explore")
        explore = sample < epsilon
        action = ("BUY" if draw("action") < .5 else "SKIP") if explore else ("BUY" if q_buy > .002 else "SKIP")
        return {
            "mode": "experimental", "version": POLICY_VERSION, "state": state,
            "action": action, "q_buy": q_buy, "q_skip": 0.0,
            "state_visits": visits, "closed_episodes": total,
            "epsilon": epsilon, "exploration_draw": sample,
            "exploratory": explore, "trained": total > 0,
            "note": "Q is mean realized net paper return for BUY in this state; SKIP has no counterfactual outcome.",
        }

    def open_episode(self, mint: str, state: str, observation_id: int, cost: float):
        self.store.db.execute("""INSERT INTO rl_episodes
            (mint,state,entry_observation_id,entry_cost,status) VALUES (?,?,?,?,'open')""",
            (mint, state, observation_id, cost))

    def close_episode(self, mint: str, observation_id: int, proceeds: float) -> float | None:
        episode = self.store.db.execute("SELECT * FROM rl_episodes WHERE mint=? AND status='open'", (mint,)).fetchone()
        if episode is None:
            return None  # Pre-existing baseline position: never invent a training label.
        reward = proceeds / episode["entry_cost"] - 1
        row = self.store.db.execute("SELECT q,visits FROM rl_values WHERE state=?", (episode["state"],)).fetchone()
        visits = int(row["visits"]) if row else 0
        q = (float(row["q"]) * visits + reward) / (visits + 1) if row else reward
        self.store.db.execute("""INSERT INTO rl_values(state,q,visits) VALUES (?,?,?)
            ON CONFLICT(state) DO UPDATE SET q=excluded.q,visits=excluded.visits""",
            (episode["state"], q, visits + 1))
        self.store.db.execute("""UPDATE rl_episodes SET status='closed',exit_observation_id=?,
            exit_proceeds=?,reward=? WHERE id=?""", (observation_id, proceeds, reward, episode["id"]))
        return reward
