"""ইন্ডিকেটর টেস্ট: পরিচিত মান, সীমান্ত অবস্থা, লুক-অ্যাহেড নেই।"""
import numpy as np
import pandas as pd

from src.indicators import add_indicators, atr, ema, rsi


def s(values):
    return pd.Series(values, dtype=float)


def test_ema_known_values():
    out = ema(s(range(1, 11)), 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2] == 2.0            # প্রথম ৩টার গড়
    assert abs(out.iloc[3] - 3.0) < 1e-12  # ০.৫×৪ + ০.৫×২
    assert abs(out.iloc[-1] - 9.0) < 1e-12


def test_ema_short_series_is_all_nan():
    assert ema(s([1, 2]), 5).isna().all()


def test_rsi_extremes():
    assert rsi(s(range(1, 40)), 14).iloc[-1] == 100.0
    assert rsi(s(range(40, 1, -1)), 14).iloc[-1] == 0.0
    assert rsi(s([5.0] * 40), 14).iloc[-1] == 50.0


def test_rsi_hand_computed():
    out = rsi(s([1, 2, 1, 2]), 2)
    assert out.iloc[:2].isna().all()
    assert abs(out.iloc[2] - 50.0) < 1e-12
    assert abs(out.iloc[3] - 75.0) < 1e-12   # avg_gain=0.75, avg_loss=0.25


def test_atr_constant_range():
    c = s([100.0] * 30)
    assert abs(atr(c + 1, c - 1, c, 14).iloc[-1] - 2.0) < 1e-12


def test_atr_uses_previous_close_gap():
    h, l, c = s([10, 12]), s([9, 11]), s([9.5, 11.5])
    out = atr(h, l, c, 2)
    assert np.isnan(out.iloc[0])
    assert abs(out.iloc[1] - 1.75) < 1e-12   # TR: 1.0 এবং max(1, 2.5, 1.5)=2.5


def test_matches_pandas_rma_after_warmup():
    rng = np.random.default_rng(7)
    c = s(100 + np.cumsum(rng.normal(0, 1, 800)))
    d = c.diff()
    g = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    ref = 100 - 100 / (1 + g / l)
    assert abs(rsi(c, 14).iloc[-1] - ref.iloc[-1]) < 1e-6


def test_no_lookahead():
    rng = np.random.default_rng(3)
    c = s(100 + np.cumsum(rng.normal(0, 1, 300)))
    h, l = c + 0.5, c - 0.5
    i = 200
    assert abs(ema(c, 50).iloc[i] - ema(c[: i + 1], 50).iloc[i]) < 1e-12
    assert abs(rsi(c, 14).iloc[i] - rsi(c[: i + 1], 14).iloc[i]) < 1e-12
    assert abs(atr(h, l, c, 14).iloc[i] - atr(h[: i + 1], l[: i + 1], c[: i + 1], 14).iloc[i]) < 1e-12


def test_add_indicators_columns():
    cfg = {"indicators": {"ema_fast": 20, "ema_mid": 50, "ema_slow": 200, "rsi_period": 14, "atr_period": 14}}
    c = s(100 + np.arange(300) * 0.1)
    df = pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c})
    out = add_indicators(df, cfg)
    for col in ["ema_fast", "ema_mid", "ema_slow", "rsi", "atr"]:
        assert col in out and not np.isnan(out[col].iloc[-1])
    assert "ema_fast" not in df  # মূল ডেটা অপরিবর্তিত
