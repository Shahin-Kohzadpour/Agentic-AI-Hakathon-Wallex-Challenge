import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from .backtest_candidates import Simulation, simulate
from .momentum_research import prepare
from .test_candidates import META, execution_frame, rows


def dataset(directory,n):
    meta={}
    for symbol,rate in (("BTCUSDT",.0003),("ETHUSDT",.0004)):
        candles=[]
        for i in range(n):
            p=100*np.exp(rate*i)+np.sin(i/6)*.1
            candles.append({"t":i*3600,"o":p,"h":p+1,"l":p-1,"c":p+.01,"v":10})
        path=directory/f"{symbol}.json"
        path.write_text(json.dumps(candles))
        meta[symbol]={"3600":{"file":path.name,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()},
                      "metadata":META["BTCUSDT"],"spread_snapshot_bps":15}
    (directory/"manifest.json").write_text(json.dumps({"symbols":meta,"end":n*3600}))


class MomentumChecks(unittest.TestCase):
    def test_future_bars_do_not_change_prior_features_or_cross_market_choices(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            dataset(folder,1200)
            short,_,_=prepare(folder,48)
            dataset(folder,1500)
            full,_,_=prepare(folder,48)
            for symbol in short:
                cols=["mom7","mom28","momentum_score","atr","momentum_rotation"]
                pd.testing.assert_frame_equal(short[symbol][cols],full[symbol].iloc[:1200][cols])
            self.assertGreater(sum(int(f.momentum_rotation.sum()) for f in full.values()),0)

    def test_scheduled_rebalance_can_close_and_reopen_without_skipping_a_period(self):
        f=execution_frame(rows(5))
        f["BTCUSDT"]["trend_pullback"]=True
        result=simulate(f,META,"trend_pullback",900,4500,900,
                        Simulation(max_hold_bars=1,cooldown_bars=0),trade_limit=2)
        t=result["trades_detail"]
        self.assertEqual(t[0]["closed_epoch"],t[1]["opened_epoch"])
        self.assertEqual(result["trades"],2)
        self.assertGreater(result["fees"],0)
