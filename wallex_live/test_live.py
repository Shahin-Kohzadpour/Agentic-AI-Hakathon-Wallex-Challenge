import asyncio
import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from .client import ExchangeError, Wallex
from .engine import Engine
from .journal import Journal
from .policy import CUTOFF, CAPITAL_IRR, entry_allowed, validate_buy
from .service import Handler, STATE
from http.server import ThreadingHTTPServer

BEFORE = CUTOFF.timestamp() - 20000
AFTER = CUTOFF.timestamp() + 1
MARKET = {"is_spot": True, "base_asset": "BTC", "quote_asset": "USDT",
          "amount_precision": 4, "price_precision": 2, "min_quantity": ".0001", "min_notional": ".01"}


class FakeAPI:
    account_type = "main"
    def __init__(self):
        self.sent, self.canceled = [], []
        self.wallet = {"USDT": {"total": "100", "locked": "0", "free": "100"},
                       "BTC": {"total": "0", "locked": "0", "free": "0"}}
        self.timeout = False
    async def balances(self):
        return self.wallet
    async def create(self, body):
        self.sent.append(body)
        if self.timeout:
            raise ExchangeError("wallex_transport_error")
        return {"clientOrderId": body["client_id"], "active": True}
    async def order(self, cid):
        raise ExchangeError("wallex_http_404", 404)
    async def open_orders(self):
        return []
    async def cancel(self, cid):
        self.canceled.append(cid)
    async def book(self, symbol):
        return {"symbol": symbol, "received_epoch": AFTER, "bids": [(100000, 10000)],
                "asks": [(100100, 10000)]}


class GuardTests(unittest.TestCase):
    def test_cutoff_timezone_and_currency_units(self):
        self.assertEqual(CUTOFF.astimezone(__import__("datetime").timezone.utc).isoformat(),
                         "2026-10-26T20:20:00+00:00")
        self.assertEqual(CAPITAL_IRR, 50000000)
        self.assertFalse(entry_allowed(CUTOFF.timestamp()))
        self.assertFalse(entry_allowed(CUTOFF.timestamp() - 7500))
        self.assertTrue(entry_allowed(CUTOFF.timestamp() - 7501))
    def test_cap_includes_fees_and_live_tmn_rate(self):
        validate_buy("BTCUSDT", ".01", "100", ".002", "100000", BEFORE, BEFORE)
        for quantity, rate, observed, now in [
            (".05", "100000", BEFORE, BEFORE),
            (".01", "1000000", BEFORE, BEFORE),
            (".01", "100000", BEFORE - 9, BEFORE),
            (".01", "100000", AFTER, AFTER),
        ]:
            with self.subTest(quantity=quantity, rate=rate, now=now), self.assertRaises(ValueError):
                validate_buy("BTCUSDT", quantity, 100, ".002", rate, now, observed)
    def test_account_scope_is_explicit(self):
        for kind, cid in [("", ""), ("credit", ""), ("main", "unexpected")]:
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                Wallex(None, "test_key", kind, cid)
    def test_only_unused_unverified_journal_can_rotate_a_rejected_key(self):
        with tempfile.TemporaryDirectory() as path:
            journal = Journal(path, "old_rejected_key")
            journal.close()
            journal = Journal(path, "new_key")
            self.assertEqual(journal.get("identity"), "new_key")
            journal.set("account_verified", True)
            journal.close()
            with self.assertRaisesRegex(ValueError, "journal_account_identity_changed"):
                Journal(path, "different_verified_account")


class BrokerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = Journal(self.temp.name, "test_identity")
        self.api = FakeAPI()
        self.engine = Engine(self.api, self.journal, enabled=True)
        self.engine.markets = {"BTCUSDT": dict(MARKET)}
        self.engine.status["connected"] = True
        self.body = {"symbol": "BTCUSDT", "type": "LIMIT", "side": "BUY",
                     "quantity": ".01", "price": "100"}
        self.rate = {"asks": [(100000, 1000)], "received_epoch": BEFORE}
    async def asyncTearDown(self):
        self.journal.close()
        self.temp.cleanup()
    async def test_deadline_and_disarmed_paths_never_submit(self):
        with patch("wallex_live.engine.time.time", return_value=AFTER):
            with self.assertRaises(ValueError):
                await self.engine.submit(self.body, "entry", self.rate, ".002")
        self.engine.enabled = False
        with patch("wallex_live.engine.time.time", return_value=BEFORE):
            with self.assertRaises(ValueError):
                await self.engine.submit(self.body, "entry", self.rate, ".002")
        self.assertEqual(self.api.sent, [])
    async def test_fresh_balance_and_cap_are_rechecked(self):
        with patch("wallex_live.engine.time.time", return_value=BEFORE):
            self.api.wallet["USDT"]["free"] = ".5"
            with self.assertRaisesRegex(ValueError, "insufficient_real_usdt"):
                await self.engine.submit(self.body, "entry", self.rate, ".002")
            self.api.wallet["USDT"]["free"] = "100"
            with self.assertRaisesRegex(ValueError, "real_capital_limit"):
                await self.engine.submit({**self.body, "quantity": ".1"}, "entry", self.rate, ".002")
        self.assertEqual(self.api.sent, [])
    async def test_timeout_remains_unresolved_across_restart_without_resubmit(self):
        self.api.timeout = True
        with patch("wallex_live.engine.time.time", return_value=BEFORE):
            with self.assertRaises(ExchangeError):
                await self.engine.submit(self.body, "entry", self.rate, ".002")
            self.assertEqual(len(self.api.sent), 1)
            self.journal.close()
            self.journal = Journal(self.temp.name, "test_identity")
            restarted = Engine(self.api, self.journal, enabled=True)
            with self.assertRaisesRegex(ValueError, "ambiguous_order_requires_review"):
                await restarted.reconcile()
            self.assertEqual(self.journal.pending()[0]["stage"], "submission_unknown")
            self.assertEqual(len(self.api.sent), 1)
    async def test_settlement_cannot_sell_before_cutoff(self):
        self.api.wallet["BTC"] = {"total": "1", "locked": "0", "free": "1"}
        with patch("wallex_live.engine.time.time", return_value=BEFORE):
            with self.assertRaisesRegex(ValueError, "settlement_not_due"):
                await self.engine.submit({**self.body, "side": "SELL"}, "settlement")
        self.assertEqual(self.api.sent, [])
    async def test_sell_ack_is_not_reported_as_completed_settlement(self):
        self.engine.markets = {"USDTTMN": {**MARKET, "base_asset": "USDT", "quote_asset": "TMN"}}
        with patch("wallex_live.engine.time.time", return_value=AFTER):
            await self.engine.settle([])
        self.assertEqual(len(self.api.sent), 1)
        self.assertNotEqual(self.engine.status["state"], "settled_irr")
        self.assertIsNone(self.journal.get("settled_at"))
    async def test_dust_remains_visible_and_is_not_fake_settled(self):
        self.engine.markets = {"USDTTMN": {**MARKET, "base_asset": "USDT", "quote_asset": "TMN",
                                         "min_notional": "100000000"}}
        with patch("wallex_live.engine.time.time", return_value=AFTER):
            await self.engine.settle([])
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.engine.status["state"], "settlement_pending_residuals")
    async def test_terminal_fill_application_is_idempotent(self):
        cid, body = self.journal.prepare(self.body, "entry")
        self.journal.set("entry_context", {"median_log_basis": 0, "sigma": .001})
        order = {"executedQty": ".01", "executedSum": "1", "fills": [{"fee": ".002", "feeAsset": "USDT"}]}
        self.journal.update(cid, "terminal", order)
        await self.engine.reconcile()
        first = self.journal.get("position")
        await self.engine.reconcile()
        self.assertEqual(first, self.journal.get("position"))
        self.assertEqual(first["spent"], "1.002")
    async def test_policy_change_on_existing_state_requires_review(self):
        self.journal.set("policy", {"capital_tmn": "999999999"})
        with self.assertRaisesRegex(ValueError, "persisted_live_policy_changed"):
            Engine(self.api, self.journal, enabled=True)
    async def test_credit_permissions_and_unowned_exit_fail_closed(self):
        self.api.account_type = "credit"
        with self.assertRaisesRegex(ValueError, "market_not_permitted"):
            await self.engine.submit(self.body, "entry", self.rate, ".002")
        self.api.account_type = "main"
        self.api.wallet["BTC"] = {"total": "1", "locked": "0", "free": "1"}
        with self.assertRaisesRegex(ValueError, "owned_exit_must_sell"):
            await self.engine.submit({**self.body, "side": "SELL"}, "exit")
        self.assertEqual(self.api.sent, [])

    async def run_strategy_fixture(self, veto_refresh=False):
        from wallex_sandbox.fair_value_core import BasisWindow, FairConfig
        from wallex_sandbox.fair_value_feeds import FairFeeds
        from .policy import decimal as D
        self.engine.markets["ETHUSDT"] = {**MARKET, "base_asset": "ETH"}
        self.engine.fees = {s: {"takerFeeRate": ".002"} for s in ("BTCUSDT", "ETHUSDT")}
        self.engine.balances = self.api.wallet
        self.engine.pending_signals["BTCUSDT"] = {"since": BEFORE - 31, "samples": 3}
        for symbol in ("BTCUSDT", "ETHUSDT"):
            window = BasisWindow(FairConfig())
            for i in range(91):
                window.add(BEFORE - 910 + i * 10, 0)
            self.engine.windows[symbol] = window
        count = 0
        async def fake_public(_feeds, venue, symbol):
            nonlocal count
            price = D("100")
            if venue == "wallex" and symbol == "BTCUSDT":
                count += 1
                price = D("100") if veto_refresh and count > 1 else D("98")
            return {"symbol": symbol, "venue": venue, "quote": "USDT", "market_type": "spot",
                    "received_epoch": BEFORE, "exchange_epoch": None, "cache_age": 0,
                    "bids": [(price - D(".01"), D("1000"))],
                    "asks": [(price + D(".01"), D("1000"))]}
        async def fake_rate(symbol):
            self.assertEqual(symbol, "USDTTMN")
            return {"asks": [(D("100000"), D("1000"))], "bids": [(D("99900"), D("1000"))],
                    "received_epoch": BEFORE}
        self.api.book = fake_rate
        with patch("wallex_live.engine.time.time", return_value=BEFORE), patch.object(FairFeeds, "book", fake_public):
            await self.engine.strategy(None)

    async def test_real_rule_to_durable_broker_path_respects_tmn_cap(self):
        from .policy import decimal as D
        await self.run_strategy_fixture()
        self.assertEqual(len(self.api.sent), 1)
        sent = self.api.sent[0]
        cost_tmn = D(sent["quantity"]) * D(sent["price"]) * D("1.002") * D("100000")
        self.assertLessEqual(cost_tmn, 500000)
        self.assertEqual(sent["side"], "BUY")
        self.assertEqual(len(self.journal.pending()), 1)

    async def test_real_rule_rechecks_edge_and_abstains_on_fresh_veto(self):
        await self.run_strategy_fixture(veto_refresh=True)
        self.assertEqual(self.api.sent, [])
        self.assertEqual(self.journal.pending(), [])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_rolling_release_waits_for_previous_writer_and_recovers(self):
        from .service import worker, snapshot, STOP
        STOP.clear()
        with tempfile.TemporaryDirectory() as directory:
            held = Journal(directory, "unused_initial_identity")
            async def one_cycle(engine, _http):
                engine.status["state"] = "fixture_cycle_completed"
                STOP.set()
            env = {"WALLEX_API_KEY": "test_runtime_key", "WALLEX_ACCOUNT_TYPE": "main",
                   "WALLEX_ENABLE_LIVE_ORDERS": "0", "WALLEX_DATA_DIR": directory}
            try:
                with patch.dict("os.environ", env), patch.object(Engine, "cycle", one_cycle):
                    task = asyncio.create_task(worker())
                    await asyncio.sleep(.1)
                    self.assertEqual(snapshot()["state"], "waiting_for_single_writer")
                    held.close()
                    held = None
                    await asyncio.wait_for(task, timeout=5)
                    self.assertEqual(snapshot()["state"], "fixture_cycle_completed")
            finally:
                if held:
                    held.close()
                STOP.clear()

    async def test_real_worker_cannot_arm_before_final_liquidation_scope_is_confirmed(self):
        from .service import worker, snapshot
        with patch.dict("os.environ", {"WALLEX_ENABLE_LIVE_ORDERS": "1",
                                      "WALLEX_CONFIRM_ENTIRE_ACCOUNT_SETTLEMENT": "0"}):
            await worker()
        state = snapshot()
        self.assertFalse(state["connected"])
        self.assertFalse(state["ready"])
        self.assertEqual(state["error"], "final_liquidation_scope_confirmation_required")

    async def test_private_key_is_never_added_to_public_requests_and_redirects_disabled(self):
        calls = []
        class Response:
            status = 200
            headers = {}
            async def __aenter__(self): return self
            async def __aexit__(self, *_): pass
            async def text(self): return '{"success":true,"result":{}}'
        class HTTP:
            def request(self, method, url, **kw):
                calls.append((method, url, kw))
                return Response()
        api = Wallex(HTTP(), "test_secret_not_real", "main")
        await api.request("GET", "/v1/markets")
        await api.request("GET", "/v1/account/fee", private=True)
        self.assertNotIn("X-API-Key", calls[0][2]["headers"])
        self.assertEqual(calls[1][2]["headers"]["X-API-Key"], "test_secret_not_real")
        self.assertTrue(all(c[2]["allow_redirects"] is False for c in calls))
        for method, path in [("POST", "/v1/account/money-withdrawal"),
                             ("GET", "//attacker.example/account"), ("POST", "/v1/markets")]:
            with self.assertRaises(ValueError):
                await api.request(method, path, private=True)
        self.assertEqual(len(calls), 2)
    async def test_credit_header_and_wallet_route(self):
        api = Wallex(None, "test_key", "credit", "test_client")
        seen = []
        async def request(method, path, **kwargs):
            seen.append(path)
            return {"result": {"USDT": {"total": "10", "freeze": "2", "available": "8"}}}
        api.request = request
        balances = await api.balances()
        self.assertEqual(seen, ["/v1/account/balances-detail"])
        self.assertEqual(balances["USDT"]["free"], "8")


class DashboardTests(unittest.TestCase):
    def test_account_data_requires_auth_and_script_is_not_served(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        password = "test_password_at_least_20_characters"
        try:
            with patch.dict("os.environ", {"WALLEX_DASHBOARD_PASSWORD": password}):
                self.assertEqual(urllib.request.urlopen(base + "/health").status, 200)
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(base + "/api/status")
                self.assertEqual(error.exception.code, 401)
                auth = "Basic " + base64.b64encode(("operator:" + password).encode()).decode()
                req = urllib.request.Request(base + "/", headers={"Authorization": auth})
                self.assertIn("سامانه معاملاتی".encode(), urllib.request.urlopen(req).read())
                req = urllib.request.Request(base + "/docs/VIDEO_SCRIPT_FA.md", headers={"Authorization": auth})
                with self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(req)
                self.assertEqual(error.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
