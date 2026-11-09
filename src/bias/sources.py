"""ডেটা সোর্স: FRED, CFTC COT, ইভেন্ট ক্যালেন্ডার, খবরের RSS। প্রতিটি ব্যর্থ হলেও বাকিগুলো চলে।

নিয়ম:
- Key শুধু environment থেকে (FRED_API_KEY), কোডে বা আউটপুটে নয়। এরর বার্তা থেকে Key মুছে ফেলা হয়।
- পাবলিক রিপোতে কাঁচা ডেটা নয়: এখানে শুধু পার্স করা মান ফেরত আসে, লেখা হয় স্কোর/ইভেন্টের নাম-সময়।
- সব ফাংশন `get` (requests.get-এর মতো) নিতে পারে, যাতে টেস্টে নেটওয়ার্ক লাগে না।
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

NY = ZoneInfo("America/New_York")
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 arafat-fx-agent")


class SourceError(Exception):
    pass


def _get_json(get, url: str, params: dict | None, key: str | None, timeout: int = 20):
    try:
        r = get(url, params=params, timeout=timeout, headers={"User-Agent": "arafat-fx-agent"})
        if r.status_code != 200:
            raise SourceError(f"HTTP {r.status_code}")
        return r.json()
    except SourceError:
        raise
    except Exception as e:                                    # নেটওয়ার্ক/JSON ত্রুটি
        msg = str(e)
        if key:
            msg = msg.replace(key, "***")
        raise SourceError(f"{type(e).__name__}: {msg}"[:160]) from None


# ---------- FRED ----------

def parse_fred(payload: dict) -> pd.Series:
    """FRED observations → তারিখ-সূচক Series (মান "." হলে বাদ)। পুরনো থেকে নতুন।"""
    rows = {}
    for o in payload.get("observations", []):
        try:
            v = float(o["value"])
        except (ValueError, KeyError, TypeError):
            continue
        rows[pd.Timestamp(o["date"])] = v
    return pd.Series(rows, dtype=float).sort_index()


def fetch_fred(series_id: str, api_key: str, lookback_days: int, now: datetime, get=requests.get) -> pd.Series:
    start = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    p = {"series_id": series_id, "api_key": api_key, "file_type": "json", "observation_start": start}
    s = parse_fred(_get_json(get, "https://api.stlouisfed.org/fred/series/observations", p, api_key))
    if s.empty:
        raise SourceError(f"{series_id}: ডেটা খালি")
    return s


def fetch_fred_releases(api_key: str, now: datetime, days: int, get=requests.get) -> list[dict]:
    """আসন্ন রিলিজের তারিখ: [{name, date}] (সময় নেই, শুধু তারিখ)।"""
    p = {"api_key": api_key, "file_type": "json", "include_release_dates_with_no_data": "true",
         "realtime_start": now.strftime("%Y-%m-%d"), "realtime_end": "9999-12-31",
         "order_by": "release_date", "sort_order": "asc", "limit": 1000}
    js = _get_json(get, "https://api.stlouisfed.org/fred/releases/dates", p, api_key)
    out, end = [], (now + timedelta(days=days)).date()
    for r in js.get("release_dates", []):
        try:
            d = datetime.strptime(r["date"], "%Y-%m-%d").date()
        except (KeyError, ValueError):
            continue
        if now.date() <= d <= end:
            out.append({"name": r.get("release_name", ""), "date": d})
    return out


def events_from_fred(releases: list[dict], cfg_events: dict) -> list[dict]:
    """(FOMC এখানে নয়: FRED-এর FOMC তারিখ ভরসাযোগ্য নয়, তাই কনফিগের ক্যালেন্ডার ব্যবহার হয়) আগ্রহের রিলিজ বেছে আনুমানিক UTC সময়ে (নিউ ইয়র্কের সময় থেকে, DST মেনে) ইভেন্ট বানায়।"""
    out, seen = [], set()
    for r in releases:
        for part, spec in cfg_events["fred_releases"].items():
            if part.lower() in r["name"].lower():
                hh, mm = (int(x) for x in spec["time_et"].split(":"))
                local = datetime(r["date"].year, r["date"].month, r["date"].day, hh, mm, tzinfo=NY)
                key = (spec["label"], r["date"])
                if key in seen:
                    continue
                seen.add(key)
                out.append({"title": spec["label"], "currency": "USD", "impact": "High",
                            "utc": local.astimezone(timezone.utc), "approx": True, "source": "FRED"})
    return sorted(out, key=lambda e: e["utc"])


# ---------- Forex Factory (অনানুষ্ঠানিক ফিড; ফোরকাস্ট ও আগের মানের জন্য) ----------

def _short(v) -> str | None:
    t = str(v).strip() if v is not None else ""
    return t[:14] if t else None


def parse_ff(payload: list, min_impact_usd: str = "Medium") -> list[dict]:
    """নাম, মুদ্রা, সময়, গুরুত্ব, ফোরকাস্ট, আগের মান রাখে। USD-এর জন্য min_impact_usd+, অন্য মুদ্রায় শুধু High।"""
    rank = {"Low": 1, "Medium": 2, "High": 3}
    out = []
    for e in payload if isinstance(payload, list) else []:
        try:
            imp, ccy = e["impact"], str(e["country"])[:3].upper()
            need = rank[min_impact_usd] if ccy == "USD" else rank["High"]
            if rank.get(imp, 0) < need:
                continue
            t = datetime.fromisoformat(e["date"]).astimezone(timezone.utc)
            out.append({"title": str(e["title"])[:80], "currency": ccy, "impact": imp, "utc": t, "approx": False,
                        "source": "FF", "forecast": _short(e.get("forecast")), "previous": _short(e.get("previous"))})
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda e: e["utc"])


def fetch_ff(url: str, min_impact_usd: str, get=requests.get) -> list[dict]:
    return parse_ff(_get_json(get, url, None, None), min_impact_usd)


_CATS = [("cpi", ("cpi", "consumer price")), ("nfp", ("non-farm", "nonfarm", "employment situation", "payroll")),
         ("gdp", ("gdp", "gross domestic")), ("pce", ("pce", "personal income")), ("ppi", ("ppi", "producer price")),
         ("retail", ("retail sales",)), ("jolts", ("jolts", "job openings")), ("fomc", ("fomc", "federal funds rate")),
         ("unemp", ("unemployment rate", "unemployment claims", "jobless claims"))]


def event_category(title: str) -> str | None:
    t = title.lower()
    if "adp" in t:                                   # ADP বেসরকারি অনুমান, সরকারি NFP নয়
        return None
    for cat, keys in _CATS:
        if any(k in t for k in keys):
            return cat
    return None


def classify(e: dict, cfg_events: dict) -> dict:
    """ইভেন্টে category ও level (high = লাল, medium = কমলা) বসায়। না মিললে level = None।"""
    cat = event_category(e["title"])
    lv = cfg_events.get("levels", {})
    level = None
    if e.get("currency") == "USD" and cat in lv.get("high", []):
        level = "high"
    elif e.get("impact") == "High":
        level = "high"
    elif e.get("currency") == "USD" and (cat in lv.get("medium", []) or e.get("impact") == "Medium"):
        level = "medium"
    e["category"], e["level"] = cat, level
    return e


def events_from_schedule(dates: list[str], time_et: str, now: datetime, days: int) -> list[dict]:
    """FOMC-র সিদ্ধান্তের দিন (কনফিগের তালিকা থেকে), নিউ ইয়র্কের সময় থেকে UTC।"""
    hh, mm = (int(x) for x in time_et.split(":"))
    out, end = [], now + timedelta(days=days)
    for ds in dates:
        try:
            d = datetime.strptime(ds, "%Y-%m-%d")
        except ValueError:
            continue
        t = datetime(d.year, d.month, d.day, hh, mm, tzinfo=NY).astimezone(timezone.utc)
        if now - timedelta(hours=2) <= t <= end:
            out.append({"title": "FOMC Statement", "currency": "USD", "impact": "High", "utc": t, "approx": True,
                        "source": "Fed-ক্যালেন্ডার"})
    return out


def merge_events(primary: list[dict], extra: list[dict], now: datetime, days: int) -> list[dict]:
    """FRED/ক্যালেন্ডারের আনুমানিক ইভেন্ট (primary) + FF (extra)। FF-এ একই তারিখ ও ধরনের ইভেন্ট থাকলে আনুমানিকটা বাদ।"""
    end = now + timedelta(days=days)
    win = lambda e: now - timedelta(hours=2) <= e["utc"] <= end
    ff = [e for e in extra if win(e)]
    covered = {(event_category(e["title"]), e["utc"].date(), e["currency"]) for e in ff if event_category(e["title"])}
    out, seen = [], set()
    for e in ff + [x for x in primary if win(x)]:
        cat = event_category(e["title"])
        if e["approx"] and cat and (cat, e["utc"].date(), e["currency"]) in covered:
            continue
        k = (e["title"], e["utc"], e["currency"])
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return sorted(out, key=lambda e: e["utc"])


# ---------- আগের মান (FRED, হেডলাইন) ----------

def previous_value(spec: dict, s: pd.Series) -> dict | None:
    """সর্বশেষ প্রকাশিত মান, spec.transform অনুযায়ী (yoy / mom / diff / level)।"""
    s = s.dropna()
    if len(s) < 2:
        return None
    last_d, last = s.index[-1], float(s.iloc[-1])
    tr = spec["transform"]
    if tr == "yoy":
        target = last_d - pd.Timedelta(days=365)
        i = int(abs(s.index - target).argmin())
        if abs((s.index[i] - target).days) > 20 or float(s.iloc[i]) == 0:
            return None
        v = (last / float(s.iloc[i]) - 1) * 100
    elif tr == "mom":
        v = (last / float(s.iloc[-2]) - 1) * 100
    elif tr == "diff":
        v = last - float(s.iloc[-2])
    else:
        v = last
    unit = spec.get("unit", "")
    text = (f"{v:+,.0f}K" if unit == "K" else f"{v / 1000:.2f}M" if unit == "Mk" else f"{v:.1f}%" if unit == "%" else f"{v:.2f}")
    return {"text": text, "period": last_d.strftime("%Y-%m"), "label": spec.get("label", "")}


def fetch_previous(spec: dict, api_key: str, now: datetime, get=requests.get) -> dict | None:
    return previous_value(spec, fetch_fred(spec["id"], api_key, 500, now, get))


# ---------- CFTC COT (গোল্ড) ----------

def parse_cot(rows: list) -> list[dict]:
    """নতুন থেকে পুরনো: [{date, net, oi}] (ম্যানেজড মানি নেট = লং − শর্ট)।"""
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            out.append({"date": pd.Timestamp(r["report_date_as_yyyy_mm_dd"][:10]),
                        "net": float(r["m_money_positions_long_all"]) - float(r["m_money_positions_short_all"]),
                        "oi": float(r["open_interest_all"])})
        except (KeyError, ValueError, TypeError):
            continue
    return sorted(out, key=lambda x: x["date"], reverse=True)


def fetch_cot(url: str, code: str, rows: int, get=requests.get) -> list[dict]:
    p = {"$where": f"cftc_contract_market_code='{code}'", "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": rows}
    out = parse_cot(_get_json(get, url, p, None))
    if len(out) < 5:
        raise SourceError("COT সারি কম")
    return out


# ---------- খবরের RSS ----------

TAGS = [("ফেড/সুদের হার", ("fomc", "federal reserve", "interest rate", "rate decision", "monetary policy", "powell")),
        ("মুদ্রাস্ফীতি", ("inflation", "cpi", "price index", "pce")),
        ("চাকরি/শ্রমবাজার", ("employment", "payroll", "unemployment", "jobs", "labor")),
        ("প্রবৃদ্ধি", ("gdp", "growth", "retail sales")),
        ("ইসিবি/ইউরো", ("ecb", "euro area", "eurosystem", "lagarde"))]


def tag_headline(title: str) -> list[str]:
    t = title.lower()
    return [name for name, keys in TAGS if any(k in t for k in keys)]


def parse_rss(xml_text: str, source: str) -> list[dict]:
    """RSS 2.0 / Atom → [{title, link, published (UTC), source, tags}]। শুধু শিরোনাম ও লিংক রাখা হয়।"""
    if len(xml_text) > 2_000_000:
        raise SourceError("ফিড অতিরিক্ত বড়")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise SourceError(f"XML ত্রুটি: {e}"[:100]) from None
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        d = {c.tag.split("}")[-1]: c for c in it}
        title = (d["title"].text or "").strip() if "title" in d else ""
        link = (d["link"].text or d["link"].get("href") or "").strip() if "link" in d else ""
        raw = None
        for k in ("pubDate", "published", "updated", "date"):
            if k in d and d[k].text:
                raw = d[k].text.strip()
                break
        if not title or not link.startswith("http") or not raw:
            continue
        try:
            t = parsedate_to_datetime(raw) if not re.match(r"\d{4}-\d{2}-\d{2}", raw) else datetime.fromisoformat(raw.replace("Z", "+00:00"))
            t = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        items.append({"title": re.sub(r"\s+", " ", title)[:160], "link": link, "published": t, "source": source,
                      "tags": tag_headline(title)})
    return items


def fetch_news(feeds: list[dict], now: datetime, max_items: int, max_age_days: int, get=requests.get) -> tuple[list[dict], list[str]]:
    items, notes = [], []
    for f in feeds:
        try:
            r = get(f["url"], timeout=20, headers={"User-Agent": BROWSER_UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"})
            if r.status_code != 200:
                raise SourceError(f"HTTP {r.status_code}")
            items += parse_rss(r.text, f["name"])
        except Exception as e:
            notes.append(f"খবর ({f['name']}): {type(e).__name__}: {str(e)[:60]}")
    cut = now - timedelta(days=max_age_days)
    items = [i for i in items if cut <= i["published"] <= now + timedelta(hours=1)]
    items.sort(key=lambda i: i["published"], reverse=True)
    return items[:max_items], notes
