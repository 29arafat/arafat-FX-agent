"""Telegram বার্তা: ফরম্যাট ও পাঠানো। Token/Chat ID শুধু পরিবেশ-চলক (Secrets) থেকে, কখনো লগে নয়।

- নতুন সিগন্যাল, TP/SL হিট, সিস্টেম এরর (একই এরর ৬ ঘণ্টায় একবার)।
- সময় সৌদি (UTC+3), দাম কনফিগের decimals অনুযায়ী।
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.telegram.org"


def fmt_price(x: float, decimals: int) -> str:
    return f"{x:.{decimals}f}"


def fmt_time(iso_utc: str, offset_hours: float = 3) -> str:
    t = datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=offset_hours)))
    return t.strftime("%Y-%m-%d %H:%M")


def format_open(sig: dict, cfg: dict) -> str:
    dp = cfg["pairs"][sig["pair"]]["decimals"]
    off = cfg["notify"]["display_utc_offset_hours"]
    icon = "🟢" if sig["direction"] == "BUY" else "🔴"
    conf = " · ✅ কনফার্মড" if sig.get("confirmed") else ""
    return (
        f"{icon} নতুন সিগন্যাল · {sig['pair']} · {sig['direction']} · {sig['timeframe']}\n"
        f"এন্ট্রি: {fmt_price(sig['entry'], dp)} (বৈধ {fmt_price(sig['entry_valid_until'], dp)} পর্যন্ত)\n"
        f"SL: {fmt_price(sig['sl'], dp)}\n"
        f"TP: {fmt_price(sig['tp'], dp)}\n"
        f"R:R {sig['rr']} · স্কোর {sig['score']}{conf}\n"
        f"সময়: {fmt_time(sig['opened_at'], off)} (সৌদি)\n"
        f"{sig['id']} · {sig.get('strategy_version', '')}"
    )


def format_close(sig: dict, cfg: dict) -> str:
    off = cfg["notify"]["display_utc_offset_hours"]
    win = sig["status"] == "TP"
    head = "🎯 TP হিট" if win else "🛑 SL হিট"
    amb = "\n⚠ একই ক্যান্ডেলে TP ও SL দুটোই ছুঁয়েছে, সাবধানী নিয়মে SL ধরা হয়েছে" if sig.get("ambiguous") else ""
    return (
        f"{head} · {sig['pair']} · {sig['direction']} · {sig['timeframe']}\n"
        f"ফল: {sig['result_r']:+.2f}R{amb}\n"
        f"বন্ধ: {fmt_time(sig['closed_at'], off)} (সৌদি)\n"
        f"{sig['id']}"
    )


def format_error(msg: str) -> str:
    return f"⚠️ সিস্টেম সতর্কতা\n{msg[:300]}"


def error_key(msg: str) -> str:
    return hashlib.sha1(msg.encode("utf-8")).hexdigest()[:12]


class Telegram:
    """বাস্তব Telegram পাঠানো। send() সফল হলে True।"""

    def __init__(self, token: str, chat_id: str, session=None, timeout: float = 15, retries: int = 2, sleep=None):
        self._token, self._chat, self.timeout, self.retries = token, str(chat_id), timeout, retries
        self.session = session or requests.Session()
        import time
        self.sleep = sleep or time.sleep
        self.last_error = ""

    @classmethod
    def from_env(cls, **kw) -> "Telegram":
        tok, chat = os.environ.get("TELEGRAM_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not tok or not chat:
            raise RuntimeError("TELEGRAM_TOKEN বা TELEGRAM_CHAT_ID পরিবেশে নেই")
        return cls(tok, chat, **kw)

    def _clean(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    def send(self, text: str) -> bool:
        url = f"{API}/bot{self._token}/sendMessage"
        body = {"chat_id": self._chat, "text": text, "disable_web_page_preview": True}
        for attempt in range(self.retries + 1):
            try:
                r = self.session.post(url, json=body, timeout=self.timeout)
                if r.status_code == 200:
                    return True
                if r.status_code == 429:
                    wait = 5
                    try:
                        wait = min(int(r.json().get("parameters", {}).get("retry_after", 5)), 30)
                    except Exception:
                        pass
                    self.last_error = "Telegram 429 (থামিয়ে চেষ্টা)"
                    self.sleep(wait)
                    continue
                if 400 <= r.status_code < 500:        # ভুল Token/Chat ID ইত্যাদি, আবার চেষ্টা বৃথা
                    self.last_error = f"Telegram HTTP {r.status_code}"
                    return False
                self.last_error = f"Telegram HTTP {r.status_code}"
            except requests.RequestException as e:
                self.last_error = self._clean(f"{type(e).__name__}: {e}")[:160]
            self.sleep(2 ** attempt)
        return False


class DryRun:
    """পাঠায় না, শুধু টার্মিনালে দেখায় (ডিফল্ট)।"""
    last_error = ""

    def __init__(self):
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        self.sent.append(text)
        print("---- [ড্রাই-রান: Telegram-এ যেত] ----\n" + text + "\n----")
        return True
