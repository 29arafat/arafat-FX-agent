"""ডায়াগনস্টিক (ধাপ ৩ক): কৌশল কেন কাজ করছে/করছে না বোঝার জন্য। কোনো নিয়ম বদলায় না, টিউনও করে না।

চালানো:  python -m backtest.diagnose --pairs XAUUSD      (ক্যাশ থাকলে API লাগে না)
দেখায়:
1. BUY বনাম SELL (গোল্ডের একমুখী উত্থান লাভ বানাচ্ছে কিনা)
2. বছরভিত্তিক ফল (একটা বছরের ভাগ্যে নির্ভরশীল কিনা)
3. স্কোর ও RR ভিত্তিক ফল
4. বেসলাইন: (ক) এলোমেলো দিক, (খ) শুধু D1 ট্রেন্ডের দিকে এন্ট্রি (পুলব্যাক/কনফার্মেশন ছাড়া)।
   আমাদের নিয়ম বেসলাইনের চেয়ে ভালো না হলে নিয়মগুলো কিছু যোগ করছে না।
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd

from backtest import backtest as B
from src.fetch import Throttle, clean, load_config, utcnow
from src.stats import summarize
from src.strategy import NS_PER_MIN, build_frames, cooldown_ns, friday_blocked, trend_direction
from src.tracker import find_exit, result_r


def baseline(pair: str, tf: str, frames: dict, cfg: dict, spread: float, mode: str,
             seed: int, prob: float = 0.05, rr: int = 2) -> list[dict]:
    """একই ট্র্যাকিং/নিয়মে বেসলাইন ট্রেড। mode: 'random' (দিক এলোমেলো) বা 'trend' (D1 ট্রেন্ডের দিকে)।"""
    rng = np.random.default_rng(seed)
    m = cfg["strategy"]["tf_map"][tf]
    trig, tr_fr, h1 = frames[m["trigger"]], frames[m["trend"]], frames["H1"]
    warm, cut = cfg["backtest"]["warmup_bars"], cfg["rules"]["friday_cutoff_utc"]
    cool, mult = cooldown_ns(tf, cfg), cfg["risk"]["sl_atr_multiplier"]
    out, free = [], -1
    for i in range(warm, len(trig)):
        t = int(trig.end_ns[i])
        if t < free or friday_blocked(t, cut) or rng.random() > prob:
            continue
        atr = float(trig.atr[i])
        if not atr > 0:
            continue
        if mode == "trend":
            ti = tr_fr.last_closed(t)
            d = trend_direction(tr_fr, ti, cfg) if ti >= 0 else None
            if d is None:
                continue
        else:
            d = "BUY" if rng.random() < 0.5 else "SELL"
        entry, dist = float(trig.c[i]), mult * atr
        sign = 1 if d == "BUY" else -1
        sl, tp = entry - sign * dist, entry + sign * rr * dist
        k0 = int(np.searchsorted(h1.start_ns, t, side="left"))
        ex = find_exit(d, sl, tp, h1.h[k0:], h1.l[k0:], spread)
        if ex is None:
            break
        k, status, amb = ex
        exit_ns = int(h1.end_ns[k0 + k])
        free = exit_ns + cool
        out.append({"dir": d, "t": t, "exit_t": exit_ns, "r": result_r(d, entry, sl, tp, status, spread)})
    return out


def _line(label: str, rs: list[float], mt: int) -> str:
    s = summarize(rs, mt)
    pf = "∞" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
    flag = "" if s["reliable"] else " ⚠"
    return f"| {label} | {s['trades']}{flag} | {s['win_rate']:.0f}% | {s['total_r']:+.1f} | {s['expectancy']:+.2f} | {pf} |"


HEAD = "| | ট্রেড | উইন রেট | মোট R | গড় R/ট্রেড | PF |\n|---|---|---|---|---|---|"


def diagnose(pair: str, hist: pd.DataFrame, cfg: dict, now, seeds: int = 20) -> str:
    h1 = B.clean_h1(hist, cfg, now)
    frames = build_frames(h1, cfg, now)
    sp, mt = cfg["backtest"]["spread"][pair], cfg["backtest"]["min_trades_for_confidence"]
    L = [f"# ডায়াগনস্টিক: {pair}", "", "নেট (স্প্রেডসহ)। কোনো কাঁচা দাম নেই।", ""]
    first, last = int(frames["H1"].start_ns[0]), int(frames["H1"].end_ns[-1])
    drift = float(frames["D1"].c[-1] / frames["D1"].c[0] - 1) * 100
    L += [f"ডেটার মোট দামের পরিবর্তন (প্রথম→শেষ D1 ক্লোজ): **{drift:+.0f}%**। বড় একমুখী চলন থাকলে ট্রেন্ড-অনুসারী কৌশল সহজে লাভ দেখায়, তাই বেসলাইনের সাথে তুলনা জরুরি।", ""]
    for tf in cfg["timeframes"]:
        tr = B.simulate(pair, tf, frames, cfg, sp)["trades"]
        L += [f"## {tf}", "", "### BUY বনাম SELL", "", HEAD,
              _line("সব", [x["r"] for x in tr], mt),
              _line("BUY", [x["r"] for x in tr if x["dir"] == "BUY"], mt),
              _line("SELL", [x["r"] for x in tr if x["dir"] == "SELL"], mt), ""]
        years = sorted({pd.Timestamp(x["t"], tz="UTC").year for x in tr})
        L += ["### বছরভিত্তিক", "", HEAD] + [
            _line(str(y), [x["r"] for x in tr if pd.Timestamp(x["t"], tz="UTC").year == y], mt) for y in years] + [""]
        L += ["### স্কোর ও RR ভিত্তিক", "", HEAD]
        for sc in sorted({x["score"] for x in tr}):
            L.append(_line(f"স্কোর {sc}", [x["r"] for x in tr if x["score"] == sc], mt))
        for rr in sorted({x["rr"] for x in tr}):
            L.append(_line(f"RR 1:{rr}", [x["r"] for x in tr if x["rr"] == rr], mt))
        L += ["", f"### বেসলাইন ({seeds}টি র‍্যান্ডম সিড, গড়; RR ১:২, SL ১.৫×ATR)", "", HEAD]
        for mode, name in (("random", "এলোমেলো দিক"), ("trend", "শুধু D1 ট্রেন্ডের দিকে")):
            runs = [baseline(pair, tf, frames, cfg, sp, mode, s) for s in range(seeds)]
            rs = [x["r"] for r in runs for x in r]
            n = np.mean([len(r) for r in runs]) if runs else 0
            s = summarize(rs, mt)
            L.append(f"| {name} | {n:.0f}/সিড | {s['win_rate']:.0f}% | — | {s['expectancy']:+.2f} | "
                     f"{'∞' if s['profit_factor'] is None else format(s['profit_factor'], '.2f')} |")
        L.append("")
    L += ["## কীভাবে পড়বেন", "",
          "- **BUY বনাম SELL:** শুধু BUY লাভজনক, SELL ঋণাত্মক হলে লাভ দিক-পক্ষপাত থেকে আসছে, নিয়ম থেকে নয়।",
          "- **বছরভিত্তিক:** এক-দুই বছরে সব লাভ আর বাকিগুলো ঋণাত্মক হলে ভরসা নেই।",
          "- **বেসলাইন:** আমাদের নিয়মের গড় R/ট্রেড 'শুধু D1 ট্রেন্ডের দিকে' বেসলাইনের চেয়ে স্পষ্ট বেশি না হলে পুলব্যাক ও কনফার্মেশন কিছু যোগ করছে না।",
          "- ⚠ = ট্রেড ৩০-এর কম।"]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ডায়াগনস্টিক")
    ap.add_argument("--pairs", default="XAUUSD")
    ap.add_argument("--seeds", type=int, default=20)
    args = ap.parse_args(argv)
    cfg = load_config()
    try:
        out = []
        for p in [x.strip().upper() for x in args.pairs.split(",")]:
            hist = B.load_history(p, cfg, None, Throttle(0), cfg["backtest"]["years"], False)
            out.append(diagnose(p, hist, cfg, utcnow(), args.seeds))
        B.OUT.mkdir(parents=True, exist_ok=True)
        (B.OUT / "diagnose.md").write_text("\n".join(out), encoding="utf-8")
    except Exception as e:
        print(f"ত্রুটি: {type(e).__name__}: {clean(str(e), None)}"[:500], file=sys.stderr)
        return 1
    print(f"শেষ। রিপোর্ট: {B.OUT / 'diagnose.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
