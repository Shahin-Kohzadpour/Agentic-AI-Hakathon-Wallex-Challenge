import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from .live_paper import Config, Run, Session, d
from .strategy import entry_rejections, validated_forecast
from .test_live_paper import book


def candles(rate=0.0008):
    end=int(time.time())//60*60
    rows=[]
    for i in range(1440):
        price=100*(1+rate)**i
        rows.append({"t":end-(1440-i)*60,"o":price,"h":price*1.001,
                     "l":price*.999,"c":price,"v":10})
    return rows


class StrategyChecks(unittest.TestCase):
    def test_purged_disjoint_validation_and_past_only_labels(self):
        rows=candles()
        model=validated_forecast(rows)
        v=model["validation"]
        self.assertLess(v["train_labels_end"],v["calibration_features_start"])
        self.assertLess(v["calibration_labels_end"],v["validation_features_start"])
        self.assertEqual(model["training_labels_available_by"],rows[-1]["t"]+60)
        self.assertLessEqual(v["validation_samples"],len(rows)*.2/15+1)
        self.assertTrue(model["validation_passed"])
        self.assertEqual(entry_rejections(model,.005,.001),[])

    def test_flat_market_is_rejected_even_when_it_is_best_candidate(self):
        model=validated_forecast(candles(0))
        self.assertFalse(model["validation_passed"])
        self.assertIn("conservative_forecast_does_not_cover_costs_and_margin",entry_rejections(model,.005,.001))

    def test_costs_can_veto_a_profitable_gross_forecast(self):
        model=validated_forecast(candles())
        self.assertGreater(model["forecast_lower_return"],0)
        self.assertTrue(entry_rejections(model,.02,.001))

    def test_validation_future_cannot_change_prior_calibration_margin(self):
        rows=candles()
        original=validated_forecast(rows)
        for r in rows[-200:]:
            r["c"]*=.9
        changed=validated_forecast(rows)
        self.assertEqual(original["overprediction_margin"],changed["overprediction_margin"])

    def test_small_history_fails_closed(self):
        with self.assertRaisesRegex(ValueError,"900"):
            validated_forecast(candles()[-400:])

    def test_select_abstains_without_filling(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Run(Path(temp),Config())
            self.addCleanup(run.close)
            class Feed:
                async def candles(self,symbol,count):
                    return candles(0)
                async def book(self,symbol):
                    return book()
            session=Session(run,Feed())
            session.meta={"BTCUSDT":{}}
            self.assertIsNone(asyncio.run(session.select()))
            self.assertEqual(run.state["cash_usdt"],"1000")
            self.assertEqual(run.state["trades"],[])
            self.assertEqual(run.state["abstentions"],1)

    def test_fresh_depth_cost_veto_happens_before_any_cash_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Run(Path(temp),Config())
            self.addCleanup(run.close)
            class Feed:
                async def book(self,symbol):
                    return book()
            session=Session(run,Feed())
            session.meta={"BTCUSDT":{"quantity_precision":8,"min_notional":"1","min_quantity":"0.00000001"}}
            model={"forecast_gross_return":.001,"forecast_lower_return":.001,"validation_passed":True,
                   "validation_rejections":[],"horizon_minutes":15,"last_feature_closed_at":time.time()}
            with self.assertRaisesRegex(ValueError,"Fresh executable costs"):
                asyncio.run(session.enter({"symbol":"BTCUSDT","model":model}))
            self.assertEqual(run.state["cash_usdt"],"1000")
            self.assertIsNone(run.state["position"])

    def test_no_entry_without_enough_time_to_observe_forecast(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Run(Path(temp),Config(max_runtime_seconds=900))
            self.addCleanup(run.close)
            class Feed:
                async def book(self,symbol):
                    return book()
            session=Session(run,Feed())
            with self.assertRaisesRegex(ValueError,"observation time"):
                asyncio.run(session.enter({"symbol":"BTCUSDT","model":{}}))
            self.assertEqual(run.state["cash_usdt"],"1000")

    def test_profit_exit_uses_net_proceeds_after_fees_and_slippage(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Run(Path(temp),Config())
            self.addCleanup(run.close)
            session=Session(run,None)
            run.state["position"]={"symbol":"BTCUSDT","quantity":"0.1","entry_price":"1000"}
            # A 0.5% gross move does not meet even a 0.5% net target.
            net=session.liquidation_return(book("1005"))
            expected=d("1005")*d("0.9999")*d("0.998")/(d("1000")*d("1.002"))-1
            self.assertEqual(net,expected)
            self.assertLess(net,d("0.005"))

    def test_session_can_finish_with_zero_trades_instead_of_forcing_quota(self):
        with tempfile.TemporaryDirectory() as temp:
            run=Run(Path(temp),Config(holding_seconds=60,max_runtime_seconds=120))
            self.addCleanup(run.close)
            class Feed:
                requests=0
            session=Session(run,Feed())
            session.setup=AsyncMock()
            session.select=AsyncMock(return_value=None)
            session.enter=AsyncMock()
            clock=[run.state["started_epoch"]]
            async def advance(seconds):
                clock[0]+=seconds
            with patch("wallex_sandbox.live_paper.time.time",side_effect=lambda:clock[0]),patch("wallex_sandbox.live_paper.asyncio.sleep",side_effect=advance):
                result=asyncio.run(session.execute())
            self.assertEqual(result["status"],"complete")
            self.assertEqual(result["completed_round_trips"],0)
            self.assertEqual(result["final_cash_usdt"],"1000")
            session.select.assert_awaited()
            session.enter.assert_not_awaited()
