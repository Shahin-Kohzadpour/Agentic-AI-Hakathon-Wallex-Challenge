import asyncio
import json
import math
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from .fair_value_core import BasisWindow, FairConfig, PaperWallet, opportunity, reference_price, sized_entry
from .fair_value_feeds import FairFeeds
from .fair_value_paper import FairSession, fees_from_file
from .live_paper import Config, Run, d
from .sandbox import offline_only
from .wallex_fee_probe import fee_rates, probe

META={"quantity_precision":8,"min_quantity":"0.00000001","min_notional":"1"}


def book(price="100",venue="wallex",now=None):
    now=time.time() if now is None else now
    price=d(price)
    return {"venue":venue,"symbol":"BTCUSDT","quote":"USDT","market_type":"spot","received_epoch":now,
            "exchange_epoch":now if venue!="wallex" else None,"cache_age":0,
            "bids":[(price-d("0.01"),d("1000"))],"asks":[(price+d("0.01"),d("1000"))]}


def ready(now,basis=0):
    w=BasisWindow(FairConfig())
    for i in range(91):
        w.add(now-910+i*10,basis)
    return w


def decision(now=None):
    now=time.time() if now is None else now
    c=FairConfig()
    return opportunity(book("98",now=now),d("100"),ready(now).snapshot(now),"1000",d(".002"),META,c)


class CoreChecks(unittest.TestCase):
    def test_normal_local_discount_is_not_mistaken_for_an_edge(self):
        now=time.time()
        x=opportunity(book("99",now=now),d("100"),ready(now,math.log(.99)).snapshot(now),"1000",d(".002"),META,FairConfig())
        self.assertFalse(x["eligible"])
        self.assertAlmostEqual(float(x["fair_mid"]),99)
        self.assertLess(d(x["potential_net_return_at_normal_basis"]),0)

    def test_baseline_requires_time_and_count_and_drops_after_data_gap(self):
        c=FairConfig()
        w=BasisWindow(c)
        for i in range(100):
            w.add(1000+i*.01,0)
        self.assertFalse(w.snapshot(1002)["ready"])
        self.assertEqual(w.snapshot(1203)["samples"],0)

    def test_reference_venues_must_agree_be_fresh_and_same_spot_quote(self):
        now=time.time()
        local=book(now=now)
        a,b=book(venue="okx",now=now),book(venue="bybit",now=now)
        self.assertEqual(reference_price(local,[a,b],now,FairConfig()),d("100"))
        for bad,reason in (({**b,"exchange_epoch":now-30},"timestamp"),({**b,"quote":"USD"},"mismatch"),
                           ({**b,"received_epoch":now-5,"exchange_epoch":now-5},"asynchronous")):
            with self.subTest(reason=reason),self.assertRaisesRegex(ValueError,reason):
                reference_price(local,[a,bad],now,FairConfig())
        with self.assertRaisesRegex(ValueError,"disagree"):
            reference_price(local,[a,book("101",venue="bybit",now=now)],now,FairConfig())
        with self.assertRaisesRegex(ValueError,"independent"):
            reference_price(local,[a,a],now,FairConfig())

    def test_depth_and_cost_checks_can_veto_large_apparent_discount(self):
        now=time.time()
        shallow=book("98",now=now)
        shallow["asks"]=[(d("98.01"),d("0.001")),(d("105"),d("1000"))]
        x=opportunity(shallow,d("100"),ready(now).snapshot(now),"1000",d(".002"),META,FairConfig())
        self.assertFalse(x["eligible"])
        self.assertTrue(any("impact" in reason for reason in x["reasons"]))

    def test_fees_can_eliminate_target_room(self):
        now=time.time()
        c=FairConfig()
        x=opportunity(book("99.7",now=now),d("100"),ready(now).snapshot(now),"1000",d(".002"),META,c)
        self.assertFalse(x["eligible"])
        self.assertLess(d(x["potential_net_return_at_normal_basis"]),0)

    def test_good_synthetic_opportunity_and_roundtrip_reconcile(self):
        now=time.time()
        x=decision(now)
        self.assertTrue(x["eligible"])
        w=PaperWallet(FairConfig())
        w.enter("BTCUSDT",x["order"],x,d(".002"),now)
        self.assertLessEqual(d(x["order"]["spent"]),100)
        self.assertLess(w.equity(book("98",now=now)),1000)
        trade=w.exit(book("100",now=now+5),"synthetic_test",now+5)
        self.assertGreater(d(trade["net_pnl_usdt"]),0)
        self.assertEqual(d(w.state["cash_usdt"])-1000,d(trade["net_pnl_usdt"]))
        w.reconcile()

    def test_wallet_independently_refuses_forged_spend_and_corrupt_resume(self):
        x=decision()
        for amount in ("-10","101"):
            w=PaperWallet(FairConfig())
            with self.subTest(amount=amount),self.assertRaises(ValueError):
                w.enter("BTCUSDT",{**x["order"],"spent":amount},x,d(".002"),time.time())
            self.assertEqual(w.state["cash_usdt"],"1000")
        state=PaperWallet(FairConfig()).state
        state["cash_usdt"]="1001"
        with self.assertRaisesRegex(ValueError,"reconcile"):
            PaperWallet(FairConfig(),state)

    def test_wallet_resume_preserves_position_and_entry_fee(self):
        x=decision()
        wallet=PaperWallet(FairConfig())
        wallet.enter("BTCUSDT",x["order"],x,d(".002"),time.time())
        restored=PaperWallet(FairConfig(),json.loads(json.dumps(wallet.state)))
        self.assertEqual(restored.state["position"],wallet.state["position"])
        restored.reconcile()

    def test_stale_exit_cannot_create_a_fill(self):
        now=time.time()
        x=decision(now)
        w=PaperWallet(FairConfig())
        w.enter("BTCUSDT",x["order"],x,d(".002"),now)
        with self.assertRaisesRegex(ValueError,"stale"):
            w.exit(book("100",now=now),"test",now+30)
        self.assertEqual(w.state["trades"],[])

    def test_trade_ceiling_and_cooldown_are_enforced_by_wallet(self):
        now=time.time()
        x=decision(now)
        config=replace(FairConfig(),maximum_trades=1)
        wallet=PaperWallet(config)
        wallet.enter("BTCUSDT",x["order"],x,d(".002"),now)
        wallet.exit(book("100",now=now+1),"synthetic_test",now+1)
        with self.assertRaisesRegex(ValueError,"cooldown"):
            wallet.enter("BTCUSDT",x["order"],x,d(".002"),now+2)
        with self.assertRaisesRegex(ValueError,"ceiling"):
            wallet.enter("BTCUSDT",x["order"],x,d(".002"),now+1000)

    def test_stale_verified_fee_report_falls_back_to_explicit_assumptions(self):
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/"fees.json"
            p.write_text(json.dumps({"at":"2020-01-01T00:00:00+00:00","status":"verified",
                         "rates":{"BTCUSDT":{"takerFeeRate":"0"}}}))
            rates,info=fees_from_file(p,FairConfig(),time.time())
            self.assertEqual(rates["BTCUSDT"],d(".002"))
            self.assertEqual(info["source"]["BTCUSDT"],"assumed")

    def test_fee_report_filters_private_fields_and_rejects_invalid_rates(self):
        p={"success":True,"result":{"BTCUSDT":{"makerFeeRate":".001","takerFeeRate":".002","recent_days_sum":"PRIVATE"}}}
        self.assertEqual(fee_rates(p,["BTCUSDT"]),{"BTCUSDT":{"makerFeeRate":"0.001","takerFeeRate":"0.002"}})
        p["result"]["BTCUSDT"]["takerFeeRate"]="NaN"
        with self.assertRaises(ValueError):
            fee_rates(p,["BTCUSDT"])

    def test_private_or_order_endpoints_are_never_accepted_by_public_feeds(self):
        f=FairFeeds(None,None)
        for venue,path in (("wallex","/v1/account/fee"),("wallex","/v1/account/orders"),("bybit","/v5/order/create"),("other","/api/v5/market/books")):
            with self.subTest(path=path),self.assertRaisesRegex(ValueError,"public GET"):
                asyncio.run(f.get(venue,path))


class SessionChecks(unittest.IsolatedAsyncioTestCase):
    def session(self,folder,feeds,observe=True):
        c=FairConfig()
        run=Run(folder,Config())
        self.addCleanup(run.close)
        rates,info=fees_from_file(None,c,time.time())
        s=FairSession(run,c,feeds,rates,info,observe)
        s.meta={"BTCUSDT":META}
        s.windows["BTCUSDT"]=ready(time.time())
        return s

    async def test_current_observation_is_not_used_to_calibrate_its_own_decision(self):
        with tempfile.TemporaryDirectory() as temp,offline_only():
            s=self.session(Path(temp),None)
            now=time.time()
            snap={"symbol":"BTCUSDT","local":book("98",now=now),"references":[book(venue="okx",now=now),book(venue="bybit",now=now)],"errors":[]}
            await s.step({"BTCUSDT":snap})
            row=s.run.db.execute("SELECT payload FROM events WHERE kind='fair_decision' ORDER BY seq DESC LIMIT 1").fetchone()
            data=json.loads(row[0])
            self.assertEqual(data["baseline"]["samples"],91)
            self.assertEqual(data["baseline"]["median_log_basis"],0)
            self.assertTrue(data["eligible"])
            self.assertEqual(len(s.windows["BTCUSDT"].rows),92)
            self.assertEqual(s.wallet.state["trades"],[])

    async def test_refresh_vetoes_entry_without_debiting_cash(self):
        class Feeds:
            async def book(self,venue,symbol):
                return book("100",venue)
        with tempfile.TemporaryDirectory() as temp,offline_only():
            s=self.session(Path(temp),Feeds(),False)
            x=decision()
            x.update(symbol="BTCUSDT",signal_observed_epoch=time.time())
            await s.enter(x)
            self.assertEqual(s.wallet.state["cash_usdt"],"1000")
            self.assertIsNone(s.wallet.state["position"])

    async def test_reference_outage_does_not_block_a_net_stop_exit(self):
        class Feeds:
            async def book(self,venue,symbol):
                if venue!="wallex":
                    raise ValueError("reference_offline")
                return book("96",venue)
        with tempfile.TemporaryDirectory() as temp,offline_only():
            s=self.session(Path(temp),Feeds(),False)
            x=decision()
            s.wallet.enter("BTCUSDT",x["order"],x,d(".002"),time.time()-10)
            s.wallet.state["position"]["executor_id"]="test_paper_owned"
            # The prior local result has aged while reference requests timed out.
            snap={"symbol":"BTCUSDT","local":book("96",now=time.time()-30),"references":[],"errors":["offline"]}
            await s.step({"BTCUSDT":snap})
            self.assertIsNone(s.wallet.state["position"])
            self.assertEqual(s.wallet.state["trades"][0]["exit_reason"],"net_stop")
            self.assertLess(d(s.wallet.state["trades"][0]["net_pnl_usdt"]),0)
            s.wallet.reconcile()

    async def test_valid_synthetic_entry_uses_condor_risk_and_persists_once(self):
        class Feeds:
            async def book(self,venue,symbol):
                return book("98" if venue=="wallex" else "100",venue)
        with tempfile.TemporaryDirectory() as temp,offline_only():
            s=self.session(Path(temp),Feeds(),False)
            x=decision()
            x.update(symbol="BTCUSDT",signal_observed_epoch=time.time())
            await s.enter(x)
            self.assertIsNotNone(s.wallet.state["position"])
            self.assertEqual(s.wallet.state["position"]["decision"]["condor_permission"]["outcome"]["outcome"],"selected")
            stored=json.loads(s.run.db.execute("SELECT payload FROM state WHERE id=1").fetchone()[0])
            self.assertEqual(stored["fair_wallet"],s.wallet.state)
            self.assertLess(d(stored["cash_usdt"]),1000)
            with self.assertRaisesRegex(ValueError,"one paper position"):
                s.wallet.enter("BTCUSDT",x["order"],x,d(".002"),time.time())

    async def test_bad_snapshot_resets_persistence(self):
        with tempfile.TemporaryDirectory() as temp,offline_only():
            s=self.session(Path(temp),None)
            start=time.time()
            for offset,valid in ((0,True),(10,False),(20,True)):
                now=start+offset
                snap={"symbol":"BTCUSDT","local":book("98",now=now),"references":
                      [book(venue="okx",now=now),book(venue="bybit",now=now)] if valid else [],"errors":[]}
                with patch("wallex_sandbox.fair_value_paper.time.time",return_value=now):
                    await s.step({"BTCUSDT":snap})
            self.assertEqual(s.pending["BTCUSDT"]["samples"],1)
            self.assertEqual(s.pending["BTCUSDT"]["since"],start+20)

    async def test_isolated_fee_probe_never_logs_credentials_or_account_volume(self):
        secret="a"*64
        calls=[]
        class Response:
            status=200
            async def __aenter__(self): return self
            async def __aexit__(self,*args): return False
            async def json(self):
                return {"success":True,"result":{"BTCUSDT":{"makerFeeRate":".001","takerFeeRate":".002","recent_days_sum":"PRIVATE_VOLUME"}}}
        class HTTP:
            def __init__(self,**kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): return False
            def get(self,url,**kwargs):
                calls.append((url,kwargs))
                return Response()
        with tempfile.TemporaryDirectory() as temp,offline_only():
            path=Path(temp)/"key.txt"
            path.write_text("api_key="+secret)
            with patch("wallex_sandbox.wallex_fee_probe.aiohttp.ClientSession",HTTP):
                result=await probe(path,["BTCUSDT"])
            self.assertEqual(result["status"],"verified")
            self.assertNotIn(secret,json.dumps(result))
            self.assertNotIn("PRIVATE_VOLUME",json.dumps(result))
            self.assertEqual(calls[0][0],"https://api.wallex.ir/v1/account/fee")
            self.assertFalse(calls[0][1]["allow_redirects"])
