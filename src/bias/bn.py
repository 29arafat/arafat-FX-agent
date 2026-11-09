"""বাংলা ব্যাখ্যার টেমপ্লেট (নিয়ম-ভিত্তিক, LLM নয়)। প্রতিটি বাক্য শুধু গণনা করা মান থেকে।"""
from __future__ import annotations

HORIZON_BN = {"h4": "আগামী ৪ ঘণ্টা", "d1": "আগামী ২৪ ঘণ্টা (কাল পর্যন্ত)", "w1": "আগামী সপ্তাহ"}
BIAS_BN = {"UP": "ঊর্ধ্বমুখী (আপ)", "DOWN": "নিম্নমুখী (ডাউন)", "NEUTRAL": "নিরপেক্ষ / অস্পষ্ট"}
CONF_BN = {"LOW": "কম", "MEDIUM": "মাঝারি", "HIGH": "বেশি", "NONE": "-"}
MODE_BN = {"full": "পূর্ণ বিশ্লেষণ", "limited": "সীমিত বিশ্লেষণ (চার্ট + ডলার)"}
DISCLAIMER_BN = ("এটা প্রেক্ষাপট-সূচক, নিশ্চিত ভবিষ্যদ্বাণী নয়। দামের সীমা ওঠানামার পরিসর (ATR ভিত্তিক), "
                 "প্রতিশ্রুতি নয়। এখনো পরীক্ষামূলক (শ্যাডো মোড)। ট্রেডের সিদ্ধান্ত সিগন্যাল ও আপনার ঝুঁকি-নিয়মে।")


def _dir(score: float, up: str, down: str) -> str:
    return up if score > 0 else down


def reason(key: str, score: float, value: float | None, pair: str, usd_sign: int) -> str:
    """একটা উপাদানের বাংলা বাক্য। score > 0 = পেয়ারের ওপরের দিকে সহায়ক।"""
    v = value
    if key == "trend_d1":
        return _dir(score, "D1 চার্টে বড় ট্রেন্ড ঊর্ধ্বমুখী (EMA50, EMA200-এর ওপরে)", "D1 চার্টে বড় ট্রেন্ড নিম্নমুখী (EMA50, EMA200-এর নিচে)")
    if key == "trend_h4":
        return _dir(score, "H4-এ স্বল্পমেয়াদি ট্রেন্ড ঊর্ধ্বমুখী (EMA20, EMA50-এর ওপরে)", "H4-এ স্বল্পমেয়াদি ট্রেন্ড নিম্নমুখী (EMA20, EMA50-এর নিচে)")
    if key == "trend_h1":
        return _dir(score, "H1-এ ছোট ট্রেন্ড ঊর্ধ্বমুখী", "H1-এ ছোট ট্রেন্ড নিম্নমুখী")
    if key in ("rsi_d1", "rsi_h1"):
        tf = "D1" if key == "rsi_d1" else "H1"
        return f"{tf} RSI {v:.0f}: " + _dir(score, "৫০-এর ওপরে, গতি ক্রেতার পক্ষে", "৫০-এর নিচে, গতি বিক্রেতার পক্ষে")
    if key == "ema_pos_h1":
        return _dir(score, "দাম H1 EMA20-এর ওপরে আছে", "দাম H1 EMA20-এর নিচে আছে")
    if key == "real_yield":
        move = "কমেছে" if (v or 0) < 0 else "বেড়েছে"
        eff = "গোল্ডের পক্ষে (সুদহীন সম্পদের আকর্ষণ বাড়ে)" if score > 0 else "গোল্ডের বিপক্ষে (সুদহীন সম্পদের আকর্ষণ কমে)"
        return f"মার্কিন ১০ বছরের রিয়েল ইয়িল্ড ৫ দিনে {abs(v or 0):.0f} bp {move}, যা {eff}"
    if key == "usd":
        move = "বেড়েছে" if (v or 0) > 0 else "কমেছে"
        eff = "ওপরের দিকে সহায়ক" if score > 0 else "নিচের দিকে চাপ"
        return f"ডলার ইনডেক্স ৫ দিনে {abs(v or 0):.2f}% {move}, যা {pair}-এর জন্য {eff}"
    if key == "vix":
        move = "বেড়েছে" if (v or 0) > 0 else "কমেছে"
        eff = "ভয়ের বাজারে নিরাপদ আশ্রয় হিসেবে গোল্ডের পক্ষে" if score > 0 else "ভয় কমছে, নিরাপদ আশ্রয়ের চাহিদা কম"
        return f"VIX ৫ দিনে {abs(v or 0):.1f} পয়েন্ট {move}, {eff}"
    if key == "cot":
        move = "বেড়েছে" if (v or 0) > 0 else "কমেছে"
        eff = "বড় ফান্ডগুলো ক্রেতার দিকে" if score > 0 else "বড় ফান্ডগুলো বিক্রেতার দিকে"
        return f"CFTC রিপোর্ট: ম্যানেজড-মানির নেট পজিশন ৪ সপ্তাহে {abs((v or 0) * 100):.1f}% (ওপেন ইন্টারেস্টের) {move}, {eff}"
    return key


def warning_rsi(key: str, value: float) -> str | None:
    if key in ("rsi_d1", "rsi_h1"):
        tf = "D1" if key == "rsi_d1" else "H1"
        if value >= 70:
            return f"{tf} RSI {value:.0f}: অতিক্রয় অঞ্চলে, ফিরতি নামার ঝুঁকি আছে"
        if value <= 30:
            return f"{tf} RSI {value:.0f}: অতিবিক্রয় অঞ্চলে, ফিরতি ওঠার ঝুঁকি আছে"
    return None


def summary(hkey: str, bias: str, conf: str) -> str:
    b = BIAS_BN[bias]
    return f"{HORIZON_BN[hkey]}: {b}" + (f" (আস্থা: {CONF_BN[conf]})" if bias != "NEUTRAL" else "")


def event_warning(title: str, when_local: str, approx: bool) -> str:
    t = f"{when_local}" + (" (আনুমানিক)" if approx else "")
    return f"{t} {title}: খবরের সময় দাম হঠাৎ লাফাতে পারে, দামের সীমা ছাড়িয়ে যেতে পারে"


# ---------- ইভেন্টের গোল্ড-প্রভাব (সাধারণ প্রবণতা, ভবিষ্যদ্বাণী নয়) ----------

LEVEL_BN = {"high": "অত্যন্ত গুরুত্বপূর্ণ", "medium": "মাঝারি গুরুত্ব"}
EFFECT_CAVEAT_BN = ("এটা প্রাথমিক প্রতিক্রিয়ার সাধারণ প্রবণতা। বাস্তবে দাম অনেক সময় উল্টে যায়, "
                    "তাই এটা ট্রেডের সংকেত নয়।")


def event_effect(cat: str | None, rule: str | None, forecast: str | None, previous: str | None) -> dict | None:
    """rule = 'down' হলে আসল ফল বেশি হলে গোল্ড চাপে, 'up' হলে বেশি হলে গোল্ডের পক্ষে। না জানলে None।"""
    if not cat or rule not in ("down", "up"):
        return None
    if cat == "fomc":
        hi, lo = ("কড়া সুর (সুদ বাড়ানো/ধরে রাখার কড়া ইঙ্গিত)", "নরম সুর (সুদ কমানোর ইঙ্গিত)")
        up_t, dn_t = "ডলার ও ইয়িল্ড বাড়তে পারে", "ডলার ও ইয়িল্ডে চাপ পড়তে পারে"
        basis, basis_bn = "text", "ভাষার সুরের ওপর নির্ভর করে, সংখ্যা নেই"
    else:
        hi, lo = "ফোরকাস্টের চেয়ে বেশি এলে", "ফোরকাস্টের চেয়ে কম এলে"
        if cat == "unemp":
            up_t, dn_t = "অর্থনীতি দুর্বল ধরা হয়, সুদ কমার আশা বাড়ে", "শ্রমবাজার শক্ত ধরা হয়, সুদ কমার আশা কমে"
        else:
            up_t, dn_t = "ডলার ও ইয়িল্ড বাড়তে পারে", "ডলার ও ইয়িল্ডে চাপ পড়তে পারে"
        if forecast:
            basis, basis_bn = "forecast", "ফোরকাস্টের সাথে আসল ফল তুলনা করে (বাজার এভাবেই নড়ে)"
        else:
            basis, basis_bn = "previous", "ফোরকাস্ট পাওয়া যায়নি, তাই আগের মানের সাথে তুলনা (দুর্বল সূত্র)"
            hi, lo = "আগের মানের চেয়ে বেশি এলে", "আগের মানের চেয়ে কম এলে"
    up_g, dn_g = "গোল্ডে সাধারণত সমর্থন (↑)", "গোল্ডে সাধারণত চাপ (↓)"
    if rule == "down":       # বেশি হলে ডলার/ইয়িল্ড বাড়ে → গোল্ডে চাপ
        higher = {"dir": "DOWN", "text": f"{hi}: {up_t}, {dn_g}"}
        lower = {"dir": "UP", "text": f"{lo}: {dn_t}, {up_g}"}
    else:                    # বেশি হলে অর্থনীতি দুর্বল ধরা হয় → গোল্ডের পক্ষে
        higher = {"dir": "UP", "text": f"{hi}: {up_t}, {up_g}"}
        lower = {"dir": "DOWN", "text": f"{lo}: {dn_t}, {dn_g}"}
    return {"higher": higher, "lower": lower, "basis": basis, "basis_bn": basis_bn, "caveat_bn": EFFECT_CAVEAT_BN}
