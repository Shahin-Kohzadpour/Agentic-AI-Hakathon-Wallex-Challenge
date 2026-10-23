import unittest

import numpy as np
import pandas as pd

from .backtest_candidates import Simulation, aggregate, simulate
from .collect_research_data import normalize, resample15
from .indicator_candidates import indicators, signals


def rows(n=1000,step=900,start=0):
    result=[]
    for i in range(n):
        p=100+np.sin(i/15)*5+i*.01
        result.append({"t":start+i*step,"o":p,"h":p+1,"l":p-1,"c":p+.1,"v":10+i%9})
    return result


META={"BTCUSDT":{"quantity_precision":8,"min_quantity":"0.00000001","min_notional":"1"}}


def execution_frame(bars):
    frame=pd.DataFrame(bars)
    frame["available_at"]=frame.t+900
    frame["atr"]=1.
    frame["regime"]=True
    frame["trend_pullback"]=False
    frame.loc[0,"trend_pullback"]=True
    return {"BTCUSDT":frame}


class CandidateChecks(unittest.TestCase):
    def test_indicators_are_unchanged_when_future_candles_are_appended(self):
        full=pd.DataFrame(rows())
        prefix=indicators(full.iloc[:500])
        future=indicators(full)
        pd.testing.assert_frame_equal(prefix,future.iloc[:500])

    def test_hourly_close_is_unavailable_until_hour_finishes(self):
        fast=pd.DataFrame(rows(1000))
        slow=pd.DataFrame(rows(300,3600,start=-36000))
        base=signals(fast,slow)
        changed=slow.copy()
        # Modify the hour starting at the chosen fast candle's open. It cannot
        # influence a signal closing 15 minutes later, before that hour closes.
        t=900*800
        changed.loc[changed.t>=t,["o","h","l","c"]]*=10
        result=signals(fast,changed)
        columns=["regime","regime_ready","trend_pullback","macd_bb_filtered","supertrend"]
        pd.testing.assert_frame_equal(base.loc[base.available_at<t+3600,columns],
                                      result.loc[result.available_at<t+3600,columns])

    def test_resampling_drops_incomplete_candles(self):
        minute=rows(30,60)
        self.assertEqual(len(resample15(minute)),2)
        minute.pop(4)
        self.assertEqual(len(resample15(minute)),1)
        self.assertEqual(len(aggregate(rows(7,3600),3600,14400)),1)

    def test_invalid_candle_is_not_silently_accepted(self):
        payload={"s":"ok","t":[0],"o":[100],"h":[90],"l":[80],"c":[100],"v":[1]}
        with self.assertRaisesRegex(ValueError,"OHLCV"):
            normalize(payload,0,60,60)

    def test_stop_wins_when_same_bar_touches_both_stop_and_target(self):
        frame=execution_frame([
            {"t":0,"o":100,"h":101,"l":99,"c":100,"v":1},
            {"t":900,"o":100,"h":110,"l":90,"c":100,"v":1}])
        r=simulate(frame,META,"trend_pullback",900,1800,900)
        self.assertEqual(r["trades"],1)
        self.assertEqual(r["trades_detail"][0]["reason"],"stop_loss")
        self.assertLess(r["net_pnl"],0)
        self.assertAlmostEqual(r["final"]-1000,r["net_pnl"])
        self.assertLessEqual(r["trades_detail"][0]["spent"],100)
        self.assertLessEqual(r["trades_detail"][0]["planned_net_risk"],1+1e-9)

    def test_open_gap_cannot_fill_at_old_stop_price(self):
        frame=execution_frame([
            {"t":0,"o":100,"h":101,"l":99,"c":100,"v":1},
            {"t":900,"o":100,"h":100.5,"l":99,"c":100,"v":1},
            {"t":1800,"o":90,"h":91,"l":89,"c":90,"v":1}])
        r=simulate(frame,META,"trend_pullback",900,2700,900)
        t=r["trades_detail"][0]
        self.assertEqual(t["reason"],"gap_stop")
        self.assertLess(t["exit_price"],90)
        self.assertGreater(1000-r["final"],t["planned_net_risk"])

    def test_signal_never_fills_on_its_own_candle(self):
        frame=execution_frame(rows(5))
        r=simulate(frame,META,"trend_pullback",0,4500,900)
        self.assertEqual(r["trades_detail"][0]["opened_epoch"],900)
        self.assertLessEqual(r["trades_detail"][0]["signal_available_at"],900)

    def test_missing_next_candle_does_not_reuse_old_signal(self):
        frame=execution_frame([rows(1)[0],rows(3)[2]])
        r=simulate(frame,META,"trend_pullback",0,2700,900)
        self.assertEqual(r["trades"],0)

    def test_net_target_is_two_times_net_risk(self):
        frame=execution_frame([
            {"t":0,"o":100,"h":101,"l":99,"c":100,"v":1},
            {"t":900,"o":100,"h":120,"l":99,"c":110,"v":1}])
        r=simulate(frame,META,"trend_pullback",900,1800,900)
        t=r["trades_detail"][0]
        self.assertEqual(t["reason"],"take_profit")
        self.assertAlmostEqual(t["net_pnl"],2*t["planned_net_risk"])

    def test_stressed_friction_decreases_same_fill_profit(self):
        frame=execution_frame(rows(5))
        base=simulate(frame,META,"trend_pullback",0,4500,900)
        stress=simulate(frame,META,"trend_pullback",0,4500,900,Simulation(half_spread_bps=15,slippage_bps=2))
        self.assertLess(stress["net_pnl"],base["net_pnl"])

    def test_expensive_market_keeps_its_own_spread_costs(self):
        frame=execution_frame(rows(5))
        base=simulate(frame,META,"trend_pullback",0,4500,900)
        wide=simulate(frame,META,"trend_pullback",0,4500,900,
                      Simulation(symbol_half_spread_bps={"BTCUSDT":100}))
        self.assertLess(wide["net_pnl"],base["net_pnl"])

    def test_band_target_is_rejected_when_fees_consume_the_reward(self):
        frame=execution_frame(rows(3))
        f=frame["BTCUSDT"]
        f["bb_reentry"]=f.trend_pullback
        f["bb20_mid"]=f.o+0.01
        result=simulate(frame,META,"bb_reentry",0,2700,900)
        self.assertEqual(result["trades"],0)
        self.assertEqual(result["selection_rejections"]["insufficient_net_reward"],1)
