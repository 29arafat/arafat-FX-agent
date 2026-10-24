"""TP/SL মেলানো: ব্যাকটেস্ট ও লাইভ ট্র্যাকার একই যুক্তি ব্যবহার করে।

নিয়ম (সাবধানী):
- সিগন্যাল খোলার পর থেকে সব ক্লোজড H1 ক্যান্ডেল পরীক্ষা হয়।
- একই ক্যান্ডেলে TP ও SL দুটোই ছুঁলে SL ধরা হয় (ambiguous = true)।
- দাম বিড (চার্ট) দামে। BUY: এন্ট্রি অ্যাস্কে (চার্ট দাম + স্প্রেড), বের হওয়া বিডে।
  SELL: এন্ট্রি বিডে, বের হওয়ার ট্রিগার অ্যাস্কে (ক্যান্ডেল + স্প্রেড)।
"""
from __future__ import annotations

import numpy as np


def find_exit(direction: str, sl: float, tp: float, high: np.ndarray, low: np.ndarray,
              spread: float = 0.0):
    """প্রথম ক্যান্ডেল খোঁজে যেখানে SL বা TP ছুঁয়েছে।

    ফেরত: (সূচক, "TP"/"SL", ambiguous) অথবা None (এখনো খোলা)।
    """
    if len(high) == 0:
        return None
    if direction == "BUY":
        sl_hit, tp_hit = low <= sl, high >= tp
    elif direction == "SELL":
        sl_hit, tp_hit = (high + spread) >= sl, (low + spread) <= tp
    else:
        raise ValueError("direction BUY বা SELL হতে হবে")
    any_hit = sl_hit | tp_hit
    if not any_hit.any():
        return None
    k = int(np.argmax(any_hit))
    if sl_hit[k]:
        return k, "SL", bool(tp_hit[k])
    return k, "TP", False


def result_r(direction: str, entry: float, sl: float, tp: float, status: str,
             spread: float = 0.0) -> float:
    """ফলাফল R-এ। SL হিট = −১। TP হিটে (পুরস্কার ÷ প্রকৃত ঝুঁকি), স্প্রেডসহ।"""
    if status == "SL":
        return -1.0
    if direction == "BUY":
        fill = entry + spread                 # অ্যাস্কে কেনা
        return (tp - fill) / (fill - sl)
    return (entry - tp) / (sl - entry)        # বিডে বিক্রি; স্প্রেড ট্রিগারে ধরা হয়েছে
