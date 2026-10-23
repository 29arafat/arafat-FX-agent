"""fetch টেস্ট: নকল সার্ভার দিয়ে, নেটওয়ার্ক ছাড়া।"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from src import fetch
from src.fetch import (FetchError, Throttle, aggregate, check_alignment, drop_running,
                       due_timeframes, fetch_all, fetch_candles, fetch_h1, parse_response)

KEY = "SECRETKEY123"
NOW = datetime(2026, 10, 23, 4, 7, tzinfo=timezone.utc)  # শুক্রবার, ০৪:০৭ UTC


def row(ts, o=100.0, h=101.0, l=99.0, c=100.5):
    return {"datetime": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": str(o), "high": str(h),
            "low": str(l), "close": str(c)}


def payload(rows):
    return {"meta": {"symbol": "X"}, "values": rows, "status": "ok"}


def series(minutes, n, last_start):
    """নতুন→পুরনো ক্রমে n টা ক্যান্ডেল, last_start-এ শেষ।"""
    rows = []
    for i in range(n):
        ts = last_start - timedelta(minutes=minutes * i)
        base = 100 + (n - i) * 0.01
        rows.append(row(ts, base, base + 0.5, base - 0.5, base + 0.1))
    return rows


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


class Session:
    def __init__(self, responder):
        self.responder, self.calls = responder, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params), dict(headers)))
        out = self.responder(len(self.calls), params)
        if isinstance(out, Exception):
            raise out
        return out


def thr():
    t = Throttle(8, sleep=lambda s: t.slept.append(s), clock=lambda: 1e9)
    t.slept = []
    return t


# ---------- পার্স ----------

def test_parse_sorts_ascending_and_utc():
    df = parse_response(payload(series(240, 3, datetime(2026, 10, 23, 4, 0))))
    assert list(df.columns) == ["open", "high", "low", "close"]
    assert df.index.is_monotonic_increasing and str(df.index.tz) == "UTC"


def test_parse_error_codes():
    with pytest.raises(FetchError) as e:
        parse_response({"code": 400, "message": "bad", "status": "error"})
    assert not e.value.retryable
    with pytest.raises(fetch.RateLimitError):
        parse_response({"code": 429, "message": "limit", "status": "error"})
    with pytest.raises(FetchError):
        parse_response({"status": "ok", "values": []})


def test_parse_rejects_bad_numbers_and_inverted_candle():
    r = row(datetime(2026, 10, 23, 4, 0))
    r["close"] = "abc"
    with pytest.raises(FetchError):
        parse_response(payload([r]))
    with pytest.raises(FetchError):
        parse_response(payload([row(datetime(2026, 10, 23, 4, 0), h=98.0, l=99.0)]))


# ---------- সময় ----------

def test_alignment():
    good = parse_response(payload(series(240, 3, datetime(2026, 10, 23, 4, 0))))
    check_alignment(good, "H4")
    bad = parse_response(payload(series(240, 3, datetime(2026, 10, 23, 15, 0))))  # UTC+11 ভুল
    with pytest.raises(FetchError):
        check_alignment(bad, "H4")


def test_drop_running_candle():
    df = parse_response(payload(series(240, 3, datetime(2026, 10, 23, 4, 0))))
    out = drop_running(df, 240, NOW)  # ০৪:০০ ক্যান্ডেল ০৮:০০-তে শেষ, এখনো চলছে
    assert out.index[-1] == pd.Timestamp("2026-10-23 00:00", tz="UTC")
    exact = drop_running(df, 240, datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc))
    assert exact.index[-1] == pd.Timestamp("2026-10-23 04:00", tz="UTC")


# ---------- নেটওয়ার্ক আচরণ ----------

def test_key_in_header_not_url_and_utc_param():
    s = Session(lambda n, p: Resp(payload(series(60, 5, datetime(2026, 10, 23, 4, 0)))))
    fetch_candles("XAU/USD", "H1", 5, KEY, thr(), session=s)
    url, params, headers = s.calls[0]
    assert KEY not in url and KEY not in str(params)
    assert headers["Authorization"] == f"apikey {KEY}"
    assert params["timezone"] == "UTC" and params["interval"] == "1h"


def test_rate_limit_waits_then_succeeds():
    ok = Resp(payload(series(60, 5, datetime(2026, 10, 23, 4, 0))))
    s = Session(lambda n, p: Resp({"code": 429, "message": "x", "status": "error"}) if n == 1 else ok)
    t = thr()
    fetch_candles("EUR/USD", "H1", 5, KEY, t, session=s)
    assert 61 in t.slept and t.credits_used == 2


def test_server_error_retried_but_client_error_not():
    ok = Resp(payload(series(60, 5, datetime(2026, 10, 23, 4, 0))))
    s = Session(lambda n, p: Resp({}, 503) if n == 1 else ok)
    fetch_candles("EUR/USD", "H1", 5, KEY, thr(), session=s)
    assert len(s.calls) == 2
    s2 = Session(lambda n, p: Resp({"code": 401, "message": "bad key", "status": "error"}))
    with pytest.raises(FetchError):
        fetch_candles("EUR/USD", "H1", 5, KEY, thr(), session=s2)
    assert len(s2.calls) == 1


def test_network_error_message_never_leaks_key():
    s = Session(lambda n, p: requests.ConnectionError(f"failed https://x?apikey={KEY}"))
    with pytest.raises(FetchError) as e:
        fetch_candles("EUR/USD", "H1", 5, KEY, thr(), session=s, retries=1)
    assert KEY not in str(e.value)


def test_throttle_spaces_requests():
    clock = {"t": 0.0}
    sleeps = []
    t = Throttle(8, sleep=lambda x: (sleeps.append(x), clock.__setitem__("t", clock["t"] + x)),
                 clock=lambda: clock["t"])
    t.wait()
    clock["t"] += 3
    t.wait()
    assert abs(sleeps[0] - 5) < 1e-9


# ---------- H1 থেকে H4/D1 নির্মাণ ----------

def week_h1(weeks=3):
    """H1 ক্যান্ডেল: রবি ২৩:০০ থেকে শুক্র ২১:০০ UTC পর্যন্ত (সপ্তাহান্ত বিরতিসহ)।"""
    idx = []
    sunday = datetime(2026, 10, 4, tzinfo=timezone.utc)
    for w in range(weeks):
        t0 = sunday + timedelta(days=7 * w, hours=23)
        t1 = sunday + timedelta(days=7 * w + 5, hours=21)  # শুক্র ২১:০০ শুরু
        t = t0
        while t <= t1:
            idx.append(t)
            t += timedelta(hours=1)
    n = len(idx)
    return pd.DataFrame({"open": range(n), "high": [i + 10 for i in range(n)],
                         "low": [i - 10 for i in range(n)], "close": [i + 0.5 for i in range(n)]},
                        index=pd.DatetimeIndex(idx, tz="UTC", name="datetime"), dtype=float)


def ts(s):
    return pd.Timestamp(s, tz="UTC")


def test_h4_grid_is_0_4_8_and_ohlc_rules():
    h1 = week_h1()
    h4 = aggregate(h1, 4, 0, 1)
    assert (h4.index.hour % 4 == 0).all()
    c = h4.loc[ts("2026-10-13 04:00")]               # ০৪:০০-০৮:০০ = ৪টা H1
    part = h1.loc[ts("2026-10-13 04:00"):ts("2026-10-13 07:00")]
    assert c["bars"] == 4 and c["open"] == part["open"].iloc[0]
    assert c["high"] == part["high"].max() and c["low"] == part["low"].min()
    assert c["close"] == part["close"].iloc[-1]


def test_h4_sunday_open_candle_has_single_hour():
    h4 = aggregate(week_h1(), 4, 0, 1)
    assert h4.loc[ts("2026-10-11 20:00")]["bars"] == 1   # রবি ২৩:০০-এ বাজার খোলে


def test_d1_start20_merges_friday_tail_backward():
    h1 = week_h1()
    d1 = aggregate(h1, 24, 20, 3)
    assert (d1.index.hour == 20).all()
    thu = d1.loc[ts("2026-10-15 20:00")]              # বৃহস্পতি ২০:০০ → শুক্র ২০:০০
    assert thu["bars"] == 26                           # ২৪ + শুক্র ২০:০০, ২১:০০-এর টুকরো ২টা
    assert thu["close"] == h1.loc[ts("2026-10-16 21:00")]["close"]
    assert d1.loc[ts("2026-10-11 20:00")]["bars"] == 21  # রবি ২৩:০০ → সোম ১৯:০০ পূর্ণ দিন


def test_d1_start0_merges_sunday_into_monday():
    h1 = week_h1()
    d1 = aggregate(h1, 24, 0, 3)
    assert 6 not in d1.index.dayofweek
    mon = d1.loc[ts("2026-10-12")]
    assert mon["bars"] == 25                           # রবি ২৩:০০ + সোমবারের ২৪টা
    assert mon["open"] == h1.loc[ts("2026-10-11 23:00")]["open"]


def test_first_incomplete_group_dropped_and_empty_ok():
    h1 = week_h1()
    assert aggregate(h1, 24, 0, 3).index[0] > h1.index[0].floor("D")
    assert aggregate(h1.iloc[0:0], 24, 0, 3).empty


def test_trailing_sunday_fragment_without_monday_is_dropped():
    h1 = week_h1(2)
    cut = h1.loc[:ts("2026-10-11 23:00")]                # দ্বিতীয় রবিবার রাত পর্যন্ত, সোমবার এখনো আসেনি
    d1 = aggregate(cut, 24, 0, 3)
    assert d1.index.max() < ts("2026-10-11")


# ---------- সময়সূচি ----------

def cfg_for(start=20):
    return {"data": {"d1_day_start_hour": start}}


def test_due_timeframes():
    at = lambda h: datetime(2026, 10, 23, h, 7, tzinfo=timezone.utc)
    assert due_timeframes(at(5), cfg_for(20)) == ["H1"]
    assert due_timeframes(at(4), cfg_for(20)) == ["H1", "H4"]
    assert due_timeframes(at(0), cfg_for(20)) == ["H1", "H4"]
    assert due_timeframes(at(20), cfg_for(20)) == ["H1", "H4", "D1"]
    assert due_timeframes(at(0), cfg_for(0)) == ["H1", "H4", "D1"]


# ---------- সম্পূর্ণ ফেচ (নকল সার্ভার) ----------

def server(fail_symbols=()):
    """H1 নকল সার্ভার: end_date মানলে পুরনো পেজ, নইলে সর্বশেষ (০৪:০০ চলমান) থেকে পেছনে।"""
    def responder(n, params):
        if params["symbol"] in fail_symbols:
            return Resp({"code": 400, "message": "bad symbol", "status": "error"})
        assert params["interval"] == "1h", "শুধু H1 আনার কথা"
        last = datetime(2026, 10, 23, 4, 0)
        if "end_date" in params:
            last = datetime.strptime(params["end_date"], "%Y-%m-%dT%H:%M:%S")
        return Resp(payload(series(60, params["outputsize"], last)))
    return responder


CFG = {
    "data": {"timezone": "UTC", "h1_bars": 2500, "d1_h1_bars": 7000, "d1_day_start_hour": 20,
             "d1_min_h1_bars": 3, "retries": 1, "request_timeout_seconds": 5,
             "min_bars": {"H1": 250, "H4": 250, "D1": 250}},
    "api_limits": {"sleep_between_requests_seconds": 0},
    "pairs": {"XAUUSD": {"symbol": "XAU/USD", "decimals": 2, "enabled": True},
              "EURUSD": {"symbol": "EUR/USD", "decimals": 5, "enabled": True}},
}


def test_hourly_run_is_one_call_per_pair():
    s = Session(server())
    data, errors, t = fetch_all(CFG, KEY, NOW, session=s, due=["H1"])
    assert not errors and t.credits_used == 2
    h1 = data["XAUUSD"]["H1"]
    assert h1.index[-1] == ts("2026-10-23 03:00")     # ০৪:০০ চলমান, বাদ
    assert set(data["XAUUSD"]) == {"H1"}


def test_deep_fetch_paginates_and_builds_h4_d1():
    s = Session(server())
    data, errors, t = fetch_all(CFG, KEY, NOW, session=s, due=["H1", "H4", "D1"])
    assert not errors and t.credits_used == 4          # প্রতি পেয়ারে ২ পেজ, H4/D1 আলাদা কল নয়
    assert s.calls[0][1]["outputsize"] == 5000 and s.calls[1][1]["outputsize"] == 2000
    assert "end_date" in s.calls[1][1] and "end_date" not in s.calls[0][1]
    x = data["XAUUSD"]
    assert len(x["H1"]) == 7000 - 1 or len(x["H1"]) >= 6990   # ডুপ্লিকেট বাদে ~৭০০০
    assert x["H1"].index.is_unique and x["H1"].index.is_monotonic_increasing
    assert x["H4"].index[-1] == ts("2026-10-23 00:00")
    assert x["D1"].index[-1] == ts("2026-10-22 20:00") - timedelta(days=1)  # ২২ ২০:০০-এর দিন চলমান


def test_fetch_h1_second_page_overlap_deduplicated():
    s = Session(server())
    df = fetch_h1("XAU/USD", 6000, api_key=KEY, throttle=thr(), session=s, retries=0)
    assert df.index.is_unique and len(df) >= 5990


def test_one_pair_failing_does_not_stop_others():
    data, errors, _ = fetch_all(CFG, KEY, NOW, session=Session(server({"EUR/USD"})), due=["H1"])
    assert "XAUUSD" in data and "EURUSD" in errors and "400" in errors["EURUSD"]


def test_too_few_candles_is_an_error():
    cfg = {**CFG, "data": {**CFG["data"], "h1_bars": 100}}
    data, errors, _ = fetch_all(cfg, KEY, NOW, session=Session(server()), due=["H1"])
    assert not data and "কম" in errors["XAUUSD"]


# ---------- কমান্ড-লাইন (--check) ----------

def run_cli(args):
    import io, os
    from contextlib import redirect_stdout, redirect_stderr
    real = requests.Session.get
    cfg_real = fetch.load_config
    fake_cfg = {**CFG, "data": {**CFG["data"]}, "pairs": {"XAUUSD": CFG["pairs"]["XAUUSD"]}}
    srv = Session(server())

    def fake_get(self, url, params=None, headers=None, timeout=None):
        p = dict(params)
        if p["interval"] == "4h":      # --diagnose: প্রোভাইডারের নিজস্ব H4 (অসম গ্রিডসহ)
            return Resp(payload(series(240, p["outputsize"], datetime(2026, 10, 23, 4, 0))
                                + series(240, 5, datetime(2026, 5, 1, 1, 0))))
        return srv.get(url, params=params, headers=headers)

    requests.Session.get = fake_get
    fetch.load_config = lambda path=None: fake_cfg
    fetch.utcnow = lambda: NOW
    os.environ["PRICE_API_KEY"] = KEY
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = fetch.main(args)
    finally:
        requests.Session.get = real
        fetch.load_config = cfg_real
        fetch.utcnow = lambda: datetime.now(timezone.utc)
        os.environ.pop("PRICE_API_KEY", None)
    return code, out.getvalue(), err.getvalue()


def test_cli_check_with_and_without_prices():
    code, out, _ = run_cli(["--check"])
    assert code == 0 and "XAUUSD" in out and "O=" in out and KEY not in out
    assert "দিন শুরু 20:00" in out and "H4 (বানানো" in out
    code, out, _ = run_cli(["--check", "--no-prices"])
    assert code == 0 and "O=" not in out and "bars=" in out


def test_cli_diagnose_reports_off_grid_candles_without_prices():
    code, out, _ = run_cli(["--check", "--no-prices", "--diagnose"])
    assert code == 0 and "গ্রিডের বাইরে" in out and "01h" in out


def test_cli_without_key_exits_cleanly():
    import os
    os.environ.pop("PRICE_API_KEY", None)
    from io import StringIO
    from contextlib import redirect_stderr
    err = StringIO()
    with redirect_stderr(err):
        assert fetch.main(["--check"]) == 2
