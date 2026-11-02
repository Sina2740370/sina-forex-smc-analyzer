"""تست‌های فاز ۰ چارچوب AEE: آمار، تقسیم‌کنندهٔ purge/embargo، ضدنشتی ویژگی‌ها."""
import sys

sys.path.insert(0, "/home/z/my-project/sweep-dev")

import numpy as np
import pandas as pd
import pytest

from aee import PurgedWalkForward, benjamini_hochberg, bootstrap_mean_ci, event_study_summary
from aee.features import BASE_FEATURES, build_features


# ---------- Benjamini–Hochberg ----------

def test_bh_known_case():
    p = [0.01, 0.04, 0.03, 0.005]
    q = benjamini_hochberg(p)
    # مقدار مرجع محاسبه‌شده با statsmodels multipletests(method='fdr_bh')
    expected = [0.02, 0.04, 0.04, 0.02]
    assert np.allclose(q, expected)


def test_bh_monotone_and_bounded():
    rng = np.random.default_rng(0)
    p = rng.uniform(0, 1, 500)
    q = benjamini_hochberg(p)
    assert np.all((q >= 0) & (q <= 1))
    # q یک تابع غیرنزولی از p است
    order = np.argsort(p)
    assert np.all(np.diff(q[order]) >= -1e-12)
    # p=1 دقیقاً q=1 می‌ماند (n=1)
    assert benjamini_hochberg([1.0])[0] == 1.0


def test_bh_empty():
    assert benjamini_hochberg([]).size == 0


# ---------- bootstrap ----------

def test_bootstrap_ci_covers_true_mean():
    rng = np.random.default_rng(1)
    x = rng.normal(0.5, 1.0, 2000)
    m, lo, hi = bootstrap_mean_ci(x, n_boot=2000, seed=3)
    assert abs(m - x.mean()) < 1e-12
    assert lo < 0.5 < hi
    assert lo < hi


def test_bootstrap_nan_and_tiny_input():
    m, lo, hi = bootstrap_mean_ci([np.nan, 1.0])
    assert lo != lo and hi != hi  # نمونهٔ تک‌عضوی → بدون CI
    m, lo, hi = bootstrap_mean_ci([1.0, 2.0, np.nan])
    assert m == 1.5 and lo <= 1.5 <= hi


# ---------- event study ----------

def test_event_study_summary_positive_edge():
    rng = np.random.default_rng(7)
    x = rng.normal(0.0, 1.0, 800)
    x += 0.1 - x.mean()          # میانگین دقیقاً 0.1 با نویز واقع‌نما
    s = event_study_summary(x, "demo")
    assert s["n"] == 800
    assert s["ci_lo"] > 0          # لبهٔ مثبت واقعی → CI مثبت
    assert s["win_rate"] > 0.5
    assert s["p_ttest"] < 0.01


def test_event_study_no_edge():
    rng = np.random.default_rng(8)
    x = rng.normal(0.0, 1.0, 800)
    s = event_study_summary(x, "demo")
    assert s["ci_lo"] < 0 < s["ci_hi"]


# ---------- PurgedWalkForward ----------

def _toy_index(n=100):
    return pd.date_range("2020-01-01", periods=n, freq="15min")


def test_pwf_geometry_and_purge_gap():
    idx = _toy_index(200)
    folds = PurgedWalkForward(train_bars=50, test_bars=20, embargo_bars=5).split(idx)
    assert folds, "حداقل یک فولد"
    for f in folds:
        tr, te = f["train_slice"], f["test_slice"]
        assert tr.stop + 5 == te.start          # گپ تحریم دقیقاً ۵ کندل
        assert te.stop - te.start <= 20
        assert tr.start >= 0 and te.stop <= len(idx)
    # پنجره‌ها جلو می‌روند و هم‌پوشانی ندارند
    for a, b in zip(folds, folds[1:]):
        assert b["test_slice"].start > a["test_slice"].start


def test_pwf_by_time_embargo():
    idx = pd.date_range("2020-01-01", periods=96 * 40, freq="15min")
    folds = PurgedWalkForward(1, 1).split_by_time(idx, train_days=10, test_days=5,
                                                  embargo_hours=6)
    assert len(folds) >= 3
    for f in folds:
        gap = f["test"][0] - f["train"][-1]
        assert gap >= pd.Timedelta(hours=6) - pd.Timedelta(minutes=15)
        assert (f["test"][-1] - f["test"][0]) <= pd.Timedelta(days=5)
        assert len(f["test"]) > 0


# ---------- ضدنشتی ویژگی‌ها ----------

def _synthetic_ohlc(n=600, seed=5):
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.0007, n)
    close = 1.1 + np.cumsum(steps)
    open_ = np.roll(close, 1)
    open_[0] = close[0]
    high = np.maximum(open_, close) + rng.uniform(0, 0.0005, n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.0005, n)
    df = pd.DataFrame({
        "time": pd.date_range("2021-01-01", periods=n, freq="15min"),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.uniform(100, 1000, n),
    })
    return df


def test_features_are_prefix_invariant():
    """خاصیت ضدنشتی: ویژگی در نقطهٔ t فقط تابع دادهٔ ≤ t است."""
    df = _synthetic_ohlc()
    full = build_features(df)
    for k in (30, 120, 400):
        part = build_features(df.iloc[:k].reset_index(drop=True))
        np.testing.assert_allclose(
            full.iloc[:k - 1].dropna(axis=1, how="all").to_numpy(dtype=float),
            part.iloc[:k - 1].dropna(axis=1, how="all").to_numpy(dtype=float),
            rtol=1e-12, atol=1e-15,
        )


def test_features_warmup_nan():
    """ویژگی‌های پنجره‌ای قبل از گرم‌شدنی NaN می‌مانند؛ بازده‌ها فقط سرِ سری NaN دارند."""
    df = _synthetic_ohlc()
    feats = build_features(df)
    for name, warm in (("realized_vol_20", 19), ("atr_14", 13),
                       ("range_expansion_20", 20), ("donchian_pos_20", 20)):
        assert feats[name].iloc[:warm].isna().all(), name
        assert feats[name].iloc[-50:].notna().any(), name
    for name in ("ret_1", "ret_4", "ret_16", "body_ratio"):
        assert feats[name].iloc[-50:].notna().all(), name


def test_donchian_and_session_ranges():
    df = _synthetic_ohlc(400)
    feats = build_features(df)
    d = feats["donchian_pos_20"].dropna()
    # موقعیت کانال حق دارد در برک‌اوت از [0,1] خارج شود؛ اما متناهی و پوشاننده است
    assert np.isfinite(d).all()
    assert d.min() < 0.05 and d.max() > 0.95
    sess_cols = [c for c in feats.columns if c.startswith("session_")]
    assert feats[sess_cols].sum(axis=1).eq(1.0).all()   # دقیقاً یک نشست
