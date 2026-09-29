"""Deterministic, paper-only strategy, risk checks and fill simulation."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass

from .market import Snapshot, SUPPORTED_QUOTES
from .store import Store
from .rl import EntryPolicy, POLICY_VERSION, state_key

STRATEGY_VERSION = POLICY_VERSION + '+capital-3'
DAY_MS = 86_400_000


@dataclass(frozen=True)
class Limits:
    min_liquidity: float = 25_000
    min_volume_5m: float = 5_000
    max_age_ms: int = 3_600_000
    max_feed_age_ms: int = 30_000
    max_position_fraction: float = 0.02
    max_total_exposure_fraction: float = 0.10
    min_cash_reserve_fraction: float = 0.50
    max_positions: int = 4
    max_position_quote_age_ms: int = 120_000
    max_trade_loss_fraction: float = 0.0025
    max_impact_fraction: float = 0.02
    max_daily_loss_fraction: float = 0.02
    max_drawdown_fraction: float = 0.05
    fee_fraction: float = 0.003
    network_fee_usd: float = 0.02
    base_slippage_fraction: float = 0.005
    max_hold_ms: int = 1_200_000
    reentry_cooldown_ms: int = 2_700_000

    def __post_init__(self):
        fractions = (self.max_position_fraction, self.max_total_exposure_fraction,
                     self.min_cash_reserve_fraction, self.max_trade_loss_fraction)
        if any(not math.isfinite(v) or not 0 < v < 1 for v in fractions):
            raise ValueError('capital risk fractions must be between zero and one')
        if self.max_positions < 1 or self.max_position_quote_age_ms <= 0:
            raise ValueError('invalid position risk limit')
        if self.reentry_cooldown_ms < 0 or self.max_hold_ms <= 0:
            raise ValueError('invalid timing risk limit')


def limits_from_env(env=None) -> Limits:
    """Capital thresholds from .env; percentage variables are entered as plain numbers."""
    env = os.environ if env is None else env

    def value(key, default, cast):
        raw = str(env.get(key, '')).strip()
        if not raw:
            return default
        try:
            return cast(raw)
        except ValueError:
            raise ValueError(f"{key} must be a number, got {raw!r}")

    def percent(key, default):
        return value(key, default, float) / 100

    return Limits(
        max_position_fraction=percent("MAX_POSITION_PCT", 2.0),
        max_total_exposure_fraction=percent("MAX_EXPOSURE_PCT", 10.0),
        min_cash_reserve_fraction=percent("MIN_CASH_RESERVE_PCT", 50.0),
        max_trade_loss_fraction=percent("MAX_TRADE_LOSS_PCT", 0.25),
        max_positions=value("MAX_OPEN_POSITIONS", 4, int),
        max_position_quote_age_ms=value("MAX_POSITION_QUOTE_AGE_S", 120, int) * 1000,
        reentry_cooldown_ms=value("REENTRY_COOLDOWN_MINUTES", 45, int) * 60_000,
    )


class PaperEngine:
    def __init__(self, store: Store, limits: Limits = Limits(), policy: EntryPolicy | None = None):
        self.store = store
        self.limits = limits
        self.policy = policy or EntryPolicy(store, os.getenv("RL_SEED", "paper-v1"))

    def _impact(self, notional: float, liquidity: float) -> float:
        return self.limits.base_slippage_fraction + 2 * notional / liquidity

    def _record(self, observation_id: int, action: str, reason: str, params: dict):
        params["final_action"] = action
        params["reason"] = reason
        params["risk_gate"] = ("blocked" if action == "BLOCK" else
                               "passed" if params["rl"]["mode"] == "experimental" else
                               "candidate_not_met" if action == "WATCH" else
                               "exit_unfilled" if action == "UNFILLED" else
                               "existing_position" if action in ("HOLD", "SELL") else "not_applicable")
        self.store.db.execute("""INSERT INTO decisions
            (observation_id,action,reason,strategy_version,params) VALUES (?,?,?,?,?)""",
            (observation_id, action, reason, STRATEGY_VERSION, json.dumps(params, separators=(",", ":"))))
        return action, reason

    def on_snapshot(self, snap: Snapshot, now: int | None = None) -> tuple[str, str]:
        # 'now' is observation availability time in replay, wall time in live polling.
        now = snap.received_at if now is None else now
        db = self.store
        lim = self.limits
        with db.transaction():
            observation_id = db.insert_snapshot(snap)
            if observation_id is None:
                return "SKIP", "duplicate observation"
            previous = db.previous(snap.mint, snap.pair, snap.received_at)
            growth = snap.price / previous["price"] - 1 if previous and previous["price"] > 0 else None
            try:
                raw = json.loads(snap.raw)
                quote = (raw.get("quoteToken") or {}).get("address")
            except (TypeError, ValueError, AttributeError):
                quote = None
            params = {
                "source": "DexScreener", "received_at": snap.received_at,
                "features": {
                    "price_usd": snap.price, "price_change_observed_pct": growth * 100 if growth is not None else None,
                    "previous_received_at": previous["received_at"] if previous else None,
                    "liquidity_usd": snap.liquidity, "volume_5m_usd": snap.volume_5m,
                    "buys_5m": snap.buys_5m, "sells_5m": snap.sells_5m,
                    "buy_sell_ratio": snap.buys_5m / max(1, snap.sells_5m),
                    "buy_share_5m_pct": snap.buys_5m / (snap.buys_5m + snap.sells_5m) * 100
                                         if snap.buys_5m + snap.sells_5m else None,
                    "liquidity_change_observed_pct": (snap.liquidity / previous["liquidity"] - 1) * 100
                                                      if previous and previous["liquidity"] > 0 else None,
                    "sample_gap_s": (snap.received_at - previous["received_at"]) / 1000 if previous else None,
                    "pair_age_s": (now - snap.created_at) / 1000 if snap.created_at else None,
                    "data_age_s": (now - snap.received_at) / 1000, "quote_mint": quote,
                },
                "limits": {
                    "min_liquidity_usd": lim.min_liquidity,
                    "min_volume_5m_usd": lim.min_volume_5m,
                    "max_pair_age_s": lim.max_age_ms / 1000,
                    "max_feed_age_s": lim.max_feed_age_ms / 1000,
                    "max_position_pct": lim.max_position_fraction * 100,
                    "max_total_exposure_pct": lim.max_total_exposure_fraction * 100,
                    "min_cash_reserve_pct": lim.min_cash_reserve_fraction * 100,
                    "max_open_positions": lim.max_positions,
                    "max_position_quote_age_s": lim.max_position_quote_age_ms / 1000,
                    "max_trade_loss_pct": lim.max_trade_loss_fraction * 100,
                    "reentry_cooldown_s": lim.reentry_cooldown_ms / 1000,
                    "max_impact_pct": lim.max_impact_fraction * 100,
                    "max_daily_loss_pct": lim.max_daily_loss_fraction * 100,
                    "max_drawdown_pct": lim.max_drawdown_fraction * 100,
                    "entry_momentum_range_pct": [2, 15], "min_buy_sell_ratio": 1.5,
                },
                "rl": {"mode": "not_evaluated", "version": POLICY_VERSION,
                       "note": "Hard safety and candidate checks precede the RL entry policy."},
            }
            def record(action: str, reason: str):
                return self._record(observation_id, action, reason, params)
            if now < snap.received_at or now - snap.received_at > lim.max_feed_age_ms:
                return record("BLOCK", "stale or future feed")
            if snap.price <= 0 or snap.liquidity <= 0:
                return record("BLOCK", "invalid market")
            position = db.position(snap.mint)
            day = now // DAY_MS
            equity = db.equity(now)
            if day != int(db.value("day_number")):
                db.set_value("day_number", day)
                db.set_value("day_start_equity", equity)
            peak = max(db.value("peak_equity"), equity)
            db.set_value("peak_equity", peak)

            if position:
                if position["pair"] != snap.pair:
                    return record("HOLD", "position on another pool")
                move = snap.price / position["entry_price"] - 1
                age = now - position["entry_at"]
                reason = None
                if snap.liquidity < lim.min_liquidity or (previous and snap.liquidity < previous["liquidity"] * 0.7):
                    reason = "liquidity alarm"
                elif move <= -0.12:
                    reason = "stop trigger"
                elif move >= 0.30:
                    reason = "take profit trigger"
                elif age >= lim.max_hold_ms:
                    reason = "time exit"
                if reason is None:
                    return record("HOLD", "no exit trigger (deterministic)")
                gross = position["units"] * snap.price
                impact = self._impact(gross, snap.liquidity)
                if snap.liquidity < lim.min_liquidity or impact > lim.max_impact_fraction:
                    return record("UNFILLED", reason + "; no credible exit liquidity")
                fill_price = snap.price * (1 - impact)
                fee = position["units"] * fill_price * lim.fee_fraction + lim.network_fee_usd
                proceeds = position["units"] * fill_price - fee
                if proceeds <= 0:
                    return record("UNFILLED", "negative net proceeds")
                db.set_value("cash", db.value("cash") + proceeds)
                db.db.execute("DELETE FROM positions WHERE mint=?", (snap.mint,))
                db.db.execute("INSERT INTO fills VALUES (NULL,?,?,?,?,?,?,?)",
                              (observation_id, snap.mint, "SELL", position["units"], fill_price, proceeds, fee))
                reward = self.policy.close_episode(snap.mint, observation_id, proceeds)
                params["rl"] = {"mode": "reward_update" if reward is not None else "not_evaluated",
                                "version": POLICY_VERSION, "reward_net_pct": reward * 100 if reward is not None else None,
                                "note": "Net realized paper return; exits remain deterministic."}
                return record("SELL", reason)

            if equity <= db.value("day_start_equity") * (1 - lim.max_daily_loss_fraction):
                return record("BLOCK", "daily loss limit")
            if equity <= peak * (1 - lim.max_drawdown_fraction):
                return record("BLOCK", "drawdown limit")
            if quote not in SUPPORTED_QUOTES:
                return record("BLOCK", "unsupported quote asset")
            if snap.created_at is None or not 0 <= now - snap.created_at <= lim.max_age_ms:
                return record("BLOCK", "unknown or out-of-range pair age")
            if snap.liquidity < lim.min_liquidity or snap.volume_5m < lim.min_volume_5m:
                return record("BLOCK", "liquidity or volume too low")
            if previous is None or previous["received_at"] >= now - 15_000:
                return record("WATCH", "need earlier observed price")
            if not 0.02 <= growth <= 0.15 or snap.buys_5m <= snap.sells_5m * 1.5:
                return record("WATCH", "candidate filter absent")
            last_exit = db.last_sell_at(snap.mint)
            if last_exit is not None and now - last_exit < lim.reentry_cooldown_ms:
                # Churn guard: one closed paper episode per mint must stay separated in time,
                # otherwise a single token dominates the RL sample and the training signal.
                params['portfolio_risk'] = {'last_exit_at': last_exit,
                                            'cooldown_remaining_s': (lim.reentry_cooldown_ms - (now - last_exit)) / 1000}
                return record('BLOCK', 'mint re-entry cooldown')
            cash = db.value("cash")
            risk = db.portfolio_risk(now, lim.max_position_quote_age_ms)
            params['portfolio_risk'] = risk
            if risk['stale_positions']:
                return record('BLOCK', 'stale price for open position; new entries paused')
            if risk['open_positions'] >= lim.max_positions:
                return record('BLOCK', 'maximum open positions reached')
            headroom = equity * lim.max_total_exposure_fraction - risk['exposure_usd']
            reserve_available = cash - equity * lim.min_cash_reserve_fraction
            params['portfolio_risk'].update({
                'exposure_headroom_usd': max(0, headroom),
                'cash_reserve_usd': equity * lim.min_cash_reserve_fraction,
                'reserve_available_usd': max(0, reserve_available),
            })
            if headroom <= 1:
                return record('BLOCK', 'portfolio exposure cap reached')
            if reserve_available <= 1:
                return record('BLOCK', 'virtual cash reserve protected')
            # Conservative allowance for the 12% trigger plus estimated entry/exit costs.
            # This is a sizing heuristic, not an upper bound on losses in illiquid markets.
            assumed_loss = 0.12 + 2 * lim.fee_fraction + 2 * lim.base_slippage_fraction + lim.max_impact_fraction
            params['portfolio_risk']['assumed_stop_loss_pct'] = assumed_loss * 100
            stop_risk_budget = equity * lim.max_trade_loss_fraction / assumed_loss
            budget = min(cash - lim.network_fee_usd, equity * lim.max_position_fraction,
                         headroom, reserve_available, stop_risk_budget)
            params['portfolio_risk']['entry_budget_usd'] = max(0, budget)
            if budget <= 1:
                return record("BLOCK", "capital risk budget too small")
            impact = self._impact(budget, snap.liquidity)
            if impact > lim.max_impact_fraction:
                return record("BLOCK", "estimated entry impact too high")
            state = state_key(growth, snap.liquidity, snap.volume_5m, snap.buys_5m, snap.sells_5m)
            params["rl"] = self.policy.evaluate(state, snap.mint, snap.received_at)
            params["proposed_position_usd"] = budget if params["rl"]["action"] == "BUY" else 0
            params["estimated_entry_impact_pct"] = impact * 100
            if params["rl"]["action"] != "BUY":
                return record("SKIP", "RL declined entry")
            fill_price = snap.price * (1 + impact)
            fee = budget * lim.fee_fraction + lim.network_fee_usd
            units = (budget - fee) / fill_price
            if units <= 0:
                return record("BLOCK", "fees exceed budget")
            db.set_value("cash", cash - budget)
            db.db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?)",
                          (snap.mint, snap.pair, units, now, fill_price, budget))
            db.db.execute("INSERT INTO fills VALUES (NULL,?,?,?,?,?,?,?)",
                          (observation_id, snap.mint, "BUY", units, fill_price, -budget, fee))
            self.policy.open_episode(snap.mint, state, observation_id, budget)
            return record("BUY", "RL entry approved after deterministic risk checks")
