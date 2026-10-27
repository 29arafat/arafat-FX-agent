"""ধাপ ৩গ গবেষণা-মডিউলের টেস্ট: ভবিষ্যৎ-দেখা নেই, সিমুলেশন সঠিক, রায়-যুক্তি ঠিক।"""
import numpy as np
import pandas as pd
import pytest

from backtest import research as R
from src.fetch import INTERVALS, load_config

CFG = load_config()


def synth(weeks=30, seed=1, drift=0.0):
    rng = np.random.default_rng(seed)
    idx = []
    start = pd.Timestamp("2024-01-01", tz="UTC")           # সোমবার
    for w in range(weeks):
        base = start + pd.Timedelta(weeks=w)
        idx.append(pd.date_range(base, base + pd.Timedelta(days=4, hours=21, minutes=45), freq="15min"))
    idx = idx[0].append(idx[1:])
    n = len(idx)
    ret = rng.normal(drift, 0.9, n)
    c = 2000 + np.cumsum(ret)
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0.1, 1.0, n)
    l = np.minimum(o, c) - rng.uniform(0.1, 1.0, n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=pd.DatetimeIndex(idx, name="datetime"))


def test_m15_interval_known():
    assert INTERVALS["M15"] == ("15min", 15)


def test_resample_drops_partial_groups():
    m = synth(1)
    m = m.drop(m.index[5])                      # ০০:৩০–০১:০০ M30 গ্রুপে ০১:১৫? নয়: ০০:৪৫-এর ক্যান্ডেল বাদ → গ্রুপ ০০:৩০ অসম্পূর্ণ
    h = R.resample_tf(m, 30)
    assert pd.Timestamp("2024-01-01 01:15", tz="UTC") not in h.index
    assert pd.Timestamp("2024-01-01 01:30", tz="UTC") in h.index
    assert (h.index.minute % 30 == 0).all()
    one = R.resample_tf(synth(1), 60)
    first = synth(1).iloc[:4]
    assert one.iloc[0]["high"] == first["high"].max() and one.iloc[0]["open"] == first["open"].iloc[0]
    assert one.iloc[0]["close"] == first["close"].iloc[-1]


@pytest.mark.parametrize("cand,gen", R.CANDIDATES, ids=[c.name for c, _ in R.CANDIDATES])
def test_no_lookahead_future_change_does_not_alter_past_signals(cand, gen):
    m15 = synth(26, seed=3)
    minutes = R.TF_MIN[cand.tf]
    full = R.resample_tf(m15, minutes)
    cut_ts = full.index[int(len(full) * 0.6)]
    base = gen(full, minutes)
    mutated = full.copy()
    fut = mutated.index >= cut_ts
    rng = np.random.default_rng(99)
    mutated.loc[fut, ["open", "high", "low", "close"]] = mutated.loc[fut, ["open", "high", "low", "close"]].to_numpy() * 1.0 + rng.normal(0, 5, (fut.sum(), 1))
    alt = gen(mutated, minutes)
    k = int(np.searchsorted(R.ns(full.index), cut_ts.value))
    past = lambda s: [x for x in s if x["i"] < k]
    assert past(base) == past(alt)


def test_candidates_produce_signals_on_noise():
    m15 = synth(30, seed=5)
    counts = {}
    for cand, gen in R.CANDIDATES:
        counts[cand.name] = len(gen(R.resample_tf(m15, R.TF_MIN[cand.tf]), R.TF_MIN[cand.tf]))
    assert all(v > 0 for v in counts.values()), counts


def _tiny(highs, lows, closes=None):
    n = len(highs)
    idx = pd.date_range("2024-01-02 10:00", periods=n, freq="15min", tz="UTC")     # মঙ্গলবার
    closes = closes or [(h + l) / 2 for h, l in zip(highs, lows)]
    return pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes}, index=idx)


def _run(m15, sig, **kw):
    tf = m15.iloc[:1]
    return R.simulate(m15, tf, 15, [sig], kw.pop("spread", 0.0), **kw)


def test_simulate_tp_sl_and_ambiguity():
    base = [100.0] * 4
    m = _tiny([100, 100, 100.5, 103.2] + [100] * 120, [100, 100, 99.5, 100] + [100] * 120)
    sig = {"i": 0, "dir": "BUY", "entry": 100.0, "sl": 99.0, "tp": 102.0}
    # tf ক্যান্ডেল ০ শেষ হয় ১০:১৫, তাই পরের M15 = সূচক ১ (১০:১৫); ৩ নম্বরে high ১০৩.২ → TP, কিন্তু সূচক ২ low ৯৯.৫ SL ছোঁয়নি
    t = _run(m, sig)
    assert len(t) == 1 and t[0]["status"] == "TP" and t[0]["r"] == pytest.approx(2.0)
    m2 = _tiny([100, 100, 103.2] + [100] * 120, [100, 100, 98.0] + [100] * 120)     # একই ক্যান্ডেলে দুটোই
    t2 = _run(m2, sig)
    assert t2[0]["status"] == "SL" and t2[0]["r"] == -1.0 and t2[0]["amb"] is True


def test_simulate_reverse_is_mirror():
    m = _tiny([100, 100, 100.5, 103.2] + [100] * 120, [100, 100, 99.5, 100] + [100] * 120)
    sig = {"i": 0, "dir": "BUY", "entry": 100.0, "sl": 99.0, "tp": 102.0}
    t = _run(m, sig, reverse=True)
    # উল্টো SELL: SL = ১০১, TP = ৯৮। high ১০৩.২ → SL
    assert t[0]["dir"] == "SELL" and t[0]["status"] == "SL"


def test_simulate_spread_reduces_buy_tp_r():
    m = _tiny([100, 100, 100.5, 103.2] + [100] * 120, [100, 100, 99.5, 100] + [100] * 120)
    sig = {"i": 0, "dir": "BUY", "entry": 100.0, "sl": 99.0, "tp": 102.0}
    t = _run(m, sig, spread=0.2)
    assert t[0]["r"] < 2.0


def test_simulate_delay_misses_trade_if_stopped_meanwhile():
    # সূচক ১ ও ২-এ দাম SL ভাঙে; ৩০ মিনিট দেরিতে এন্ট্রি → হাতছাড়া
    m = _tiny([100, 100, 100, 100] + [100] * 120, [100, 98.0, 98.0, 100] + [100] * 120)
    sig = {"i": 0, "dir": "BUY", "entry": 100.0, "sl": 99.0, "tp": 102.0}
    assert _run(m, sig, delay_min=30) == []
    assert len(_run(m, sig, delay_min=0)) == 1


def test_simulate_time_exit_and_one_trade_at_a_time():
    m = _tiny([100.1] * 200, [99.9] * 200, [100.0] * 200)
    tf = m.copy()
    sigs = [{"i": 0, "dir": "BUY", "entry": 100.0, "sl": 90.0, "tp": 110.0},
            {"i": 1, "dir": "BUY", "entry": 100.0, "sl": 90.0, "tp": 110.0}]
    t = R.simulate(m, tf, 15, sigs, 0.0)
    assert len(t) == 1 and t[0]["status"] == "TIME" and abs(t[0]["r"]) < 0.05


def test_bootstrap_ci():
    lo, hi = R.bootstrap_ci([1.0] * 50 + [-1.0] * 10, 0.99)
    assert 0 < lo < hi
    assert np.isnan(R.bootstrap_ci([1.0, 2.0], 0.95)[0])
    lo2, hi2 = R.bootstrap_ci(list(np.random.default_rng(0).normal(0, 1, 400)), 0.99)
    assert lo2 < 0 < hi2


def _fake(rs_first, rs_second, d60=None):
    cut = 1000
    mk = lambda rs, t0: [{"t": t0 + i, "exit_t": t0 + i, "dir": "BUY", "status": "TP", "r": r, "amb": False} for i, r in enumerate(rs)]
    net = mk(rs_first, 0) + mk(rs_second, 2000)
    return {"cand": R.CANDIDATES[0][0], "net_d0": net, "net_d60": mk(d60 if d60 is not None else rs_first + rs_second, 0),
            "net_d30": net, "gross": net, "reverse": net, "cut_ns": cut, "signals_raw": len(net)}


def test_judge_pass_weak_fail():
    good = [1.5] * 120 + [-1.0] * 80
    assert R.judge(_fake(good, good), 300)["verdict"] == "PASS"
    zero = [1.0, -1.0] * 100
    assert R.judge(_fake(zero, zero), 300)["verdict"] == "FAIL"
    assert R.judge(_fake(good[:50], good[:50]), 300)["verdict"] == "FAIL"        # নমুনা কম
    assert R.judge(_fake(good, good, d60=[-1.0] * 50 + [0.5] * 100), 300)["verdict"] == "FAIL"   # দেরিতে ভাঙে
    assert R.judge(_fake(good, [-0.5, 0.3] * 100), 300)["verdict"] == "FAIL"     # দ্বিতীয় অর্ধেক নেগেটিভ


def test_end_to_end_report_on_random_walk(monkeypatch):
    monkeypatch.setattr(R, "MAX_HOLD_M15", 96)
    m15 = synth(30, seed=11)
    report, summ = R.run(m15, CFG, "XAUUSD", pd.Timestamp("2030-01-01", tz="UTC").to_pydatetime())
    assert "আগে-ঘোষিত" in report and "সীমাবদ্ধতা" in report
    assert len(summ["candidates"]) == len(R.CANDIDATES)
    # র‍্যান্ডম ওয়াকে ৯৯% CI-তে একসাথে সবাই PASS করা অসম্ভবপ্রায়
    assert sum(c["verdict"] == "PASS" for c in summ["candidates"]) <= 1
    assert "2000" not in report.split("## সীমাবদ্ধতা")[0].replace("2024", "")   # কাঁচা দাম নেই
