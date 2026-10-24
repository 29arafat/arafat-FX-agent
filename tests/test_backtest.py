"""ধাপ ৩: কৃত্রিম ডেটায় stats, tracker, strategy (ভবিষ্যৎ-দেখা নেই), simulate, fetch_h1_history।"""
import re
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from backtest import backtest as B
from src.fetch import FetchError, Throttle, fetch_h1_history, load_config
from src.stats import max_drawdown, summarize
from src.strategy import Signal, build_frames, evaluate, friday_blocked
from src.tracker import find_exit, result_r

NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


def synth(seed=1, start="2024-01-01", end="2026-06-01"):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, end, freq="1h", tz="UTC")
    idx = idx[~(((idx.weekday == 4) & (idx.hour >= 22)) | (idx.weekday == 5) | ((idx.weekday == 6) & (idx.hour < 22)))]
    n = len(idx)
    c = 1.1 * np.exp(np.cumsum(rng.normal(np.sin(np.arange(n) / 900) * 0.0002, 0.0008)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) * (1 + np.abs(rng.normal(0, 0.0003, n)))
    l = np.minimum(o, c) * (1 - np.abs(rng.normal(0, 0.0003, n)))
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 0.0}, index=idx)
    df.index.name = "datetime"
    return df


# ---------- stats ----------
def test_stats_basic():
    s = summarize([2, -1, -1, 2], min_trades=3)
    assert s["trades"] == 4 and s["wins"] == 2 and s["win_rate"] == 50.0
    assert s["total_r"] == 2 and abs(s["expectancy"] - 0.5) < 1e-9
    assert abs(s["profit_factor"] - 2.0) < 1e-9 and s["reliable"]
    assert max_drawdown([2, -1, -1, 2]) == 2


def test_stats_empty_and_no_loss():
    assert summarize([])["trades"] == 0 and not summarize([])["reliable"]
    assert summarize([2, 2])["profit_factor"] is None


# ---------- tracker ----------
def test_exit_buy_tp_and_sl():
    hi, lo = np.array([1.02, 1.06]), np.array([1.00, 1.01])
    assert find_exit("BUY", 0.99, 1.05, hi, lo) == (1, "TP", False)
    assert find_exit("BUY", 1.005, 1.05, np.array([1.02]), np.array([1.00])) == (0, "SL", False)
    assert find_exit("BUY", 0.5, 2.0, hi, lo) is None


def test_exit_ambiguous_is_sl():
    assert find_exit("BUY", 1.00, 1.05, np.array([1.06]), np.array([0.99])) == (0, "SL", True)
    assert find_exit("SELL", 1.10, 1.00, np.array([1.11]), np.array([0.99])) == (0, "SL", True)


def test_exit_spread_effect():
    # SELL: TP ছোঁয়া চার্টে 1.0000 পৌঁছালেও অ্যাস্ক (low+spread) 1.0002 → TP নয়
    assert find_exit("SELL", 1.1, 1.0, np.array([1.05]), np.array([1.0000]), spread=0.0002) is None
    assert find_exit("SELL", 1.1, 1.0, np.array([1.05]), np.array([1.0000]), spread=0.0) == (0, "TP", False)
    assert find_exit("SELL", 1.1, 1.0, np.array([1.0999]), np.array([1.05]), spread=0.0002) == (0, "SL", False)


def test_result_r():
    assert result_r("BUY", 1.0, 0.9, 1.2, "SL") == -1.0
    assert abs(result_r("BUY", 1.0, 0.9, 1.2, "TP") - 2.0) < 1e-9
    assert result_r("BUY", 1.0, 0.9, 1.2, "TP", spread=0.01) < 2.0     # স্প্রেডে লাভ কমে
    assert abs(result_r("SELL", 1.0, 1.1, 0.8, "TP") - 2.0) < 1e-9


# ---------- নিয়ম ----------
def test_friday_blocked():
    fri17 = int(pd.Timestamp("2026-10-23 17:59", tz="UTC").value)   # শুক্রবার
    fri18 = int(pd.Timestamp("2026-10-23 18:00", tz="UTC").value)
    sat = int(pd.Timestamp("2026-10-24 03:00", tz="UTC").value)
    mon = int(pd.Timestamp("2026-10-26 10:00", tz="UTC").value)
    assert not friday_blocked(fri17) and friday_blocked(fri18) and friday_blocked(sat) and not friday_blocked(mon)


# ---------- strategy: ভবিষ্যৎ দেখা নিষিদ্ধ ----------
def _signals(frames, cfg, tf="H1"):
    trig = frames[cfg["strategy"]["tf_map"][tf]["trigger"]]
    out = {}
    for i in range(300, len(trig)):
        s = evaluate("EURUSD", tf, frames, i, cfg)
        if s:
            out[s.time_ns] = (s.direction, round(s.entry, 8), round(s.sl, 8), round(s.tp, 8), s.score)
    return out


def test_no_lookahead_h1():
    cfg = load_config()
    df = synth()
    full = _signals(build_frames(df, cfg, NOW), cfg)
    assert len(full) > 5, "টেস্টের জন্য যথেষ্ট সিগন্যাল নেই"
    cut = df.index[int(len(df) * 0.8)]
    part = _signals(build_frames(df[df.index < cut], cfg, NOW), cfg)
    limit = int(cut.value) - 86_400_000_000_000   # প্রান্তের টুকরো এড়াতে ১ দিন আগে পর্যন্ত
    a = {k: v for k, v in full.items() if k <= limit}
    b = {k: v for k, v in part.items() if k <= limit}
    assert a == b, "ভবিষ্যতের ডেটা সিগন্যাল বদলে দিচ্ছে (look-ahead)"


def test_no_lookahead_future_garbage():
    """ভবিষ্যৎ ক্যান্ডেল নষ্ট করলেও আগের সিগন্যাল একই থাকতে হবে।"""
    cfg = load_config()
    df = synth()
    k = int(len(df) * 0.7)
    bad = df.copy()
    bad.iloc[k:, bad.columns.get_indexer(["open", "high", "low", "close"])] *= 3.0
    limit = int(df.index[k].value) - 86_400_000_000_000   # প্রান্তের টুকরো এড়াতে ১ দিন আগে পর্যন্ত
    a = {t: v for t, v in _signals(build_frames(df, cfg, NOW), cfg).items() if t <= limit}
    b = {t: v for t, v in _signals(build_frames(bad, cfg, NOW), cfg).items() if t <= limit}
    assert a and a == b


def test_no_lookahead_replay_at_each_moment():
    """'t সময়ে শুধু t পর্যন্ত ডেটা' দিয়ে চালালে ফল, পুরো ডেটার ফলের সাথে হুবহু এক হতে হবে (লাইভ = ব্যাকটেস্ট)।"""
    cfg = load_config()
    df = synth(end="2025-06-01")
    full_fr = build_frames(df, cfg, NOW)
    trig = full_fr["H1"]
    rng = np.random.default_rng(7)
    sig_idx = [i for i in range(400, len(trig)) if evaluate("EURUSD", "H1", full_fr, i, cfg)]
    pick = sig_idx[:12] + [int(x) for x in rng.integers(400, len(trig), 12)]
    assert len(sig_idx) >= 5
    for i in pick:
        t_ns = int(trig.end_ns[i])
        t = pd.Timestamp(t_ns, tz="UTC")
        part = df[df.index + pd.Timedelta(hours=1) <= t]
        fr = build_frames(part, cfg, t.to_pydatetime())
        j = len(fr["H1"]) - 1
        assert int(fr["H1"].end_ns[j]) == t_ns
        a = evaluate("EURUSD", "H1", full_fr, i, cfg)
        b = evaluate("EURUSD", "H1", fr, j, cfg)
        assert (a is None) == (b is None), f"সূচক {i}: সিগন্যাল থাকা/না-থাকা মেলে না"
        if a:
            assert (a.direction, round(a.entry, 8), round(a.sl, 8), round(a.tp, 8), a.score) == \
                   (b.direction, round(b.entry, 8), round(b.sl, 8), round(b.tp, 8), b.score)


def test_signal_geometry():
    cfg = load_config()
    fr = build_frames(synth(), cfg, NOW)
    n = 0
    for tf in ("H1", "H4"):
        trig = fr[cfg["strategy"]["tf_map"][tf]["trigger"]]
        for i in range(300, len(trig)):
            s = evaluate("EURUSD", tf, fr, i, cfg)
            if not s:
                continue
            n += 1
            assert s.score >= cfg["scoring"]["threshold"] and s.rr in (2, 3)
            if s.direction == "BUY":
                assert s.sl < s.entry < s.tp
            else:
                assert s.tp < s.entry < s.sl
            assert abs(abs(s.tp - s.entry) - s.rr * abs(s.entry - s.sl)) < 1e-9
    assert n > 5


# ---------- simulate ----------
def test_simulate_no_overlap_and_net_not_better():
    cfg = load_config()
    fr = build_frames(synth(), cfg, NOW)
    g = B.simulate("EURUSD", "H1", fr, cfg, 0.0)["trades"]
    n = B.simulate("EURUSD", "H1", fr, cfg, 0.0002)["trades"]
    assert len(g) > 5
    for tr in (g, n):
        for a, b in zip(tr, tr[1:]):
            assert b["t"] >= a["exit_t"], "একই সাথে দুটো ট্রেড চলছে"
            assert all(x["r"] == -1.0 or x["r"] > 0 for x in (a, b))
    assert sum(x["r"] for x in n) / max(len(n), 1) <= sum(x["r"] for x in g) / max(len(g), 1) + 0.15


def test_summary_and_report_have_no_prices():
    cfg = load_config()
    run = B.run_all({"EURUSD": synth()}, cfg, NOW, ["EURUSD"])
    sm = B.build_summary(run, cfg, ["EURUSD"])
    rep = B.render_report(sm, cfg)
    assert "H1" in rep and "Out-of-sample" in rep and re.search(r"\d\.\d{4,}", rep) is None  # কাঁচা দাম নেই
    assert sm["modes"]["net"]["by_tf"]["H1"]["all"]["trades"] >= 0


# ---------- fetch_h1_history ----------
def test_history_pages_then_stops():
    import src.fetch as F
    avail = 7000
    full = pd.DataFrame({"open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0, "volume": 0.0},
                        index=pd.date_range("2025-01-01", periods=avail, freq="1h", tz="UTC"))
    calls = []

    def fake(symbol, tf, bars, end_date=None, **kw):
        calls.append(bars)
        part = full if end_date is None else full[full.index <= pd.Timestamp(end_date)]
        if part.empty:
            raise FetchError("no data", False)
        return part.iloc[-bars:]          # end_date ও সেই ক্যান্ডেল সহ, ঠিক API-র মতো

    orig = F.fetch_candles
    F.fetch_candles = fake
    try:
        df = fetch_h1_history("X", 30000, page_bars=5000, max_pages=12, api_key="k",
                              throttle=Throttle(0, sleep=lambda s: None))
    finally:
        F.fetch_candles = orig
    assert len(df) == avail and df.index.is_monotonic_increasing and not df.index.duplicated().any()
    assert len(calls) <= 3


def test_history_first_page_error_raises():
    import src.fetch as F
    orig = F.fetch_candles

    def boom(*a, **k):
        raise FetchError("x", False)
    F.fetch_candles = boom
    try:
        try:
            fetch_h1_history("X", 100, api_key="k", throttle=Throttle(0, sleep=lambda s: None))
            raise AssertionError("ত্রুটি ওঠেনি")
        except FetchError:
            pass
    finally:
        F.fetch_candles = orig


# ---------- ডায়াগনস্টিক ----------
def test_diagnose_runs_and_baselines_sane():
    from backtest import diagnose as D
    cfg = load_config()
    df = synth()
    rep = D.diagnose("EURUSD", df, cfg, NOW, seeds=3)
    assert "BUY বনাম SELL" in rep and "বেসলাইন" in rep and "বছরভিত্তিক" in rep
    assert re.search(r"\d\.\d{4,}", rep) is None
    fr = build_frames(B.clean_h1(df, cfg, NOW), cfg, NOW)
    a = D.baseline("EURUSD", "H1", fr, cfg, 0.0, "random", 1)
    b = D.baseline("EURUSD", "H1", fr, cfg, 0.0, "random", 1)
    assert a == b and len(a) > 20          # একই সিডে একই ফল
    for x, y in zip(a, a[1:]):
        assert y["t"] >= x["exit_t"]
    t = D.baseline("EURUSD", "H1", fr, cfg, 0.0, "trend", 1)
    assert len(t) > 5
