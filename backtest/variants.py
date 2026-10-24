"""ধাপ ৩খ: আগে-ঘোষিত কয়েকটি সরল রূপ (ablation), বুটস্ট্র্যাপ ভরসা-সীমা ও বছরভিত্তিক (walk-forward) ফল।

রূপগুলো এই ফাইলে আগে থেকে লেখা, ফল দেখে বদলানো হয় না। মোট ৪টি রূপ × ৩ টাইমফ্রেম = ১২টি তুলনা:
  V0 পুরনো নিয়ম (৩ শর্ত) | V1 পুলব্যাক বাদ | V2 কনফার্মেশন বাদ | V3 শুধু ট্রেন্ড
অনেক তুলনায় কোনো একটা ভাগ্যক্রমে ভালো দেখাবেই (multiple testing), তাই বিজয়ী বাছতে শুধু গড় নয়,
৯৫% ভরসা-সীমার নিচের প্রান্ত ও বছরগুলোর সামঞ্জস্য দেখা হয়।

চালানো:  python -m backtest.variants --pairs XAUUSD
"""
from __future__ import annotations

import argparse
import copy
import sys

import numpy as np
import pandas as pd

from backtest import backtest as B
from src.fetch import Throttle, clean, load_config, utcnow
from src.stats import summarize
from src.strategy import build_frames

VARIANTS = {
    "V0 পুরনো নিয়ম (৩ শর্ত)": {"required": ["clear_trend", "pullback_zone", "confirmation"], "threshold": 7},
    "V1 পুলব্যাক বাদ": {"required": ["clear_trend", "confirmation"], "threshold": 4},
    "V2 কনফার্মেশন বাদ": {"required": ["clear_trend", "pullback_zone"], "threshold": 4},
    "V3 শুধু ট্রেন্ড": {"required": ["clear_trend"], "threshold": 2},
}


def with_variant(cfg: dict, v: dict) -> dict:
    c = copy.deepcopy(cfg)
    c["scoring"]["required"] = list(v["required"])
    c["scoring"]["threshold"] = v["threshold"]
    return c


def bootstrap_ci(rs: list[float], n_boot: int = 4000, seed: int = 0, alpha: float = 0.05):
    """গড় R-এর বুটস্ট্র্যাপ ভরসা-সীমা (নিম্ন, উচ্চ)। ট্রেড < ২ হলে (nan, nan)।"""
    if len(rs) < 2:
        return float("nan"), float("nan")
    a = np.asarray(rs, float)
    rng = np.random.default_rng(seed)
    means = a[rng.integers(0, len(a), size=(n_boot, len(a)))].mean(axis=1)
    return float(np.quantile(means, alpha / 2)), float(np.quantile(means, 1 - alpha / 2))


def diff_ci(a: list[float], b: list[float], n_boot: int = 4000, seed: int = 0):
    """গড়(a) − গড়(b)-এর ভরসা-সীমা (স্বাধীন নমুনা ধরে)।"""
    if len(a) < 2 or len(b) < 2:
        return float("nan"), float("nan")
    x, y = np.asarray(a, float), np.asarray(b, float)
    rng = np.random.default_rng(seed)
    d = x[rng.integers(0, len(x), (n_boot, len(x)))].mean(axis=1) - y[rng.integers(0, len(y), (n_boot, len(y)))].mean(axis=1)
    return float(np.quantile(d, 0.025)), float(np.quantile(d, 0.975))


def _f(x: float) -> str:
    return "—" if x != x else f"{x:+.2f}"


def year_changes(frames: dict) -> dict[int, float]:
    d1 = frames["D1"]
    yrs = pd.DatetimeIndex(d1.index).year
    out = {}
    for y in sorted(set(yrs)):
        idx = np.where(yrs == y)[0]
        out[int(y)] = float(d1.c[idx[-1]] / d1.c[idx[0]] - 1) * 100
    return out


def run(pair: str, hist: pd.DataFrame, cfg: dict, now) -> str:
    h1 = B.clean_h1(hist, cfg, now)
    frames = build_frames(h1, cfg, now)
    sp, mt = cfg["backtest"]["spread"][pair], cfg["backtest"]["min_trades_for_confidence"]
    yc = year_changes(frames)
    L = [f"# রূপ-তুলনা ও walk-forward: {pair}", "",
         "নেট (স্প্রেডসহ)। ভরসা-সীমা = গড় R/ট্রেডের ৯৫% বুটস্ট্র্যাপ সীমা; নিচের প্রান্ত ০-র ওপরে হলে তবেই 'সম্ভবত সত্যিকারের সুবিধা'।",
         "১২টি তুলনার কয়েকটা ভাগ্যে ভালো দেখাবে, তাই একটা সংখ্যায় ভুলবেন না।", "",
         "বছরে দামের পরিবর্তন (D1 ক্লোজ, %): " + ", ".join(f"{y}: {c:+.0f}%" for y, c in yc.items()), ""]
    res: dict[tuple[str, str], list[dict]] = {}
    for vn, v in VARIANTS.items():
        c = with_variant(cfg, v)
        for tf in cfg["timeframes"]:
            res[(vn, tf)] = B.simulate(pair, tf, frames, c, sp)["trades"]
    for tf in cfg["timeframes"]:
        L += [f"## {tf}", "", "| রূপ | ট্রেড | উইন রেট | গড় R/ট্রেড | ৯৫% সীমা | PF | ড্রডাউন R | ধনাত্মক বছর | V3-এর চেয়ে ভালো? (ফারাক, ৯৫% সীমা) |",
              "|---|---|---|---|---|---|---|---|---|"]
        base = [x["r"] for x in res[("V3 শুধু ট্রেন্ড", tf)]]
        for vn in VARIANTS:
            tr = sorted(res[(vn, tf)], key=lambda x: x["exit_t"])
            rs = [x["r"] for x in tr]
            s = summarize(rs, mt)
            lo, hi = bootstrap_ci(rs)
            by_year: dict[int, list[float]] = {}
            for x in tr:
                by_year.setdefault(pd.Timestamp(x["t"], tz="UTC").year, []).append(x["r"])
            pos = sum(1 for r in by_year.values() if sum(r) > 0)
            if vn.startswith("V3"):
                cmp = "—"
            else:
                dl, dh = diff_ci(rs, base)
                cmp = f"{_f(s['expectancy'] - (np.mean(base) if base else 0))} ({_f(dl)} … {_f(dh)})"
            pf = "∞" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
            flag = "" if s["reliable"] else " ⚠"
            L.append(f"| {vn} | {s['trades']}{flag} | {s['win_rate']:.0f}% | {_f(s['expectancy'])} | {_f(lo)} … {_f(hi)} | {pf} | "
                     f"{s['max_drawdown_r']:.1f} | {pos}/{len(by_year)} | {cmp} |")
        L += ["", "বছরভিত্তিক গড় R/ট্রেড:", "", "| রূপ | " + " | ".join(str(y) for y in yc) + " |", "|---|" + "---|" * len(yc)]
        for vn in VARIANTS:
            row = []
            for y in yc:
                rs = [x["r"] for x in res[(vn, tf)] if pd.Timestamp(x["t"], tz="UTC").year == y]
                row.append(f"{_f(float(np.mean(rs)))} ({len(rs)})" if rs else "—")
            L.append(f"| {vn} | " + " | ".join(row) + " |")
        L.append("")
    L += ["## কীভাবে পড়বেন", "",
          "- 'V3-এর চেয়ে ভালো?' কলামে ৯৫% সীমার নিচের প্রান্ত ০-র ওপরে না থাকলে ঐ রূপ শুধু-ট্রেন্ডের চেয়ে ভালো বলার প্রমাণ নেই।",
          "- ধনাত্মক বছর কম হলে লাভ কয়েক বছরের ওপর নির্ভরশীল। দামের পরিবর্তনের সারির সাথে মিলিয়ে দেখুন।",
          "- ⚠ = ট্রেড ৩০-এর কম। কোনো সংখ্যাই ভবিষ্যতের নিশ্চয়তা নয়।"]
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="রূপ-তুলনা")
    ap.add_argument("--pairs", default="XAUUSD")
    args = ap.parse_args(argv)
    cfg = load_config()
    try:
        out = []
        for p in [x.strip().upper() for x in args.pairs.split(",")]:
            hist = B.load_history(p, cfg, None, Throttle(0), cfg["backtest"]["years"], False)
            out.append(run(p, hist, cfg, utcnow()))
        B.OUT.mkdir(parents=True, exist_ok=True)
        (B.OUT / "variants.md").write_text("\n".join(out), encoding="utf-8")
    except Exception as e:
        print(f"ত্রুটি: {type(e).__name__}: {clean(str(e), None)}"[:500], file=sys.stderr)
        return 1
    print(f"শেষ। রিপোর্ট: {B.OUT / 'variants.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
