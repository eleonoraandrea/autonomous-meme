import json
import os
import re
import tempfile
import time
import unittest
from http.server import ThreadingHTTPServer
from html.parser import HTMLParser
from pathlib import Path
from threading import Barrier, Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from meme_paper.engine import Limits, PaperEngine, STRATEGY_VERSION, limits_from_env
from meme_paper.market import DexScreener, Snapshot, parse_pair
from meme_paper.store import Store
from meme_paper.tui import dashboard_lines, load_dashboard, load_token_analysis
from meme_paper.web import handler_for
from meme_paper.config import load_env
from meme_paper.rl import EntryPolicy, state_key
from meme_paper.analytics import analyze_samples, cohort_summary
from meme_paper.history import load_history, read_rl_events
from meme_paper.discovery import parse_creation

T0 = 1_780_000_000_000


class AlwaysBuyPolicy(EntryPolicy):
    """Deterministic test policy, not the production default."""
    def evaluate(self, state, mint, received_at):
        return {**super().evaluate(state, mint, received_at), "action": "BUY", "exploratory": False}


def snapshot(time=T0, price=1, liquidity=100_000, volume=20_000, buys=30, sells=10,
             mint="mint1", pair="pair1", created=T0 - 60_000):
    return Snapshot(mint, pair, time, created, price, liquidity, volume, buys, sells,
                    json.dumps({"quoteToken": {"address": "So11111111111111111111111111111111111111112"}}))


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Store(str(Path(self.tmp.name) / "paper.db"))
        self.addCleanup(self.db.db.close)
        self.engine = PaperEngine(self.db, policy=AlwaysBuyPolicy(self.db))

    def test_parse_pair_rejects_wrong_side_and_invalid_values(self):
        pair = {"chainId": "solana", "pairAddress": "pool", "baseToken": {"address": "mint1"},
                "priceUsd": "0.1", "liquidity": {"usd": 30_000}, "volume": {"m5": 100},
                "txns": {"m5": {"buys": 2, "sells": 1}}, "pairCreatedAt": T0 - 10_000}
        self.assertEqual(parse_pair("mint1", pair, T0).price, .1)
        self.assertIsNone(parse_pair("other", pair, T0))
        pair["priceUsd"] = "NaN"
        self.assertIsNone(parse_pair("mint1", pair, T0))

    def test_open_position_is_tracked_after_profile_disappears(self):
        mint = "a" * 32
        class FakeDex(DexScreener):
            def get(self, path):
                if path == "/token-profiles/latest/v1":
                    return []
                self.assert_path = path
                return [{"chainId": "solana", "pairAddress": "current", "baseToken": {"address": mint},
                         "priceUsd": "1", "liquidity": {"usd": 10_000}},
                        {"chainId": "solana", "pairAddress": "held", "baseToken": {"address": mint},
                         "priceUsd": "1", "liquidity": {"usd": 1_000}}]

        client = FakeDex()
        snaps = client.snapshots(received_at=T0, tracked_pairs={mint: "held"})
        self.assertEqual(client.assert_path, "/tokens/v1/solana/" + mint)
        self.assertEqual(snaps[0].pair, "held")

    def test_first_observation_cannot_buy_and_duplicate_is_idempotent(self):
        self.assertEqual(self.engine.on_snapshot(snapshot())[0], "WATCH")
        self.assertEqual(self.engine.on_snapshot(snapshot())[0], "SKIP")
        self.assertEqual(self.db.report()["observations"], 1)
        self.assertEqual(self.db.report()["fills"], 0)

    def test_buy_and_sell_with_costs(self):
        self.engine.on_snapshot(snapshot())
        action, _ = self.engine.on_snapshot(snapshot(time=T0 + 20_000, price=1.05))
        self.assertEqual(action, "BUY")
        self.assertLess(self.db.value("cash"), 1000)
        self.assertEqual(self.db.report()["open_positions"], 1)
        action, _ = self.engine.on_snapshot(snapshot(time=T0 + 40_000, price=1.40))
        self.assertEqual(action, "SELL")
        self.assertEqual(self.db.report()["fills"], 2)
        self.assertEqual(self.db.report()["open_positions"], 0)
        self.assertGreater(self.db.value("cash"), 1000)

    def test_liquidity_collapse_does_not_fake_exit(self):
        self.engine.on_snapshot(snapshot())
        self.engine.on_snapshot(snapshot(time=T0 + 20_000, price=1.05))
        action, reason = self.engine.on_snapshot(snapshot(time=T0 + 40_000, price=.8, liquidity=1000))
        self.assertEqual(action, "UNFILLED")
        self.assertIn("no credible exit", reason)
        self.assertEqual(self.db.report()["open_positions"], 1)

    def test_stale_feed_fails_closed(self):
        action, _ = self.engine.on_snapshot(snapshot(), now=T0 + 35_000)
        self.assertEqual(action, "BLOCK")
        self.assertEqual(self.db.report()["fills"], 0)

    def test_low_liquidity_and_unknown_age_block(self):
        self.assertEqual(self.engine.on_snapshot(snapshot(liquidity=1000))[0], "BLOCK")
        self.assertEqual(self.engine.on_snapshot(snapshot(time=T0 + 60_000, created=None))[0], "BLOCK")

    def test_unrelated_quote_asset_cannot_trigger_paper_entry(self):
        item = snapshot()
        unsupported = Snapshot(item.mint, item.pair, item.received_at, item.created_at, item.price,
                               item.liquidity, item.volume_5m, item.buys_5m, item.sells_5m,
                               json.dumps({"quoteToken": {"address": "unrelated-mint"}}))
        self.assertEqual(self.engine.on_snapshot(unsupported), ("BLOCK", "unsupported quote asset"))

    def test_restart_and_replay_same_ledger(self):
        observations = [snapshot(), snapshot(time=T0 + 20_000, price=1.05),
                        snapshot(time=T0 + 40_000, price=1.4)]
        for item in observations:
            self.engine.on_snapshot(item)
        replay = Store(str(Path(self.tmp.name) / "replay.db"))
        self.addCleanup(replay.db.close)
        for row in self.db.observations():
            item = Snapshot(*(row[key] for key in ("mint", "pair", "received_at", "created_at", "price",
                                                    "liquidity", "volume_5m", "buys_5m", "sells_5m", "raw")))
            PaperEngine(replay, policy=AlwaysBuyPolicy(replay)).on_snapshot(item)
        self.assertEqual(self.db.report(), replay.report())
        reopened = Store(str(Path(self.tmp.name) / "paper.db"))
        self.addCleanup(reopened.db.close)
        self.assertEqual(reopened.report(), self.db.report())

    def test_tui_reads_ledger_without_modifying_it(self):
        path = str(Path(self.tmp.name) / "paper.db")
        missing_path = str(Path(self.tmp.name) / "absent.db")
        self.assertTrue(load_dashboard(missing_path)["missing"])
        self.assertFalse(Path(missing_path).exists())
        self.engine.on_snapshot(snapshot())
        self.engine.on_snapshot(snapshot(time=T0 + 20_000, price=1.05))
        before = self.db.report()
        data = load_dashboard(path, now_ms=T0 + 30_000)
        self.assertEqual(data["observations"], 2)
        self.assertEqual(data["fills"], 1)
        self.assertEqual(data["latest"], T0 + 20_000)
        self.assertEqual(len(data["positions"]), 1)
        self.assertEqual(len(data["markets"]), 1)
        self.assertEqual(data["markets"][0]["price"], 1.05)
        self.assertAlmostEqual(data["markets"][0]["sample_change_pct"], 5)
        self.assertEqual(data["markets"][0]["history"], [1, 1.05])
        self.assertEqual(data["markets"][0]["analysis"]["sample_count"], 2)
        self.assertAlmostEqual(data["markets"][0]["analysis"]["price_return_pct"], 5)
        self.assertEqual(data["cohort"]["observed"], 1)
        self.assertIn("NO SIGNER", dashboard_lines(data)[0])
        self.assertIn("10s ago", dashboard_lines(data)[4])
        self.assertEqual(self.db.report(), before)

    def test_web_dashboard_is_read_only_and_does_not_leak_config(self):
        self.engine.on_snapshot(snapshot())
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(str(Path(self.tmp.name) / "paper.db")))
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        root = f"http://127.0.0.1:{server.server_port}"
        with patch.dict(os.environ, {"HELIUS_API_KEY": "test-secret-not-for-browser"}):
            with urlopen(root + "/api/status") as response:
                body = response.read()
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(json.loads(body)["observations"], 1)
                self.assertEqual(json.loads(body)["markets"][0]["liquidity"], 100_000)
                self.assertNotIn(b"test-secret-not-for-browser", body)
            with urlopen(root + "/") as response:
                self.assertIn(b"MEME/PAPER", response.read())
            with urlopen(root + "/assets/app.js") as response:
                self.assertIn(b"refreshHistory()", response.read())
            with urlopen(root + "/api/decisions?kind=all&page=1") as response:
                history = json.load(response)
                self.assertEqual(history["total"], 1)
                self.assertEqual(history["items"][0]["action"], "WATCH")
                self.assertNotIn("test-secret-not-for-browser", json.dumps(history))
            with urlopen(root + "/api/decisions?kind=rl&page=1") as response:
                self.assertEqual(json.load(response)["total"], 0)
            # Fresh, in-window observation so the 15-minute analysis window is real, not a fixture timestamp.
            self.engine.on_snapshot(snapshot(time=int(time.time() * 1000), mint='fresh1'))
            with urlopen(root + "/api/analysis?mint=fresh1") as response:
                token = json.load(response)
                self.assertTrue(token["found"])
                self.assertEqual(token["analysis"]["sample_count"], 1)
                self.assertEqual(len(token["analysis"]["series"]), 1)
            with urlopen(root + "/api/analysis?mint=" + "q" * 44) as response:
                self.assertFalse(json.load(response)["found"])
            with self.assertRaises(HTTPError) as badmint:
                urlopen(root + "/api/analysis?mint=bad%3Bmints")
            self.assertEqual(badmint.exception.code, 400)
            badmint.exception.close()
            with self.assertRaises(HTTPError) as invalid:
                urlopen(root + "/api/decisions?kind=unknown&page=-1")
            self.assertEqual(invalid.exception.code, 400)
            invalid.exception.close()
            with self.assertRaises(HTTPError) as failure:
                urlopen(Request(root + "/api/status", data=b"{}", method="POST"))
            self.assertEqual(failure.exception.code, 405)
            failure.exception.close()
            with self.assertRaises(HTTPError) as missing:
                urlopen(root + "/.env")
            self.assertEqual(missing.exception.code, 404)
            missing.exception.close()
        self.assertEqual(self.db.report()["fills"], 0)

    def test_local_env_keeps_existing_environment_priority(self):
        file = Path(self.tmp.name) / ".env"
        file.write_text("MODE=paper\nWEB_PORT=9123\nHELIUS_API_KEY=local-only\n")
        with patch.dict(os.environ, {"WEB_PORT": "9000"}, clear=True):
            load_env(str(file))
            self.assertEqual(os.environ["WEB_PORT"], "9000")
            self.assertEqual(os.environ["HELIUS_API_KEY"], "local-only")

    def test_real_policy_is_deterministic_and_learns_from_closed_paper_episode(self):
        policy = EntryPolicy(self.db, seed="test-seed")
        state = state_key(.05, 100_000, 20_000, 30, 10)
        # Only use a candidate for which the actual exploratory policy proposes BUY.
        mint = next(f"token{i}" for i in range(1000)
                    if policy.evaluate(state, f"token{i}", T0 + 20_000)["action"] == "BUY")
        self.assertEqual(policy.evaluate(state, mint, T0 + 20_000),
                         policy.evaluate(state, mint, T0 + 20_000))
        engine = PaperEngine(self.db, policy=policy)
        engine.on_snapshot(snapshot(mint=mint))
        self.assertEqual(engine.on_snapshot(snapshot(mint=mint, time=T0 + 20_000, price=1.05))[0], "BUY")
        self.assertEqual(engine.on_snapshot(snapshot(mint=mint, time=T0 + 40_000, price=1.4))[0], "SELL")
        episode = self.db.db.execute("SELECT reward,status,entry_cost,exit_proceeds FROM rl_episodes").fetchone()
        self.assertEqual(episode["status"], "closed")
        self.assertAlmostEqual(episode["reward"], episode["exit_proceeds"] / episode["entry_cost"] - 1)
        q = self.db.db.execute("SELECT q,visits FROM rl_values WHERE state=?", (state,)).fetchone()
        self.assertEqual(q["visits"], 1)
        self.assertAlmostEqual(q["q"], episode["reward"])
        params = json.loads(self.db.db.execute("SELECT params FROM decisions WHERE action='BUY'").fetchone()[0])
        self.assertEqual(params["rl"]["mode"], "experimental")
        self.assertEqual(params["rl"]["closed_episodes"], 0)
        self.assertEqual(params["rl"]["action"], "BUY")
        self.assertEqual(params["limits"]["min_liquidity_usd"], 25_000)
        self.assertEqual(params["final_action"], "BUY")
        dashboard = load_dashboard(str(Path(self.tmp.name) / "paper.db"), now_ms=T0 + 40_000)
        self.assertEqual(dashboard["rl_summary"]["closed_episodes"], 1)
        self.assertEqual(dashboard["rl_summary"]["trained_states"], 1)
        self.assertEqual(dashboard["rl_summary"]["last_policy_decision"]["action"], "BUY")
        self.assertEqual(dashboard["rl_summary"]["last_policy_decision"]["params"]["rl"]["state"], state)
        self.assertEqual(dashboard["markets"][0]["params"]["rl"]["mode"], "reward_update")
        reopened = Store(str(Path(self.tmp.name) / "paper.db"))
        self.addCleanup(reopened.db.close)
        self.assertAlmostEqual(EntryPolicy(reopened, "test-seed").evaluate(state, mint, T0 + 60_000)["q_buy"], q["q"])

    def test_risk_block_prevents_rl_exploration(self):
        engine = PaperEngine(self.db, policy=EntryPolicy(self.db, seed="test-seed"))
        engine.on_snapshot(snapshot(liquidity=1000))
        params = json.loads(self.db.db.execute("SELECT params FROM decisions").fetchone()[0])
        self.assertEqual(params["rl"]["mode"], "not_evaluated")
        self.assertEqual(params["risk_gate"], "blocked")
        self.assertEqual(self.db.db.execute("SELECT COUNT(*) FROM rl_episodes").fetchone()[0], 0)

    def test_analysis_uses_only_received_samples_in_window(self):
        samples = [
            {"received_at": T0 - 1_000_000, "price": 50, "liquidity": 500},  # out of window
            {"received_at": T0 - 120_000, "price": 1, "liquidity": 100_000},
            {"received_at": T0 - 60_000, "price": 1.2, "liquidity": 120_000},
            {"received_at": T0, "price": .9, "liquidity": 90_000},
            {"received_at": T0 + 60_000, "price": 100, "liquidity": 1000},  # future
        ]
        a = analyze_samples(samples, T0, buys_5m=30, sells_5m=10, created_at=T0-600_000, quote_supported=True)
        self.assertEqual(a["sample_count"], 3)
        self.assertEqual(a["observed_span_seconds"], 120)
        self.assertEqual(a["median_gap_seconds"], 60)
        self.assertAlmostEqual(a["price_return_pct"], -10)
        self.assertAlmostEqual(a["liquidity_change_pct"], -10)
        self.assertAlmostEqual(a["sampled_max_drawdown_pct"], 25)
        self.assertEqual(a["buy_share_5m_pct"], 75)
        self.assertEqual(a["flags"], [])
        self.assertEqual(len(a["series"]), 3)

    def test_sparse_stale_analysis_reports_unknowns(self):
        a = analyze_samples([], T0, buys_5m=0, sells_5m=0, created_at=None, quote_supported=False)
        self.assertIsNone(a["price_return_pct"])
        self.assertIsNone(a["liquidity_change_pct"])
        self.assertIsNone(a["sampled_max_drawdown_pct"])
        self.assertIsNone(a["buy_share_5m_pct"])
        self.assertEqual(set(a["flags"]), {"stale", "sparse_samples", "unknown_pair_age", "unsupported_quote"})

    def test_complete_history_is_paginated_and_anchor_is_stable(self):
        for i in range(26):
            self.engine.on_snapshot(snapshot(time=T0 + i * 1000, mint=f"mint{i}"))
        path = str(Path(self.tmp.name) / "paper.db")
        first = load_history(path, "all", 1)
        self.assertEqual(first["total"], 26)
        self.assertEqual(len(first["items"]), 25)
        self.assertEqual(first["items"][0]["mint"], "mint25")
        second = load_history(path, "all", 2, first["anchor"])
        self.assertEqual([item["mint"] for item in second["items"]], ["mint0"])
        self.engine.on_snapshot(snapshot(time=T0 + 26_000, mint="new-mint"))
        pinned = load_history(path, "all", 2, first["anchor"])
        self.assertEqual(pinned["items"], second["items"])
        self.assertEqual(load_history(path, "all", 1)["total"], 27)
        with self.assertRaises(ValueError):
            load_history(path, "bad-kind")

    def test_sse_pushes_each_committed_rl_evaluation(self):
        path = str(Path(self.tmp.name) / "paper.db")
        self.engine.on_snapshot(snapshot())  # First observation: no RL evaluation.
        self.assertEqual(read_rl_events(path, 0)[0], [])
        cursor = self.db.db.execute("SELECT MAX(id) FROM decisions").fetchone()[0]
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(path))
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        root = f"http://127.0.0.1:{server.server_port}"
        with urlopen(root + f"/api/events?since={cursor}", timeout=4) as response:
            self.assertEqual(response.headers["Content-Type"], "text/event-stream; charset=utf-8")
            self.assertIn("retry: 2000", response.readline().decode())
            self.engine.on_snapshot(snapshot(time=T0 + 20_000, price=1.05))
            received = {}
            for _ in range(6):
                line = response.readline().decode().strip()
                if line.startswith("id: "):
                    received["id"] = int(line.removeprefix("id: "))
                if line.startswith("event: "):
                    received["event"] = line.removeprefix("event: ")
                if line.startswith("data: "):
                    received["data"] = json.loads(line.removeprefix("data: "))
                    break
        self.assertEqual(received["event"], "rl-evaluation")
        self.assertEqual(received["data"]["action"], "BUY")
        self.assertEqual(received["data"]["id"], received["id"])
        self.assertEqual(received["data"]["params"]["rl"]["mode"], "experimental")
        self.assertEqual(read_rl_events(path, received["id"])[0], [])
        with self.assertRaises(HTTPError) as invalid:
            urlopen(root + "/api/events?since=-1")
        self.assertEqual(invalid.exception.code, 400)
        invalid.exception.close()

    def test_web_script_references_existing_elements(self):
        class IDs(HTMLParser):
            def __init__(self):
                super().__init__()
                self.values = set()
            def handle_starttag(self, tag, attrs):
                self.values.update(value for key, value in attrs if key == "id")
        root = Path(__file__).parents[1] / "meme_paper" / "web_assets"
        parser = IDs()
        parser.feed((root / "index.html").read_text())
        script = (root / "app.js").read_text()
        referenced = set(re.findall(r"byId\(['\"]([^'\"]+)['\"]\)", script))
        self.assertEqual(referenced - parser.values, set())

    def test_public_ws_creations_are_discovery_only_and_idempotent(self):
        mint = 'A' * 32
        event = json.dumps({'signature': 'B' * 88, 'mint': mint, 'txType': 'create',
                            'symbol': 'NEW', 'marketCapSol': 123456})
        parsed = parse_creation(event, T0)
        self.assertEqual(parsed[:3], ('B' * 88, mint, T0))
        self.assertIsNone(parse_creation(event.replace('create', 'buy'), T0))
        self.assertIsNone(parse_creation(json.dumps({'txType': 'create', 'signature': 'x', 'mint': mint}), T0))
        self.assertIsNone(parse_creation('not json', T0))
        self.db.feed_status('connected', T0)
        self.assertTrue(self.db.record_discovery(*parsed))
        self.assertFalse(self.db.record_discovery(*parsed))
        self.assertEqual(self.db.discovery_candidates(T0 + 30_000), [mint])
        self.assertEqual(self.db.report()['observations'], 0)
        self.assertEqual(self.db.report()['fills'], 0)
        path = str(Path(self.tmp.name) / 'paper.db')
        discovery = load_dashboard(path, now_ms=T0 + 30_000)['discovery']
        self.assertEqual(discovery['events'], 1)
        self.assertEqual(discovery['priced_mints'], 0)
        self.assertIsNone(discovery['recent'][0]['first_priced'])
        self.assertEqual(discovery['last_event_at'], T0)
        self.engine.on_snapshot(snapshot(time=T0 + 31_000, mint=mint))
        discovery = load_dashboard(path, now_ms=T0 + 40_000)['discovery']
        self.assertEqual(discovery['priced_mints'], 1)
        self.assertEqual(discovery['recent'][0]['first_priced'], T0 + 31_000)
        self.assertEqual(self.db.report()['fills'], 0)
        self.assertEqual(self.db.discovery_candidates(T0 + 1_300_000), [])

    def test_dex_enrichment_keeps_profiles_and_open_positions(self):
        mint = 'A' * 32
        profile = 'B' * 32
        held = 'C' * 32
        class FakeDex(DexScreener):
            def __init__(self):
                self.requested = []
            def get(self, path):
                self.requested.append(path)
                if path == '/token-profiles/latest/v1':
                    return [{'chainId': 'solana', 'tokenAddress': profile}]
                return [{'chainId': 'solana', 'pairAddress': 'pool',
                         'baseToken': {'address': address}, 'priceUsd': '1',
                         'liquidity': {'usd': 100000}} for address in (mint, profile, held)]
        dex = FakeDex()
        snaps = dex.snapshots(received_at=T0, tracked_pairs={held: 'pool'}, discovered_mints=[mint])
        self.assertEqual({s.mint for s in snaps}, {mint, profile, held})
        self.assertIn(mint, dex.requested[1])
        self.assertIn(profile, dex.requested[1])
        self.assertIn(held, dex.requested[1])

    def test_discovery_enrichment_reserves_room_for_unpriced_mints(self):
        for i in range(12):
            mint = f'{i+1:032d}'
            self.db.record_discovery('A' * 80 + f'{i+1:08d}', mint, T0, '{}')
            self.engine.on_snapshot(snapshot(time=T0 + 1000, mint=mint))
        unseen = '9' * 32
        self.db.record_discovery('A' * 80 + '99999999', unseen, T0 + 2000, '{}')
        candidates = self.db.discovery_candidates(T0 + 3000)
        self.assertIn(unseen, candidates)
        self.assertEqual(len(candidates), 11)

    def test_supported_quote_pool_wins_over_liquidity_and_tracked_pair_wins_all(self):
        mint, sol = 'A' * 32, 'So11111111111111111111111111111111111111112'
        meme_quote = 'B' * 44
        def pair(pool, quote, liquidity):
            return {'chainId': 'solana', 'pairAddress': pool, 'baseToken': {'address': mint},
                    'quoteToken': {'address': quote}, 'priceUsd': '1',
                    'liquidity': {'usd': liquidity}}
        class FakeDex(DexScreener):
            def get(self, path):
                if path == '/token-profiles/latest/v1':
                    return [{'chainId': 'solana', 'tokenAddress': mint}]
                return [pair('meme-pool', meme_quote, 900_000), pair('sol-pool', sol, 60_000)]
        chosen = FakeDex().snapshots(received_at=T0)[0]
        self.assertEqual(chosen.pair, 'sol-pool')  # Tradeable pool beats a bigger meme-quoted one.
        class HeldDex(FakeDex):
            def get(self, path):
                if path == '/token-profiles/latest/v1':
                    return [{'chainId': 'solana', 'tokenAddress': mint}]
                return [pair('sol-pool', sol, 60_000), pair('meme-pool', meme_quote, 900_000)]
        # Once a position exists, its pool stays authoritative even if unsupported.
        self.assertEqual(HeldDex().snapshots(received_at=T0, tracked_pairs={mint: 'meme-pool'})[0].pair, 'meme-pool')
        only_meme = FakeDex()
        only_meme_response = [pair('meme-pool', meme_quote, 900_000)]
        only_meme.get = lambda path: [{'chainId': 'solana', 'tokenAddress': mint}] if path == '/token-profiles/latest/v1' else only_meme_response
        self.assertEqual(only_meme.snapshots(received_at=T0)[0].pair, 'meme-pool')

    def test_mint_reentry_cooldown_prevents_churn(self):
        mint = 'churn'
        self.engine.on_snapshot(snapshot(mint=mint))
        self.assertEqual(self.engine.on_snapshot(snapshot(time=T0 + 20_000, price=1.05, mint=mint))[0], 'BUY')
        self.assertEqual(self.engine.on_snapshot(snapshot(time=T0 + 40_000, price=1.4, mint=mint))[0], 'SELL')
        action, reason = self.engine.on_snapshot(snapshot(time=T0 + 60_000, price=1.45, mint=mint))
        self.assertEqual((action, reason), ('BLOCK', 'mint re-entry cooldown'))
        params = json.loads(self.db.db.execute("SELECT params FROM decisions ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertGreater(params['portfolio_risk']['cooldown_remaining_s'], 44 * 60)
        self.assertEqual(self.db.report()['open_positions'], 0)  # Blocked means no position opened.
        # The same mint becomes eligible again once its cooldown has elapsed.
        fast = PaperEngine(self.db, Limits(reentry_cooldown_ms=90_000), AlwaysBuyPolicy(self.db))
        self.assertEqual(fast.on_snapshot(snapshot(time=T0 + 200_000, price=1.5, mint=mint))[0], 'BUY')
        self.assertEqual(self.db.report()['open_positions'], 1)

    def test_decision_funnel_buckets_current_strategy_version_only(self):
        self.engine.on_snapshot(snapshot())  # WATCH
        self.engine.on_snapshot(snapshot(time=T0 + 20_000, price=1.05))  # BUY (entry)
        with self.db.transaction():  # A decision from an older strategy version, same window.
            legacy = self.db.insert_snapshot(snapshot(time=T0 + 25_000, mint='legacy'))
            self.db.db.execute("INSERT INTO decisions (observation_id,action,reason,strategy_version,params) "
                               "VALUES (?,'BLOCK','unsupported quote asset','old-version','{}')", (legacy,))
        dashboard = load_dashboard(str(Path(self.tmp.name) / 'paper.db'), now_ms=T0 + 30_000)
        funnel = dashboard['funnel']
        self.assertEqual(funnel['strategy_version'], STRATEGY_VERSION)
        self.assertEqual(funnel['observations'], 3)
        self.assertEqual(funnel['buckets']['watch_samples'], 1)
        self.assertEqual(funnel['buckets']['entry'], 1)
        self.assertNotIn('unsupported_quote', funnel['buckets'])  # old version excluded
        self.assertEqual(sum(funnel['buckets'].values()), 2)

    def test_token_analysis_window_survives_the_latest_20_cutoff(self):
        path = str(Path(self.tmp.name) / 'paper.db')
        with self.assertRaises(ValueError):
            load_token_analysis(path, 'not an address!')
        self.assertFalse(load_token_analysis(path, 'mint1', now_ms=T0)['found'])  # no data yet
        self.engine.on_snapshot(snapshot())
        self.engine.on_snapshot(snapshot(time=T0 + 20_000, price=1.05))
        # Push the mint out of the newest-20 page with other tokens.
        for i in range(25):
            self.engine.on_snapshot(snapshot(time=T0 + 30_000 + i * 1000, mint=f'other{i}', pair=f'p{i}'))
        data = load_token_analysis(path, 'mint1', now_ms=T0 + 60_000)
        self.assertTrue(data['found'])
        self.assertEqual(data['analysis']['sample_count'], 2)
        self.assertEqual([p['price'] for p in data['analysis']['series']], [1, 1.05])
        self.assertEqual(data['decision'], 'BUY')
        self.assertFalse(load_token_analysis(path, 'noms03Absento', now_ms=T0 + 60_000)['found'])

    def test_limits_from_env_parses_and_validates(self):
        limits = limits_from_env({'MAX_POSITION_PCT': '1.5', 'MAX_OPEN_POSITIONS': '2',
                                  'REENTRY_COOLDOWN_MINUTES': '10', 'MAX_POSITION_QUOTE_AGE_S': '90',
                                  'MIN_CASH_RESERVE_PCT': '60', 'MAX_EXPOSURE_PCT': '8',
                                  'MAX_TRADE_LOSS_PCT': '0.5'})
        self.assertEqual(limits.max_position_fraction, .015)
        self.assertEqual(limits.max_positions, 2)
        self.assertEqual(limits.reentry_cooldown_ms, 600_000)
        self.assertEqual(limits.max_position_quote_age_ms, 90_000)
        self.assertEqual(limits.min_cash_reserve_fraction, .6)
        with self.assertRaises(ValueError):
            limits_from_env({'MAX_POSITION_PCT': 'oops'})
        with self.assertRaises(ValueError):
            limits_from_env({'MAX_POSITION_PCT': '150'})
        self.assertEqual(limits_from_env({}), Limits())

    def test_parallel_batches_return_ordered_real_snapshots_without_parallel_writes(self):
        barrier = Barrier(2, timeout=2)
        mints = [f'{i+1:032d}' for i in range(30)]
        class ConcurrentDex(DexScreener):
            def get(self, path):
                if path == '/token-profiles/latest/v1':
                    return [{'chainId': 'solana', 'tokenAddress': mint} for mint in mints]
                barrier.wait()
                return [{'chainId': 'solana', 'pairAddress': 'pool',
                         'baseToken': {'address': mint}, 'priceUsd': '1',
                         'liquidity': {'usd': 100000}} for mint in path.split('/')[-1].split(',')]
        snaps = ConcurrentDex().snapshots(received_at=T0, limit=30)
        self.assertEqual([s.mint for s in snaps], mints)
        self.assertTrue(all(s.received_at == T0 for s in snaps))
        self.assertEqual(self.db.report()['observations'], 0)  # Fetch threads cannot mutate ledger.

    def test_capital_risk_caps_positions_and_is_visible_in_decision_trace(self):
        for i in range(5):
            mint = f'mint{i}'
            self.engine.on_snapshot(snapshot(mint=mint))
            action, reason = self.engine.on_snapshot(snapshot(time=T0+20_000, price=1.05, mint=mint))
            if i < 4:
                self.assertEqual(action, 'BUY')
            else:
                self.assertEqual((action, reason), ('BLOCK', 'maximum open positions reached'))
        self.assertEqual(self.db.report()['open_positions'], 4)
        self.assertEqual(self.db.report()['fills'], 4)
        risk = self.db.portfolio_risk(T0+20_000)
        self.assertLessEqual(risk['exposure_usd'], 100)
        params = json.loads(self.db.db.execute("SELECT params FROM decisions ORDER BY id DESC LIMIT 1").fetchone()[0])
        self.assertEqual(params['rl']['mode'], 'not_evaluated')
        self.assertEqual(params['portfolio_risk']['open_positions'], 4)
        self.assertEqual(params['limits']['max_open_positions'], 4)
        dashboard = load_dashboard(str(Path(self.tmp.name) / 'paper.db'), now_ms=T0+20_000)
        self.assertTrue(dashboard['capital_risk']['entry_paused'])
        self.assertIn('numero massimo posizioni', dashboard['capital_risk']['reasons'])

    def test_exposure_reserve_and_stale_position_block_new_entries(self):
        engine = PaperEngine(self.db, Limits(max_total_exposure_fraction=.015), AlwaysBuyPolicy(self.db))
        engine.on_snapshot(snapshot(mint='first'))
        self.assertEqual(engine.on_snapshot(snapshot(time=T0+20_000, price=1.05, mint='first'))[0], 'BUY')
        engine.on_snapshot(snapshot(mint='second'))
        self.assertEqual(engine.on_snapshot(snapshot(time=T0+20_000, price=1.05, mint='second')),
                         ('BLOCK', 'portfolio exposure cap reached'))
        engine.on_snapshot(snapshot(time=T0+140_000, mint='third'))
        self.assertEqual(engine.on_snapshot(snapshot(time=T0+160_000, price=1.05, mint='third')),
                         ('BLOCK', 'stale price for open position; new entries paused'))
        # Exits of the open position are still possible even when its last quote was stale.
        self.assertEqual(engine.on_snapshot(snapshot(time=T0+161_000, price=1.4, mint='first'))[0], 'SELL')
        separate = Store(str(Path(self.tmp.name) / 'reserve.db'))
        self.addCleanup(separate.db.close)
        reserve_engine = PaperEngine(separate, Limits(min_cash_reserve_fraction=.9999), AlwaysBuyPolicy(separate))
        reserve_engine.on_snapshot(snapshot())
        self.assertEqual(reserve_engine.on_snapshot(snapshot(time=T0+20_000, price=1.05)),
                         ('BLOCK', 'virtual cash reserve protected'))
        self.assertEqual(separate.report()['fills'], 0)


if __name__ == "__main__":
    unittest.main()
