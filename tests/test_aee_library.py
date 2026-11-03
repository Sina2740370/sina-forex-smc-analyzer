"""تست‌های فاز ۲ — کتابخانهٔ ویژگی‌ها و چارچوب نمره‌دهی."""
import sys

sys.path.insert(0, "/home/z/my-project/sweep-dev")

import numpy as np
import pandas as pd
import pytest

from aee.library import LIBRARY_FEATURES, build_library
from aee.scoring import (causal_zscore, extract_events, forward_outcome,
                         fdr_pass_two_windows)


def _synthetic_ohlc(n=800, seed=11):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.0007, n)
    close = 1.1 + np.cumsum(steps)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + rng.uniform(0, 0.0005, n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.0005, n)
    return pd.DataFrame({
        "time": pd.date_range("2021-01-04 00:00", periods=n, freq="15min"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.uniform(100, 1000, n),
    })


@pytest.fixture(scope="module")
def df():
    return _synthetic_ohlc()


def test_all_library_features_prefix_invariant(df):
    """خاصیت ضدنشتی برای هر ۲۲ ویژگی: ویژگی(t) فقط تابع دادهٔ ≤ t."""
    full = build_library(df)
    for k in (150, 500):
        part = build_library(df.iloc[:k].reset_index(drop=True))
        for col in full.columns:
            a = full[col].iloc[:k].to_numpy(dtype=float)
            b = part[col].iloc[:k].to_numpy(dtype=float)
            m = ~(np.isnan(a) & np.isnan(b))
            assert np.allclose(a[m], b[m], rtol=1e-10, atol=1e-12), col


def test_library_warmup_and_eventual_values(df):
    feats = build_library(df)
    for col in feats.columns:
        assert feats[col].iloc[100:].notna().any(), col   # بعد از گرم‌شدنی مقدار دارد
    for col in ("vol_ratio_5_20", "efficiency_16", "macd_norm", "boll_pos_20",
                "ret_64", "donchian_pos_50", "inside_bar_ratio_20"):
        assert feats[col].iloc[:15].isna().all(), col     # گرم‌شدنی پنجره‌ای
    assert feats["rsi_14"].iloc[:14].isna().all()
    # ویژگی‌های رویدادی قبل از گرم‌شدنی «بدون رویداد» (0) هستند، نه NaN
    assert (feats["pivot_break_20"].iloc[:20] == 0).all()
    assert (feats["fvg_flag"].iloc[:2] == 0).all()


def test_streak_counts_consecutive_direction():
    df = _synthetic_ohlc(8, seed=1)
    df["close"] = [1.0, 1.1, 1.2, 1.15, 1.05, 1.03, 1.08, 1.20]
    s = build_library(df)["streak"]
    assert list(s.iloc[1:7]) == [1.0, 2.0, -1.0, -2.0, -3.0, 1.0]


def test_pivot_break_flags_breakout():
    df = _synthetic_ohlc(60, seed=2)
    df["close"] = 1.1 + np.sin(np.arange(60)) * 0.0001   # نوسان جزئی
    df["high"] = df["close"] + 0.0001
    df["low"] = df["close"] - 0.0001
    df.loc[55, ["open", "high", "low", "close"]] = [1.15, 1.1501, 1.1499, 1.15]
    df.loc[56:, ["open", "high", "low", "close"]] = [1.15, 1.1501, 1.1499, 1.15]
    s = build_library(df)["pivot_break_20"]
    assert s.iloc[55] == 1.0                    # خودِ کندل شکست رویداد است
    assert (s.iloc[:55].dropna() == 0).all()    # قبل از شکست هیچ رویدادی نیست


def test_fvg_flag_detects_gap():
    df = _synthetic_ohlc(10, seed=3)
    # کندل سوم جایگزین: کندل صعودی جابه‌جاشده که low آن بالای high کندل اول است
    df.loc[2, ["open", "high", "low", "close"]] = [1.1027, 1.1037, 1.1026, 1.1030]
    s = build_library(df)["fvg_flag"]
    assert s.iloc[2] == 1.0


def test_causal_zscore_prefix_invariant():
    df = _synthetic_ohlc(400)
    z_full = causal_zscore(df["close"]).to_numpy(dtype=float)
    z_part = causal_zscore(df["close"].iloc[:200].reset_index(drop=True)).to_numpy(dtype=float)
    m = ~np.isnan(z_part)
    assert np.allclose(z_full[:200][m], z_part[m], rtol=1e-10)


def test_extract_events_cooldown():
    idx = pd.RangeIndex(20)
    z = pd.Series([0, 3, 3, 0, 0, -3, 0, 0, 0, 0, 2.5, 0, 0, 0, 0, 0, 0, -2.1, 0, 0], dtype=float, index=idx)
    ev = extract_events(z, threshold=2.0, cooldown=3)
    # رویداد ۱ در idx=1، سپس تا idx=3 مسکوت؛ idx=5 مجاز؛ idx=10 داخل cooldown رویداد ۵ (۵+3=8)? خیر، 10>8 → مجاز؛ idx=17 مجاز
    assert list(ev["idx"]) == [1, 5, 10, 17]
    assert list(ev["direction"]) == [1.0, -1.0, 1.0, -1.0]


def test_forward_outcome_value():
    n = 40
    df = pd.DataFrame({
        "time": pd.date_range("2021-01-01", periods=n, freq="15min"),
        "open": np.full(n, 1.0), "high": np.full(n, 1.1), "low": np.full(n, 0.9),
        "close": np.concatenate([np.full(20, 1.0), np.full(20, 1.2)]),
        "volume": np.ones(n),
    })
    ev = pd.DataFrame({"idx": [19], "time": [df["time"].iloc[19]], "direction": [1.0]})
    out = forward_outcome(df, ev, horizon=16)
    # ATR لحظهٔ 19 ≈ 0.2 (کلوز از 1.0→1.2 همان ساعت جاری)؛ بازده 16 کندل بعد = +0.2
    assert out.iloc[0] == pytest.approx(1.0, rel=0.15)


def test_fdr_pass_requires_both_windows_same_sign():
    res1 = pd.DataFrame({"feature": ["a", "b", "c"],
                         "er": [0.5, -0.4, 0.2],
                         "p_ttest": [0.001, 0.001, 0.001]})
    res2 = pd.DataFrame({"feature": ["a", "b", "c"],
                         "er": [0.4, -0.35, -0.2],
                         "p_ttest": [0.002, 0.002, 0.002]})
    passed = fdr_pass_two_windows(res1, res2, alpha=0.05)
    assert passed == ["a", "b"]          # c در دو پنجره علامت عوض می‌کند


def test_fdr_rejects_when_one_window_weak():
    res1 = pd.DataFrame({"feature": ["a"], "er": [0.5], "p_ttest": [0.001]})
    res2 = pd.DataFrame({"feature": ["a"], "er": [0.4], "p_ttest": [0.4]})
    assert fdr_pass_two_windows(res1, res2) == []
