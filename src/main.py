"""মূল চালক (ধাপ ৪: লোকাল রান)।

  python -m src.main --once                  # ড্রাই-রান: ডেটা আনে, সিগন্যাল খোঁজে, বার্তা শুধু টার্মিনালে দেখায়
  python -m src.main --once --send           # আসল Telegram-এ পাঠায়
  python -m src.main --test-telegram         # শুধু একটা পরীক্ষার বার্তা পাঠায়

ধাপ ৪-এ state লোকাল ফাইলে (state/signals.json)। GitHub Actions-এ নিজে নিজে চালানো ধাপ ৫-এ (state data ব্রাঞ্চে গেলে)।
"""
from __future__ import annotations

import argparse
import os
import sys

from .engine import run_once
from .fetch import clean, fetch_all, load_config, utcnow
from .notify import DryRun, Telegram
from .storage import load_state, save_state


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="সিগন্যাল ইঞ্জিন")
    ap.add_argument("--once", action="store_true", help="একবার চালাও")
    ap.add_argument("--send", action="store_true", help="আসল Telegram-এ পাঠাও (না দিলে ড্রাই-রান)")
    ap.add_argument("--test-telegram", action="store_true", help="একটা পরীক্ষার বার্তা পাঠাও")
    args = ap.parse_args(argv)
    cfg = load_config()

    if args.test_telegram:
        try:
            tg = Telegram.from_env()
        except RuntimeError as e:
            print(f"ত্রুটি: {e}", file=sys.stderr)
            return 2
        ok = tg.send("✅ পরীক্ষা: Telegram সংযোগ ঠিক আছে (arafat-FX-agent)")
        print("পাঠানো হয়েছে" if ok else f"ব্যর্থ: {tg.last_error}")
        return 0 if ok else 1

    if not args.once:
        ap.print_help()
        return 0

    key = os.environ.get("PRICE_API_KEY", "").strip()
    if not key:
        print("PRICE_API_KEY পাওয়া যায়নি।", file=sys.stderr)
        return 2
    try:
        notifier = Telegram.from_env() if args.send else DryRun()
    except RuntimeError as e:
        print(f"ত্রুটি: {e}", file=sys.stderr)
        return 2
    path = cfg["storage"]["state_file"]
    try:
        state = load_state(path)
        now = utcnow()
        data, errors, thr = fetch_all(cfg, key, now=now, due=["H1", "H4", "D1"])
        h1 = {p: v["H1"] for p, v in data.items()}
        report = run_once(cfg, h1, errors, state, notifier, now, save=lambda: save_state(path, state))
        save_state(path, state)
    except Exception as e:  # কখনো Key ফাঁস করবে না
        print(f"ত্রুটি: {type(e).__name__}: {clean(str(e), key)}"[:500], file=sys.stderr)
        return 1
    mode = "আসল Telegram" if args.send else "ড্রাই-রান"
    print(f"শেষ ({mode}) | নতুন সিগন্যাল: {report['new']} | বন্ধ হয়েছে: {report['closed']} | "
          f"বার্তা: {report['sent']} | এরর: {report['errors']} | API ক্রেডিট: {thr.credits_used}")
    for p, m in errors.items():
        print(f"  {p}: {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
