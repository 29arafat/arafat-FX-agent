"""পরিসংখ্যান: উইন রেট, মোট R, এক্সপেক্টেন্সি, প্রফিট ফ্যাক্টর, ড্রডাউন (R-এ)।

ব্যাকটেস্ট ও লাইভ stats একই ফাংশন ব্যবহার করে। ইনপুট: ক্লোজ হওয়ার সময়ের ক্রমে R-এর তালিকা।
"""
from __future__ import annotations


def max_drawdown(rs: list[float]) -> float:
    """সঞ্চিত R-এর শীর্ষ থেকে সর্বোচ্চ পতন (ধনাত্মক সংখ্যা)।"""
    peak = cum = worst = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def summarize(rs: list[float], min_trades: int = 30) -> dict:
    n = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    if gross_loss > 0:
        pf = gross_win / gross_loss
    else:
        pf = None if gross_win > 0 else 0.0   # None = অসীম (কোনো লস নেই)
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / n * 100.0) if n else 0.0,
        "total_r": sum(rs),
        "expectancy": (sum(rs) / n) if n else 0.0,
        "profit_factor": pf,
        "max_drawdown_r": max_drawdown(rs),
        "avg_win_r": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss_r": (-gross_loss / len(losses)) if losses else 0.0,
        "reliable": n >= min_trades,      # নমুনা যথেষ্ট কিনা
    }


def build_stats(signals: list[dict], now_iso: str, version: str = "", min_trades: int = 30) -> dict:
    """সিগন্যালের তালিকা থেকে stats.json। শুধু TP/SL-এ বন্ধ সিগন্যাল গণনায় (Running বাদ)।

    ব্যাকটেস্টের একই summarize ব্যবহার, তাই সংখ্যার অর্থ এক।
    """
    from datetime import datetime, timedelta

    def ts(x: str) -> datetime:
        return datetime.fromisoformat(x.replace("Z", "+00:00"))

    now = ts(now_iso)
    closed = sorted((x for x in signals if x["status"] in ("TP", "SL") and x.get("closed_at")), key=lambda x: x["closed_at"])

    def block(sel: list[dict]) -> dict:
        out = summarize([float(x["result_r"]) for x in sel], min_trades)
        out["ambiguous"] = sum(1 for x in sel if x.get("ambiguous"))
        return out

    cum, equity = 0.0, []
    for x in closed:
        cum += float(x["result_r"])
        equity.append({"t": x["closed_at"], "r": round(cum, 2)})
    return {
        "generated_at": now_iso,
        "strategy_version": version,
        "running": sum(1 for x in signals if x["status"] == "Running"),
        "overall": block(closed),
        "by_pair": {p: block([x for x in closed if x["pair"] == p]) for p in sorted({x["pair"] for x in closed})},
        "by_timeframe": {t: block([x for x in closed if x["timeframe"] == t]) for t in ("H1", "H4", "D1")},
        "last_7d": block([x for x in closed if ts(x["closed_at"]) >= now - timedelta(days=7)]),
        "last_30d": block([x for x in closed if ts(x["closed_at"]) >= now - timedelta(days=30)]),
        "equity": equity,
    }
