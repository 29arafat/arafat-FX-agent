"""সিগন্যাল ইঞ্জিন: নতুন সিগন্যাল তৈরি, চলমান সিগন্যালের TP/SL মেলানো, Telegram পাঠানো।

ব্যাকটেস্টের একই কোড ব্যবহার হয় (strategy.evaluate, tracker.find_exit/result_r)।

নিয়ম:
- শুধু সাম্প্রতিক ক্লোজড ট্রিগার ক্যান্ডেল (rules.max_signal_age_minutes), পুরনো ক্যান্ডেলে সিগন্যাল পাঠানো হয় না।
- একই (পেয়ার, টাইমফ্রেম, ক্যান্ডেল) দুবার নয় (key দিয়ে), রান পুনরায় চললেও ডুপ্লিকেট নেই।
- পেয়ার+টাইমফ্রেমে একসাথে একটাই Running; TP/SL-এর পর কুলডাউন; শুক্রবার cutoff।
- সিগন্যাল তৈরি হওয়ার আগেই TP/SL ছুঁয়ে গেলে বা দাম এন্ট্রি-বৈধতার বাইরে চলে গেলে পাঠানো হয় না।
- Telegram ব্যর্থ হলে সিগন্যাল state-এ থাকে (notified_open=false), পরের রানে আবার চেষ্টা, কিন্তু বয়স সীমার পরে নয়।
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from .notify import error_key, format_close, format_error, format_open
from .strategy import NS_PER_MIN, build_frames, cooldown_ns, evaluate, friday_blocked
from .tracker import find_exit, result_r


def iso(ns: int) -> str:
    return pd.Timestamp(ns, tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def to_ns(iso_s: str) -> int:
    return int(pd.Timestamp(iso_s).tz_convert("UTC").value)


def _next_id(state: dict, opened_ns: int) -> str:
    day = pd.Timestamp(opened_ns, tz="UTC").strftime("%Y%m%d")
    n = sum(1 for s in state["signals"] if s["id"].startswith(f"SIG-{day}-")) + 1
    return f"SIG-{day}-{n:02d}"


def _round(x: float, dp: int) -> float:
    return round(float(x), dp)


def track_running(state: dict, h1, cfg: dict, now_ns: int) -> list[dict]:
    """চলমান সিগন্যালগুলো খোলার পর থেকে সব ক্লোজড H1 ক্যান্ডেলে মেলায়। ফেরত: এইমাত্র বন্ধ হওয়া সিগন্যাল।"""
    closed = []
    for s in state["signals"]:
        if s["status"] != "Running":
            continue
        sp = cfg["backtest"]["spread"][s["pair"]]
        k0 = int(np.searchsorted(h1.start_ns, to_ns(s["opened_at"]), side="left"))
        ex = find_exit(s["direction"], s["sl"], s["tp"], h1.h[k0:], h1.l[k0:], sp)
        if ex is None:
            continue
        k, status, amb = ex
        s["status"], s["ambiguous"] = status, bool(amb)
        s["closed_at"] = iso(int(h1.end_ns[k0 + k]))
        s["result_r"] = round(result_r(s["direction"], s["entry"], s["sl"], s["tp"], status, sp), 2)
        s["notified_close"] = False
        closed.append(s)
    return closed


def _blocked(state: dict, pair: str, tf: str, t_ns: int, cfg: dict) -> bool:
    cool = cooldown_ns(tf, cfg)
    for s in state["signals"]:
        if s["pair"] != pair or s["timeframe"] != tf:
            continue
        if s["status"] == "Running":
            return True
        if s["closed_at"] and t_ns < to_ns(s["closed_at"]) + cool:
            return True
    return False


def new_signals(state: dict, pair: str, frames: dict, cfg: dict, now_ns: int) -> list[dict]:
    created = []
    h1 = frames["H1"]
    last_close = float(h1.c[-1])
    keys = {s["key"] for s in state["signals"]}
    dp = cfg["pairs"][pair]["decimals"]
    for tf in cfg["timeframes"]:
        trig = frames[cfg["strategy"]["tf_map"][tf]["trigger"]]
        max_age = cfg["rules"]["max_signal_age_minutes"][tf] * NS_PER_MIN
        idx = [i for i in range(max(0, len(trig) - 6), len(trig))
               if trig.end_ns[i] <= now_ns and now_ns - trig.end_ns[i] <= max_age]
        for i in idx:
            t = int(trig.end_ns[i])
            key = f"{pair}|{tf}|{t}"
            if key in keys or friday_blocked(t, cfg["rules"]["friday_cutoff_utc"]) or _blocked(state, pair, tf, t, cfg):
                continue
            sig = evaluate(pair, tf, frames, i, cfg)
            if sig is None:
                continue
            entry, sl, tp = (_round(x, dp) for x in (sig.entry, sig.sl, sig.tp))
            frac = cfg["risk"]["entry_valid_atr_fraction"]
            valid = _round(entry + frac * sig.atr, dp) if sig.direction == "BUY" else _round(entry - frac * sig.atr, dp)
            # দেরিতে এলে: দাম বৈধতার বাইরে, বা ইতিমধ্যে TP/SL ছুঁয়েছে
            if (sig.direction == "BUY" and last_close > valid) or (sig.direction == "SELL" and last_close < valid):
                continue
            k0 = int(np.searchsorted(h1.start_ns, t, side="left"))
            if find_exit(sig.direction, sl, tp, h1.h[k0:], h1.l[k0:], cfg["backtest"]["spread"][pair]) is not None:
                continue
            rec = {
                "id": _next_id(state, t), "key": key, "pair": pair, "direction": sig.direction,
                "entry": entry, "tp": tp, "sl": sl, "rr": f"1:{sig.rr}", "timeframe": tf, "score": sig.score,
                "confirmed": bool(sig.parts.get("confirmation")) and sig.score >= cfg["strategy"]["confirmed_min_score"],
                "opened_at": iso(t), "status": "Running", "closed_at": None, "result_r": 0,
                "ambiguous": False, "entry_valid_until": valid,
                "strategy_version": cfg["strategy"]["version"],
                "notified_open": False, "notified_close": None,
            }
            state["signals"].append(rec)
            keys.add(key)
            created.append(rec)
    return created


def send_pending(state: dict, notifier, cfg: dict, now_ns: int, save=None) -> int:
    """না-পাঠানো বার্তা পাঠায়। সফল হলে ফ্ল্যাগ true। ফেরত: পাঠানো সংখ্যা।"""
    sent = 0
    for s in state["signals"]:
        if not s["notified_open"]:
            age = now_ns - to_ns(s["opened_at"])
            if s["status"] != "Running" or age > cfg["rules"]["max_signal_age_minutes"][s["timeframe"]] * NS_PER_MIN:
                s["notified_open"] = "expired"          # দেরি হয়ে গেছে, আর পাঠানো অর্থহীন
            elif notifier.send(format_open(s, cfg)):
                s["notified_open"] = True
                sent += 1
                if save:
                    save()
        if s["status"] != "Running" and s.get("notified_close") is False:
            if s["notified_open"] is not True:
                s["notified_close"] = "skipped"          # শুরুর বার্তাই যায়নি, ফলাফলের বার্তা অর্থহীন
            elif notifier.send(format_close(s, cfg)):
                s["notified_close"] = True
                sent += 1
                if save:
                    save()
    return sent


def alert_error(state: dict, notifier, cfg: dict, msg: str, now_ns: int) -> bool:
    """একই এরর cooldown-এর মধ্যে আবার পাঠায় না।"""
    k = error_key(msg)
    last = state["errors"].get(k)
    gap = cfg["notify"]["same_error_cooldown_hours"] * 3600 * 1_000_000_000
    if last is not None and now_ns - last < gap:
        return False
    if notifier.send(format_error(msg)):
        state["errors"][k] = now_ns
        return True
    return False


def run_once(cfg: dict, h1_by_pair: dict, errors: dict, state: dict, notifier, now: datetime | None = None, save=None) -> dict:
    """এক রান। h1_by_pair: {পেয়ার: পরিষ্কার ক্লোজড H1}, errors: {পেয়ার: বার্তা}। state জায়গায় বদলায়।"""
    now = now or datetime.now(timezone.utc)
    now_ns = int(pd.Timestamp(now).value)
    report = {"new": 0, "closed": 0, "sent": 0, "errors": len(errors)}
    for pair, msg in errors.items():
        alert_error(state, notifier, cfg, f"{pair}: ডেটা আনা যায়নি, এই রানে সিগন্যাল তৈরি হয়নি ({msg})", now_ns)
    for pair, h1 in h1_by_pair.items():
        try:
            frames = build_frames(h1, cfg, now)
        except Exception as e:                           # ডেটা অদ্ভুত হলে রান বাদ, সতর্কতা
            alert_error(state, notifier, cfg, f"{pair}: ফ্রেম বানানো যায়নি ({type(e).__name__})", now_ns)
            report["errors"] += 1
            continue
        report["closed"] += len(track_running(state, frames["H1"], cfg, now_ns))   # আগে ট্র্যাকিং, তারপর নতুন
        report["new"] += len(new_signals(state, pair, frames, cfg, now_ns))
    state["last_check"] = iso(now_ns)
    if save:
        save()                       # আগে সিগন্যাল সংরক্ষণ, তারপর Telegram: ক্র্যাশ হলেও সিগন্যাল হারায় না, ডুপ্লিকেটও হয় না
    report["sent"] = send_pending(state, notifier, cfg, now_ns, save)
    return report
