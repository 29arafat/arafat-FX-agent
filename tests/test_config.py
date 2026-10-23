"""কাঠামো যাচাই: config.yaml ঠিক আছে কিনা, আর তাতে গোপন তথ্য নেই কিনা।"""
from pathlib import Path
import re
import yaml

ROOT = Path(__file__).resolve().parent.parent
TEXT = (ROOT / "config.yaml").read_text(encoding="utf-8")
CFG = yaml.safe_load(TEXT)


def test_timezone_is_utc():
    assert CFG["data"]["timezone"] == "UTC"


def test_six_pairs_with_symbols():
    assert set(CFG["pairs"]) == {"XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"}
    for p in CFG["pairs"].values():
        assert "/" in p["symbol"] and p["decimals"] >= 2


def test_timeframes_and_rules():
    assert CFG["timeframes"] == ["H1", "H4", "D1"]
    assert set(CFG["rules"]["cooldown_candles"]) == {"H1", "H4", "D1"}
    assert CFG["rules"]["tracking_timeframe"] == "H1"


def test_d1_day_start_is_valid_hour():
    assert CFG["data"]["d1_day_start_hour"] in range(24)


def test_api_budget_fits_free_limit():
    # প্রতিদিন: H1 = পেয়ার×২৪ কল; D1-এর দিনে একটা বাড়তি পেজ (পেয়ার×১); H4 ও D1 H1 থেকে বানানো
    n = len(CFG["pairs"])
    daily = n * 24 + n * 1
    assert daily < CFG["api_limits"]["credits_per_day"]


def test_no_secrets_in_config():
    # দীর্ঘ Token-এর মতো লেখা বা পরিচিত কী-নাম থাকলে ফেল
    assert not re.search(r"\d{8,}:[A-Za-z0-9_-]{20,}", TEXT)
    assert not re.search(r"(?i)(api[_-]?key|token)\s*:\s*\S{12,}", TEXT)


def test_data_quality_settings():
    d = CFG["data"]
    assert set(d["min_bars"]) == {"H1", "H4", "D1"}
    assert d["min_bars"]["D1"] >= 250 and d["d1_min_h1_bars"] >= 2
    # সপ্তাহান্ত বাদে ৫/৭ ঘণ্টা থাকে; D1: দিনে ১টা, H4: দিনে ~৬টা
    assert d["d1_h1_bars"] / 24 * 5 / 7 >= d["min_bars"]["D1"] + 10
    assert d["h1_bars"] / 24 * 5 / 7 * 6 >= d["min_bars"]["H4"] + 200   # EMA200 + ওয়ার্মআপ
    assert d["drop_flat_candles"] is True
    assert d["d1_h1_bars"] <= 10000 and "h4_bars" not in d
