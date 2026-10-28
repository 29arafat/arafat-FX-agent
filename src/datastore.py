"""data ব্রাঞ্চের ফাইল লেখা, কমিট ও পুশ।

ফাইল (সবই পাবলিক, কোনো Secret নেই):
- state.json   : ইঞ্জিনের পূর্ণ অবস্থা (ফ্ল্যাগসহ)
- signals.json : পাবলিক সিগন্যাল তালিকা (স্পেকের ক্ষেত্রগুলো)
- status.json  : শেষ রান, পেয়ারভিত্তিক অবস্থা
- stats.json   : পরিসংখ্যান ও R-কার্ভ
পাবলিক JSON-এ বর্তমান দাম (last_price) নেই (config storage.public_last_price)।
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .stats import build_stats

PUBLIC_FIELDS = ["id", "pair", "direction", "entry", "tp", "sl", "rr", "timeframe", "score", "confirmed", "opened_at",
                 "status", "closed_at", "result_r", "ambiguous", "entry_valid_until", "strategy_version", "parts", "risk_atr"]
IDENT = ["-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com"]


def public_signals(state: dict) -> list[dict]:
    rows = [{k: s.get(k) for k in PUBLIC_FIELDS} for s in state["signals"]]
    return sorted(rows, key=lambda r: r["opened_at"], reverse=True)


def public_status(state: dict, cfg: dict) -> dict:
    return {"last_check": state.get("last_check"), "strategy_version": cfg["strategy"]["version"],
            "stale_after_hours": cfg["heartbeat"]["stale_after_hours"], "pairs": state.get("pairs", {})}


def _atomic(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class DataStore:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    def write(self, state: dict, cfg: dict) -> None:
        now = state.get("last_check") or ""
        _atomic(self.state_path, state)
        _atomic(self.dir / "signals.json", public_signals(state))
        _atomic(self.dir / "status.json", public_status(state, cfg))
        _atomic(self.dir / "stats.json", build_stats(state["signals"], now or "1970-01-01T00:00:00Z",
                                                       cfg["strategy"]["version"], cfg["backtest"]["min_trades_for_confidence"]))

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run(["git", *IDENT, *args], cwd=self.dir, capture_output=True, text=True)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {args[0]} ব্যর্থ: {(r.stderr or r.stdout).strip()[:200]}")
        return r

    def commit_push(self, message: str, retries: int = 3) -> bool:
        """পরিবর্তন থাকলে কমিট ও পুশ। অন্য কেউ আগে পুশ করলে rebase করে আবার চেষ্টা। সফল হলে True।"""
        self._git("add", "-A")
        if self._git("diff", "--cached", "--quiet", check=False).returncode == 0:
            return True                                       # বদলায়নি
        self._git("commit", "-q", "-m", message)
        last = ""
        branch = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        for _ in range(retries):
            r = self._git("push", "-q", "origin", "HEAD", check=False)
            if r.returncode == 0:
                return True
            last = (r.stderr or r.stdout).strip()[:200]
            self._git("pull", "-q", "--rebase", "origin", branch, check=False)
        raise RuntimeError(f"git push ব্যর্থ: {last}")
