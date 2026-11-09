"""বায়াস লগ, মেয়াদ-শেষে যাচাই ও সঠিকতার পরিসংখ্যান।

- প্রতিটি সময়সীমা নিজের পর্বে (h4: ৪ ঘণ্টা, d1: দিন, w1: সপ্তাহ) একবারই লগ হয়, তাই নমুনা স্বাধীন।
- বাজার বন্ধের সময় মেয়াদের অর্ধেক বা বেশি পড়লে লগ হয় না।
- যাচাই: মেয়াদ শেষের শেষ H1 ক্লোজ বনাম লগের দাম। নিরপেক্ষ বায়াস দিকে গণনা হয় না, শুধু আলাদা দেখানো হয়।
- বেসলাইন: ওই গ্রুপে "সবসময় বেশি-ঘটা দিক" ধরলে যত শতাংশ মিলত। বায়াস এর চেয়ে ভালো না হলে কোনো মূল্য নেই।
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import numpy as np

H = 3_600_000_000_000


def iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_ns(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() * 1e9)


def market_closed_hour(t: datetime) -> bool:
    """সপ্তাহান্ত: শুক্র ২২:০০ থেকে রবি ২২:০০ UTC পর্যন্ত বন্ধ।"""
    wd, hr = t.weekday(), t.hour
    return (wd == 4 and hr >= 22) or wd == 5 or (wd == 6 and hr < 22)


def closed_fraction(start: datetime, hours: int) -> float:
    n = sum(market_closed_hour(start + timedelta(hours=k)) for k in range(hours))
    return n / hours


def bucket_info(hkey: str, made: datetime, cfg: dict) -> tuple[str, datetime]:
    """(পর্বের আইডি, পর্বের শুরু)। d1/w1 নিজের anchor ঘণ্টায় শুরু হয় (w1: রবিবার)।"""
    a = cfg["bias"]["log"]["anchor_utc_hour"][hkey]
    hours = cfg["bias"]["horizons"][hkey]["hours"]
    base = datetime(2026, 1, 4, a, tzinfo=timezone.utc)          # ২০২৬-০১-০৪ রবিবার
    k = int((made - base).total_seconds() // (hours * 3600))
    return f"{hkey}:{k}", base + timedelta(hours=k * hours)


def new_entries(result: dict, log: list[dict], now: datetime, cfg: dict) -> list[dict]:
    """result = {pair: {"horizons": {h: {...}}}}। এই পর্বে লগ না থাকলে এবং বাজার চালু থাকলে নতুন এন্ট্রি।"""
    have = {(e["pair"], e["bucket"]) for e in log}
    out = []
    for pair, p in result.items():
        for hkey, h in p["horizons"].items():
            if h.get("ref_price") is None or h.get("range") is None:
                continue
            bid, start = bucket_info(hkey, now, cfg)
            if (pair, bid) in have:
                continue
            if (now - start).total_seconds() / 3600 > cfg["bias"]["log"]["max_lag_hours"][hkey]:
                continue                                         # পর্বের শুরুর রান মিস হয়েছে; দেরিতে লগ করলে নমুনা বিকৃত হয়
            hours = cfg["bias"]["horizons"][hkey]["hours"]
            ref = now.replace(minute=0, second=0, microsecond=0)  # সর্বশেষ ক্লোজড H1 ক্যান্ডেলের শেষ = ref দামের সময়
            if closed_fraction(ref, hours) >= cfg["bias"]["log"]["closed_overlap_skip"]:
                continue
            made, ref_ns = int(now.timestamp() * 1e9), int(ref.timestamp() * 1e9)
            out.append({"id": f"{pair}|{hkey}|{made // 1_000_000_000}", "pair": pair, "horizon": hkey, "bucket": bid,
                        "made_at": iso(made), "ref_at": iso(ref_ns), "expire_at": iso(ref_ns + hours * H),
                        "bias": h["bias"], "score": h["score"],
                        "confidence": h["confidence"], "ref_price": h["ref_price"], "lo": h["range"]["lo"],
                        "hi": h["range"]["hi"], "R": h["range"]["R"], "status": "Pending"})
    return out


def resolve(log: list[dict], h1_by_pair: dict, now_ns: int, cfg: dict) -> int:
    """মেয়াদ শেষ হওয়া Pending এন্ট্রি যাচাই। H1 ক্যান্ডেল মেয়াদ পর্যন্ত না থাকলে অপেক্ষায় থাকে।"""
    n = 0
    for e in log:
        if e["status"] != "Pending" or to_ns(e["expire_at"]) > now_ns:
            continue
        fr = h1_by_pair.get(e["pair"])
        if fr is None:
            continue
        t0, t1 = to_ns(e["ref_at"]), to_ns(e["expire_at"])
        j = int(np.searchsorted(fr.end_ns, t1, side="right")) - 1       # মেয়াদের আগে শেষ-হওয়া শেষ ক্যান্ডেল
        closed_at_expiry = market_closed_hour(datetime.fromtimestamp(t1 / 1e9, tz=timezone.utc))
        reached = j >= 0 and (int(fr.end_ns[j]) >= t1 or (closed_at_expiry and now_ns >= t1 + 3 * H))
        if not reached or int(fr.end_ns[-1]) < int(fr.end_ns[j]):
            continue                                                    # ডেটা এখনো মেয়াদ পর্যন্ত পৌঁছায়নি
        k0 = int(np.searchsorted(fr.start_ns, t0, side="left"))         # ref ক্যান্ডেলের পরের ক্যান্ডেল থেকে পথ
        seg_h, seg_l = fr.h[k0:j + 1], fr.l[k0:j + 1]
        end = float(fr.c[j])
        move = end - e["ref_price"]
        e.update({"status": "Resolved", "end_price": end, "move_r": round(move / e["R"], 3) if e["R"] else None,
                  "in_range": bool(e["lo"] <= end <= e["hi"]),
                  "breach_up": bool(len(seg_h) and seg_h.max() > e["hi"]), "breach_down": bool(len(seg_l) and seg_l.min() < e["lo"]),
                  "up": bool(move > 0)})
        e["correct"] = None if e["bias"] == "NEUTRAL" or move == 0 else (e["bias"] == "UP") == (move > 0)
        n += 1
    return n


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - m, c + m


def _group(rows: list[dict], min_n: int) -> dict:
    res = [r for r in rows if r["status"] == "Resolved"]
    dirn = [r for r in res if r["correct"] is not None]
    hits = sum(1 for r in dirn if r["correct"])
    up_rate = (sum(1 for r in res if r["up"]) / len(res)) if res else float("nan")
    lo, hi = wilson(hits, len(dirn))
    base = max(up_rate, 1 - up_rate) if res else float("nan")
    cov = [r["in_range"] for r in res]
    return {"n_logged": len(rows), "n_resolved": len(res), "n_directional": len(dirn), "n_neutral": len(res) - len(dirn),
            "hit_rate": (hits / len(dirn)) if dirn else None, "hit_ci95": [None if math.isnan(lo) else round(lo, 3),
                                                                           None if math.isnan(hi) else round(hi, 3)],
            "base_rate_always_one_side": None if math.isnan(base) else round(base, 3),
            "up_rate": None if math.isnan(up_rate) else round(up_rate, 3),
            "range_coverage": (sum(cov) / len(cov)) if cov else None,
            "enough_samples": len(dirn) >= min_n}


def build_stats(log: list[dict], cfg: dict, now_iso: str) -> dict:
    min_n = cfg["bias"]["log"]["min_samples_for_judgement"]
    out = {"version": 1, "generated_at": now_iso, "min_samples": min_n, "groups": {}}
    keys = sorted({(e["pair"], e["horizon"]) for e in log})
    for pair, h in keys:
        out["groups"][f"{pair}|{h}"] = _group([e for e in log if e["pair"] == pair and e["horizon"] == h], min_n)
    for h in cfg["bias"]["horizons"]:
        rows = [e for e in log if e["horizon"] == h]
        if rows:
            out["groups"][f"ALL|{h}"] = _group(rows, min_n)
    for g in out["groups"].values():
        for k in ("hit_rate", "range_coverage"):
            if g[k] is not None:
                g[k] = round(g[k], 3)
    return out


def trim(log: list[dict], max_entries: int) -> list[dict]:
    return log[-max_entries:] if len(log) > max_entries else log
