"""বায়াস মডেল: উপাদান-স্কোর (−১..+১) → ওজনসহ যোগ → আপ/ডাউন/নিরপেক্ষ, দামের সীমা, লক্ষ্য-লেভেল, কারণ।

নিয়ম: শুধু ক্লোজড ডেটা (frames-এর শেষ ক্যান্ডেল ক্লোজড ধরা হয়)। সব সংখ্যা config.yaml-এ।
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import bn

H_UTC = timezone.utc


def clip(x: float, lim: float = 1.0) -> float:
    return float(max(-lim, min(lim, x)))


def _ok(*xs) -> bool:
    return all(x is not None and not (isinstance(x, float) and math.isnan(x)) for x in xs)


# ---------- উপাদান ----------

def chart_components(frames: dict, cfg: dict) -> dict:
    """চার্ট থেকে {key: {score, value}}। ডেটা না থাকলে ওই key বাদ।"""
    sc, out = cfg["bias"]["scales"], {}

    def last(tf):
        fr = frames.get(tf)
        return (fr, len(fr) - 1) if fr is not None and len(fr) else (None, -1)

    fr, i = last("D1")
    if fr is not None:
        a, em, es, r = fr.atr[i], fr.em[i], fr.es[i], fr.rsi[i]
        if _ok(a, em, es) and a > 0:
            out["trend_d1"] = {"score": clip((em - es) / a / sc["trend_gap_atr"]), "value": (em - es) / a}
        if _ok(r):
            out["rsi_d1"] = {"score": clip((r - 50) / sc["rsi_span"]), "value": float(r)}
    for tf, key in (("H4", "trend_h4"), ("H1", "trend_h1")):
        fr, i = last(tf)
        if fr is not None:
            a, ef, em = fr.atr[i], fr.ef[i], fr.em[i]
            if _ok(a, ef, em) and a > 0:
                out[key] = {"score": clip((ef - em) / a / sc["trend_fast_atr"]), "value": (ef - em) / a}
    fr, i = last("H1")
    if fr is not None:
        a, ef, r, c = fr.atr[i], fr.ef[i], fr.rsi[i], fr.c[i]
        if _ok(a, ef, c) and a > 0:
            out["ema_pos_h1"] = {"score": clip((c - ef) / a / sc["ema_pos_atr"]), "value": (c - ef) / a}
        if _ok(r):
            out["rsi_h1"] = {"score": clip((r - 50) / sc["rsi_span"]), "value": float(r)}
    return out


def _fresh(s: pd.Series, now: datetime, max_days: int) -> bool:
    return len(s) > 0 and (pd.Timestamp(now).tz_localize(None) - s.index[-1]).days <= max_days


def fund_components(fund: dict, pair: str, cfg: dict, now: datetime, notes: list[str]) -> dict:
    """ফান্ডামেন্টাল উপাদান। পুরনো (stale) বা অনুপস্থিত হলে বাদ ও নোট।"""
    b = cfg["bias"]
    sc, mx = b["scales"], b["sources"]["max_stale_days"]
    n = int(sc["change_obs"])
    out = {}
    usd_sign = b["usd_sign"].get(pair, 0)

    def series(name):
        s = fund.get(name)
        if s is None:
            return None
        lim = mx.get(name, mx["fred"])
        if not _fresh(s, now, lim) or len(s) <= n:
            age = (pd.Timestamp(now).tz_localize(None) - s.index[-1]).days if len(s) else None
            notes.append(f"{name}: ডেটা পুরনো বা অপর্যাপ্ত, বাদ (সর্বশেষ মান {age} দিন আগের, সীমা {lim} দিন, সংখ্যা {len(s)})" if age is not None
                         else f"{name}: ডেটা খালি, বাদ")
            return None
        return s

    s = series("usd")
    if s is not None and usd_sign:
        pct = (s.iloc[-1] / s.iloc[-1 - n] - 1) * 100
        out["usd"] = {"score": clip(usd_sign * pct / sc["usd_pct"]), "value": float(pct)}
    if pair in b["full_fundamentals"]:
        s = series("real_yield")
        if s is not None:
            bp = (s.iloc[-1] - s.iloc[-1 - n]) * 100
            out["real_yield"] = {"score": clip(-bp / sc["real_yield_bp"]), "value": float(bp)}
        s = series("vix")
        if s is not None:
            d = s.iloc[-1] - s.iloc[-1 - n]
            out["vix"] = {"score": clip(d / sc["vix_pts"]), "value": float(d)}
        cot = fund.get("cot")
        if cot and len(cot) >= 5:
            age = (pd.Timestamp(now).tz_localize(None) - cot[0]["date"]).days
            if age <= mx["cot"] and cot[0]["oi"] > 0:
                d = (cot[0]["net"] - cot[4]["net"]) / cot[0]["oi"]
                out["cot"] = {"score": clip(d / sc["cot_oi_frac"]), "value": float(d)}
            else:
                notes.append("COT: ডেটা পুরনো, বাদ")
    return out


# ---------- স্কোর ও লেবেল ----------

def combine(weights: dict, comps: dict) -> tuple[float, float, list[str]]:
    """(স্কোর, পাওয়া-ওজনের ভাগ, ব্যবহৃত key)। অনুপস্থিত উপাদানের ওজন বাকিদের মধ্যে পুনর্বণ্টন।"""
    total = sum(weights.values())
    used = [k for k in weights if k in comps]
    got = sum(weights[k] for k in used)
    if not used or got <= 0:
        return 0.0, 0.0, []
    score = sum(weights[k] * comps[k]["score"] for k in used) / got
    return float(score), float(got / total), used


def label(score: float, share: float, cfg: dict) -> tuple[str, str]:
    """(বায়াস, আস্থা)। ডেটা-ভাগ কম বা স্কোর ব্যান্ডের নিচে হলে নিরপেক্ষ।"""
    b = cfg["bias"]
    lo, mid, hi = b["confidence_bands"]
    if share < b["min_weight_share"] or abs(score) < b["neutral_band"]:
        return "NEUTRAL", "NONE"
    a = abs(score)
    conf = "HIGH" if a >= hi else ("MEDIUM" if a >= mid else "LOW")
    return ("UP" if score > 0 else "DOWN"), conf


# ---------- দামের সীমা ও লেভেল ----------

def nearest_levels(fr, price: float, k: int, lookback: int) -> tuple[float | None, float | None]:
    """নিশ্চিত সুইং থেকে নিকটতম ওপরের রেজিস্ট্যান্স ও নিচের সাপোর্ট (j ≤ শেষ − k, ভবিষ্যৎ দেখে না)।"""
    n = len(fr)
    hi_i = n - 1 - k
    lo_i = max(0, n - 1 - lookback)
    if hi_i < lo_i:
        return None, None
    seg = slice(lo_i, hi_i + 1)
    hs = fr.h[seg][fr.sh[seg]]
    ls = fr.l[seg][fr.sl_flag[seg]]
    hs, ls = hs[hs > price], ls[ls < price]
    return (float(hs.min()) if len(hs) else None), (float(ls.max()) if len(ls) else None)


def price_range(frames: dict, hcfg: dict, cfg: dict, price: float, dp: int) -> dict:
    fr = frames[hcfg["range_frame"]]
    a = float(fr.atr[-1])
    if not _ok(a) or a <= 0:
        return {}
    r = cfg["bias"]["range_k"] * a * hcfg["range_mult"]
    k = cfg["strategy"]["sr"]["swing_k"]
    up, dn = nearest_levels(frames[hcfg["level_frame"]], price, k, cfg["bias"]["level_lookback"])
    return {"R": r, "lo": round(price - r, dp), "hi": round(price + r, dp),
            "up_level": None if up is None else round(up, dp), "down_level": None if dn is None else round(dn, dp)}


def target(bias: str, price: float, rng: dict, cfg: dict, dp: int) -> float | None:
    if bias == "NEUTRAL" or not rng:
        return None
    reach = cfg["bias"]["level_reach"] * rng["R"]
    if bias == "UP":
        lv = rng["up_level"]
        return round(lv if lv is not None and lv - price <= reach else rng["hi"], dp)
    lv = rng["down_level"]
    return round(lv if lv is not None and price - lv <= reach else rng["lo"], dp)


# ---------- এক সময়সীমার পূর্ণ ফলাফল ----------

def build_horizon(pair: str, hkey: str, comps: dict, frames: dict, cfg: dict, now: datetime,
                  events: list[dict], mode: str) -> dict:
    b = cfg["bias"]
    h = b["horizons"][hkey]
    price = float(frames["H1"].c[-1])
    dp = cfg["pairs"][pair]["decimals"]
    score, share, used = combine(b["weights"][hkey], comps)
    bias, conf = label(score, share, cfg)
    rng = price_range(frames, h, cfg, price, dp)
    tgt = target(bias, price, rng, cfg, dp)
    usd_sign = b["usd_sign"].get(pair, 0)

    rows = []
    for k in used:
        w = b["weights"][hkey][k]
        c = comps[k]
        rows.append({"key": k, "score": round(c["score"], 3), "weight": w,
                     "text": bn.reason(k, c["score"], c["value"], pair, usd_sign) if abs(c["score"]) >= b["min_reason_score"]
                     else None, "_v": c["value"]})
    reasons = [r["text"] for r in sorted(rows, key=lambda r: -abs(r["score"]) * r["weight"]) if r["text"]]
    warnings = [w for r in rows for w in [bn.warning_rsi(r["key"], r["_v"])] if w]
    if share < b["min_weight_share"]:
        warnings.append("ডেটা অপর্যাপ্ত, তাই নিরপেক্ষ দেখানো হচ্ছে")
    end = now + timedelta(hours=h["hours"])
    for e in events:
        if now <= e["utc"] <= end and (e.get("level") == "high" or (e.get("level") is None and e.get("impact") == "High")) and e["currency"] in ("USD",) + tuple(_pair_ccy(pair)):
            local = (e["utc"] + timedelta(hours=cfg["notify"]["display_utc_offset_hours"])).strftime("%d-%m %H:%M")
            warnings.append(bn.event_warning(e["title"], local, e.get("approx", False)))
    for r in rows:
        r.pop("_v")
    return {"bias": bias, "bias_bn": bn.BIAS_BN[bias], "confidence": conf, "confidence_bn": bn.CONF_BN[conf],
            "score": round(score, 3), "data_share": round(share, 2), "ref_price": round(price, dp),
            "range": ({"lo": rng["lo"], "hi": rng["hi"], "R": round(rng["R"], dp + 1)} if rng else None),
            "levels": ({"up": rng["up_level"], "down": rng["down_level"]} if rng else None),
            "target": tgt, "summary_bn": bn.summary(hkey, bias, conf), "reasons_bn": reasons,
            "warnings_bn": warnings, "components": rows, "valid_until": end.strftime("%Y-%m-%dT%H:%M:%SZ")}


def _pair_ccy(pair: str) -> list[str]:
    return [pair[:3], pair[3:]] if len(pair) == 6 else ["XAU", "USD"]
