"""সিগন্যালের যুক্তি। ব্যাকটেস্ট ও লাইভ ইঞ্জিন একই কোড ব্যবহার করে।

কাঠামো (সব সংখ্যা config.yaml-এ; ব্যাকটেস্টে যাচাইযোগ্য খসড়া, টিউন করা নয়):
- তিন টাইমফ্রেমের স্বতন্ত্র সিগন্যাল, `strategy.tf_map` অনুযায়ী:
    H1: ট্রেন্ড D1, সেটআপ H4, ট্রিগার H1
    H4: ট্রেন্ড D1, সেটআপ H4, ট্রিগার H4
    D1: ট্রেন্ড D1, সেটআপ D1, ট্রিগার D1
- ট্রেন্ড (ট্রেন্ড-টাইমফ্রেমের শেষ ক্লোজড ক্যান্ডেল): EMA50 − EMA200 ≥ gap × ATR হলে BUY, ≤ −gap × ATR হলে SELL।
- পুলব্যাক জোন (সেটআপ-টাইমফ্রেম, শেষ N ক্যান্ডেল): BUY-তে কোনো ক্যান্ডেলের Low ≤ EMA20, এবং কোনো ক্যান্ডেলের
  Close EMA50-এর নিচে নয় (SELL উল্টো)।
- কনফার্মেশন (ট্রিগার ক্যান্ডেল): ট্রেন্ডের দিকে ক্লোজ, আর RSI ঘুরেছে (শেষ ক'টা ক্যান্ডেলে ডিপ লেভেল ছুঁয়ে আবার উঠছে)।
- SL: শেষ swing_lookback ক্যান্ডেলের সুইং প্রান্ত + বাফার ও sl_atr_multiplier × ATR-এর মধ্যে যেটা দূরে; খুব দূরে হলে বাদ।
- TP: ১:২, অথবা সামনে কোনো বিপরীত সুইং-লেভেল ৩R-এর মধ্যে না থাকলে ১:৩।
- স্কোর: প্রতিটি শর্তের ওজন যোগ; `scoring.required`-এর শর্ত বাধ্যতামূলক, তারপর স্কোর ≥ threshold।
শুধু ক্লোজড ক্যান্ডেল, শুধু অতীত। ভবিষ্যতের ডেটা কখনো ব্যবহার হয় না।
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .fetch import aggregate, drop_running
from .indicators import add_indicators

TF_MINUTES = {"H1": 60, "H4": 240, "D1": 1440}
NS_PER_MIN = 60_000_000_000
NS_PER_DAY = 86_400_000_000_000


def _swing_flags(high: np.ndarray, low: np.ndarray, k: int):
    """সুইং হাই/লো: দুই পাশের k ক্যান্ডেলের চেয়ে বেশি উঁচু/নিচু (ডানে ≥, বামে >)।"""
    hs, ls = pd.Series(high), pd.Series(low)
    left_hi, right_hi = hs.shift(1).rolling(k).max(), hs[::-1].shift(1).rolling(k).max()[::-1]
    left_lo, right_lo = ls.shift(1).rolling(k).min(), ls[::-1].shift(1).rolling(k).min()[::-1]
    return ((hs > left_hi) & (hs >= right_hi)).to_numpy(), ((ls < left_lo) & (ls <= right_lo)).to_numpy()


class Frame:
    """একটা টাইমফ্রেমের ক্লোজড ক্যান্ডেল ও ইন্ডিকেটর (দ্রুত numpy অ্যারে)।"""

    def __init__(self, df: pd.DataFrame, tf: str, cfg: dict):
        d = add_indicators(df, cfg)
        idx = pd.DatetimeIndex(d.index).as_unit("ns")
        self.tf, self.index = tf, idx
        self.start_ns = idx.asi8.copy()
        self.end_ns = self.start_ns + TF_MINUTES[tf] * NS_PER_MIN
        self.o, self.h, self.l, self.c = (d[x].to_numpy(float) for x in ("open", "high", "low", "close"))
        self.ef, self.em, self.es = (d[x].to_numpy(float) for x in ("ema_fast", "ema_mid", "ema_slow"))
        self.rsi, self.atr = d["rsi"].to_numpy(float), d["atr"].to_numpy(float)
        self.sh, self.sl_flag = _swing_flags(self.h, self.l, cfg["strategy"]["sr"]["swing_k"])

    def __len__(self) -> int:
        return len(self.start_ns)

    def last_closed(self, t_ns: int) -> int:
        """t সময়ে বা তার আগে শেষ হওয়া সর্বশেষ ক্যান্ডেলের সূচক (না থাকলে -১)।"""
        return int(np.searchsorted(self.end_ns, t_ns, side="right")) - 1


@dataclass(frozen=True)
class Signal:
    pair: str
    timeframe: str
    direction: str       # BUY / SELL
    time_ns: int         # সিগন্যালের সময় = ট্রিগার ক্যান্ডেলের শেষ (UTC ns)
    entry: float
    sl: float
    tp: float
    rr: int              # ২ বা ৩
    score: int
    risk: float          # |entry − sl|
    atr: float
    parts: dict = field(default_factory=dict)


def build_frames(h1: pd.DataFrame, cfg: dict, now) -> dict[str, Frame]:
    """পরিষ্কার ও ক্লোজড H1 থেকে H1/H4/D1 ফ্রেম (ব্যাকটেস্ট ও লাইভ একই)।"""
    d = cfg["data"]
    h4 = drop_running(aggregate(h1, 4, 0, 1), 240, now)
    d1 = drop_running(aggregate(h1, 24, d["d1_day_start_hour"], d["d1_min_h1_bars"]), 1440, now)
    return {"H1": Frame(h1, "H1", cfg), "H4": Frame(h4, "H4", cfg), "D1": Frame(d1, "D1", cfg)}


# ---------- শর্তগুলো ----------

def trend_direction(fr: Frame, i: int, cfg: dict):
    em, es, atr = fr.em[i], fr.es[i], fr.atr[i]
    if np.isnan(em) or np.isnan(es) or np.isnan(atr) or atr <= 0:
        return None
    gap, need = em - es, cfg["trend"]["ema_gap_min_atr"] * atr
    if gap >= need:
        return "BUY"
    if gap <= -need:
        return "SELL"
    return None


def pullback_ok(fr: Frame, si: int, direction: str, cfg: dict) -> bool:
    n = cfg["strategy"]["pullback_lookback"]
    lo = si - n + 1
    if lo < 0:
        return False
    s = slice(lo, si + 1)
    ef, em = fr.ef[s], fr.em[s]
    if np.isnan(ef).any() or np.isnan(em).any():
        return False
    brk = cfg["strategy"]["zone_break_close"]
    if direction == "BUY":
        return bool((fr.l[s] <= ef).any() and not (brk and (fr.c[s] < em).any()))
    return bool((fr.h[s] >= ef).any() and not (brk and (fr.c[s] > em).any()))


def confirmation_ok(fr: Frame, i: int, direction: str, cfg: dict) -> bool:
    st = cfg["strategy"]
    n = st["rsi_dip_lookback"]
    if i < n:
        return False
    window = fr.rsi[i - n + 1:i + 1]
    if np.isnan(window).any() or np.isnan(fr.rsi[i - 1]):
        return False
    if direction == "BUY":
        return bool(fr.c[i] > fr.o[i] and fr.rsi[i] > fr.rsi[i - 1] and window.min() <= st["rsi_dip_level"])
    return bool(fr.c[i] < fr.o[i] and fr.rsi[i] < fr.rsi[i - 1] and window.max() >= 100 - st["rsi_dip_level"])


def rsi_state_ok(rsi: float, direction: str, cfg: dict) -> bool:
    lo, hi = cfg["strategy"]["rsi_buy_range"]
    if np.isnan(rsi):
        return False
    return lo <= rsi <= hi if direction == "BUY" else 100 - hi <= rsi <= 100 - lo


def nearest_opposing_r(fr: Frame, si: int, direction: str, entry: float, risk: float, cfg: dict) -> float:
    """সামনের নিকটতম বিপরীত সুইং-লেভেল কত R দূরে (না থাকলে অসীম)। নিশ্চিত সুইং, অর্থাৎ j ≤ si − k।"""
    sr = cfg["strategy"]["sr"]
    k = sr["swing_k"]
    lo, hi = max(0, si - sr["lookback"]), si - k
    if hi < lo or risk <= 0:
        return float("inf")
    if direction == "BUY":
        lv = fr.h[lo:hi + 1][fr.sh[lo:hi + 1]]
        lv = lv[lv > entry]
        return float((lv.min() - entry) / risk) if len(lv) else float("inf")
    lv = fr.l[lo:hi + 1][fr.sl_flag[lo:hi + 1]]
    lv = lv[lv < entry]
    return float((entry - lv.max()) / risk) if len(lv) else float("inf")


def evaluate(pair: str, tf: str, frames: dict[str, Frame], i: int, cfg: dict):
    """ট্রিগার ফ্রেমের i-তম (ক্লোজড) ক্যান্ডেলে সিগন্যাল আছে কিনা। ফেরত Signal বা None।"""
    st = cfg["strategy"]
    m = st["tf_map"][tf]
    trig = frames[m["trigger"]]
    t = int(trig.end_ns[i])
    tr_fr, se_fr = frames[m["trend"]], frames[m["setup"]]
    ti, si = tr_fr.last_closed(t), se_fr.last_closed(t)
    if ti < 0 or si < 0:
        return None
    direction = trend_direction(tr_fr, ti, cfg)
    if direction is None:
        return None
    parts = {"clear_trend": True,
             "pullback_zone": pullback_ok(se_fr, si, direction, cfg),
             "confirmation": confirmation_ok(trig, i, direction, cfg)}
    if not all(parts.get(r, False) for r in cfg["scoring"]["required"]):
        return None
    atr = float(trig.atr[i])
    if np.isnan(atr) or atr <= 0:
        return None
    risk_cfg = cfg["risk"]
    lo = i - risk_cfg["swing_lookback"] + 1
    if lo < 0:
        return None
    entry = float(trig.c[i])
    buf = st["sl_buffer_atr"] * atr
    floor_dist = risk_cfg["sl_atr_multiplier"] * atr
    if direction == "BUY":
        dist = max(entry - float(trig.l[lo:i + 1].min()) + buf, floor_dist)
    else:
        dist = max(float(trig.h[lo:i + 1].max()) - entry + buf, floor_dist)
    if dist > st["max_sl_atr"] * atr:
        return None
    d_r = nearest_opposing_r(se_fr, si, direction, entry, dist, cfg)
    parts["no_opposing_level"] = d_r >= risk_cfg["rr_default"]
    parts["rsi_state"] = rsi_state_ok(float(trig.rsi[i]), direction, cfg)
    rr = risk_cfg["rr_clear_path"] if d_r >= risk_cfg["rr_clear_path"] else risk_cfg["rr_default"]
    score = sum(w for name, w in cfg["scoring"]["weights"].items() if parts.get(name))
    if score < cfg["scoring"]["threshold"]:
        return None
    sign = 1 if direction == "BUY" else -1
    return Signal(pair, tf, direction, t, entry, entry - sign * dist, entry + sign * rr * dist,
                  int(rr), int(score), float(dist), atr, parts)


# ---------- নিয়ম ----------

def friday_blocked(t_ns: int, cutoff: str = "18:00") -> bool:
    """শুক্রবার cutoff-এর পর (UTC) এবং শনিবার নতুন সিগন্যাল নয়।"""
    weekday = ((t_ns // NS_PER_DAY) + 3) % 7        # ১৯৭০-০১-০১ বৃহস্পতিবার (৩), সোমবার = ০
    minute = (t_ns % NS_PER_DAY) // NS_PER_MIN
    hh, mm = (int(x) for x in cutoff.split(":"))
    return bool((weekday == 4 and minute >= hh * 60 + mm) or weekday == 5)


def cooldown_ns(tf: str, cfg: dict) -> int:
    return cfg["rules"]["cooldown_candles"][tf] * TF_MINUTES[tf] * NS_PER_MIN
