"""ডেটা আনা: Twelve Data থেকে H1/H4 ক্যান্ডেল, H4 থেকে D1 বানানো।

মূল নিয়ম (যাচাই করা, ২০২৬-১০-২৩):
- সময় সবসময় timezone=UTC (প্রোভাইডারের ডিফল্ট UTC নয়)।
- `datetime` = ক্যান্ডেলের শুরুর সময়। সর্বশেষ ক্যান্ডেল চলমান, তাই বাদ দেওয়া হয়।
- প্রোভাইডারের D1 ২০:০০-২০:০০ UTC, তাই D1 আমরা H4 জুড়ে নিজে বানাই।
- API Key কখনো URL-এ যায় না (Authorization হেডারে), লগেও ছাপা হয় না।
- ১ রিকোয়েস্ট = ১ ক্রেডিট; মিনিটে ৮, দিনে ৮০০।

এক কমান্ডে যাচাই:  python -m src.fetch --check
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
import yaml

API_URL = "https://api.twelvedata.com/time_series"
INTERVALS = {"H1": ("1h", 60), "H4": ("4h", 240)}  # টাইমফ্রেম -> (API নাম, মিনিট)
OHLC = ["open", "high", "low", "close"]
ROOT = Path(__file__).resolve().parent.parent


class FetchError(Exception):
    """ডেটা আনতে ব্যর্থ। retryable=True হলে আবার চেষ্টা করা যায়।"""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class RateLimitError(FetchError):
    def __init__(self, message: str):
        super().__init__(message, retryable=True)


def load_config(path: str | Path | None = None) -> dict:
    with open(path or ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def clean(text: str, secret: str | None) -> str:
    """বার্তা থেকে গোপন Key মুছে দেয়।"""
    return text.replace(secret, "***") if secret else text


class Throttle:
    """রিকোয়েস্টের মাঝে ন্যূনতম বিরতি রাখে এবং ব্যবহৃত ক্রেডিট গোনে।"""

    def __init__(self, min_interval: float, sleep=time.sleep, clock=time.monotonic):
        self.min_interval = min_interval
        self.sleep = sleep
        self.clock = clock
        self.credits_used = 0
        self._last: float | None = None

    def wait(self) -> None:
        if self._last is not None:
            gap = self.clock() - self._last
            if gap < self.min_interval:
                self.sleep(self.min_interval - gap)
        self._last = self.clock()

    def count(self) -> None:
        self.credits_used += 1


# ---------- ক্যান্ডেল পার্স ও যাচাই ----------

def parse_response(payload: object) -> pd.DataFrame:
    """API-র JSON থেকে UTC-সূচকযুক্ত, পুরনো→নতুন ক্রমে OHLC DataFrame।"""
    if not isinstance(payload, dict):
        raise FetchError("অপ্রত্যাশিত উত্তর (JSON অবজেক্ট নয়)", retryable=True)
    if payload.get("status") == "error" or ("code" in payload and "values" not in payload):
        code = payload.get("code")
        msg = str(payload.get("message", ""))[:200]
        if code == 429:
            raise RateLimitError(f"API লিমিট ({code}): {msg}")
        raise FetchError(f"API এরর {code}: {msg}", retryable=(code == 500))
    values = payload.get("values")
    if not values:
        raise FetchError("কোনো ক্যান্ডেল আসেনি")
    df = pd.DataFrame(values)
    missing = [c for c in ["datetime", *OHLC] if c not in df.columns]
    if missing:
        raise FetchError(f"কলাম নেই: {missing}")
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for c in OHLC:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if df[OHLC].isna().any().any():
        raise FetchError("ক্যান্ডেলে সংখ্যা নয় এমন মান আছে")
    df = df.drop_duplicates("datetime").sort_values("datetime").set_index("datetime")
    if (df["high"] < df["low"]).any():
        raise FetchError("ক্যান্ডেল অসঙ্গত: high < low")
    return df[OHLC]


def check_alignment(df: pd.DataFrame, tf: str) -> None:
    """ক্যান্ডেলের শুরুর সময় টাইমফ্রেমের সীমানায় না পড়লে ব্যর্থ (টাইমজোন ভুলের সংকেত)।"""
    step = INTERVALS[tf][1]
    minutes = df.index.hour * 60 + df.index.minute
    if (minutes % step != 0).any():
        raise FetchError(f"{tf} ক্যান্ডেলের সময় সারিবদ্ধ নয় (টাইমজোন যাচাই করুন)")


def drop_running(df: pd.DataFrame, minutes: int, now: datetime) -> pd.DataFrame:
    """যে ক্যান্ডেলের শেষ হওয়ার সময় এখনো আসেনি (চলমান), সেটা বাদ।"""
    ends = df.index + pd.Timedelta(minutes=minutes)
    return df[ends <= pd.Timestamp(now)]


# ---------- নেটওয়ার্ক ----------

def fetch_candles(symbol: str, tf: str, bars: int, api_key: str, throttle: Throttle,
                  session=None, retries: int = 3, timeout: float = 20, tz: str = "UTC") -> pd.DataFrame:
    """একটা সিম্বল ও টাইমফ্রেমের ক্যান্ডেল (চলমান সহ, UTC)। ব্যর্থ হলে FetchError।"""
    if not 1 <= bars <= 5000:
        raise ValueError("bars ১ থেকে ৫০০০ হতে হবে")
    session = session or requests.Session()
    params = {"symbol": symbol, "interval": INTERVALS[tf][0], "outputsize": bars, "timezone": tz}
    headers = {"Authorization": f"apikey {api_key}"}
    last_err = "অজানা"
    for attempt in range(retries + 1):
        throttle.wait()
        try:
            resp = session.get(API_URL, params=params, headers=headers, timeout=timeout)
            throttle.count()
            if resp.status_code >= 500:
                raise FetchError(f"সার্ভার এরর {resp.status_code}", retryable=True)
            df = parse_response(resp.json())
            check_alignment(df, tf)
            return df
        except RateLimitError as e:
            last_err = str(e)
            throttle.sleep(61)  # মিনিটের জানালা পার হতে দিই
        except FetchError as e:
            if not e.retryable:
                raise FetchError(clean(str(e), api_key)) from None
            last_err = str(e)
            throttle.sleep(2 ** attempt)
        except (requests.RequestException, ValueError) as e:
            last_err = f"{type(e).__name__}: {clean(str(e), api_key)}"[:200]
            throttle.sleep(2 ** attempt)
    raise FetchError(f"{symbol} {tf}: {retries + 1} বার চেষ্টার পরও ব্যর্থ ({last_err})")


# ---------- H4 থেকে D1 ----------

def build_d1(h4: pd.DataFrame, day_start_hour: int = 0, min_h4_bars: int = 2) -> pd.DataFrame:
    """ক্লোজড H4 ক্যান্ডেল জুড়ে D1 বানায়। দিন শুরু `day_start_hour` UTC-তে।

    - ইতিহাসের প্রথম (অসম্পূর্ণ) দিন বাদ।
    - কম ক্যান্ডেলের টুকরো দিন (সপ্তাহান্তের আগে-পরে) পাশের দিনে জুড়ে যায়:
      সপ্তাহান্ত-বিরতির পরের টুকরো সামনের দিনে, অন্যটা আগের দিনে।
    - ফেরত DataFrame-এ চলমান দিনও থাকতে পারে; drop_running(…, 1440, now) দিয়ে বাদ দিন।
    """
    if h4.empty:
        return h4.copy()
    idx = h4.index
    shift = pd.Timedelta(hours=day_start_hour)
    keys = pd.Series((idx - shift).floor("D") + shift, index=idx)
    order = list(dict.fromkeys(keys))
    counts = keys.value_counts()
    first_key = order[0]
    drop_mask = pd.Series(False, index=idx)
    for k in order:
        if counts[k] >= min_h4_bars or k == first_key:
            continue
        members = idx[keys == k]
        pos = idx.get_loc(members[0])
        gap = members[0] - idx[pos - 1] if pos > 0 else pd.Timedelta(0)
        k_pos = order.index(k)
        if gap > pd.Timedelta(hours=24):  # সপ্তাহান্তের পরের টুকরো, সামনে জোড়া
            if k_pos + 1 < len(order):
                keys[keys == k] = order[k_pos + 1]
            else:
                drop_mask[keys == k] = True  # সামনের দিন এখনো আসেনি
        elif k_pos > 0:                      # আগের দিনে জোড়া
            keys[keys == k] = order[k_pos - 1]
    keep = ~drop_mask & (keys != first_key)
    h4k, kk = h4[keep], keys[keep]
    if h4k.empty:
        return h4k.copy()
    grp = h4k.groupby(kk.to_numpy())
    d1 = pd.DataFrame({
        "open": grp["open"].first(),
        "high": grp["high"].max(),
        "low": grp["low"].min(),
        "close": grp["close"].last(),
        "bars": grp["close"].count(),
    })
    d1.index = pd.DatetimeIndex(d1.index, name="datetime")
    if d1.index.tz is None:
        d1.index = d1.index.tz_localize("UTC")
    return d1.sort_index()


# ---------- সময়সূচি ও সম্পূর্ণ ফেচ ----------

def due_timeframes(now: datetime, cfg: dict) -> list[str]:
    """এই ঘণ্টায় কোন টাইমফ্রেম ক্লোজ হয়েছে: H1 প্রতিবার, H4 প্রতি ৪ ঘণ্টায়, D1 দিনের শুরুর ঘণ্টায়।"""
    due = ["H1"]
    if now.hour % 4 == 0:
        due.append("H4")
    if now.hour == cfg["data"]["d1_day_start_hour"]:
        due.append("D1")
    return due


def fetch_pair(cfg: dict, pair: str, due: list[str], api_key: str, throttle: Throttle,
               now: datetime, session=None) -> dict[str, pd.DataFrame]:
    """একটা পেয়ারের ক্লোজড ক্যান্ডেল: {টাইমফ্রেম: DataFrame}।"""
    d = cfg["data"]
    symbol = cfg["pairs"][pair]["symbol"]
    common = dict(api_key=api_key, throttle=throttle, session=session,
                  retries=d["retries"], timeout=d["request_timeout_seconds"], tz=d["timezone"])
    min_bars = d["min_bars"]
    out: dict[str, pd.DataFrame] = {}
    if "H1" in due:
        out["H1"] = drop_running(fetch_candles(symbol, "H1", d["h1_bars"], **common), 60, now)
    if "H4" in due or "D1" in due:
        h4 = drop_running(fetch_candles(symbol, "H4", d["h4_bars"], **common), 240, now)
        if "H4" in due:
            out["H4"] = h4
        if "D1" in due:
            d1 = build_d1(h4, d["d1_day_start_hour"], d.get("d1_min_h4_bars", 2))
            out["D1"] = drop_running(d1, 1440, now)
    for tf, df in out.items():
        if len(df) < min_bars[tf]:
            raise FetchError(f"{pair} {tf}: ক্যান্ডেল কম ({len(df)} < {min_bars[tf]})")
    return out


def fetch_all(cfg: dict, api_key: str, now: datetime | None = None, session=None,
              pairs: list[str] | None = None, due: list[str] | None = None):
    """সব সক্রিয় পেয়ার। ফেরত: (ডেটা, এরর, throttle)। এক পেয়ার ব্যর্থ হলে বাকিগুলো চলে।"""
    now = now or utcnow()
    due = due or due_timeframes(now, cfg)
    throttle = Throttle(cfg["api_limits"]["sleep_between_requests_seconds"])
    data, errors = {}, {}
    names = pairs or [p for p, v in cfg["pairs"].items() if v.get("enabled", True)]
    for pair in names:
        try:
            data[pair] = fetch_pair(cfg, pair, due, api_key, throttle, now, session)
        except FetchError as e:
            errors[pair] = clean(str(e), api_key)
    return data, errors, throttle


# ---------- এক কমান্ডে যাচাই ----------

def _fmt(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d %H:%M")


def run_check(cfg: dict, api_key: str, pairs: list[str] | None, show_prices: bool) -> int:
    now = utcnow()
    started = time.monotonic()
    print(f"এখন (UTC): {_fmt(now)} | দাম দেখানো: {'হ্যাঁ' if show_prices else 'না'}")
    day0 = cfg["data"]["d1_day_start_hour"]
    data, errors, throttle = fetch_all(cfg, api_key, now, pairs=pairs, due=["H1", "H4", "D1"])
    for pair, tfs in data.items():
        h1, h4 = tfs["H1"], tfs["H4"]
        print(f"\n== {pair} ({cfg['pairs'][pair]['symbol']}) ==")
        print(f"H1: {len(h1)} ক্লোজড | শেষ ক্যান্ডেল শুরু {_fmt(h1.index[-1])} UTC")
        print(f"H4: {len(h4)} ক্লোজড | শেষ ক্যান্ডেল শুরু {_fmt(h4.index[-1])} UTC | প্রথম {_fmt(h4.index[0])}")
        for start in sorted({0, 20, day0}):
            d1 = build_d1(h4, start, cfg["data"].get("d1_min_h4_bars", 2))
            d1 = drop_running(d1, 1440, now)
            tag = " (config)" if start == day0 else ""
            print(f"D1 (দিন শুরু {start:02d}:00 UTC{tag}): {len(d1)} ক্যান্ডেল")
            for ts, r in d1.tail(3).iterrows():
                line = f"   {_fmt(ts)}  bars={int(r['bars'])}"
                if show_prices:
                    dp = cfg["pairs"][pair]["decimals"]
                    line += f"  O={r['open']:.{dp}f} H={r['high']:.{dp}f} L={r['low']:.{dp}f} C={r['close']:.{dp}f}"
                print(line)
    for pair, err in errors.items():
        print(f"\n!! {pair}: {err}")
    print(f"\nমোট ক্রেডিট ব্যবহৃত: {throttle.credits_used} | সময়: {time.monotonic() - started:.0f} সেকেন্ড")
    print(f"সফল: {len(data)} | ব্যর্থ: {len(errors)}")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ডেটা ফেচ যাচাই")
    ap.add_argument("--check", action="store_true", help="সব পেয়ারের ডেটা এনে সারাংশ দেখাও")
    ap.add_argument("--pairs", help="কমা দিয়ে, যেমন XAUUSD,EURUSD (ডিফল্ট: সব)")
    ap.add_argument("--no-prices", action="store_true", help="দাম ছাপবে না (পাবলিক লগের জন্য)")
    args = ap.parse_args(argv)
    key = os.environ.get("PRICE_API_KEY", "").strip()
    if not key:
        print("PRICE_API_KEY পাওয়া যায়নি। README-র 'ডেটা যাচাই' অংশ দেখুন।", file=sys.stderr)
        return 2
    cfg = load_config()
    if not args.check:
        ap.print_help()
        return 0
    pairs = [p.strip().upper() for p in args.pairs.split(",")] if args.pairs else None
    try:
        return run_check(cfg, key, pairs, show_prices=not args.no_prices)
    except Exception as e:  # কখনো Key ফাঁস করবে না
        print(f"ত্রুটি: {type(e).__name__}: {clean(str(e), key)}"[:500], file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
