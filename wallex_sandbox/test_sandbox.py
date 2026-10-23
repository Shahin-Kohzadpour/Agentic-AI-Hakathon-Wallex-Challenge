from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

from condor.agents.risk import RiskEngine, RiskLimits, RiskState, auto_approve_with_risk_check

from .sandbox import CONTROLLER, CONNECTOR, OPTIONS, PaperHarness, buy, demonstration, number, offline_only


class CondorPaperChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.offline = offline_only()
        self.offline.__enter__()
        self.addCleanup(self.offline.__exit__, None, None, None)
        self.h = PaperHarness(self.root)
        self.h.broker.quote("BTC/TMN", "2000000")
        self.h.broker.quote("ETH/TMN", "1000000")

    def dispatch(self, name, call, **kwargs):
        return asyncio.run(self.h.dispatch(name, call, **kwargs))

    def denied(self, name, call, *, contains=None, **kwargs):
        result = self.dispatch(name, call, **kwargs)
        self.assertEqual(result["outcome"], "DENIED", result)
        if contains:
            self.assertIn(contains.lower(), result["reason"].lower(), result)
        return result

    def sell(self, quantity="0.1", executor_id="buy"):
        return {"tool": "stop_executor", "input": {"executor_id": executor_id, "connector_name": CONNECTOR,
                "trading_pair": "BTC-TMN", "amount": quantity}}

    def test_round_trip_and_fees(self):
        result = asyncio.run(demonstration(self.root/"round_trip"))
        self.assertEqual([r["outcome"] for r in result["results"]], ["FILLED", "DENIED", "DENIED", "FILLED"])
        self.assertEqual(number(result["final_cash_tmn"]), number("5000794"))

    def test_condor_permission_reaches_fake_fill(self):
        result = self.dispatch("buy", buy())
        self.assertEqual(result["condor_permission"]["outcome"]["outcome"], "selected")
        self.assertEqual(result["fill"]["mode"], "FAKE")

    def test_oversized_order(self):
        self.denied("oversized", buy(quantity="1"), contains="position limit")
        self.assertEqual(self.h.broker.state["orders"], {})

    def test_cumulative_exposure(self):
        self.assertEqual(self.dispatch("buy", buy(quantity="0.2"))["outcome"], "FILLED")
        self.denied("second", buy(), contains="position limit")
        self.assertEqual(len(self.h.broker.state["orders"]), 1)

    def test_executor_count(self):
        self.h.limits.max_open_executors = 1
        self.dispatch("buy", buy())
        self.denied("second", buy("ETH/TMN"), contains="Max open executors")

    def test_excluded_market(self):
        self.assertIn("TON/TMN", self.h.excluded)
        self.denied("excluded", buy("TON/TMN"))

    def test_complete_exclusion_manifest(self):
        self.assertEqual(len(self.h.excluded), 345)

    def test_usdt_outside_initial_paper_universe(self):
        self.denied("usdt", buy("BTC/USDT"))

    def test_foreign_exchange(self):
        self.denied("foreign", buy(connector_name="binance"))

    def test_wrong_session_owner(self):
        self.denied("wrong_owner", buy(controller_id="someone_else"))

    def test_leverage(self):
        self.denied("leverage", buy(leverage=5), contains="leverage")

    def test_stale_quote(self):
        self.denied("stale", buy(), now=1011)

    def test_future_quote(self):
        self.denied("future", buy(), now=999)

    def test_missing_quote(self):
        self.h.broker.quotes.pop("BTC/TMN")
        self.denied("missing", buy())

    def test_non_finite_quantity(self):
        for value in ("NaN", "Infinity", "-Infinity", "not-a-number"):
            with self.subTest(value=value):
                self.denied(value, buy(quantity=value))

    def test_negative_or_zero_quantity(self):
        for value in ("0", "-0.1"):
            with self.subTest(value=value):
                self.denied(value, buy(quantity=value))

    def test_minimum_notional(self):
        self.denied("too_small", buy(quantity="0.00001"))

    def test_insufficient_cash(self):
        self.h.broker.state["cash_tmn"] = "100000"
        self.denied("no_cash", buy(), contains="Insufficient fake cash")

    def test_partial_sell_inventory_and_realized_cost(self):
        self.dispatch("buy", buy())
        self.h.broker.quote("BTC/TMN", "2020000")
        self.assertEqual(self.dispatch("half_sell", self.sell("0.05"))["outcome"], "FILLED")
        position = self.h.broker.state["positions"]["BTC/TMN"]
        self.assertEqual(number(position["quantity"]), number("0.05"))
        self.assertEqual(number(self.h.broker.state["realized_pnl_tmn"]), number("397"))

    def test_cannot_sell_unheld_inventory(self):
        self.dispatch("buy", buy())
        self.denied("oversell", self.sell("0.2"), contains="Cannot sell more")

    def test_dry_run_blocks_even_fake_mutations(self):
        self.h.mode = "dry_run"
        self.denied("dry", buy())
        self.assertEqual(self.h.broker.state["orders"], {})

    def test_dry_run_keeps_market_reads(self):
        self.h.mode = "dry_run"
        result = self.dispatch("read", {"tool": "get_market_data", "input": {"action": "candles"}})
        self.assertEqual(result["outcome"], "READ_ONLY")

    def test_raw_order_and_arbitrary_code_not_exposed(self):
        for tool in ("place_order", "run_code", "wallex_spot_post_account_orders"):
            with self.subTest(tool=tool):
                self.denied(tool, {"tool": tool, "input": {}})

    def test_soft_drawdown_blocks_new_exposure(self):
        self.denied("drawdown", buy(), drawdown=2.5, contains="Drawdown")

    def test_shutdown_drawdown_blocks_new_exposure(self):
        self.denied("shutdown", buy(), drawdown=4.5, contains="shutdown")

    def test_shutdown_allows_owned_exit(self):
        self.dispatch("buy", buy())
        result = self.dispatch("exit", self.sell(), drawdown=4.5)
        self.assertEqual(result["outcome"], "FILLED", result)

    def test_untrusted_book_blocks_entry(self):
        self.denied("drift", buy(), book_trusted=False)

    def test_repeated_intent_does_not_duplicate_fill(self):
        self.dispatch("buy", buy())
        cash = self.h.broker.state["cash_tmn"]
        result = self.dispatch("buy", buy())
        self.assertEqual(result["outcome"], "RECONCILED")
        self.assertEqual(cash, self.h.broker.state["cash_tmn"])
        self.assertEqual(len(self.h.broker.state["orders"]), 1)

    def test_changed_intent_with_same_id_is_refused(self):
        self.dispatch("buy", buy())
        self.denied("buy", buy(quantity="0.2"))

    def test_timeout_after_acceptance_is_reconciled_after_restart(self):
        first = self.dispatch("buy", buy(), timeout_after_acceptance=True)
        self.assertEqual(first["outcome"], "UNKNOWN")
        self.h = PaperHarness(self.root)
        self.h.broker.quote("BTC/TMN", "2000000")
        result = self.dispatch("buy", buy())
        self.assertEqual(result["outcome"], "RECONCILED")
        self.assertEqual(number(self.h.broker.state["positions"]["BTC/TMN"]["quantity"]), number("0.1"))
        self.assertEqual(len(self.h.broker.state["orders"]), 1)

    def test_restart_preserves_cash_inventory_and_fees(self):
        self.dispatch("buy", buy())
        previous = self.h.broker.state
        resumed = PaperHarness(self.root)
        self.assertEqual(resumed.broker.state, previous)

    def test_ip_connections_are_disabled(self):
        with socket.socket() as connection:
            with self.assertRaisesRegex(RuntimeError, "NETWORK_DISABLED"):
                connection.connect(("127.0.0.1", 443))

    def test_condor_same_tick_approvals_accumulate(self):
        engine = RiskEngine(RiskLimits(max_position_size_quote=500000))
        state = RiskState()
        call = {"tool": "create_grid_executor", "input": {"controller_id": CONTROLLER, "total_amount_quote": 300000}}
        self.assertTrue(engine.check_executor_action(call, state, 300000)[0])
        self.assertFalse(engine.check_executor_action(call, state, 300000)[0])

    def test_condor_native_dry_run_refuses_code_execution(self):
        callback = auto_approve_with_risk_check(self.h.risk, RiskState(), execution_mode="dry_run")
        result = asyncio.run(callback({"tool": "run_code", "input": {"code": "print(1)"}}, OPTIONS))
        self.assertEqual(result["outcome"]["outcome"], "cancelled")


if __name__ == "__main__":
    unittest.main()
