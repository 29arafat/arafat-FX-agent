"""fetch টেস্ট: নকল সার্ভার দিয়ে, নেটওয়ার্ক ছাড়া।"""
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from src import fetch
from src.fetch import (FetchError, Throttle, build_d1, check_alignment, drop_running,
                       due_timeframes, fetch_all, fetch_candles, parse_response)

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


# ---------- D1 নির্মাণ ----------

def week_h4():
    """৩ সপ্তাহ: রবি ২০:০০ থেকে শুক্র ২০:০০ পর্যন্ত H4 (সপ্তাহান্ত বিরতিসহ)।"""
    idx = []
    sunday = datetime(2026, 10, 4, tzinfo=timezone.utc)  # রবিবার
    for w in range(3):
        t = sunday + timedelta(days=7 * w, hours=20)
        while t <= sunday + timedelta(days=7 * w + 5, hours=20):  # শুক্র ২০:০০
            idx.append(t)
            t += timedelta(hours=4)
    df = pd.DataFrame({"open": range(len(idx)), "high": [i + 10 for i in range(len(idx))],
                       "low": [i - 10 for i in range(len(idx))], "close": [i + 0.5 for i in range(len(idx))]},
                      index=pd.DatetimeIndex(idx, tz="UTC", name="datetime"), dtype=float)
    return df


def test_build_d1_start0_merges_sunday_into_monday():
    h4 = week_h4()
    d1 = build_d1(h4, 0)
    assert (d1.index.hour == 0).all()
    assert 6 not in d1.index.dayofweek          # রবিবারের আলাদা ক্যান্ডেল নেই
    mon = d1.loc[pd.Timestamp("2026-10-12", tz="UTC")]  # দ্বিতীয় সপ্তাহের সোমবার
    assert mon["bars"] == 7                      # রবি ২০:০০-র ক্যান্ডেলসহ
    sun_bar = h4.loc[pd.Timestamp("2026-10-11 20:00", tz="UTC")]
    assert mon["open"] == sun_bar["open"]
    # OHLC নিয়ম
    assert mon["high"] == h4.loc["2026-10-11 20:00":"2026-10-12 20:00"]["high"].max()
    assert mon["close"] == h4.loc[pd.Timestamp("2026-10-12 20:00", tz="UTC")]["close"]


def test_build_d1_start20_merges_friday_tail_backward():
    h4 = week_h4()
    d1 = build_d1(h4, 20)
    assert (d1.index.hour == 20).all()
    # শুক্র ২০:০০-র ক্যান্ডেল আগের দিনে (বৃহস্পতি ২০:০০ শুরু) জুড়েছে
    thu = d1.loc[pd.Timestamp("2026-10-15 20:00", tz="UTC")]
    assert thu["bars"] == 7
    assert thu["close"] == h4.loc[pd.Timestamp("2026-10-16 20:00", tz="UTC")]["close"]
    # রবি ২০:০০ শুরুর দিন পূর্ণ (৬টা)
    assert d1.loc[pd.Timestamp("2026-10-11 20:00", tz="UTC")]["bars"] == 6


def test_build_d1_drops_first_incomplete_day():
    h4 = week_h4()
    d1 = build_d1(h4, 0)
    assert d1.index[0] > h4.index[0].floor("D")


def test_build_d1_empty():
    assert build_d1(week_h4().iloc[0:0], 0).empty


# ---------- সময়সূচি ----------

def cfg_for(start=0):
    return {"data": {"d1_day_start_hour": start}}


def test_due_timeframes():
    at = lambda h: datetime(2026, 10, 23, h, 7, tzinfo=timezone.utc)
    assert due_timeframes(at(5), cfg_for(0)) == ["H1"]
    assert due_timeframes(at(4), cfg_for(0)) == ["H1", "H4"]
    assert due_timeframes(at(0), cfg_for(0)) == ["H1", "H4", "D1"]
    assert due_timeframes(at(20), cfg_for(20)) == ["H1", "H4", "D1"]
    assert due_timeframes(at(20), cfg_for(0)) == ["H1", "H4"]


# ---------- সম্পূর্ণ ফেচ (নকল সার্ভার) ----------

def server(fail_symbols=()):
    def responder(n, params):
        if params["symbol"] in fail_symbols:
            return Resp({"code": 400, "message": "bad symbol", "status": "error"})
        if params["interval"] == "1h":
            return Resp(payload(series(60, params["outputsize"], datetime(2026, 10, 23, 4, 0))))
        return Resp(payload(series(240, params["outputsize"], datetime(2026, 10, 23, 4, 0))))
    return responder


CFG = {
    "data": {"timezone": "UTC", "h1_bars": 500, "h4_bars": 1800, "d1_day_start_hour": 0,
             "d1_min_h4_bars": 2, "retries": 1, "request_timeout_seconds": 5,
             "min_bars": {"H1": 250, "H4": 250, "D1": 250}},
    "api_limits": {"sleep_between_requests_seconds": 0},
    "pairs": {"XAUUSD": {"symbol": "XAU/USD", "decimals": 2, "enabled": True},
              "EURUSD": {"symbol": "EUR/USD", "decimals": 5, "enabled": True}},
}


def test_fetch_all_returns_closed_candles_only():
    s = Session(server())
    data, errors, t = fetch_all(CFG, KEY, NOW, session=s, due=["H1", "H4", "D1"])
    assert not errors and set(data) == {"XAUUSD", "EURUSD"}
    h1, h4, d1 = data["XAUUSD"]["H1"], data["XAUUSD"]["H4"], data["XAUUSD"]["D1"]
    assert h1.index[-1] == pd.Timestamp("2026-10-23 03:00", tz="UTC")  # ০৪:০০ চলমান, বাদ
    assert h4.index[-1] == pd.Timestamp("2026-10-23 00:00", tz="UTC")
    assert d1.index[-1] == pd.Timestamp("2026-10-22 00:00", tz="UTC")  # ২৩-এর দিন চলমান
    assert t.credits_used == 4  # প্রতি পেয়ারে H1 + H4, D1 আলাদা কল নয়


def test_one_pair_failing_does_not_stop_others():
    data, errors, _ = fetch_all(CFG, KEY, NOW, session=Session(server({"EUR/USD"})), due=["H1"])
    assert "XAUUSD" in data and "EURUSD" in errors and "400" in errors["EURUSD"]


def test_too_few_candles_is_an_error():
    cfg = {**CFG, "data": {**CFG["data"], "h1_bars": 100}}
    data, errors, _ = fetch_all(cfg, KEY, NOW, session=Session(server()), due=["H1"])
    assert not data and "কম" in errors["XAUUSD"]


# ---------- কমান্ড-লাইন (--check) ----------

def run_cli(args, capsys_free=True):
    import io, os
    from contextlib import redirect_stdout, redirect_stderr
    real = requests.Session.get
    cfg_real = fetch.load_config
    fake_cfg = {**CFG, "data": {**CFG["data"]}, "pairs": {"XAUUSD": CFG["pairs"]["XAUUSD"]}}
    srv = Session(server())
    requests.Session.get = lambda self, *a, **k: srv.get(*a, **k)
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
    assert "দিন শুরু 00:00" in out and "দিন শুরু 20:00" in out
    code, out, _ = run_cli(["--check", "--no-prices"])
    assert code == 0 and "O=" not in out and "bars=" in out


def test_cli_without_key_exits_cleanly():
    import os
    os.environ.pop("PRICE_API_KEY", None)
    from io import StringIO
    from contextlib import redirect_stderr
    err = StringIO()
    with redirect_stderr(err):
        assert fetch.main(["--check"]) == 2
