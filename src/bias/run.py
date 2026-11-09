"""বায়াস চালক। চালানো:  python -m src.bias.run --once [--publish DIR]

- PRICE_API_KEY আবশ্যক; FRED_API_KEY না থাকলে ফান্ডামেন্টাল বাদ, বায়াস "সীমিত" (চার্ট + যা আছে)।
- সিগন্যাল ইঞ্জিনের state/ফাইলে হাত দেয় না। শুধু bias.json, bias_log.json, bias_stats.json লেখে।
- কোনো সোর্স ব্যর্থ হলে বাকিগুলো চলে; কারণ data_notes-এ থাকে।
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from ..datastore import DataStore, _atomic
from ..fetch import FetchError, clean, fetch_all, load_config, utcnow
from ..strategy import build_frames
from . import bn, log as blog, model, sources


def gather_fund(cfg: dict, fred_key: str | None, now: datetime, get=requests.get) -> tuple[dict, list[dict], list[str]]:
    """ফান্ডামেন্টাল, ইভেন্ট, নোট। প্রতিটি সোর্স আলাদাভাবে নিরাপদ।"""
    sc = cfg["bias"]["sources"]
    fund, notes, events = {}, [], []
    if fred_key:
        for name, sid in sc["fred"]["series"].items():
            try:
                fund[name] = sources.fetch_fred(sid, fred_key, sc["fred"]["lookback_days"], now, get)
            except sources.SourceError as e:
                notes.append(f"FRED {name}: {e}")
        try:
            rel = sources.fetch_fred_releases(fred_key, now, sc["events"]["window_days"], get)
            events += sources.events_from_fred(rel, sc["events"])
            if not rel:
                notes.append("FRED রিলিজ-ক্যালেন্ডার: আসন্ন কোনো তারিখ মেলেনি")
        except sources.SourceError as e:
            notes.append(f"FRED ক্যালেন্ডার: {e}")
    else:
        notes.append("FRED_API_KEY নেই: রিয়েল ইয়িল্ড, ডলার, VIX ও ইভেন্ট-ক্যালেন্ডার বাদ")
    try:
        c = sc["cot"]
        fund["cot"] = sources.fetch_cot(c["url"], c["gold_code"], c["rows"], get)
    except sources.SourceError as e:
        notes.append(f"COT: {e}")
    ev = sc["events"]
    events += sources.events_from_schedule(ev.get("fomc_dates", []), ev.get("fomc_time_et", "14:00"), now, ev["window_days"])
    extra = []
    ff = ev["forex_factory"]
    if ff["enabled"]:
        try:
            extra = sources.fetch_ff(ff["url"], ff.get("min_impact_usd", "Medium"), get)
        except sources.SourceError as e:
            notes.append(f"Forex Factory (ফোরকাস্ট আসবে না): {e}")
    events = [sources.classify(e, ev) for e in sources.merge_events(events, extra, now, ev["window_days"])]
    if fred_key:
        cache: dict = {}
        for e in events:
            spec = ev.get("previous", {}).get(e.get("category"))
            if not spec or e["currency"] != "USD":
                continue
            if e["category"] not in cache:
                try:
                    cache[e["category"]] = sources.fetch_previous(spec, fred_key, now, get)
                except sources.SourceError as x:
                    cache[e["category"]] = None
                    notes.append(f"আগের মান ({e['category']}): {x}")
            e["fred_prev"] = cache[e["category"]]
    return fund, events, notes


def build_result(cfg: dict, data: dict, fund: dict, events: list[dict], news: list[dict], notes: list[str],
                 now: datetime) -> tuple[dict, dict]:
    """(bias.json-এর বিষয়বস্তু, {pair: Frame H1} যাচাইয়ের জন্য)।"""
    b = cfg["bias"]
    res_pairs, h1s = {}, {}
    ccys = {"USD", "EUR", "GBP", "JPY", "AUD", "CAD"}
    evc = b["sources"]["events"]
    for e in events:
        if "level" not in e:
            sources.classify(e, evc)
    shown_events = [e for e in events if e.get("level") in ("high", "medium") and e["currency"] in ccys][:14]
    for pair in b["pairs"]:
        if pair not in data:
            continue
        frames = build_frames(data[pair]["H1"], cfg, now)
        h1s[pair] = frames["H1"]
        pn: list[str] = []
        comps = {**model.chart_components(frames, cfg), **model.fund_components(fund, pair, cfg, now, pn)}
        full = pair in b["full_fundamentals"] and all(k in comps for k in ("real_yield", "usd"))
        res_pairs[pair] = {"mode": "full" if full else "limited", "mode_bn": bn.MODE_BN["full" if full else "limited"],
                           "notes": pn,
                           "horizons": {h: model.build_horizon(pair, h, comps, frames, cfg, now, shown_events,
                                                              "full" if full else "limited") for h in b["horizons"]}}
    off = cfg["notify"]["display_utc_offset_hours"]
    ev_out = []
    for e in shown_events:
        fp = e.get("fred_prev")
        prev = e.get("previous") or (fp["text"] if fp else None)
        info = {"level": e["level"], "level_bn": bn.LEVEL_BN[e["level"]], "forecast": e.get("forecast"), "previous": prev,
                "previous_src": "FF" if e.get("previous") else ("FRED" if fp else None),
                "fred_previous": ({"text": fp["text"], "period": fp["period"], "label": fp["label"]} if fp else None),
                "effect": bn.event_effect(e.get("category"), evc.get("gold_effect", {}).get(e.get("category")),
                                          e.get("forecast"), prev)}
        ev_out.append({"title": e["title"], "currency": e["currency"], "impact": e["impact"], "approx": e["approx"],
                       "source": e["source"], "utc": e["utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "local": (e["utc"] + timedelta(hours=off)).strftime("%d-%m %H:%M"), "info": info})
    news_out = [{"title": n["title"], "link": n["link"], "source": n["source"], "tags": n["tags"],
                 "published": n["published"].strftime("%Y-%m-%dT%H:%M:%SZ")} for n in news]
    out = {"version": 1, "bias_version": b["version"], "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "disclaimer_bn": bn.DISCLAIMER_BN, "horizon_names_bn": bn.HORIZON_BN, "tz_offset_hours": off,
           "pairs": res_pairs, "events": ev_out, "news": news_out, "data_notes": notes}
    return out, h1s


def update_log(out_dir: Path, result: dict, h1s: dict, cfg: dict, now: datetime) -> tuple[list, dict]:
    p = out_dir / "bias_log.json"
    log = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    blog.resolve(log, h1s, int(now.timestamp() * 1e9), cfg)
    log += blog.new_entries(result["pairs"], log, now, cfg)
    log = blog.trim(log, cfg["bias"]["log"]["max_entries"])
    stats = blog.build_stats(log, cfg, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return log, stats


def write_all(out_dir: Path, result: dict, log: list, stats: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic(out_dir / "bias.json", result)
    _atomic(out_dir / "bias_log.json", log)
    _atomic(out_dir / "bias_stats.json", stats)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="মার্কেট বায়াস (শ্যাডো মোড)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--publish", metavar="DIR", help="data ব্রাঞ্চের ফোল্ডার: JSON লিখে কমিট+পুশ")
    ap.add_argument("--out", metavar="DIR", default="state/bias", help="লোকাল রানে লেখার ফোল্ডার (--publish না দিলে)")
    ap.add_argument("--pairs", help="কমা দিয়ে পেয়ার (ডিফল্ট: config)")
    a = ap.parse_args(argv)
    if not a.once:
        ap.print_help()
        return 0
    cfg = load_config()
    key = os.environ.get("PRICE_API_KEY", "").strip()
    fred = os.environ.get("FRED_API_KEY", "").strip() or None
    if not key:
        print("PRICE_API_KEY পাওয়া যায়নি।", file=sys.stderr)
        return 2
    if a.pairs:
        cfg["bias"]["pairs"] = [p.strip().upper() for p in a.pairs.split(",") if p.strip()]
    try:
        now = utcnow()
        data, errors, thr = fetch_all(cfg, key, now=now, pairs=cfg["bias"]["pairs"], due=["H1", "H4", "D1"])
        fund, events, notes = gather_fund(cfg, fred, now)
        sc = cfg["bias"]["sources"]["news"]
        news, nn = sources.fetch_news(sc["feeds"], now, sc["max_items"], sc["max_age_days"])
        notes += nn + [f"{p}: {m}" for p, m in errors.items()]
        if fred:
            notes = [clean(n, fred) for n in notes]
        result, h1s = build_result(cfg, data, fund, events, news, notes, now)
        if not result["pairs"]:
            raise FetchError("কোনো পেয়ারের ডেটা পাওয়া যায়নি")
        out_dir = Path(a.publish or a.out)
        log, stats = update_log(out_dir, result, h1s, cfg, now)
        write_all(out_dir, result, log, stats)
        if a.publish:
            DataStore(a.publish).commit_push(f"bias: {result['generated_at']}")
    except Exception as e:
        msg = clean(str(e), key)
        msg = clean(msg, fred) if fred else msg
        print(f"ত্রুটি: {type(e).__name__}: {msg}"[:500], file=sys.stderr)
        return 1
    for p, v in result["pairs"].items():
        print(p, "|", v["mode"], "|", " | ".join(h["summary_bn"] for h in v["horizons"].values()))
    print(f"নোট: {len(notes)} | লগ: {len(log)} | API ক্রেডিট: {thr.credits_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
