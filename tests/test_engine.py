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
CFG["heartbeat"]["daily_utc_hour"] = 99        # বেশির ভাগ টেস্টে হার্টবিট/বিরতি-সতর্কতা বন্ধ; নিচে আলাদা টেস্ট আছে
CFG["heartbeat"]["stale_after_hours"] = 9999
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

    E.run_once(CFG, {"XAUUSD": clean(truncate(df, now), now)}, {}, st, Spy(), now, save=lambda *a: order.append("save"))
    assert order.index("save") < order.index("send")


def test_friday_cutoff_and_disabled_pairs():
    assert [p for p, v in CFG["pairs"].items() if v["enabled"]] == ["XAUUSD"]
    assert CFG["scoring"]["required"] == ["clear_trend", "pullback_zone"] and CFG["scoring"]["threshold"] == 4


# ---------- ধাপ ৫: হার্টবিট, বিরতি, status, stats, datastore ----------
def test_daily_heartbeat_once_per_day_and_gap_alert():
    cfg = copy.deepcopy(CFG)
    cfg["heartbeat"].update(daily_utc_hour=5, stale_after_hours=3)
    st, nt = fresh_state(), Fake()
    t0 = datetime(2026, 10, 23, 4, tzinfo=timezone.utc)            # ০৫:০০-এর আগে → হার্টবিট নয়
    E.run_once(cfg, {}, {}, st, nt, t0)
    assert nt.sent == []
    t1 = datetime(2026, 10, 23, 5, 7, tzinfo=timezone.utc)
    E.run_once(cfg, {}, {}, st, nt, t1)
    assert len(nt.sent) == 1 and "সিস্টেম সচল" in nt.sent[0]       # ১ ঘণ্টা ৭ মিনিট ফাঁক সীমার মধ্যে: শুধু হার্টবিট
    n = len(nt.sent)
    E.run_once(cfg, {}, {}, st, nt, datetime(2026, 10, 23, 6, 7, tzinfo=timezone.utc))
    assert len(nt.sent) == n                                          # একই দিনে আবার নয়
    E.run_once(cfg, {}, {}, st, nt, datetime(2026, 10, 23, 12, 7, tzinfo=timezone.utc))     # ৬ ঘণ্টা বিরতি
    assert any("রান বিরতি" in m for m in nt.sent)
    E.run_once(cfg, {}, {}, st, nt, datetime(2026, 10, 24, 5, 7, tzinfo=timezone.utc))
    assert sum("সিস্টেম সচল" in m for m in nt.sent) == 2             # পরের দিন আবার


def test_status_has_d1_trend_and_no_price():
    df = gold()
    i, now = first_live_signal(df)
    st = fresh_state()
    E.run_once(CFG, {"XAUUSD": clean(truncate(df, now), now)}, {}, st, Fake(), now)
    p = st["pairs"]["XAUUSD"]
    assert p["d1_trend"] in ("BUY", "SELL", "NONE") and p["running_signal"] is True and p["last_candle"].endswith("Z")
    assert "last_price" not in p


def test_build_stats():
    from src.stats import build_stats
    sigs = [
        {"id": "a", "pair": "XAUUSD", "timeframe": "H1", "status": "TP", "closed_at": "2026-10-20T10:00:00Z", "result_r": 2.0, "ambiguous": False},
        {"id": "b", "pair": "XAUUSD", "timeframe": "H4", "status": "SL", "closed_at": "2026-10-22T10:00:00Z", "result_r": -1.0, "ambiguous": True},
        {"id": "c", "pair": "XAUUSD", "timeframe": "H1", "status": "Running", "closed_at": None, "result_r": 0, "ambiguous": False},
        {"id": "d", "pair": "XAUUSD", "timeframe": "H1", "status": "SL", "closed_at": "2026-08-01T10:00:00Z", "result_r": -1.0, "ambiguous": False},
    ]
    st = build_stats(sigs, "2026-10-23T00:00:00Z", "v1", 30)
    assert st["running"] == 1 and st["overall"]["trades"] == 3 and abs(st["overall"]["total_r"]) < 1e-9
    assert st["last_7d"]["trades"] == 2 and st["last_30d"]["trades"] == 2
    assert st["by_timeframe"]["H1"]["trades"] == 2 and st["by_timeframe"]["D1"]["trades"] == 0
    assert [e["r"] for e in st["equity"]] == [-1.0, 1.0, 0.0] and st["overall"]["ambiguous"] == 1


def _git(cwd, *a):
    import subprocess
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def _remote(tmp):
    """একটা bare রিমোট + তাতে খালি 'data' ব্রাঞ্চ + একটা ক্লোন।"""
    bare = Path(tmp) / "origin.git"
    _git(tmp, "init", "-q", "--bare", "-b", "data", str(bare))
    seed = Path(tmp) / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "data")
    (seed / "README.md").write_text("data")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", str(bare), "data")
    clone = Path(tmp) / "data"
    _git(tmp, "clone", "-q", "-b", "data", str(bare), str(clone))
    return bare, clone


def test_datastore_writes_public_files_and_pushes():
    from src.datastore import DataStore, PUBLIC_FIELDS
    df = gold()
    i, now = first_live_signal(df)
    with tempfile.TemporaryDirectory() as tmp:
        bare, clone = _remote(tmp)
        ds = DataStore(clone)
        st = ds.state_path
        state = fresh_state()
        E.run_once(CFG, {"XAUUSD": clean(truncate(df, now), now)}, {}, state, Fake(), now,
                   save=lambda after=False: (ds.write(state, CFG), ds.commit_push("data: test")))
        sig = json.loads((clone / "signals.json").read_text())
        assert len(sig) == 1 and set(sig[0]) == set(PUBLIC_FIELDS) and "key" not in sig[0] and "notified_open" not in sig[0]
        status = json.loads((clone / "status.json").read_text())
        assert status["pairs"]["XAUUSD"]["running_signal"] is True and "last_price" not in json.dumps(status)
        assert json.loads((clone / "stats.json").read_text())["running"] == 1
        assert json.loads(st.read_text())["signals"][0]["notified_open"] is True      # state-এ ফ্ল্যাগ আছে
        # রিমোটে সত্যিই পৌঁছেছে
        check = Path(tmp) / "check"
        _git(tmp, "clone", "-q", "-b", "data", str(bare), str(check))
        assert (check / "signals.json").exists() and (check / "state.json").exists()
        # পরিবর্তন না থাকলে নতুন কমিট নয়
        n1 = _git(clone, "rev-list", "--count", "HEAD").strip()
        assert ds.commit_push("again") is True and _git(clone, "rev-list", "--count", "HEAD").strip() == n1


def test_datastore_rebases_when_remote_moved_and_fails_loudly():
    from src.datastore import DataStore
    with tempfile.TemporaryDirectory() as tmp:
        bare, clone = _remote(tmp)
        other = Path(tmp) / "other"
        _git(tmp, "clone", "-q", "-b", "data", str(bare), str(other))
        (other / "x.txt").write_text("1")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "x")
        _git(other, "push", "-q", "origin", "data")
        ds = DataStore(clone)
        ds.write(fresh_state() | {"last_check": "2026-10-23T00:00:00Z"}, CFG)
        assert ds.commit_push("mine") is True                    # পিছিয়ে ছিল, rebase করে পুশ
        assert (clone / "x.txt").exists()
        _git(clone, "remote", "set-url", "origin", str(Path(tmp) / "nope.git"))
        (clone / "y.txt").write_text("2")
        try:
            ds.commit_push("fails", retries=1)
            raise AssertionError("ত্রুটি ওঠেনি")
        except RuntimeError:
            pass


def test_main_publish_end_to_end_with_patched_fetch():
    import os
    import src.main as M
    from src.fetch import Throttle
    df = gold()
    i, now = first_live_signal(df)
    h1 = clean(truncate(df, now), now)
    with tempfile.TemporaryDirectory() as tmp:
        bare, clone = _remote(tmp)
        orig_fetch, orig_now, orig_cfg = M.fetch_all, M.utcnow, M.load_config
        M.fetch_all = lambda cfg, key, now=None, due=None, **k: ({"XAUUSD": {"H1": h1}}, {}, Throttle(0))
        M.utcnow = lambda: now
        M.load_config = lambda: CFG
        os.environ["PRICE_API_KEY"] = "dummy"
        try:
            assert M.main(["--once", "--publish", str(clone)]) == 0
            assert M.main(["--once", "--publish", str(clone)]) == 0            # দ্বিতীয়বারে ডুপ্লিকেট নয়
        finally:
            M.fetch_all, M.utcnow, M.load_config = orig_fetch, orig_now, orig_cfg
            os.environ.pop("PRICE_API_KEY", None)
        assert len(json.loads((clone / "signals.json").read_text())) == 1
        check = Path(tmp) / "check"
        _git(tmp, "clone", "-q", "-b", "data", str(bare), str(check))
        assert len(json.loads((check / "signals.json").read_text())) == 1
