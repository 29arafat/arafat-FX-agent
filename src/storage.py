"""সিগন্যালের অবস্থা (state) সংরক্ষণ। ধাপ ৪: লোকাল JSON ফাইল। ধাপ ৫-এ data ব্রাঞ্চে সরবে।

ফাইল লেখা অ্যাটমিক (আগে অস্থায়ী ফাইল, তারপর বদল): মাঝপথে বন্ধ হলেও ভাঙা ফাইল থাকে না।
"""
from __future__ import annotations

import json
import os
from pathlib import Path

EMPTY = {"version": 1, "signals": [], "errors": {}, "last_check": None}


def load_state(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return json.loads(json.dumps(EMPTY))
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"state ফাইল নষ্ট: {p} ({e.msg}). মুছে ফেলার আগে ব্যাকআপ রাখুন।") from None
    for k, v in EMPTY.items():
        data.setdefault(k, json.loads(json.dumps(v)))
    return data


def save_state(path: str | Path, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
