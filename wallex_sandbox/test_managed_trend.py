import tempfile
import unittest
from pathlib import Path

import pandas as pd

from .backtest_candidates import Simulation, simulate
from .managed_trend import prepare
from .report_candidates import ledger_check
from .test_candidates import META, execution_frame
from .test_momentum import dataset


def bars(values):
    return [{"t":i*900,"o":o,"h":h,"l":l,"c":c,"v":10} for i,(o,h,l,c) in enumerate(values)]


class ManagedChecks(unittest.TestCase):
    def test_trailing_stop_uses_prior_close_not_the_current_intrabar_high(self):
        f=execution_frame(bars([(100,101,99,100),(100,110,99,110),(110,111,106,108)]))
        r=simulate(f,META,"trend_pullback",900,2700,900,
                   Simulation(stop_atr=2,trailing_atr_multiple=3,disable_profit_target=True))
        t=r["trades_detail"][0]
        self.assertEqual(t["closed_epoch"],2700)
        self.assertEqual(t["reason"],"stop_loss")
        self.assertAlmostEqual(t["exit_price"],107*(1-Simulation().friction))
        ledger_check(r)

    def test_gap_below_trailing_stop_fills_at_open(self):
        f=execution_frame(bars([(100,101,99,100),(100,111,99,110),(100,101,99,100)]))
        r=simulate(f,META,"trend_pullback",900,2700,900,
                   Simulation(stop_atr=2,trailing_atr_multiple=3,disable_profit_target=True))
        t=r["trades_detail"][0]
        self.assertEqual(t["reason"],"gap_stop")
        self.assertAlmostEqual(t["exit_price"],100*(1-Simulation().friction))

    def test_signal_exit_is_executed_at_next_open(self):
        f=execution_frame(bars([(100,101,99,100),(100,101,99,100),(103,104,102,103)]))
        f["BTCUSDT"]["exit_signal"]=[False,True,False]
        r=simulate(f,META,"trend_pullback",900,2700,900,Simulation(signal_exit=True,disable_profit_target=True))
        t=r["trades_detail"][0]
        self.assertEqual(t["closed_epoch"],1800)
        self.assertEqual(t["reason"],"signal_exit")
        self.assertAlmostEqual(t["exit_price"],103*(1-Simulation().friction))

    def test_retention_avoids_extra_fills_but_honors_hard_time_cap(self):
        f=execution_frame(bars([(100,101,99,100)]*6))
        f["BTCUSDT"]["retain_signal"]=True
        r=simulate(f,META,"trend_pullback",900,5400,900,
                   Simulation(max_hold_bars=1,max_retained_bars=3,disable_profit_target=True))
        self.assertEqual(r["trades"],1)
        self.assertEqual(r["trades_detail"][0]["closed_epoch"],3600)
        self.assertEqual(r["trades_detail"][0]["reason"],"time_limit")
        self.assertIsNone(r["trades_detail"][0]["target_raw"])
        ledger_check(r)

    def test_appending_future_candles_cannot_rewrite_signals_or_ranking(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            dataset(folder,1200)
            short,_,_=prepare(folder)
            dataset(folder,1500)
            full,_,_=prepare(folder)
            for name in short:
                for symbol in short[name]:
                    pd.testing.assert_frame_equal(short[name][symbol],full[name][symbol].iloc[:1200])
            self.assertGreater(sum(int(f.managed_entry.sum()) for f in full["trail4h72"].values()),0)
