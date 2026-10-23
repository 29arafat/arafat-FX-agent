"""ইন্ডিকেটর: EMA, RSI (Wilder), ATR (Wilder)। নিজে লেখা, বাইরের লাইব্রেরি ছাড়া।

নিয়ম:
- প্রথম (period-1)টা মান NaN, কারণ যথেষ্ট ডেটা নেই।
- EMA-র শুরুর মান প্রথম `period`টা ক্লোজের গড় (SMA)।
- RSI ও ATR-এ Wilder-এর মসৃণকরণ (RMA, alpha = 1/period), TradingView-এর মতো।
- শুধু অতীতের ডেটা ব্যবহার হয়, ভবিষ্যৎ নয় (লুক-অ্যাহেড নেই)।
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _check_period(period: int, n: int) -> None:
    if period < 1:
        raise ValueError("period অবশ্যই ১ বা বেশি হতে হবে")


def ema(values: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average, শুরু SMA দিয়ে।"""
    _check_period(period, len(values))
    x = values.to_numpy(dtype=float)
    out = np.full(len(x), np.nan)
    if len(x) < period:
        return pd.Series(out, index=values.index)
    alpha = 2.0 / (period + 1.0)
    out[period - 1] = x[:period].mean()
    for i in range(period, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return pd.Series(out, index=values.index)


def _wilder(x: np.ndarray, period: int, start: int) -> np.ndarray:
    """Wilder RMA: x[start:start+period]-এর গড় দিয়ে শুরু।"""
    out = np.full(len(x), np.nan)
    first = start + period - 1
    if len(x) <= first:
        return out
    out[first] = x[start : start + period].mean()
    for i in range(first + 1, len(x)):
        out[i] = (out[i - 1] * (period - 1) + x[i]) / period
    return out


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """RSI (Wilder)। সব লাভ ও লস শূন্য হলে ৫০, শুধু লস শূন্য হলে ১০০।"""
    _check_period(period, len(close))
    c = close.to_numpy(dtype=float)
    out = np.full(len(c), np.nan)
    if len(c) <= period:
        return pd.Series(out, index=close.index)
    delta = np.diff(c)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = _wilder(gain, period, 0)
    avg_loss = _wilder(loss, period, 0)
    # delta[i] = c[i+1]-c[i], তাই ফল c-এর সূচক i+1-এ বসে
    for i in range(len(delta)):
        g, l = avg_gain[i], avg_loss[i]
        if np.isnan(g):
            continue
        if g == 0.0 and l == 0.0:
            val = 50.0
        elif l == 0.0:
            val = 100.0
        else:
            val = 100.0 - 100.0 / (1.0 + g / l)
        out[i + 1] = val
    return pd.Series(out, index=close.index)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range (Wilder)।"""
    _check_period(period, len(close))
    h = high.to_numpy(dtype=float)
    l = low.to_numpy(dtype=float)
    c = close.to_numpy(dtype=float)
    tr = np.empty(len(c))
    if len(c) == 0:
        return pd.Series(tr, index=close.index)
    tr[0] = h[0] - l[0]
    prev_c = c[:-1]
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - prev_c), np.abs(l[1:] - prev_c)])
    return pd.Series(_wilder(tr, period, 0), index=close.index)


def add_indicators(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """ক্যান্ডেল DataFrame-এ ema_fast/mid/slow, rsi, atr কলাম যোগ করে (নতুন কপি ফেরত)।"""
    ind = cfg["indicators"]
    out = df.copy()
    out["ema_fast"] = ema(out["close"], ind["ema_fast"])
    out["ema_mid"] = ema(out["close"], ind["ema_mid"])
    out["ema_slow"] = ema(out["close"], ind["ema_slow"])
    out["rsi"] = rsi(out["close"], ind["rsi_period"])
    out["atr"] = atr(out["high"], out["low"], out["close"], ind["atr_period"])
    return out
