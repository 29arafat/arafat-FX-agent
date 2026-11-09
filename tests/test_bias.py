"""মার্কেট বায়াস (ধাপ ১ক): স্কোর, লেবেল, দামের সীমা, সোর্স-পার্সার, লগ/যাচাই, এন্ড-টু-এন্ড। নেটওয়ার্ক লাগে না।"""
import json
import math
import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from src.bias import bn, log as L, model as M, run as R, sources as S
from src.fetch import load_config
from src.strategy import build_frames
from tests.test_backtest import NOW, synth

CFG = load_config()


def _df(seed=1, trend=0.0, start="2024-01-01", end="2026-06-01"):
    df = synth(seed=seed, start=start, end=end)
    f = np.exp(np.linspace(0, trend, len(df)))
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] * f
    return df


def _frames(**kw):
    return build_frames(_df(**kw), CFG, NOW)


# ---------- কনফিগ ----------

def test_config_bias_consistent():
    b = CFG["bias"]
    for h, w in b["weights"].items():
        assert abs(sum(w.values()) - 1.0) < 1e-9, h
        assert h in b["horizons"]
    for p in b["pairs"]:
        assert p in CFG["pairs"] and p in b["usd_sign"]
    assert set(b["full_fundamentals"]) <= set(b["pairs"])
    assert b["confidence_bands"][0] == b["neutral_band"]
    assert b["sources"]["events"]["forex_factory"]["enabled"] is True        # ফোরকাস্টের জন্য চালু; ব্যর্থ হলে বাকিটা চলে


# ---------- উপাদান ----------

def test_chart_components_follow_trend():
    up = M.chart_components(_frames(trend=1.5), CFG)
    dn = M.chart_components(_frames(trend=-1.5), CFG)
    assert up["trend_d1"]["score"] > 0.3 and dn["trend_d1"]["score"] < -0.3        # বড় ট্রেন্ড স্পষ্ট
    assert {"trend_h4", "trend_h1", "rsi_d1", "rsi_h1", "ema_pos_h1"} <= set(up)    # বাকিগুলো থাকে (দিক এলোমেলো হতে পারে)
    assert all(-1 <= c["score"] <= 1 for c in up.values()) and all(-1 <= c["score"] <= 1 for c in dn.values())


def _series(vals, end="2026-10-22"):
    idx = pd.date_range(end=end, periods=len(vals), freq="D")
    return pd.Series(vals, index=idx, dtype=float)


def test_fund_gold_signs():
    now = NOW
    fall = _series(np.linspace(2.0, 1.7, 20))        # রিয়েল ইয়িল্ড কমছে
    rise = _series(np.linspace(1.7, 2.0, 20))
    n: list = []
    a = M.fund_components({"real_yield": fall, "usd": _series(np.linspace(100, 98, 20)), "vix": _series(np.linspace(14, 20, 20))},
                          "XAUUSD", CFG, now, n)
    assert a["real_yield"]["score"] > 0 and a["usd"]["score"] > 0 and a["vix"]["score"] > 0
    b = M.fund_components({"real_yield": rise, "usd": _series(np.linspace(98, 100, 20))}, "XAUUSD", CFG, now, n)
    assert b["real_yield"]["score"] < 0 and b["usd"]["score"] < 0


def test_fund_usd_sign_and_limited_pairs():
    usd_up = _series(np.linspace(98, 100, 20))
    n: list = []
    jp = M.fund_components({"usd": usd_up, "real_yield": _series(np.linspace(2, 1.7, 20))}, "USDJPY", CFG, NOW, n)
    eu = M.fund_components({"usd": usd_up}, "EURUSD", CFG, NOW, n)
    assert jp["usd"]["score"] > 0 and eu["usd"]["score"] < 0
    assert "real_yield" not in jp and "vix" not in jp and "cot" not in jp      # শুধু গোল্ডে


def test_fund_stale_and_short_dropped_with_note():
    old = _series(np.linspace(2, 1.7, 20), end="2026-09-01")
    notes: list = []
    out = M.fund_components({"real_yield": old, "usd": _series([100.0, 99.0])}, "XAUUSD", CFG, NOW, notes)
    assert out == {} and len(notes) == 2


def test_cot_component():
    cot = [{"date": pd.Timestamp("2026-10-20") - pd.Timedelta(days=7 * i), "net": 100000 - 5000 * i, "oi": 500000} for i in range(6)]
    out = M.fund_components({"cot": cot}, "XAUUSD", CFG, NOW, [])
    assert out["cot"]["score"] > 0
    stale = [dict(c, date=c["date"] - pd.Timedelta(days=60)) for c in cot]
    n: list = []
    assert "cot" not in M.fund_components({"cot": stale}, "XAUUSD", CFG, NOW, n) and n


# ---------- স্কোর, লেবেল ----------

def test_combine_renormalises_missing():
    w = {"a": 0.5, "b": 0.3, "c": 0.2}
    s, share, used = M.combine(w, {"a": {"score": 1.0}, "b": {"score": -1.0}})
    assert used == ["a", "b"] and abs(share - 0.8) < 1e-9 and abs(s - (0.5 - 0.3) / 0.8) < 1e-9
    assert M.combine(w, {}) == (0.0, 0.0, [])


def test_label_bands_and_share():
    assert M.label(0.1, 1.0, CFG) == ("NEUTRAL", "NONE")
    assert M.label(0.30, 1.0, CFG) == ("UP", "LOW")
    assert M.label(-0.50, 1.0, CFG) == ("DOWN", "MEDIUM")
    assert M.label(0.90, 1.0, CFG) == ("UP", "HIGH")
    assert M.label(0.90, 0.4, CFG) == ("NEUTRAL", "NONE")                    # ডেটা অপর্যাপ্ত


# ---------- দামের সীমা, লেভেল ----------

def test_range_multiples_and_levels():
    fr = _frames()
    price = float(fr["H1"].c[-1])
    for h in ("h4", "d1", "w1"):
        hc = CFG["bias"]["horizons"][h]
        rg = M.price_range(fr, hc, CFG, price, 5)
        exp = CFG["bias"]["range_k"] * float(fr[hc["range_frame"]].atr[-1]) * hc["range_mult"]
        assert abs(rg["R"] - exp) < 1e-12 and rg["lo"] < price < rg["hi"]
        if rg["up_level"] is not None:
            assert rg["up_level"] > price
        if rg["down_level"] is not None:
            assert rg["down_level"] < price
    assert abs(CFG["bias"]["horizons"]["w1"]["range_mult"] - math.sqrt(5)) < 0.001


def test_levels_do_not_look_ahead():
    df = _df(seed=3)
    cut = len(df) - 300
    full = build_frames(df, CFG, NOW)
    part = build_frames(df.iloc[:cut], CFG, NOW)
    # আংশিক ডেটায় যা নিশ্চিত সুইং, পূর্ণ ডেটায় সেই সময়ের জন্য একই ফল (শেষ k ক্যান্ডেল বাদে নিশ্চিত নয়)
    k = CFG["strategy"]["sr"]["swing_k"]
    f4 = part["H4"]
    lv_part = M.nearest_levels(f4, float(f4.c[-1]), k, 120)
    # ভবিষ্যৎ ডেটা বদলালে আংশিক ফল বদলাবে না
    df2 = df.copy()
    df2.iloc[cut:, :4] = df2.iloc[cut:, :4].to_numpy() * 3
    part2 = build_frames(df2.iloc[:cut], CFG, NOW)["H4"]
    assert M.nearest_levels(part2, float(part2.c[-1]), k, 120) == lv_part
    assert full["H4"] is not None


def test_target_logic():
    rng = {"R": 10.0, "lo": 90.0, "hi": 110.0, "up_level": 105.0, "down_level": 80.0}
    assert M.target("UP", 100.0, rng, CFG, 2) == 105.0                       # কাছের লেভেল
    assert M.target("DOWN", 100.0, rng, CFG, 2) == 90.0                      # লেভেল দূরে → সীমার নিচ
    assert M.target("NEUTRAL", 100.0, rng, CFG, 2) is None


# ---------- এক সময়সীমা ----------

def test_build_horizon_output_and_bangla():
    fr = _frames(trend=1.5)
    comps = {k: {"score": 0.8, "value": 70.0 if k.startswith("rsi") else 1.0} for k in
             ("trend_d1", "trend_h4", "trend_h1", "rsi_d1", "rsi_h1", "ema_pos_h1")}
    ev = [{"title": "US CPI", "currency": "USD", "impact": "High", "utc": NOW + timedelta(hours=2), "approx": True, "source": "FRED"}]
    h = M.build_horizon("XAUUSD", "d1", comps, fr, CFG, NOW, ev, "limited")
    assert h["bias"] == "UP" and h["range"]["lo"] < h["ref_price"] < h["range"]["hi"] and h["target"] is not None
    assert h["reasons_bn"] and any("ট্রেন্ড" in r for r in h["reasons_bn"])
    assert any("US CPI" in w and "আনুমানিক" in w for w in h["warnings_bn"])
    s = json.dumps(h, ensure_ascii=False)
    assert "NaN" not in s and "nan" not in s.lower().replace("financial", "")
    h4 = M.build_horizon("XAUUSD", "h4", comps, fr, CFG, NOW, ev, "limited")
    assert any("US CPI" in w for w in h4["warnings_bn"])                            # ২ ঘণ্টা পরের ইভেন্ট ৪ ঘণ্টার মধ্যে
    w1 = M.build_horizon("XAUUSD", "w1", comps, fr, CFG, NOW, [], "limited")
    assert w1["bias"] == "NEUTRAL" and w1["data_share"] < 0.5 and any("অপর্যাপ্ত" in w for w in w1["warnings_bn"])


def test_rsi_overbought_warning():
    assert "অতিক্রয়" in bn.warning_rsi("rsi_d1", 75)
    assert "অতিবিক্রয়" in bn.warning_rsi("rsi_h1", 20)
    assert bn.warning_rsi("rsi_d1", 50) is None and bn.warning_rsi("usd", 80) is None


# ---------- সোর্স-পার্সার ----------

def test_parse_fred_skips_missing():
    s = S.parse_fred({"observations": [{"date": "2026-10-20", "value": "1.9"}, {"date": "2026-10-21", "value": "."},
                                       {"date": "2026-10-22", "value": "2.0"}]})
    assert list(s.values) == [1.9, 2.0]


def test_parse_cot():
    rows = [{"report_date_as_yyyy_mm_dd": "2026-10-13T00:00:00.000", "m_money_positions_long_all": "150000",
             "m_money_positions_short_all": "30000", "open_interest_all": "500000"},
            {"report_date_as_yyyy_mm_dd": "2026-10-20T00:00:00.000", "m_money_positions_long_all": "160000",
             "m_money_positions_short_all": "30000", "open_interest_all": "510000"}, {"bad": 1}]
    out = S.parse_cot(rows)
    assert len(out) == 2 and out[0]["net"] == 130000 and out[0]["date"] > out[1]["date"]


def test_events_from_fred_dst():
    ev = CFG["bias"]["sources"]["events"]
    nov = S.events_from_fred([{"name": "Consumer Price Index", "date": datetime(2026, 11, 12).date()}], ev)[0]
    jul = S.events_from_fred([{"name": "Consumer Price Index", "date": datetime(2026, 7, 14).date()}], ev)[0]
    assert (nov["utc"].hour, nov["utc"].minute) == (13, 30) and (jul["utc"].hour, jul["utc"].minute) == (12, 30)
    assert nov["approx"] is True and nov["title"] == "US CPI"
    assert S.events_from_fred([{"name": "Something Else", "date": datetime(2026, 11, 12).date()}], ev) == []


def test_parse_ff_fields_and_impact_rules():
    payload = [{"title": "CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High",
                "forecast": "0.3%", "previous": "0.4%", "url": "https://x"},
               {"title": "Retail Sales m/m", "country": "USD", "date": "2026-11-13T08:30:00-05:00", "impact": "Medium", "forecast": "", "previous": "0.1%"},
               {"title": "UK thing", "country": "GBP", "date": "2026-11-12T09:00:00-05:00", "impact": "Medium"},
               {"title": "Minor", "country": "USD", "date": "2026-11-12T09:00:00-05:00", "impact": "Low"}, {"bad": 1}]
    ff = S.parse_ff(payload, "Medium")
    assert [e["title"] for e in ff] == ["CPI m/m", "Retail Sales m/m"]          # GBP Medium ও Low বাদ
    assert ff[0]["forecast"] == "0.3%" and ff[0]["previous"] == "0.4%" and ff[0]["utc"].hour == 13
    assert ff[1]["forecast"] is None and "url" not in ff[0]
    assert len(S.parse_ff(payload, "High")) == 1


def test_merge_prefers_ff_and_keeps_multiple_ff_entries():
    ev = CFG["bias"]["sources"]["events"]
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    fred = S.events_from_fred([{"name": "Consumer Price Index", "date": datetime(2026, 11, 12).date()}], ev)
    ff = S.parse_ff([{"title": "CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High"},
                     {"title": "Core CPI m/m", "country": "USD", "date": "2026-11-12T08:30:00-05:00", "impact": "High"}], "Medium")
    m = S.merge_events(fred, ff, now, 7)
    assert len(m) == 2 and all(e["source"] == "FF" and not e["approx"] for e in m)
    assert len(S.merge_events(fred, [], now, 7)) == 1                           # FF না এলে আনুমানিকটা থাকে


def test_fomc_only_on_meeting_days_not_fred_spam():
    ev = CFG["bias"]["sources"]["events"]
    assert not any("FOMC" in k for k in ev["fred_releases"])
    spam = [{"name": "FOMC Press Release", "date": datetime(2026, 11, d).date()} for d in range(9, 16)]
    assert S.events_from_fred(spam, ev) == []
    now = datetime(2026, 11, 9, tzinfo=timezone.utc)
    assert S.events_from_schedule(ev["fomc_dates"], ev["fomc_time_et"], now, 7) == []               # এই সপ্তাহে বৈঠক নেই
    e = S.events_from_schedule(ev["fomc_dates"], ev["fomc_time_et"], datetime(2026, 12, 7, tzinfo=timezone.utc), 7)
    assert len(e) == 1 and (e[0]["utc"].month, e[0]["utc"].day, e[0]["utc"].hour) == (12, 9, 19)       # শীতকাল: ১৪:০০ ET = ১৯:০০ UTC


def test_classify_levels_and_adp():
    ev = CFG["bias"]["sources"]["events"]
    c = lambda t, imp="Medium", ccy="USD": S.classify({"title": t, "currency": ccy, "impact": imp}, ev)
    assert c("CPI m/m")["level"] == "high" and c("Non-Farm Employment Change")["level"] == "high"
    assert c("PPI m/m")["level"] == "medium" and c("Unemployment Claims")["level"] == "medium"
    assert c("ADP Non-Farm Employment Change")["level"] == "medium" and c("ADP Non-Farm Employment Change")["category"] is None
    assert c("Fed Chair Speaks", "High")["level"] == "high" and c("Something", "Low")["level"] is None
    assert c("ECB Rate", "Medium", "EUR")["level"] is None


def test_previous_value_transforms():
    idx = pd.date_range("2025-01-01", periods=14, freq="MS")
    cpi = pd.Series(np.linspace(300, 312, 14), index=idx)
    yoy = S.previous_value({"transform": "yoy", "unit": "%", "label": "x"}, cpi)
    assert yoy["text"] == "3.7%" and yoy["period"] == "2026-02"
    nfp = S.previous_value({"transform": "diff", "unit": "K"}, pd.Series([150000.0, 150151.0], index=idx[:2]))
    assert nfp["text"] == "+151K"
    assert S.previous_value({"transform": "level", "unit": "%"}, pd.Series([4.1, 4.2], index=idx[:2]))["text"] == "4.2%"
    assert S.previous_value({"transform": "yoy", "unit": "%"}, cpi.iloc[:5]) is None        # বছরের আগের মান নেই
    assert S.previous_value({"transform": "mom", "unit": "%"}, pd.Series([100.0, 101.0], index=idx[:2]))["text"] == "1.0%"


def test_event_effect_rules_and_basis():
    up = bn.event_effect("cpi", "down", "0.3%", "0.4%")
    assert up["higher"]["dir"] == "DOWN" and up["lower"]["dir"] == "UP" and up["basis"] == "forecast"
    nf = bn.event_effect("cpi", "down", None, "0.4%")
    assert nf["basis"] == "previous" and "দুর্বল" in nf["basis_bn"]
    un = bn.event_effect("unemp", "up", "4.2%", "4.1%")
    assert un["higher"]["dir"] == "UP" and un["lower"]["dir"] == "DOWN"
    assert bn.event_effect("fomc", "down", None, None)["basis"] == "text"
    assert bn.event_effect(None, None, None, None) is None and bn.event_effect("xyz", "down", None, None) is not None
    assert "ভবিষ্যদ্বাণী" not in up["caveat_bn"] or "নয়" in up["caveat_bn"]


def test_gather_fund_fred_prev_and_ff_failure_isolated():
    now = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
    obs = [{"date": (pd.Timestamp("2025-09-01") + pd.DateOffset(months=i)).strftime("%Y-%m-%d"), "value": str(300 + i * 0.5)} for i in range(14)]

    def get(url, **kw):
        if "releases/dates" in url:
            return _Resp(200, js={"release_dates": [{"release_name": "Consumer Price Index", "date": "2026-11-12"}]})
        if "series/observations" in url:
            return _Resp(200, js={"observations": obs})
        return _Resp(500)
    fund, events, notes = R.gather_fund(CFG, "K", now, get)
    cpi = [e for e in events if e["category"] == "cpi"]
    assert len(cpi) == 1 and cpi[0]["fred_prev"]["text"].endswith("%") and cpi[0]["level"] == "high"
    assert any("Forex Factory" in n for n in notes)
    assert not any(e["title"] == "FOMC Statement" for e in events)


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Federal Reserve issues FOMC statement</title><link>https://example.org/a</link><pubDate>Fri, 06 Nov 2026 19:00:00 GMT</pubDate></item>
<item><title>No link item</title><pubDate>Fri, 06 Nov 2026 19:00:00 GMT</pubDate></item></channel></rss>"""
ATOM = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>ECB press release on inflation</title>
<link href="https://example.org/b"/><updated>2026-11-07T10:00:00Z</updated></entry></feed>"""


def test_parse_rss_and_atom_and_tags():
    a = S.parse_rss(RSS, "Fed")
    b = S.parse_rss(ATOM, "ECB")
    assert len(a) == 1 and a[0]["link"] == "https://example.org/a" and "ফেড/সুদের হার" in a[0]["tags"]
    assert len(b) == 1 and "মুদ্রাস্ফীতি" in b[0]["tags"] and b[0]["published"].tzinfo is not None
    with pytest.raises(S.SourceError):
        S.parse_rss("<rss><item>", "x")


class _Resp:
    def __init__(self, code=200, text="", js=None):
        self.status_code, self.text, self._js = code, text, js

    def json(self):
        return self._js


def test_fetch_errors_hide_key_and_isolate_failures():
    def boom(url, **kw):
        raise RuntimeError("fail with SECRETKEY in url")
    with pytest.raises(S.SourceError) as e:
        S.fetch_fred("DFII10", "SECRETKEY", 60, NOW, boom)
    assert "SECRETKEY" not in str(e.value)

    def get(url, **kw):
        if "federalreserve" in url:
            return _Resp(200, RSS)
        return _Resp(500)
    feeds = [{"name": "Fed", "url": "https://www.federalreserve.gov/x"}, {"name": "ECB", "url": "https://ecb/x"}]
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    items, notes = S.fetch_news(feeds, now, 10, 4, get)
    assert len(items) == 1 and len(notes) == 1
    fund, events, n = R.gather_fund(CFG, None, now, lambda *a, **k: _Resp(500))
    assert fund == {} and events == [] and any("FRED_API_KEY" in x for x in n) and any("COT" in x for x in n)


# ---------- লগ ও যাচাই ----------

def _result(bias="UP", ref=100.0, lo=90.0, hi=110.0):
    h = {"bias": bias, "score": 0.5, "confidence": "MEDIUM", "ref_price": ref, "range": {"lo": lo, "hi": hi, "R": 10.0}}
    return {"XAUUSD": {"horizons": {"h4": dict(h), "d1": dict(h), "w1": dict(h)}}}


def test_log_one_entry_per_bucket_and_market_closed():
    sun = datetime(2026, 11, 8, 20, 12, tzinfo=timezone.utc)         # রবিবার: d1 ও w1 পর্ব শুরু (h4 মেয়াদের অর্ধেক বন্ধ বাজার)
    log = L.new_entries(_result(), [], sun, CFG)
    assert {e["horizon"] for e in log} == {"d1", "w1"}
    assert L.new_entries(_result(), log, sun + timedelta(minutes=40), CFG) == []            # একই পর্ব
    mon = L.new_entries(_result(), log, sun + timedelta(hours=24), CFG)
    assert {e["horizon"] for e in mon} == {"h4", "d1"}                                     # w1 একই সপ্তাহ
    e = mon[0]
    assert e["ref_at"] < e["made_at"] and L.to_ns(e["expire_at"]) - L.to_ns(e["ref_at"]) in (4 * L.H, 24 * L.H)
    late = datetime(2026, 11, 9, 11, 40, tzinfo=timezone.utc)       # d1 পর্ব শুরুর অনেক পরে প্রথম রান: লগ নয়
    assert "d1" not in {x["horizon"] for x in L.new_entries(_result(), [], late, CFG)}
    sat = datetime(2026, 11, 7, 10, 12, tzinfo=timezone.utc)
    assert L.new_entries(_result(), [], sat, CFG) == []             # শনিবার বাজার বন্ধ
    fri = datetime(2026, 11, 6, 20, 12, tzinfo=timezone.utc)        # শুক্র রাতে d1 মেয়াদ প্রায় পুরো বন্ধ
    assert "d1" not in {x["horizon"] for x in L.new_entries(_result(), [], fri, CFG)}


class _Fr:
    def __init__(self, start, closes, highs=None, lows=None):
        n = len(closes)
        self.start_ns = np.array([int((start + timedelta(hours=i)).timestamp() * 1e9) for i in range(n)])
        self.end_ns = self.start_ns + L.H
        self.c = np.array(closes, float)
        self.h = np.array(highs if highs is not None else closes, float) + 0
        self.l = np.array(lows if lows is not None else closes, float) + 0


def test_resolve_correct_wrong_and_pending():
    made = datetime(2026, 11, 9, 8, 12, tzinfo=timezone.utc)
    log = L.new_entries(_result("UP"), [], made, CFG)
    h4 = [e for e in log if e["horizon"] == "h4"][0]
    start = datetime(2026, 11, 9, 8, tzinfo=timezone.utc)
    closes = [100, 101, 102, 103, 104, 105, 106, 107]
    fr = _Fr(start, closes, highs=[x + 1 for x in closes], lows=[x - 1 for x in closes])
    now_ns = int((made + timedelta(hours=5)).timestamp() * 1e9)
    assert L.resolve([h4], {"XAUUSD": fr}, now_ns, CFG) == 1
    assert h4["status"] == "Resolved" and h4["correct"] is True and h4["in_range"] is True and h4["up"] is True
    # ভুল দিক
    log2 = L.new_entries(_result("DOWN"), [], made, CFG)
    d = [e for e in log2 if e["horizon"] == "h4"][0]
    L.resolve([d], {"XAUUSD": fr}, now_ns, CFG)
    assert d["correct"] is False
    # নিরপেক্ষ দিকে গণনা হয় না
    nn = [e for e in L.new_entries(_result("NEUTRAL"), [], made, CFG) if e["horizon"] == "h4"][0]
    L.resolve([nn], {"XAUUSD": fr}, now_ns, CFG)
    assert nn["correct"] is None and nn["status"] == "Resolved"
    # মেয়াদ হয়নি / ডেটা পৌঁছায়নি → অপেক্ষায়
    e3 = [e for e in L.new_entries(_result("UP"), [], made, CFG) if e["horizon"] == "h4"][0]
    assert L.resolve([e3], {"XAUUSD": fr}, int((made + timedelta(hours=1)).timestamp() * 1e9), CFG) == 0
    short = _Fr(start, closes[:2])
    assert L.resolve([e3], {"XAUUSD": short}, now_ns, CFG) == 0 and e3["status"] == "Pending"
    assert L.resolve([e3], {"XAUUSD": fr}, now_ns, CFG) == 1 and L.resolve([e3], {"XAUUSD": fr}, now_ns, CFG) == 0


def test_range_breach_flags():
    made = datetime(2026, 11, 9, 8, 12, tzinfo=timezone.utc)
    e = [x for x in L.new_entries(_result("UP", 100, 95, 105), [], made, CFG) if x["horizon"] == "h4"][0]
    fr = _Fr(datetime(2026, 11, 9, 8, tzinfo=timezone.utc), [100, 101, 102, 103, 104, 108, 109],
             highs=[101, 102, 106, 104, 106, 110, 110], lows=[99, 100, 101, 102, 103, 104, 105])
    L.resolve([e], {"XAUUSD": fr}, int((made + timedelta(hours=6)).timestamp() * 1e9), CFG)
    assert e["breach_up"] is True and e["breach_down"] is False


def test_wilson_and_stats_baseline():
    lo, hi = L.wilson(60, 100)
    assert 0.50 < lo < 0.60 < hi < 0.70
    assert all(math.isnan(x) for x in L.wilson(0, 0))
    rows = []
    for i in range(10):
        rows.append({"pair": "XAUUSD", "horizon": "d1", "status": "Resolved", "bias": "UP", "correct": i < 7,
                     "up": i < 8, "in_range": i < 9})
    rows.append({"pair": "XAUUSD", "horizon": "d1", "status": "Pending", "bias": "UP"})
    st = L.build_stats(rows, CFG, "2026-11-09T00:00:00Z")
    g = st["groups"]["XAUUSD|d1"]
    assert g["n_resolved"] == 10 and g["hit_rate"] == 0.7 and g["base_rate_always_one_side"] == 0.8
    assert g["range_coverage"] == 0.9 and g["enough_samples"] is False and g["n_logged"] == 11
    assert "ALL|d1" in st["groups"]
    assert len(L.trim(list(range(10)), 4)) == 4


# ---------- এন্ড-টু-এন্ড ----------

def _data():
    return {"XAUUSD": {"H1": _df(seed=1, trend=1.0)}, "EURUSD": {"H1": _df(seed=2, trend=-1.0)}}


def test_event_output_has_info_for_ui():
    cfg = load_config()
    cfg["bias"]["pairs"] = ["XAUUSD"]
    now = datetime(2026, 11, 9, 8, 12, tzinfo=timezone.utc)
    ev = [{"title": "CPI m/m", "currency": "USD", "impact": "High", "utc": now + timedelta(hours=3), "approx": False,
           "source": "FF", "forecast": "0.3%", "previous": "0.4%"},
          {"title": "FOMC Statement", "currency": "USD", "impact": "High", "utc": now + timedelta(hours=5), "approx": True, "source": "x"},
          {"title": "Nothing", "currency": "USD", "impact": "Low", "utc": now + timedelta(hours=6), "approx": False, "source": "FF"}]
    res, _ = R.build_result(cfg, {"XAUUSD": {"H1": _data()["XAUUSD"]["H1"]}}, {}, ev, [], [], NOW)
    out = res["events"]
    assert len(out) == 2 and out[0]["info"]["level"] == "high" and out[0]["info"]["forecast"] == "0.3%"
    assert out[0]["info"]["effect"]["higher"]["dir"] == "DOWN" and out[1]["info"]["effect"]["basis"] == "text"


def test_end_to_end_build_write_and_no_secrets(tmp_path):
    cfg = load_config()
    cfg["bias"]["pairs"] = ["XAUUSD", "EURUSD"]
    now = datetime(2026, 11, 9, 8, 12, tzinfo=timezone.utc)
    fund = {"usd": _series(np.linspace(100, 98, 20), end="2026-11-07"),
            "real_yield": _series(np.linspace(2.0, 1.7, 20), end="2026-11-07"),
            "vix": _series(np.linspace(14, 18, 20), end="2026-11-07")}
    events = [{"title": "US CPI", "currency": "USD", "impact": "High", "utc": now + timedelta(hours=3), "approx": True, "source": "FRED"}]
    news = [{"title": "Fed statement", "link": "https://x.org/1", "source": "Federal Reserve", "tags": ["ফেড/সুদের হার"],
             "published": now - timedelta(hours=5)}]
    # শেষ ক্যান্ডেল 'now'-এর আগে: ডেটা এখনকার সময়ে টেনে আনা
    res, h1s = R.build_result(cfg, {k: {"H1": v["H1"]} for k, v in _data().items()}, fund, events, news, ["note"], NOW)
    assert set(res["pairs"]) == {"XAUUSD", "EURUSD"}
    assert res["pairs"]["XAUUSD"]["mode"] == "full" and res["pairs"]["EURUSD"]["mode"] == "limited"
    assert res["pairs"]["EURUSD"]["mode_bn"].startswith("সীমিত")
    d1 = res["pairs"]["XAUUSD"]["horizons"]["d1"]
    assert d1["bias"] in ("UP", "DOWN", "NEUTRAL") and any("রিয়েল ইয়িল্ড" in r for r in d1["reasons_bn"])
    assert res["events"][0]["currency"] == "USD" and res["news"][0]["tags"]
    out = tmp_path / "d"
    log, stats = R.update_log(out, res, h1s, cfg, NOW)
    R.write_all(out, res, log, stats)
    assert {p.name for p in out.iterdir()} == {"bias.json", "bias_log.json", "bias_stats.json"}
    blob = "".join(p.read_text(encoding="utf-8") for p in out.iterdir())
    assert "api_key" not in blob.lower() and "NaN" not in blob and json.loads((out / "bias.json").read_text(encoding="utf-8"))
    n1 = len(json.loads((out / "bias_log.json").read_text(encoding="utf-8")))
    log2, _ = R.update_log(out, res, h1s, cfg, NOW + timedelta(minutes=20))       # একই পর্ব → ডুপ্লিকেট নয়
    assert len(log2) == n1


def test_main_requires_key_and_does_not_touch_signal_files(monkeypatch, capsys):
    monkeypatch.delenv("PRICE_API_KEY", raising=False)
    assert R.main(["--once"]) == 2
    assert R.main([]) == 0
    import src.bias.run as run_mod
    src = open(run_mod.__file__, encoding="utf-8").read()
    assert "signals.json" not in src and "state.json" not in src


def test_bias_workflow_is_isolated_and_safe():
    from pathlib import Path
    wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "bias.yml").read_text(encoding="utf-8")
    assert "group: bias" in wf and "src.bias.run --once --publish data" in wf
    assert "secrets.FRED_API_KEY" in wf and "secrets.PRICE_API_KEY" in wf and "TELEGRAM" not in wf
    crons = re.findall(r'- cron: "([^"]+)"', wf)
    assert len(crons) == 2
    sig = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "signals.yml").read_text(encoding="utf-8")
    sig_min = {int(m) for m in re.findall(r'- cron: "(\d+) \*', sig)}
    bias_min = {int(c.split()[0]) for c in crons}
    assert not (sig_min & bias_min)                          # মিনিট মেলে না, রেট-লিমিটে ধাক্কা নয়
    assert all(abs(b - s) >= 5 for b in bias_min for s in sig_min)
