import asyncio
import tempfile
import time
import unittest
from pathlib import Path

from .live_paper import Config, PublicFeed, Run, Session, d, export, forecast, vwap
from .sandbox import offline_only


def book(price="1000"):
    return {"symbol":"BTCUSDT","received_epoch":time.time(),
            "bids":[(d(price),d("10"))],"asks":[(d(price)+1,d("10"))]}


class LivePaperChecks(unittest.TestCase):
    def test_book_depth_weighted_price(self):
        b=book()
        b["asks"]=[(d("100"),d("1")),(d("102"),d("2"))]
        self.assertEqual(vwap(b,"BUY",d("2")),d("101"))

    def test_missing_visible_depth_is_not_filled(self):
        with self.assertRaisesRegex(ValueError,"Visible book"):
            vwap(book(),"BUY",d("20"))

    def test_private_and_order_endpoints_are_rejected(self):
        feed=PublicFeed(None,None)
        for path in ("/v1/account/orders","/v1/account/balances","https://other.example"):
            with self.subTest(path=path),self.assertRaisesRegex(ValueError,"public market-data"):
                asyncio.run(feed.get(path))

    def test_model_uses_completed_history(self):
        rows=[]
        for i in range(200):
            price=100+i*0.02
            rows.append({"t":i*60,"o":price,"h":price+0.1,"l":price-0.1,"c":price,"v":1})
        model=forecast(rows)
        self.assertEqual(model["training_labels_available_by"],rows[-1]["t"]+60)
        self.assertEqual(model["last_feature_closed_at"],rows[-1]["t"]+60)
        self.assertGreater(model["training_samples"],100)

    def test_full_budget_round_trip_reconciles_after_fees(self):
        with tempfile.TemporaryDirectory() as temp,offline_only():
            run=Run(Path(temp),Config(target_trades=1,require_positive_edge=False))
            self.addCleanup(run.close)
            class Feed:
                async def book(self,symbol):
                    return book()
            session=Session(run,Feed())
            session.meta={"BTCUSDT":{"quantity_precision":8,"min_notional":"1","min_quantity":"0.00000001"}}
            decision={"symbol":"BTCUSDT","model":{"forecast_gross_return":0.001},"selection_score_net":"-0.004"}
            async def act():
                await session.enter(decision)
                position=run.state["position"]
                self.assertLessEqual(d(position["entry_notional"])+d(position["entry_fee"]),d("100"))
                await session.exit(book("1002"),"test_exit")
            asyncio.run(act())
            result=export(run)
            self.assertEqual(len(result["trades"]),1)
            self.assertEqual(result["real_orders_submitted"],0)
            self.assertEqual(d(result["final_cash_usdt"])-d("1000"),d(result["net_pnl_usdt"]))
            self.assertGreater(d(result["fees_usdt"]),0)
