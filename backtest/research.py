"""ধাপ ৩গ: বেশি-ফ্রিকোয়েন্সির প্রার্থী স্ট্র্যাটেজি যাচাই (গবেষণা, লাইভ সিস্টেমে কিছু বদলায় না)।

চালানো:  python -m backtest.research --years 3 --pair XAUUSD

নিয়ম (ফলাফল দেখার আগেই ঠিক করা, নিচে CRITERIA):
- প্রতিটি প্রার্থীর প্যারামিটার একবারই ঠিক করা; ফলাফল দেখে বদলানো নিষিদ্ধ। টিউনিং নেই।
- ভিত্তি ডেটা M15; M30/H1 তার থেকে বানানো। শুধু ক্লোজড ক্যান্ডেল। এন্ট্রি = সিগন্যাল ক্যান্ডেলের ক্লোজ।
- TP/SL মেলানো M15 ক্যান্ডেলে, একই ক্যান্ডেলে দুটোই হলে SL। স্প্রেড config থেকে।
- দেরি-পরীক্ষা: লাইভ সিস্টেম ঘণ্টায় একবার চলে, তাই ০/৩০/৬০ মিনিট দেরিতে এন্ট্রি ধরে আলাদা ফল।
- বিপরীত-বেসলাইন: একই এন্ট্রিতে উল্টো দিক (SL/TP আয়না)। দিক-নির্বাচন কাজ করছে কিনা বোঝার জন্য।
- ৯টি প্রার্থী একসাথে পরীক্ষা হয়, তাই ভাগ্যক্রমে পাস করার ঝুঁকি সামলাতে ৯৯% bootstrap CI।
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from src.fetch import (FetchError, Throttle, clean, drop_flat, drop_running, drop_weekend, fetch_h1_history,
                       load_config, utcnow)
from src.indicators import atr, ema, rsi
from src.stats import summarize
from src.strategy import NS_PER_MIN, friday_blocked
from src.tracker import find_exit

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
OUT = ROOT / "output"

# ---------- আগে-থেকে ঘোষিত সাফল্যের শর্ত (ফলাফল দেখার পরে বদলানো যাবে না) ----------
CRITERIA = {
    "min_trades": 150,          # সব ডেটায় নেট ট্রেড অন্তত এত
    "ci_level": 0.99,           # ৯টি প্রার্থী → কঠোর CI
    "weak_ci_level": 0.95,
    "need_both_halves_positive": True,   # প্রথম ও দ্বিতীয় অর্ধেক দুটোতেই নেট গড় R > 0
    "need_delay_60_positive": True,      # ৬০ মিনিট দেরিতেও নেট গড় R > 0
    "min_signals_per_day": 0.5,          # "দৈনিক ২-৩টি" লক্ষ্যের জন্য আলাদা করে দেখানো হয়, পাস/ফেলের শর্ত নয়
}
DELAYS_MIN = (0, 30, 60)
LIQUID = (6, 20)                # সিগন্যাল ক্যান্ডেল শেষ হওয়ার UTC ঘণ্টা [6, 20)
MAX_HOLD_M15 = 96               # ২৪ ঘণ্টা; এর মধ্যে TP/SL না হলে মার্কেটে বন্ধ
COOLDOWN_BARS = 2


def ns(idx) -> np.ndarray:
    """সূচক → ন্যানোসেকেন্ড (pandas ৩-এ ডিফল্ট ইউনিট মাইক্রোসেকেন্ড হতে পারে, তাই স্পষ্ট রূপান্তর)।"""
    return pd.DatetimeIndex(idx).as_unit("ns").asi8.copy()


# ---------- ডেটা ----------

def load_m15(pair: str, cfg: dict, api_key: str | None, throttle: Throttle, years: float,
             refresh: bool = False) -> pd.DataFrame:
    path = CACHE / f"{pair}_M15.csv.gz"
    if path.exists() and not refresh:
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        df.index.name = "datetime"
        return df
    if not api_key:
        raise FetchError("PRICE_API_KEY নেই এবং ক্যাশও নেই")
    d = cfg["data"]
    df = fetch_h1_history(cfg["pairs"][pair]["symbol"], int(years * 25000), page_bars=5000, max_pages=22,
                          tf="M15", api_key=api_key, throttle=throttle, retries=d["retries"],
                          timeout=d["request_timeout_seconds"], tz=d["timezone"])
    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, compression="gzip")
    return df


def clean_m15(df: pd.DataFrame, cfg: dict, now) -> pd.DataFrame:
    d = cfg["data"]
    df = drop_running(df, 15, now)
    df = drop_flat(df)
    w = d["weekend"]
    return drop_weekend(df, w["close"]["weekday"], w["close"]["hour"], w["open"]["weekday"], w["open"]["hour"])


def resample_tf(m15: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """M15 → বড় টাইমফ্রেম। অসম্পূর্ণ গ্রুপ (সব M15 নেই) বাদ। গ্রিড UTC 00:00 থেকে।"""
    if minutes == 15:
        return m15[["open", "high", "low", "close"]].copy()
    n = minutes // 15
    g = m15.resample(f"{minutes}min", label="left", closed="left")
    out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                        "close": g["close"].last(), "n": g["close"].count()})
    out = out[out["n"] == n].drop(columns="n")
    return out


# ---------- প্রার্থী স্ট্র্যাটেজি (প্রতিটি শুধু আগের ডেটা দেখে) ----------

@dataclass
class Cand:
    name: str
    family: str
    tf: str                 # M15 | M30 | H1
    desc: str


def _arrays(df: pd.DataFrame, minutes: int) -> dict:
    c = df["close"]
    a = atr(df["high"], df["low"], c, 14).to_numpy()
    e20, e50, e200 = (ema(c, p).to_numpy() for p in (20, 50, 200))
    ts = ns(df.index)
    return {"o": df["open"].to_numpy(), "h": df["high"].to_numpy(), "l": df["low"].to_numpy(),
            "c": c.to_numpy(), "atr": a, "e20": e20, "e50": e50, "e200": e200,
            "rsi": rsi(c, 14).to_numpy(), "start": ts, "end": ts + minutes * NS_PER_MIN, "n": len(df)}


def _hour(end_ns: int) -> float:
    return ((end_ns // NS_PER_MIN) % 1440) / 60.0


def _sig(i, d, entry, sl, tp):
    return {"i": i, "dir": d, "entry": float(entry), "sl": float(sl), "tp": float(tp)}


def gen_range_breakout(df: pd.DataFrame, minutes: int, r_start: float, r_end: float, w_end: float,
                       rr: float = 1.5) -> list[dict]:
    """রেঞ্জ [r_start, r_end) UTC ঘণ্টায় বানানো; পরের জানালা [r_end, w_end) তে প্রথম ক্লোজ-ব্রেকআউটে এন্ট্রি, দিনে ১টি।
    SL = রেঞ্জের উল্টো প্রান্ত (ঝুঁকি ২–১২ ATR ছাড়া বাদ), TP = rr × ঝুঁকি।"""
    A = _arrays(df, minutes)
    day = (A["start"] // (1440 * NS_PER_MIN))
    out, i, n = [], 0, A["n"]
    hr_start = ((A["start"] // NS_PER_MIN) % 1440) / 60.0
    hr_end = ((A["end"] // NS_PER_MIN) % 1440) / 60.0
    last_day = None
    rng_hi = rng_lo = None
    done_day = None
    for i in range(n):
        d = day[i]
        if d != last_day:
            last_day, rng_hi, rng_lo = d, -np.inf, np.inf
        # রেঞ্জ গঠন: ক্যান্ডেল পুরোটা রেঞ্জ-জানালায়
        if r_start <= hr_start[i] and hr_end[i] <= r_end and hr_end[i] > hr_start[i]:
            rng_hi, rng_lo = max(rng_hi, A["h"][i]), min(rng_lo, A["l"][i])
            continue
        if done_day == d or not np.isfinite(rng_hi) or not np.isfinite(rng_lo):
            continue
        # জানালা: ক্যান্ডেল শেষ হওয়ার সময় (r_end, w_end]
        if not (hr_end[i] > r_end and hr_end[i] <= w_end and hr_start[i] >= r_end):
            continue
        a = A["atr"][i]
        if not np.isfinite(a) or a <= 0:
            continue
        c = A["c"][i]
        if c > rng_hi:
            risk = c - rng_lo
            d_ = "BUY"
        elif c < rng_lo:
            risk = rng_hi - c
            d_ = "SELL"
        else:
            continue
        done_day = d
        if not (2 * a <= risk <= 12 * a):
            continue
        if d_ == "BUY":
            out.append(_sig(i, d_, c, rng_lo, c + rr * risk))
        else:
            out.append(_sig(i, d_, c, rng_hi, c - rr * risk))
    return out


def gen_asian(df, minutes):
    return gen_range_breakout(df, minutes, 0.0, 7.0, 16.0)


def gen_london(df, minutes):
    return gen_range_breakout(df, minutes, 7.0, 8.0, 16.0)


def gen_donchian(df: pd.DataFrame, minutes: int, n: int = 20) -> list[dict]:
    """গত n ক্যান্ডেলের হাই/লো ক্লোজে ভাঙলে, EMA200-এর দিকে। SL = ১.৫ ATR, TP = ৩ ATR (১:২)।"""
    A = _arrays(df, minutes)
    hh = pd.Series(A["h"]).rolling(n).max().shift(1).to_numpy()
    ll = pd.Series(A["l"]).rolling(n).min().shift(1).to_numpy()
    out = []
    for i in range(210, A["n"]):
        a, c = A["atr"][i], A["c"][i]
        if not np.isfinite(a) or a <= 0 or not np.isfinite(hh[i]) or not (LIQUID[0] <= _hour(A["end"][i]) < LIQUID[1]):
            continue
        if c > hh[i] and c > A["e200"][i]:
            out.append(_sig(i, "BUY", c, c - 1.5 * a, c + 3 * a))
        elif c < ll[i] and c < A["e200"][i]:
            out.append(_sig(i, "SELL", c, c + 1.5 * a, c - 3 * a))
    return out


def gen_pullback(df: pd.DataFrame, minutes: int) -> list[dict]:
    """ট্রেন্ড (EMA50 vs EMA200 ফাঁক ≥ ০.৩ ATR); গত ৫ ক্যান্ডেলে EMA20 ছোঁয়া; এই ক্যান্ডেল EMA20-এর ওপারে প্রথম
    ক্লোজ; গত ৬ ক্যান্ডেলে EMA50-এর ওপারে ক্লোজ নেই। SL = ১.৫ ATR, TP = ৩ ATR।"""
    A = _arrays(df, minutes)
    out = []
    for i in range(210, A["n"]):
        a, c = A["atr"][i], A["c"][i]
        if not np.isfinite(a) or a <= 0 or not (LIQUID[0] <= _hour(A["end"][i]) < LIQUID[1]):
            continue
        gap = A["e50"][i] - A["e200"][i]
        s = slice(i - 5, i + 1)
        if gap >= 0.3 * a:
            touched = (A["l"][i - 5:i] <= A["e20"][i - 5:i]).any()
            reclaim = A["c"][i - 1] <= A["e20"][i - 1] < c and c > A["o"][i]
            if touched and reclaim and not (A["c"][s] < A["e50"][s]).any():
                out.append(_sig(i, "BUY", c, c - 1.5 * a, c + 3 * a))
        elif gap <= -0.3 * a:
            touched = (A["h"][i - 5:i] >= A["e20"][i - 5:i]).any()
            reclaim = A["c"][i - 1] >= A["e20"][i - 1] > c and c < A["o"][i]
            if touched and reclaim and not (A["c"][s] > A["e50"][s]).any():
                out.append(_sig(i, "SELL", c, c + 1.5 * a, c - 3 * a))
    return out


def gen_meanrev(df: pd.DataFrame, minutes: int) -> list[dict]:
    """বলিঙ্গার(২০,২) বাইরে ক্লোজ + RSI চরম (<৩০ / >৭০), শুধু রেঞ্জ-পরিস্থিতিতে (|EMA50−EMA200| < ০.৫ ATR)।
    SL = ১.৫ ATR, TP = ১.৫ ATR (১:১)।"""
    A = _arrays(df, minutes)
    cs = pd.Series(A["c"])
    mid, sd = cs.rolling(20).mean().to_numpy(), cs.rolling(20).std(ddof=0).to_numpy()
    out = []
    for i in range(210, A["n"]):
        a, c = A["atr"][i], A["c"][i]
        if not np.isfinite(a) or a <= 0 or not np.isfinite(mid[i]) or not (LIQUID[0] <= _hour(A["end"][i]) < LIQUID[1]):
            continue
        if abs(A["e50"][i] - A["e200"][i]) >= 0.5 * a:
            continue
        if c < mid[i] - 2 * sd[i] and A["rsi"][i] < 30:
            out.append(_sig(i, "BUY", c, c - 1.5 * a, c + 1.5 * a))
        elif c > mid[i] + 2 * sd[i] and A["rsi"][i] > 70:
            out.append(_sig(i, "SELL", c, c + 1.5 * a, c - 1.5 * a))
    return out


CANDIDATES: list[tuple[Cand, callable]] = [
    (Cand("S1 এশিয়ান-রেঞ্জ ব্রেকআউট M15", "range", "M15", "রেঞ্জ ০০–০৭ UTC, জানালা ০৭–১৬, SL উল্টো প্রান্ত, TP ১.৫R"), gen_asian),
    (Cand("S2 লন্ডন-ওপেন রেঞ্জ ব্রেকআউট M15", "range", "M15", "রেঞ্জ ০৭–০৮ UTC, জানালা ০৮–১৬, SL উল্টো প্রান্ত, TP ১.৫R"), gen_london),
    (Cand("S3 ডনচিয়ান-২০ ব্রেকআউট M15", "breakout", "M15", "২০ ক্যান্ডেলের হাই/লো ভাঙা + EMA200 দিক, SL ১.৫ATR, TP ৩ATR"), gen_donchian),
    (Cand("S3 ডনচিয়ান-২০ ব্রেকআউট M30", "breakout", "M30", "একই নিয়ম M30-এ"), gen_donchian),
    (Cand("S3 ডনচিয়ান-২০ ব্রেকআউট H1", "breakout", "H1", "একই নিয়ম H1-এ"), gen_donchian),
    (Cand("S4 ট্রেন্ড-পুলব্যাক M15", "pullback", "M15", "EMA20 ছুঁয়ে ফেরা, ট্রেন্ড ফিল্টার, SL ১.৫ATR, TP ৩ATR"), gen_pullback),
    (Cand("S4 ট্রেন্ড-পুলব্যাক M30", "pullback", "M30", "একই নিয়ম M30-এ"), gen_pullback),
    (Cand("S5 মিন-রিভার্সন (বলিঙ্গার+RSI) M15", "meanrev", "M15", "রেঞ্জ-পরিস্থিতিতে চরমে উল্টো, SL ১.৫ATR, TP ১.৫ATR"), gen_meanrev),
    (Cand("S5 মিন-রিভার্সন (বলিঙ্গার+RSI) M30", "meanrev", "M30", "একই নিয়ম M30-এ"), gen_meanrev),
]
TF_MIN = {"M15": 15, "M30": 30, "H1": 60}


# ---------- সিমুলেশন ----------

def _r_market(direction, entry, sl, close, spread):
    """সময়-শেষে মার্কেটে বন্ধ: R (স্প্রেডসহ, tracker.result_r-এর সঙ্গে সঙ্গত)।"""
    if direction == "BUY":
        fill = entry + spread
        risk = fill - sl
        return (close - fill) / risk if risk > 0 else None
    risk = sl - entry
    return (entry - (close + spread)) / risk if risk > 0 else None


def simulate(m15: pd.DataFrame, tf_df: pd.DataFrame, minutes: int, sigs: list[dict], spread: float,
             delay_min: int = 0, reverse: bool = False, cutoff: str = "18:00") -> list[dict]:
    """সিগন্যাল তালিকা → ট্রেড। একবারে একটি ট্রেড; শেষ হওয়ার পর COOLDOWN_BARS টাইমফ্রেম-ক্যান্ডেল বিরতি।
    delay_min: সিগন্যালের কত মিনিট পরে এন্ট্রি (সেই সময়ের M15 ক্লোজ দামে; SL/TP আগের জায়গায়)।"""
    H, L, C = m15["high"].to_numpy(), m15["low"].to_numpy(), m15["close"].to_numpy()
    st = ns(m15.index)
    tf_end = ns(tf_df.index) + minutes * NS_PER_MIN
    trades, free = [], -1
    d_bars = delay_min // 15
    for s in sigs:
        t = int(tf_end[s["i"]])
        if t < free or friday_blocked(t, cutoff):
            continue
        k0 = int(np.searchsorted(st, t, side="left"))      # সিগন্যালের পরের প্রথম M15
        if k0 >= len(st) or st[k0] - t > 15 * NS_PER_MIN:   # ডেটায় ফাঁক (যেমন সপ্তাহান্ত): ধরব না
            continue
        d, entry, sl, tp = s["dir"], s["entry"], s["sl"], s["tp"]
        if reverse:   # আয়না: উল্টো দিক, একই দূরত্ব
            d = "SELL" if d == "BUY" else "BUY"
            sl, tp = 2 * entry - sl, 2 * entry - tp
        kx = k0
        if d_bars:
            kx = k0 + d_bars
            if kx >= len(st):
                continue
            pre = find_exit(d, sl, tp, H[k0:kx], L[k0:kx], spread)
            if pre is not None:          # দেরির মধ্যেই SL/TP ছুঁয়ে গেছে: ট্রেড হাতছাড়া
                continue
            entry = float(C[kx - 1])
            if (d == "BUY" and not sl < entry < tp) or (d == "SELL" and not tp < entry < sl):
                continue
        end_k = min(kx + MAX_HOLD_M15, len(H))
        ex = find_exit(d, sl, tp, H[kx:end_k], L[kx:end_k], spread)
        if ex is None:
            if end_k - kx < MAX_HOLD_M15:   # ডেটা শেষ, গণনার বাইরে
                break
            r = _r_market(d, entry, sl, float(C[end_k - 1]), spread)
            if r is None:
                continue
            status, k, amb = "TIME", end_k - 1 - kx, False
        else:
            k, status, amb = ex
            if status == "SL":
                r = -1.0
            else:
                if d == "BUY":
                    fill = entry + spread
                    risk = fill - sl
                    r = (tp - fill) / risk if risk > 0 else None
                else:
                    risk = sl - entry
                    r = (entry - tp) / risk if risk > 0 else None
                if r is None:
                    continue
        exit_ns = int(st[kx + k]) + 15 * NS_PER_MIN
        free = exit_ns + COOLDOWN_BARS * minutes * NS_PER_MIN
        trades.append({"t": int(st[kx]), "exit_t": exit_ns, "dir": d, "status": status, "r": float(r), "amb": amb})
    return trades


# ---------- পরিসংখ্যান ----------

def bootstrap_ci(rs: list[float], level: float, n_boot: int = 4000, seed: int = 7) -> tuple[float, float]:
    """গড় R-এর bootstrap আস্থা-ব্যবধান (নিম্ন, উচ্চ)। নমুনা ছোট/খালি হলে (nan, nan)।"""
    if len(rs) < 10:
        return float("nan"), float("nan")
    a = np.asarray(rs, dtype=float)
    rng = np.random.default_rng(seed)
    means = a[rng.integers(0, len(a), size=(n_boot, len(a)))].mean(axis=1)
    lo, hi = np.quantile(means, [(1 - level) / 2, 1 - (1 - level) / 2])
    return float(lo), float(hi)


def quarter_table(trades: list[dict]) -> list[tuple[str, int, float]]:
    if not trades:
        return []
    df = pd.DataFrame(trades)
    df["q"] = pd.to_datetime(df["t"], utc=True).dt.tz_localize(None).dt.to_period("Q").astype(str)
    return [(q, len(g), float(g["r"].mean())) for q, g in df.groupby("q")]


def evaluate_candidate(m15: pd.DataFrame, cand: Cand, gen, spread: float, cutoff: str,
                       cut_ns: int) -> dict:
    minutes = TF_MIN[cand.tf]
    tf_df = resample_tf(m15, minutes)
    sigs = gen(tf_df, minutes)
    res: dict = {"cand": cand, "signals_raw": len(sigs)}
    res["gross"] = simulate(m15, tf_df, minutes, sigs, 0.0, 0, False, cutoff)
    for dm in DELAYS_MIN:
        res[f"net_d{dm}"] = simulate(m15, tf_df, minutes, sigs, spread, dm, False, cutoff)
    res["reverse"] = simulate(m15, tf_df, minutes, sigs, spread, 0, True, cutoff)
    res["cut_ns"] = cut_ns
    return res


def judge(res: dict, span_days: float, crit: dict = CRITERIA) -> dict:
    """আগে-ঘোষিত শর্তে রায়: PASS / WEAK / FAIL।"""
    net = res["net_d0"]
    rs = [x["r"] for x in net]
    cut = res["cut_ns"]
    first = [x["r"] for x in net if x["t"] < cut]
    second = [x["r"] for x in net if x["t"] >= cut]
    exp = float(np.mean(rs)) if rs else 0.0
    lo99, hi99 = bootstrap_ci(rs, crit["ci_level"])
    lo95, hi95 = bootstrap_ci(rs, crit["weak_ci_level"])
    d60 = [x["r"] for x in res["net_d60"]]
    checks = {
        "trades": len(rs) >= crit["min_trades"],
        "halves": (not crit["need_both_halves_positive"]) or (bool(first) and bool(second) and np.mean(first) > 0 and np.mean(second) > 0),
        "delay60": (not crit["need_delay_60_positive"]) or (bool(d60) and float(np.mean(d60)) > 0),
        "ci99": lo99 > 0,
        "ci95": lo95 > 0,
    }
    if checks["trades"] and checks["halves"] and checks["delay60"] and checks["ci99"]:
        verdict = "PASS"
    elif checks["trades"] and checks["halves"] and checks["delay60"] and checks["ci95"]:
        verdict = "WEAK"
    else:
        verdict = "FAIL"
    return {"verdict": verdict, "checks": checks, "expectancy": exp, "ci99": (lo99, hi99), "ci95": (lo95, hi95),
            "per_day": len(rs) / span_days if span_days > 0 else 0.0}


# ---------- রিপোর্ট ----------

def _pf(x):
    return "∞" if x is None else f"{x:.2f}"


def _line(label, trades, mt=30):
    s = summarize([x["r"] for x in trades], mt)
    flag = "" if s["reliable"] else " ⚠"
    return (f"| {label} | {s['trades']}{flag} | {s['win_rate']:.0f}% | {s['total_r']:+.1f} | {s['expectancy']:+.3f} | "
            f"{_pf(s['profit_factor'])} | {s['max_drawdown_r']:.1f} |")


HEAD = "| | ট্রেড | উইন রেট | মোট R | গড় R/ট্রেড | PF | ড্রডাউন (R) |\n|---|---|---|---|---|---|---|"


def render(results: list[dict], judged: list[dict], meta: dict) -> str:
    L = ["# ধাপ ৩গ: বেশি-ফ্রিকোয়েন্সি স্ট্র্যাটেজি গবেষণা", "",
         f"পেয়ার: {meta['pair']} | ডেটা: {meta['start'][:10]} → {meta['end'][:10]} ({meta['days']:.0f} দিন, M15 ক্যান্ডেল {meta['bars']}) | "
         f"স্প্রেড: {meta['spread']}", "",
         "**এটা গবেষণা, লাইভ সিস্টেম বদলায়নি।** প্রার্থীদের প্যারামিটার ফলাফল দেখার আগেই ঠিক করা, কোনো টিউনিং হয়নি।", "",
         "## আগে-ঘোষিত সাফল্যের শর্ত", "",
         f"**PASS** = নেট (স্প্রেডসহ) ট্রেড ≥ {CRITERIA['min_trades']} **এবং** গড় R-এর {int(CRITERIA['ci_level']*100)}% bootstrap CI-এর নিম্নসীমা > 0 "
         "**এবং** ডেটার প্রথম ও দ্বিতীয় অর্ধেক দুটোতেই গড় R > 0 **এবং** ৬০ মিনিট দেরিতে এন্ট্রি ধরলেও গড় R > 0।",
         f"**WEAK** = উপরের সব, শুধু CI {int(CRITERIA['weak_ci_level']*100)}% এ পাস (৯৯%-এ নয়): ইঙ্গিত, নিশ্চিত নয়। **FAIL** = বাকি সব।", "",
         "৯টি প্রার্থী একসাথে পরীক্ষা হচ্ছে, তাই ভাগ্যক্রমে একটা পাস করে যাওয়ার সম্ভাবনা আছে। সেজন্য ৯৯% CI। "
         "প্যারামিটার টিউন হয়নি বলে পুরো ডেটাই কার্যত অচেনা ডেটা, তবু দুই অর্ধেক আলাদা দেখানো হলো।", "",
         "## সারসংক্ষেপ", "",
         "| প্রার্থী | রায় | নেট গড় R | ৯৯% CI | দৈনিক সিগন্যাল | ৬০মি দেরিতে গড় R | উল্টো-বেসলাইন গড় R |", "|---|---|---|---|---|---|---|"]
    for r, j in zip(results, judged):
        d60 = [x["r"] for x in r["net_d60"]]
        rev = [x["r"] for x in r["reverse"]]
        lo, hi = j["ci99"]
        ci = "–" if np.isnan(lo) else f"[{lo:+.2f}, {hi:+.2f}]"
        L.append(f"| {r['cand'].name} | **{j['verdict']}** | {j['expectancy']:+.3f} | {ci} | {j['per_day']:.2f} | "
                 f"{(np.mean(d60) if d60 else 0):+.3f} | {(np.mean(rev) if rev else 0):+.3f} |")
    passed = [(r, j) for r, j in zip(results, judged) if j["verdict"] == "PASS"]
    weak = [(r, j) for r, j in zip(results, judged) if j["verdict"] == "WEAK"]
    L += ["", "## সোজা কথা", ""]
    if passed:
        tot = sum(j["per_day"] for _, j in passed)
        L.append(f"- PASS করেছে {len(passed)}টি। সবগুলো মিলিয়ে (ওভারল্যাপ ধরলে কিছু কম) দৈনিক প্রায় {tot:.2f}টি সিগন্যাল। "
                 "এটা ইতিহাসের ফল; লাইভ ফরোয়ার্ড টেস্ট ছাড়া ভরসা নয়।")
    else:
        L.append("- কোনো প্রার্থী ঘোষিত শর্তে PASS করেনি। এর মানে: এই সহজ নিয়মগুলোতে, এই ডেটায়, স্প্রেড বাদ দিয়ে টেকার মতো এজ প্রমাণিত হয়নি।")
    if weak:
        L.append(f"- WEAK (ইঙ্গিত মাত্র): {', '.join(r['cand'].name for r, _ in weak)}।")
    hi_freq = [r["cand"].name for r, j in zip(results, judged) if j["per_day"] >= 1.0]
    L.append(f"- দৈনিক ≥১ সিগন্যাল দেয় (রায় যা-ই হোক): {', '.join(hi_freq) if hi_freq else 'কেউ না'}।")
    L += ["- 'উল্টো-বেসলাইন' মানে একই এন্ট্রিতে উল্টো দিকে ট্রেড। দুই দিকই নেগেটিভ হলে লোকসানের মূল কারণ স্প্রেড/নয়েজ, দিক-নির্বাচনে এজ নেই।", ""]
    for r, j in zip(results, judged):
        c = r["cand"]
        L += [f"## {c.name}  →  {j['verdict']}", "", f"{c.desc}। কাঁচা সিগন্যাল {r['signals_raw']}টি, একবারে-একটি-ট্রেড নিয়মে ট্রেড {len(r['net_d0'])}টি।", "", HEAD,
              _line("গ্রস (স্প্রেড ০)", r["gross"]), _line("নেট, দেরি ০", r["net_d0"]),
              _line("নেট, দেরি ৩০মি", r["net_d30"]), _line("নেট, দেরি ৬০মি", r["net_d60"]),
              _line("নেট, প্রথম অর্ধেক", [x for x in r["net_d0"] if x["t"] < r["cut_ns"]]),
              _line("নেট, দ্বিতীয় অর্ধেক", [x for x in r["net_d0"] if x["t"] >= r["cut_ns"]]),
              _line("উল্টো-বেসলাইন (নেট)", r["reverse"]), ""]
        q = quarter_table(r["net_d0"])
        if q:
            pos = sum(1 for _, n, m in q if n >= 10 and m > 0)
            tot = sum(1 for _, n, _ in q if n >= 10)
            L.append(f"ত্রৈমাসিক ধারাবাহিকতা (≥১০ ট্রেডের কোয়ার্টার): {pos}/{tot} কোয়ার্টারে নেট গড় R > 0। "
                     + " ".join(f"{k}:{m:+.2f}({n})" for k, n, m in q))
            L.append("")
        ck = j["checks"]
        L.append("শর্ত: " + " · ".join(f"{k}={'✓' if v else '✗'}" for k, v in ck.items()))
        L.append("")
    L += ["## সীমাবদ্ধতা", "",
          "- স্প্রেড স্থির ধরা (config)। আসলে খবরের সময় ও রোলওভারে বাড়ে; নিচের টাইমফ্রেমে এর প্রভাব বেশি।",
          "- স্লিপেজ, কমিশন, সুইং ধরা হয়নি। ২৪ ঘণ্টার মধ্যে TP/SL না হলে মার্কেটে বন্ধ ধরা।",
          "- লাইভ সিস্টেম এখন ঘণ্টায় একবার চলে (GitHub Actions-এর দেরিসহ)। M15/M30 সিগন্যাল লাইভ নিতে হলে দেরি ও চেক-ফ্রিকোয়েন্সি নিয়ে আলাদা সমাধান লাগবে; তাই দেরি-পরীক্ষা যোগ করা।",
          "- এক পেয়ার, ~৩ বছর: বাজারের অবস্থা (যেমন সোনার বড় ট্রেন্ড) ফলকে প্রভাবিত করতে পারে।"]
    return "\n".join(L) + "\n"


def run(m15_raw: pd.DataFrame, cfg: dict, pair: str, now) -> tuple[str, dict]:
    m15 = clean_m15(m15_raw, cfg, now)
    if len(m15) < 3000:
        raise FetchError(f"M15 ডেটা অপর্যাপ্ত ({len(m15)} ক্যান্ডেল)")
    lo, hi = m15.index[0].value, m15.index[-1].value + 15 * NS_PER_MIN
    cut = int(lo + (hi - lo) * (1 - cfg["backtest"]["oos_fraction"]))
    cut_half = int(lo + (hi - lo) * 0.5)
    spread = cfg["backtest"]["spread"][pair]
    cutoff = cfg["rules"]["friday_cutoff_utc"]
    days = (hi - lo) / (1440 * NS_PER_MIN) * 5 / 7          # মোটামুটি ট্রেডিং-দিন (সপ্তাহে ৫ দিন)
    results, judged = [], []
    for cand, gen in CANDIDATES:
        r = evaluate_candidate(m15, cand, gen, spread, cutoff, cut_half)
        results.append(r)
        judged.append(judge(r, days))
        print(f"  {cand.name}: {judged[-1]['verdict']} ({len(r['net_d0'])} ট্রেড)", flush=True)
    meta = {"pair": pair, "start": str(pd.Timestamp(lo)), "end": str(pd.Timestamp(hi)), "days": days,
            "bars": len(m15), "spread": spread}
    summ = {"meta": meta, "candidates": [{"name": r["cand"].name, "verdict": j["verdict"], "expectancy": j["expectancy"],
                                          "ci99": j["ci99"], "per_day": j["per_day"], "trades": len(r["net_d0"])}
                                         for r, j in zip(results, judged)]}
    return render(results, judged, meta), summ


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ধাপ ৩গ গবেষণা")
    ap.add_argument("--years", type=float, default=3)
    ap.add_argument("--pair", default="XAUUSD")
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    cfg = load_config()
    key = os.environ.get("PRICE_API_KEY", "").strip() or None
    pair = args.pair.upper()
    throttle = Throttle(cfg["api_limits"]["sleep_between_requests_seconds"])
    try:
        print(f"M15 ডেটা: {pair} ...", flush=True)
        raw = load_m15(pair, cfg, key, throttle, args.years, args.refresh)
        print(f"  M15 ক্যান্ডেল: {len(raw)}", flush=True)
        report, summ = run(raw, cfg, pair, utcnow())
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "research.md").write_text(report, encoding="utf-8")
        (OUT / "research.json").write_text(json.dumps(summ, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"ত্রুটি: {type(e).__name__}: {clean(str(e), key)}"[:500], file=sys.stderr)
        return 1
    print(f"শেষ। রিপোর্ট: {OUT / 'research.md'} | API ক্রেডিট: {throttle.credits_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
