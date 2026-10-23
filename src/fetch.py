"""ডেটা আনা: Twelve Data থেকে শুধু H1 আনা হয়; H4 ও D1 আমরা H1 জুড়ে নিজে বানাই।

মূল নিয়ম (যাচাই করা, ২০২৬-১০-২৩):
- সময় সবসময় timezone=UTC (প্রোভাইডারের ডিফল্ট UTC নয়)।
- `datetime` = ক্যান্ডেলের শুরুর সময়। সর্বশেষ ক্যান্ডেল চলমান, তাই বাদ দেওয়া হয়।
- প্রোভাইডারের নিজস্ব H4/D1 গ্রিড স্থির নয় (আসল ডেটায় H4 সারিবদ্ধ পাওয়া যায়নি, D1 ২০:০০-২০:০০
  UTC), তাই H4 (০,৪,৮…) ও D1 (config-এর ঘণ্টা) আমরা H1 থেকে নির্দিষ্ট UTC গ্রিডে বানাই।
- একটা কলে সর্বোচ্চ ৫০০০ ক্যান্ডেল; D1-এর জন্য বেশি লাগলে end_date দিয়ে দ্বিতীয় পেজ।
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
                  session=None, retries: int = 3, timeout: float = 20, tz: str = "UTC",
                  end_date: datetime | None = None, check_align: bool = True) -> pd.DataFrame:
    """একটা সিম্বল ও টাইমফ্রেমের ক্যান্ডেল (চলমান সহ, UTC)। ব্যর্থ হলে FetchError।"""
    if not 1 <= bars <= 5000:
        raise ValueError("bars ১ থেকে ৫০০০ হতে হবে")
    session = session or requests.Session()
    params = {"symbol": symbol, "interval": INTERVALS[tf][0], "outputsize": bars, "timezone": tz}
    if end_date is not None:
        params["end_date"] = pd.Timestamp(end_date).strftime("%Y-%m-%dT%H:%M:%S")
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
            if check_align:
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


# ---------- H1 থেকে H4 ও D1 ----------

def fetch_h1(symbol: str, total: int, **common) -> pd.DataFrame:
    """H1 ক্যান্ডেল (চলমান সহ)। total > ৫০০০ হলে end_date দিয়ে দ্বিতীয় পেজ (আরও পুরনো ডেটা)।"""
    first = fetch_candles(symbol, "H1", min(total, 5000), **common)
    if total <= 5000 or len(first) < 5000:
        return first
    second = fetch_candles(symbol, "H1", total - 5000, end_date=first.index[0].to_pydatetime(), **common)
    both = pd.concat([second, first])
    return both[~both.index.duplicated(keep="last")].sort_index()


def aggregate(df: pd.DataFrame, period_hours: int, anchor_hour: int = 0, min_bars: int = 1) -> pd.DataFrame:
    """ক্লোজড ক্যান্ডেল জুড়ে বড় টাইমফ্রেম বানায় (যেমন H1 থেকে H4 বা D1)।

    - গ্রিড: anchor_hour (UTC) থেকে period_hours পর পর। H4: (4, 0), D1: (24, config-এর ঘণ্টা)।
    - ইতিহাসের প্রথম (অসম্পূর্ণ) গ্রুপ বাদ।
    - min_bars-এর কম ক্যান্ডেলের "টুকরো" গ্রুপ পাশের গ্রুপে জোড়া লাগে: সপ্তাহান্ত-বিরতির (২৪ ঘণ্টার বেশি
      ফাঁক) পরের টুকরো সামনের গ্রুপে, অন্যটা আগের গ্রুপে। সামনের গ্রুপ না থাকলে টুকরো বাদ।
    - ফেরত DataFrame-এ চলমান গ্রুপও থাকতে পারে; drop_running(…) দিয়ে বাদ দিন।
    """
    cols = [*OHLC, "bars"]
    if df.empty:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="datetime"), dtype=float)
    idx = df.index
    shift = pd.Timedelta(hours=anchor_hour)
    keys = pd.Series((idx - shift).floor(f"{period_hours}h") + shift, index=idx)
    order = list(dict.fromkeys(keys))
    counts = keys.value_counts()
    first_key = order[0]
    drop_mask = pd.Series(False, index=idx)
    for k in order:
        if counts[k] >= min_bars or k == first_key:
            continue
        members = idx[keys == k]
        pos = idx.get_loc(members[0])
        gap = members[0] - idx[pos - 1] if pos > 0 else pd.Timedelta(0)
        k_pos = order.index(k)
        if gap > pd.Timedelta(hours=24):      # সপ্তাহান্তের পরের টুকরো, সামনে জোড়া
            if k_pos + 1 < len(order):
                keys[keys == k] = order[k_pos + 1]
            else:
                drop_mask[keys == k] = True   # সামনের গ্রুপ এখনো আসেনি
        elif k_pos > 0:                        # আগের গ্রুপে জোড়া
            keys[keys == k] = order[k_pos - 1]
    keep = ~drop_mask & (keys != first_key)
    part, kk = df[keep], keys[keep]
    if part.empty:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC", name="datetime"), dtype=float)
    grp = part.groupby(kk.to_numpy())
    out = pd.DataFrame({
        "open": grp["open"].first(),
        "high": grp["high"].max(),
        "low": grp["low"].min(),
        "close": grp["close"].last(),
        "bars": grp["close"].count(),
    })
    out.index = pd.DatetimeIndex(out.index, name="datetime")
    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    return out.sort_index()


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
    """একটা পেয়ারের ক্লোজড ক্যান্ডেল: {টাইমফ্রেম: DataFrame}। শুধু H1 আনা হয়, বাকিগুলো বানানো।"""
    d = cfg["data"]
    symbol = cfg["pairs"][pair]["symbol"]
    common = dict(api_key=api_key, throttle=throttle, session=session,
                  retries=d["retries"], timeout=d["request_timeout_seconds"], tz=d["timezone"])
    total = d["d1_h1_bars"] if "D1" in due else d["h1_bars"]   # D1 লাগলে গভীর ইতিহাস
    h1 = drop_running(fetch_h1(symbol, total, **common), 60, now)
    out: dict[str, pd.DataFrame] = {"H1": h1}
    if "H4" in due:
        out["H4"] = drop_running(aggregate(h1, 4, 0, 1), 240, now)
    if "D1" in due:
        out["D1"] = drop_running(aggregate(h1, 24, d["d1_day_start_hour"], d["d1_min_h1_bars"]), 1440, now)
    for tf, df in out.items():
        if len(df) < d["min_bars"][tf]:
            raise FetchError(f"{pair} {tf}: ক্যান্ডেল কম ({len(df)} < {d['min_bars'][tf]})")
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


def _fmt_row(ts, r, dp: int, show_prices: bool) -> str:
    line = f"   {_fmt(ts)}  bars={int(r['bars'])}"
    if show_prices:
        line += f"  O={r['open']:.{dp}f} H={r['high']:.{dp}f} L={r['low']:.{dp}f} C={r['close']:.{dp}f}"
    return line


def run_diagnose(cfg: dict, api_key: str, pairs: list[str]) -> None:
    """প্রোভাইডারের নিজস্ব H4 ক্যান্ডেল কোন ঘণ্টায় শুরু হয় তার গণনা (দাম নেই)। ১ ক্রেডিট/পেয়ার।"""
    d = cfg["data"]
    throttle = Throttle(cfg["api_limits"]["sleep_between_requests_seconds"])
    print("\n-- প্রোভাইডারের নিজস্ব H4 (শুধু রোগনির্ণয়) --")
    for pair in pairs:
        try:
            df = fetch_candles(cfg["pairs"][pair]["symbol"], "H4", 1800, api_key, throttle,
                               retries=d["retries"], timeout=d["request_timeout_seconds"],
                               tz=d["timezone"], check_align=False)
        except FetchError as e:
            print(f"{pair}: ব্যর্থ ({clean(str(e), api_key)})")
            continue
        hist = df.index.hour.value_counts().sort_index()
        print(f"{pair}: {len(df)} ক্যান্ডেল, {_fmt(df.index[0])} থেকে {_fmt(df.index[-1])}")
        print("   শুরুর ঘণ্টা (UTC): " + ", ".join(f"{h:02d}h×{n}" for h, n in hist.items()))
        odd = df[df.index.hour % 4 != 0]
        if len(odd):
            print(f"   ০/৪/৮… গ্রিডের বাইরে: {len(odd)}টি, প্রথম {_fmt(odd.index[0])}, শেষ {_fmt(odd.index[-1])}")


def run_check(cfg: dict, api_key: str, pairs: list[str] | None, show_prices: bool,
              diagnose: bool = False) -> int:
    now = utcnow()
    started = time.monotonic()
    print(f"এখন (UTC): {_fmt(now)} | দাম দেখানো: {'হ্যাঁ' if show_prices else 'না'}")
    day0 = cfg["data"]["d1_day_start_hour"]
    data, errors, throttle = fetch_all(cfg, api_key, now, pairs=pairs, due=["H1", "H4", "D1"])
    for pair, tfs in data.items():
        dp = cfg["pairs"][pair]["decimals"]
        h1, h4, d1 = tfs["H1"], tfs["H4"], tfs["D1"]
        print(f"\n== {pair} ({cfg['pairs'][pair]['symbol']}) ==")
        print(f"H1: {len(h1)} ক্লোজড | প্রথম {_fmt(h1.index[0])} | শেষ {_fmt(h1.index[-1])}")
        print(f"H4 (বানানো, ০/৪/৮… UTC): {len(h4)} ক্লোজড | শেষ ক্যান্ডেল শুরু {_fmt(h4.index[-1])}")
        for ts, r in h4.tail(2).iterrows():
            print(_fmt_row(ts, r, dp, show_prices))
        print(f"D1 (বানানো, দিন শুরু {day0:02d}:00 UTC): {len(d1)} ক্লোজড")
        for ts, r in d1.tail(3).iterrows():
            print(_fmt_row(ts, r, dp, show_prices))
    for pair, err in errors.items():
        print(f"\n!! {pair}: {err}")
    if diagnose:
        run_diagnose(cfg, api_key, pairs or [p for p, v in cfg["pairs"].items() if v.get("enabled", True)][:1])
    print(f"\nমোট ক্রেডিট ব্যবহৃত: {throttle.credits_used} | সময়: {time.monotonic() - started:.0f} সেকেন্ড")
    print(f"সফল: {len(data)} | ব্যর্থ: {len(errors)}")
    return 1 if errors else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ডেটা ফেচ যাচাই")
    ap.add_argument("--check", action="store_true", help="সব পেয়ারের ডেটা এনে সারাংশ দেখাও")
    ap.add_argument("--pairs", help="কমা দিয়ে, যেমন XAUUSD,EURUSD (ডিফল্ট: সব)")
    ap.add_argument("--no-prices", action="store_true", help="দাম ছাপবে না (পাবলিক লগের জন্য)")
    ap.add_argument("--diagnose", action="store_true", help="প্রোভাইডারের নিজস্ব H4 গ্রিডের ঘণ্টা গণনা")
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
        return run_check(cfg, key, pairs, show_prices=not args.no_prices, diagnose=args.diagnose)
    except Exception as e:  # কখনো Key ফাঁস করবে না
        print(f"ত্রুটি: {type(e).__name__}: {clean(str(e), key)}"[:500], file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
