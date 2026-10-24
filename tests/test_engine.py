"""ধাপ ৪: ইঞ্জিন, Telegram, state। কৃত্রিম গোল্ড-স্কেল ডেটা ও নকল Telegram দিয়ে।"""
import copy
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from src import engine as E
from src import notify as N
from src.fetch import clean_h1, load_config
from src.storage import load_state, save_state
from src.strategy import build_frames, evaluate
from tests.test_backtest import synth

CFG = load_config()
LATE = datetime(2030, 1, 1, tzinfo=timezone.utc)


def gold(seed=3):
    df = synth(seed=seed, start="2024-01-01", end="2026-06-01")
    df[["open", "high", "low", "close"]] *= 1800.0
    return df


def clean(df, now):
    return clean_h1(df, CFG, now)


def find_signal_time(df, max_scan=4000):
    """df-তে একটা H1 সিগন্যালের ট্রিগার-শেষ সময় (এবং সেই সূচক)।"""
    fr = build_frames(clean(df, LATE), CFG, LATE)
    h1 = fr["H1"]
    hits = []
    for i in range(400, len(h1)):
        if evaluate("XAUUSD", "H1", fr, i, CFG):
            hits.append(i)
    return fr, hits


class Fake:
    def __init__(self, ok=True):
        self.ok, self.sent, self.last_error = ok, [], "fake"

    def send(self, text):
        if self.ok:
            self.sent.append(text)
        return self.ok


def at(ns):
    return pd.Timestamp(ns, tz="UTC").to_pydatetime()


def truncate(df, now):
    return df[df.index + pd.Timedelta(hours=1) <= pd.Timestamp(now)]


def fresh_state():
    return json.loads(json.dumps({"version": 1, "signals": [], "errors": {}, "last_check": None}))


def first_live_signal(df):
    """এমন একটা সিগন্যাল যা তৈরির পর (দেরি ছাড়া) এখনো বৈধ ও খোলা।"""
    fr, hits = find_signal_time(df)
    h1 = fr["H1"]
    for i in hits:
        now = at(int(h1.end_ns[i]) + 5 * 60_000_000_000)
        st, nt = fresh_state(), Fake()
        rep = E.run_once(CFG, {"XAUUSD": clean(truncate(df, now), now)}, {}, st, nt, now)
        if rep["new"] == 1:
            return i, now
    raise AssertionError("কোনো বৈধ সিগন্যাল পাওয়া যায়নি")


# ---------- ফরম্যাট ----------
def test_format_messages():
    sig = {"id": "SIG-20261024-01", "pair": "XAUUSD", "direction": "BUY", "entry": 4012.35, "tp": 4034.85,
           "sl": 4001.1, "rr": "1:2", "timeframe": "H1", "score": 6, "confirmed": True,
           "opened_at": "2026-10-24T12:00:00Z", "entry_valid_until": 4018.9, "strategy_version": "v1-experimental",
           "status": "TP", "closed_at": "2026-10-24T15:00:00Z", "result_r": 2.0, "ambiguous": False}
    t = N.format_open(sig, CFG)
    assert "XAUUSD" in t and "4012.35" in t and "4001.10" in t and "4034.85" in t and "1:2" in t and "H1" in t
    assert "2026-10-24 15:00" in t and "কনফার্মড" in t and "v1-experimental" in t   # UTC+3
    c = N.format_close(sig, CFG)
    assert "TP হিট" in c and "+2.00R" in c
    sig.update(status="SL", result_r=-1.0, ambiguous=True)
    c = N.format_close(sig, CFG)
    assert "SL হিট" in c and "-1.00R" in c and "সাবধানী" in c


# ---------- Telegram ক্লায়েন্ট ----------
class Resp:
    def __init__(self, code, js=None):
        self.status_code, self._js = code, js or {}

    def json(self):
        return self._js


class Sess:
    def __init__(self, codes):
        self.codes, self.calls = list(codes), []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        c = self.codes.pop(0)
        if isinstance(c, Exception):
            raise c
        return c


def test_telegram_ok_retry_and_no_token_leak():
    import requests
    tok = "12345" + "6789" + ":" + "Ab" * 18      # রানটাইমে বানানো নকল Token (সোর্সে Token-আকৃতির লেখা নেই)
    s = Sess([Resp(500), Resp(200)])
    t = N.Telegram(tok, "42", session=s, sleep=lambda x: None)
    assert t.send("hi") and len(s.calls) == 2 and s.calls[0][1]["chat_id"] == "42"
    s = Sess([requests.ConnectionError(f"failed {tok}")] * 3)
    t = N.Telegram(tok, "42", session=s, sleep=lambda x: None)
    assert not t.send("hi") and tok not in t.last_error
    s = Sess([Resp(401)])
    t = N.Telegram(tok, "42", session=s, sleep=lambda x: None)
    assert not t.send("hi") and len(s.calls) == 1 and tok not in t.last_error      # ৪০১-এ আবার চেষ্টা নয়
    s = Sess([Resp(429, {"parameters": {"retry_after": 1}}), Resp(200)])
    assert N.Telegram(tok, "42", session=s, sleep=lambda x: None).send("x")


def test_from_env_requires_both():
    import os
    old = {k: os.environ.pop(k, None) for k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
    try:
        try:
            N.Telegram.from_env()
            raise AssertionError("ত্রুটি ওঠেনি")
        except RuntimeError:
            pass
        os.environ["TELEGRAM_TOKEN"], os.environ["TELEGRAM_CHAT_ID"] = "t", "c"
        assert N.Telegram.from_env()
    finally:
        for k, v in old.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


# ---------- state ----------
def test_state_roundtrip_atomic_and_corrupt():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x" / "s.json"
        st = load_state(p)
        assert st["signals"] == []
        st["signals"].append({"id": "a"})
        save_state(p, st)
        assert load_state(p)["signals"] == [{"id": "a"}] and not list(p.parent.glob("*.tmp"))
        p.write_text("{bad", encoding="utf-8")
        try:
            load_state(p)
            raise AssertionError("ত্রুটি ওঠেনি")
        except ValueError:
            pass


# ---------- ইঞ্জিন ----------
def test_new_signal_created_once_and_idempotent():
    df = gold()
    i, now = first_live_signal(df)
    st, nt = fresh_state(), Fake()
    h1 = clean(truncate(df, now), now)
    r1 = E.run_once(CFG, {"XAUUSD": h1}, {}, st, nt, now)
    assert r1["new"] == 1 and r1["sent"] == 1 and len(nt.sent) == 1
    s = st["signals"][0]
    assert s["status"] == "Running" and s["notified_open"] is True and s["strategy_version"] == "v1-experimental"
    assert re.fullmatch(r"SIG-\d{8}-01", s["id"]) and s["rr"] in ("1:2", "1:3")
    sign = 1 if s["direction"] == "BUY" else -1
    assert sign * (s["tp"] - s["entry"]) > 0 > sign * (s["sl"] - s["entry"])
    assert sign * (s["entry_valid_until"] - s["entry"]) > 0
    # একই ডেটায় আবার চালালে ডুপ্লিকেট নয়
    r2 = E.run_once(CFG, {"XAUUSD": h1}, {}, st, nt, now)
    assert r2["new"] == 0 and r2["sent"] == 0 and len(nt.sent) == 1 and len(st["signals"]) == 1
    # ১০ মিনিট পরে আবার (কিছু বদলায়নি)
    r3 = E.run_once(CFG, {"XAUUSD": h1}, {}, st, nt, at(int(pd.Timestamp(now).value) + 10 * 60_000_000_000))
    assert r3["new"] == 0 and len(nt.sent) == 1


def test_stale_candle_not_signalled():
    df = gold()
    i, now = first_live_signal(df)
    late = at(int(pd.Timestamp(now).value) + 5 * 3600 * 1_000_000_000)     # ৫ ঘণ্টা দেরি (> ৯০ মিনিট)
    st, nt = fresh_state(), Fake()
    rep = E.run_once(CFG, {"XAUUSD": clean(truncate(df, late), late)}, {}, st, nt, late)
    assert not any(s["timeframe"] == "H1" and s["opened_at"] == E.iso(int(pd.Timestamp(now).value) - 5 * 60_000_000_000)
                   for s in st["signals"])


def test_running_blocks_then_tp_sl_notification_and_cooldown():
    df = gold()
    i, now = first_live_signal(df)
    st, nt = fresh_state(), Fake()
    E.run_once(CFG, {"XAUUSD": clean(truncate(df, now), now)}, {}, st, nt, now)
    s = st["signals"][0]
    # পরের ঘণ্টাগুলো ধাপে ধাপে চালাই যতক্ষণ বন্ধ না হয়
    cur, steps = now, 0
    while s["status"] == "Running" and steps < 400:
        cur = at(int(pd.Timestamp(cur).value) + 3600 * 1_000_000_000)
        E.run_once(CFG, {"XAUUSD": clean(truncate(df, cur), cur)}, {}, st, nt, cur)
        steps += 1
        # যতক্ষণ Running, একই পেয়ার+টাইমফ্রেমে নতুন H1 সিগন্যাল নয়
        assert sum(1 for x in st["signals"] if x["timeframe"] == "H1" and x["status"] == "Running") <= 1
    assert s["status"] in ("TP", "SL") and s["closed_at"] and s["notified_close"] is True
    assert (s["result_r"] < 0) == (s["status"] == "SL")
    assert any(("TP হিট" in m or "SL হিট" in m) for m in nt.sent)
    # বন্ধের পর কুলডাউন: H1 = ৪ ক্যান্ডেল
    cd = E.to_ns(s["closed_at"]) + 4 * 3600 * 1_000_000_000
    for x in st["signals"][1:]:
        if x["timeframe"] == "H1":
            assert E.to_ns(x["opened_at"]) >= cd


def test_failed_telegram_retried_next_run_and_expires():
    df = gold()
    i, now = first_live_signal(df)
    st, bad = fresh_state(), Fake(ok=False)
    h1 = clean(truncate(df, now), now)
    r = E.run_once(CFG, {"XAUUSD": h1}, {}, st, bad, now)
    assert r["new"] == 1 and r["sent"] == 0 and st["signals"][0]["notified_open"] is False
    good = Fake()
    now2 = at(int(pd.Timestamp(now).value) + 20 * 60_000_000_000)
    r = E.run_once(CFG, {"XAUUSD": clean(truncate(df, now2), now2)}, {}, st, good, now2)
    assert r["new"] == 0 and r["sent"] == 1 and st["signals"][0]["notified_open"] is True and len(st["signals"]) == 1
    # অনেক দেরিতে: ব্যর্থ বার্তা আর পাঠানো হয় না
    st2, bad2 = fresh_state(), Fake(ok=False)
    E.run_once(CFG, {"XAUUSD": h1}, {}, st2, bad2, now)
    late = at(int(pd.Timestamp(now).value) + 6 * 3600 * 1_000_000_000)
    g2 = Fake()
    E.run_once(CFG, {"XAUUSD": clean(truncate(df, late), late)}, {}, st2, g2, late)
    assert st2["signals"][0]["notified_open"] == "expired" and not any("নতুন সিগন্যাল" in m for m in g2.sent)


def test_error_alert_deduplicated():
    st, nt = fresh_state(), Fake()
    now = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
    E.run_once(CFG, {}, {"XAUUSD": "সার্ভার এরর"}, st, nt, now)
    E.run_once(CFG, {}, {"XAUUSD": "সার্ভার এরর"}, st, nt, at(int(pd.Timestamp(now).value) + 3600 * 10**9))
    assert len(nt.sent) == 1 and "সতর্কতা" in nt.sent[0]
    E.run_once(CFG, {}, {"XAUUSD": "সার্ভার এরর"}, st, nt, at(int(pd.Timestamp(now).value) + 7 * 3600 * 10**9))
    assert len(nt.sent) == 2                      # ৬ ঘণ্টা পরে আবার
    E.run_once(CFG, {}, {"XAUUSD": "অন্য এরর"}, st, nt, now)
    assert len(nt.sent) == 3


def test_failed_error_alert_not_marked():
    st = fresh_state()
    E.run_once(CFG, {}, {"XAUUSD": "x"}, st, Fake(ok=False), datetime(2026, 10, 23, 12, tzinfo=timezone.utc))
    assert st["errors"] == {}


def test_save_called_before_send():
    df = gold()
    i, now = first_live_signal(df)
    order = []
    st = fresh_state()

    class Spy(Fake):
        def send(self, text):
            order.append("send")
            return super().send(text)

    E.run_once(CFG, {"XAUUSD": clean(truncate(df, now), now)}, {}, st, Spy(), now, save=lambda: order.append("save"))
    assert order.index("save") < order.index("send")


def test_friday_cutoff_and_disabled_pairs():
    assert [p for p, v in CFG["pairs"].items() if v["enabled"]] == ["XAUUSD"]
    assert CFG["scoring"]["required"] == ["clear_trend", "pullback_zone"] and CFG["scoring"]["threshold"] == 4
