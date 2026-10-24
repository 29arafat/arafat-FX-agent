"""ব্যাকটেস্ট: ইতিহাসে strategy.py-র নিয়ম চালিয়ে ফলাফল (R-এ) দেখায়।

চালানো:  python -m backtest.backtest --years 3
- শুধু ক্লোজড ক্যান্ডেল, ভবিষ্যৎ দেখা নিষিদ্ধ (strategy.py-তে higher-TF ক্যান্ডেল শুধু শেষ হলে ব্যবহার)।
- দুইবার চলে: স্প্রেড ০ (gross) ও config-এর স্প্রেডসহ (net)।
- ইতিহাস ভাগ: শুরুর ৭০% (in-sample) ও শেষ ৩০% (out-of-sample)। টিউনিং করলে শুধু in-sample দেখে।
- রিপোর্টে কোনো কাঁচা দাম নেই (পাবলিক লগ/আর্টিফ্যাক্ট নিরাপদ)।
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from src.fetch import FetchError, Throttle, clean, clean_h1, fetch_h1_history, load_config, utcnow
from src.stats import summarize
from src.strategy import (NS_PER_MIN, TF_MINUTES, build_frames, cooldown_ns, evaluate, friday_blocked)
from src.tracker import find_exit, result_r

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "cache"
OUT = ROOT / "output"


# ---------- ইতিহাস ----------

def load_history(pair: str, cfg: dict, api_key: str | None, throttle: Throttle,
                 years: float, refresh: bool = False) -> pd.DataFrame:
    """কাঁচা H1 ইতিহাস। ক্যাশ থাকলে (এবং --refresh না থাকলে) সেটাই, নইলে API থেকে।"""
    path = CACHE / f"{pair}_H1.csv.gz"
    if path.exists() and not refresh:
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index, utc=True)
        df.index.name = "datetime"
        return df
    if not api_key:
        raise FetchError("PRICE_API_KEY নেই এবং ক্যাশও নেই")
    b, d = cfg["backtest"], cfg["data"]
    df = fetch_h1_history(cfg["pairs"][pair]["symbol"], int(years * 8800), page_bars=b["page_bars"],
                          max_pages=b["max_pages"], api_key=api_key, throttle=throttle,
                          retries=d["retries"], timeout=d["request_timeout_seconds"], tz=d["timezone"])
    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, compression="gzip")
    return df


# ---------- সিমুলেশন ----------

def simulate(pair: str, tf: str, frames: dict, cfg: dict, spread: float) -> dict:
    """এক পেয়ার × এক টাইমফ্রেম। ক্রমানুসারে; চলমান ট্রেড বা কুলডাউনে নতুন সিগন্যাল নেই।"""
    trig = frames[cfg["strategy"]["tf_map"][tf]["trigger"]]
    h1 = frames["H1"]
    warm = cfg["backtest"]["warmup_bars"]
    cutoff = cfg["rules"]["friday_cutoff_utc"]
    cool = cooldown_ns(tf, cfg)
    trades, unresolved, free = [], 0, -1
    for i in range(warm, len(trig)):
        t = int(trig.end_ns[i])
        if t < free or friday_blocked(t, cutoff):
            continue
        sig = evaluate(pair, tf, frames, i, cfg)
        if sig is None:
            continue
        k0 = int(np.searchsorted(h1.start_ns, t, side="left"))
        ex = find_exit(sig.direction, sig.sl, sig.tp, h1.h[k0:], h1.l[k0:], spread)
        if ex is None:
            unresolved += 1          # ডেটা শেষ, ট্রেড এখনো খোলা: পরিসংখ্যানে নয়
            break
        k, status, amb = ex
        exit_ns = int(h1.end_ns[k0 + k])
        free = exit_ns + cool
        trades.append({"pair": pair, "tf": tf, "dir": sig.direction, "t": t, "exit_t": exit_ns,
                       "status": status, "ambiguous": amb, "rr": sig.rr, "score": sig.score,
                       "r": result_r(sig.direction, sig.entry, sig.sl, sig.tp, status, spread)})
    return {"trades": trades, "unresolved": unresolved}


def run_all(histories: dict[str, pd.DataFrame], cfg: dict, now, pairs: list[str]) -> dict:
    """ফেরত: {mode: {(pair, tf): {trades, unresolved}}}, ইতিহাসের সময়সীমা (ns)।"""
    res: dict = {"gross": {}, "net": {}}
    lo, hi = None, None
    for pair in pairs:
        h1 = clean_h1(histories[pair], cfg, now)
        frames = build_frames(h1, cfg, now)
        a, b = int(frames["H1"].start_ns[0]), int(frames["H1"].end_ns[-1])
        lo, hi = (a if lo is None else min(lo, a)), (b if hi is None else max(hi, b))
        sp = cfg["backtest"]["spread"][pair]
        for tf in cfg["timeframes"]:
            res["gross"][(pair, tf)] = simulate(pair, tf, frames, cfg, 0.0)
            res["net"][(pair, tf)] = simulate(pair, tf, frames, cfg, sp)
    return {"res": res, "span": (lo, hi)}


# ---------- পরিসংখ্যান ও রিপোর্ট ----------

def split_stats(trades: list[dict], cut_ns: int, min_trades: int) -> dict:
    trades = sorted(trades, key=lambda x: x["exit_t"])
    def s(sel):
        out = summarize([x["r"] for x in sel], min_trades)
        out["ambiguous"] = sum(1 for x in sel if x["ambiguous"])
        return out
    return {"all": s(trades), "is": s([x for x in trades if x["t"] < cut_ns]),
            "oos": s([x for x in trades if x["t"] >= cut_ns])}


def build_summary(run: dict, cfg: dict, pairs: list[str]) -> dict:
    lo, hi = run["span"]
    cut = int(lo + (hi - lo) * (1 - cfg["backtest"]["oos_fraction"]))
    mt = cfg["backtest"]["min_trades_for_confidence"]
    out = {"span_start": str(pd.Timestamp(lo)), "span_end": str(pd.Timestamp(hi)),
           "oos_start": str(pd.Timestamp(cut)), "pairs": pairs, "mt": mt, "modes": {}}
    for mode in ("gross", "net"):
        r = run["res"][mode]
        m = {"cells": {}, "by_tf": {}, "unresolved": sum(v["unresolved"] for v in r.values())}
        for (pair, tf), v in r.items():
            m["cells"][f"{pair}|{tf}"] = split_stats(v["trades"], cut, mt)
        for tf in cfg["timeframes"]:
            allt = [x for (p, t), v in r.items() if t == tf for x in v["trades"]]
            m["by_tf"][tf] = split_stats(allt, cut, mt)
        out["modes"][mode] = m
    return out


def _pf(x):
    return "∞" if x is None else f"{x:.2f}"


def _row(label: str, s: dict) -> str:
    flag = "" if s["reliable"] else " ⚠"
    return (f"| {label} | {s['trades']}{flag} | {s['win_rate']:.0f}% | {s['total_r']:+.1f} | "
            f"{s['expectancy']:+.2f} | {_pf(s['profit_factor'])} | {s['max_drawdown_r']:.1f} |")


HEAD = "| | ট্রেড | উইন রেট | মোট R | গড় R/ট্রেড | PF | সর্বোচ্চ ড্রডাউন (R) |\n|---|---|---|---|---|---|---|"


def render_report(sm: dict, cfg: dict) -> str:
    mt, tfs = sm["mt"], cfg["timeframes"]
    L = ["# ব্যাকটেস্ট রিপোর্ট", "",
         f"ডেটা: {sm['span_start'][:10]} → {sm['span_end'][:10]} (UTC)। Out-of-sample শুরু: {sm['oos_start'][:10]}।",
         f"পেয়ার: {', '.join(sm['pairs'])}। কোনো কাঁচা দাম এই রিপোর্টে নেই।", "",
         "**কীভাবে পড়বেন:** R = ঝুঁকির একক (SL হিট = −১R)। গড় R/ট্রেড (expectancy) ধনাত্মক ও স্থিতিশীল হলে "
         "ভালো। PF = মোট লাভ ÷ মোট লস (১-এর ওপরে ভালো)। ⚠ = ট্রেড " + str(mt) + "-এর কম, ফলাফল ভরসাযোগ্য নয়।", ""]
    for mode, title in (("net", "নেট (স্প্রেডসহ): আসল ফলাফলের কাছাকাছি"), ("gross", "গ্রস (স্প্রেড ছাড়া): শুধু তুলনার জন্য")):
        m = sm["modes"][mode]
        L += [f"## {title}", ""]
        for tf in tfs:
            c = m["by_tf"][tf]
            L += [f"### {tf}: সব পেয়ার একত্রে", "", HEAD,
                  _row("সব ডেটা", c["all"]), _row("In-sample (প্রথম ৭০%)", c["is"]),
                  _row("Out-of-sample (শেষ ৩০%)", c["oos"]), ""]
        L += [f"### পেয়ার × টাইমফ্রেম (Out-of-sample ও সব ডেটা)", "", HEAD]
        for pair in sm["pairs"]:
            for tf in tfs:
                c = m["cells"][f"{pair}|{tf}"]
                L += [_row(f"{pair} {tf} · সব", c["all"]), _row(f"{pair} {tf} · OOS", c["oos"])]
        L += ["", f"চলমান অবস্থায় ডেটা শেষ হওয়া (গণনার বাইরে): {m['unresolved']}", ""]
    notes = []
    net = sm["modes"]["net"]
    for tf in tfs:
        c = net["by_tf"][tf]
        if not c["all"]["reliable"]:
            notes.append(f"- {tf}: মোট ট্রেড {c['all']['trades']} (< {mt}), সিদ্ধান্ত নেওয়ার মতো নমুনা নেই।")
        elif c["is"]["expectancy"] > 0 and c["oos"]["expectancy"] <= 0:
            notes.append(f"- {tf}: In-sample ভালো কিন্তু Out-of-sample ভালো নয়, ওভারফিটিংয়ের সম্ভাবনা।")
        elif c["oos"]["trades"] and c["oos"]["expectancy"] > 0 and c["is"]["expectancy"] > 0:
            notes.append(f"- {tf}: দুই অংশেই গড় R ধনাত্মক। এটি ভালো লক্ষণ, তবে ভবিষ্যতের নিশ্চয়তা নয়।")
        else:
            notes.append(f"- {tf}: নেট গড় R ধনাত্মক নয়।")
        amb = c["all"].get("ambiguous", 0)
        if amb:
            notes.append(f"  (একই ক্যান্ডেলে TP ও SL দুটোই ছোঁয়া {amb}টি, সবগুলো SL ধরা হয়েছে)")
    L += ["## সতর্কতা ও ব্যাখ্যা", ""] + notes + [
        "", "- স্প্রেডগুলো config.yaml-এর প্রাথমিক অনুমান; আসল Exness স্প্রেড বসালে নেট ফল বদলাবে।",
        "- স্লিপেজ, সুইং, কমিশন ধরা হয়নি। ইতিহাস প্রোভাইডারের ডেটায়, আপনার ব্রোকারের সাথে সামান্য ফারাক থাকবে।",
        "- নিয়মগুলো ব্যাকটেস্ট দেখে ভালো দেখানোর জন্য টিউন করা হয়নি।"]
    return "\n".join(L) + "\n"


def write_outputs(sm: dict, cfg: dict, trades_all: list[dict] | None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(sm, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "report.md").write_text(render_report(sm, cfg), encoding="utf-8")
    if trades_all is not None:
        df = pd.DataFrame(trades_all)
        for c in ("t", "exit_t"):
            if c in df:
                df[c] = pd.to_datetime(df[c])
        df.to_csv(OUT / "trades.csv", index=False)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ব্যাকটেস্ট")
    ap.add_argument("--years", type=float, help="কত বছরের ইতিহাস (ডিফল্ট config)")
    ap.add_argument("--pairs", help="কমা দিয়ে (ডিফল্ট সব)")
    ap.add_argument("--refresh", action="store_true", help="ক্যাশ উপেক্ষা করে নতুন ডেটা আনো")
    ap.add_argument("--save-trades", action="store_true", help="প্রতিটি ট্রেড trades.csv-তে (শুধু লোকাল, পাবলিকে নয়)")
    args = ap.parse_args(argv)
    cfg = load_config()
    key = os.environ.get("PRICE_API_KEY", "").strip() or None
    names = [p.strip().upper() for p in args.pairs.split(",")] if args.pairs else \
        [p for p, v in cfg["pairs"].items() if v.get("enabled", True)]
    years = args.years or cfg["backtest"]["years"]
    throttle = Throttle(cfg["api_limits"]["sleep_between_requests_seconds"])
    hist: dict[str, pd.DataFrame] = {}
    try:
        for p in names:
            print(f"ডেটা: {p} ...", flush=True)
            hist[p] = load_history(p, cfg, key, throttle, years, args.refresh)
            print(f"  H1 ক্যান্ডেল: {len(hist[p])}", flush=True)
        now = utcnow()
        run = run_all(hist, cfg, now, names)
        sm = build_summary(run, cfg, names)
        trades = [x for mode in ("net",) for v in run["res"][mode].values() for x in v["trades"]] if args.save_trades else None
        write_outputs(sm, cfg, trades)
    except Exception as e:
        print(f"ত্রুটি: {type(e).__name__}: {clean(str(e), key)}"[:500], file=sys.stderr)
        return 1
    print(f"শেষ। রিপোর্ট: {OUT / 'report.md'} | API ক্রেডিট ব্যবহার: {throttle.credits_used}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
