"""Fixed, causal spot-long hypotheses; no learned parameters or order APIs."""
from __future__ import annotations

import numpy as np
import pandas as pd


CANDIDATES = ("trend_pullback", "macd_bb_filtered", "macd_bb_unfiltered", "supertrend",
              "bb_reentry", "donchian_breakout")


def indicators(frame: pd.DataFrame) -> pd.DataFrame:
    f=frame.copy()
    c=f.c
    f["ema20"]=c.ewm(span=20,adjust=False,min_periods=20).mean()
    f["ema50"]=c.ewm(span=50,adjust=False,min_periods=50).mean()
    f["ema200"]=c.ewm(span=200,adjust=False,min_periods=200).mean()
    delta=c.diff()
    gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    f["rsi"]=100-100/(1+gain/loss.replace(0,np.nan))
    f.loc[(loss == 0) & (gain > 0),"rsi"]=100
    f.loc[(loss == 0) & (gain == 0),"rsi"]=50
    tr=pd.concat([f.h-f.l,(f.h-c.shift()).abs(),(f.l-c.shift()).abs()],axis=1).max(axis=1)
    f["atr"]=tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    f["volume_mean"]=f.v.shift().rolling(20).mean()
    f["bb_mid"]=c.rolling(100).mean()
    f["bb_lower"]=f.bb_mid-2*c.rolling(100).std(ddof=0)
    f["bb20_mid"]=c.rolling(20).mean()
    f["bb20_lower"]=f.bb20_mid-2*c.rolling(20).std(ddof=0)
    f["donchian_high"]=f.h.shift().rolling(20).max()
    f["macd"]=c.ewm(span=21,adjust=False,min_periods=21).mean()-c.ewm(span=42,adjust=False,min_periods=42).mean()
    f["macd_hist"]=f.macd-f.macd.ewm(span=9,adjust=False,min_periods=9).mean()
    atr20=tr.ewm(alpha=1/20,adjust=False,min_periods=20).mean().to_numpy()
    midpoint=((f.h+f.l)/2).to_numpy()
    upper,lower=midpoint+4*atr20,midpoint-4*atr20
    direction=np.zeros(len(f),dtype=int)
    line=np.full(len(f),np.nan)
    closes=c.to_numpy()
    for i in range(1,len(f)):
        if not np.isfinite(atr20[i]):
            continue
        if not np.isfinite(line[i-1]):
            direction[i]=1
            line[i]=lower[i]
            continue
        if closes[i] > upper[i-1]:
            direction[i]=1
        elif closes[i] < lower[i-1]:
            direction[i]=-1
        else:
            direction[i]=direction[i-1]
            if direction[i] == 1:
                lower[i]=max(lower[i],lower[i-1])
            else:
                upper[i]=min(upper[i],upper[i-1])
        line[i]=lower[i] if direction[i] == 1 else upper[i]
    f["supertrend"]=line
    f["supertrend_direction"]=direction
    return f


def enrich(frame, interval):
    """Indicators on observed bars; gaps are never replaced with synthetic bars.

    Require 20 consecutive observed bars before new signals. Long moving-average
    windows count actual observations, so an old isolated missing candle does
    not discard months of legitimate history. Quote age is checked at the merge.
    """
    result=indicators(frame.sort_values("t").reset_index(drop=True))
    result["recent_contiguous"]=result.t.diff().eq(interval).rolling(20).sum().eq(20)
    return result


def signals(minutes15, hourly, entry_seconds=900, regime_seconds=3600):
    fast=enrich(minutes15,entry_seconds)
    slow=enrich(hourly,regime_seconds)
    slow["available_at"]=slow.t+regime_seconds
    slow["regime"]=(slow.c > slow.ema200)&(slow.ema50 > slow.ema200)&(slow.ema50 > slow.ema50.shift(3))
    slow["regime_ready"]=slow.ema200.notna()&slow.recent_contiguous
    fast["available_at"]=fast.t+entry_seconds
    # An hourly bar starting at 12:00 is unavailable before 13:00.
    fast=pd.merge_asof(fast.sort_values("available_at"),
                       slow[["available_at","regime","regime_ready"]].sort_values("available_at"),
                       on="available_at",direction="backward",tolerance=regime_seconds-1)
    regime=fast.regime.eq(True)
    ready=fast.regime_ready.eq(True)&fast.atr.notna()&fast.recent_contiguous
    adjacent=fast.t.diff().eq(entry_seconds)
    pullback=(fast.l.shift() <= fast.ema20.shift())&(fast.c.shift() <= fast.ema20.shift())&(fast.c > fast.ema20)
    cross=(fast.rsi.shift() < 50)&(fast.rsi >= 50)
    fast["trend_pullback"]=ready&regime&adjacent&pullback&cross&(fast.v > fast.volume_mean)
    recovery=(fast.c < fast.bb_lower)&(fast.macd_hist > 0)&(fast.macd < 0)
    fast["macd_bb_filtered"]=ready&regime&recovery
    fast["macd_bb_unfiltered"]=ready&recovery
    fast["supertrend"]=ready&regime&fast.supertrend_direction.eq(1)&((fast.c-fast.supertrend).abs()/fast.c < .01)
    fast["bb_reentry"]=(ready&adjacent&(fast.c.shift() < fast.bb20_lower.shift())&
                        (fast.c > fast.bb20_lower)&(fast.rsi > fast.rsi.shift())&(fast.rsi < 50))
    fast["donchian_breakout"]=(ready&adjacent&(fast.c > fast.donchian_high)&
                               (fast.c > fast.ema50)&(fast.v > fast.volume_mean))
    return fast
