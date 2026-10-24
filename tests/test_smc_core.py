"""تست‌های هسته‌ی SMC: swings (پلاتو و تخت و غیرمجاور) + liquidity_sweep + عدم نشتی آینده."""
import numpy as np
import pandas as pd

from engine.core import smc


def _ohlc(highs, lows=None, closes=None):
    n = len(highs)
    highs = np.asarray(highs, dtype=float)
    lows = np.asarray(lows if lows is not None else highs - 1.0, dtype=float)
    closes = np.asarray(closes if closes is not None else (highs + lows) / 2, dtype=float)
    opens = closes.copy()
    return pd.DataFrame({
        "open": opens, "high": highs, "low": lows, "close": closes,
        "time": pd.date_range("2025-01-01", periods=n, freq="15min", tz="UTC"),
    })


def test_adjacent_equal_highs_collapse_to_one_swing():
    """سقف‌های هم‌قیمت مجاور = یک سوئینگ."""
    h = [1, 1, 1, 5.0, 5.0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    df = _ohlc(h, lows=[0] * len(h))
    highs, lows = smc.swings(df, left=3, right=3)
    equal5 = [s for s in highs if s[1] == 5.0]
    assert len(equal5) == 1, f"expected 1 plateau high, got {equal5}"
    assert equal5[0][0] in (3, 4)


def test_flat_data_does_not_produce_mass_swings():
    """دادهِ تخت نباید سوئینگ انبوه بسازد."""
    n = 30
    df = _ohlc([10.0] * n, lows=[9.0] * n)
    highs, lows = smc.swings(df, left=3, right=3)
    assert len(highs) <= 1, f"flat highs produced {len(highs)} swings: {highs}"
    assert len(lows) <= 1, f"flat lows produced {len(lows)} swings: {lows}"


def test_non_adjacent_equal_highs_both_kept():
    """سقف‌های هم‌قیمت غیرمجاور (ایندکس 3 و 10) هر دو باقی بمانند."""
    h = [1, 1, 1, 5.0, 1, 1, 1, 1, 1, 1, 5.0, 1, 1, 1, 1, 1, 1, 1]
    df = _ohlc(h, lows=[0] * len(h))
    highs, _ = smc.swings(df, left=3, right=3)
    equal5 = sorted([s for s in highs if abs(s[1] - 5.0) < 1e-9])
    assert len(equal5) == 2, f"expected both non-adjacent highs, got {equal5}"
    assert equal5[0][0] == 3 and equal5[1][0] == 10


def test_triple_adjacent_plateau_collapses_to_one():
    """پلاتوی متوالی سه‌تایی (ایندکس 3،4،5 هم‌قیمت) یکی شود."""
    h = [1, 1, 1, 5.0, 5.0, 5.0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]
    df = _ohlc(h, lows=[0] * len(h))
    highs, _ = smc.swings(df, left=3, right=3)
    equal5 = [s for s in highs if abs(s[1] - 5.0) < 1e-9]
    assert len(equal5) == 1, f"expected 1 from triple plateau, got {equal5}"
    assert equal5[0][0] in (3, 4, 5)


def test_liquidity_sweep_returns_newest():
    """
    sweep نزولی روی کندل قدیمی‌ترِ recent و صعودی روی جدیدترین
    => باید BULLISH_SWEEP برگردد.
    """
    n = 110
    lookback, recent = 20, 3
    highs = np.full(n, 10.0)
    lows = np.full(n, 9.0)
    closes = np.full(n, 9.5)
    i_bear = n - 3
    i_bull = n - 1
    highs[i_bear] = 10.5
    closes[i_bear] = 9.8
    lows[i_bull] = 8.5
    closes[i_bull] = 9.2
    df = _ohlc(highs, lows=lows, closes=closes)
    sw = smc.liquidity_sweep(df, lookback=lookback, recent=recent)
    assert sw is not None
    assert sw["type"] == "BULLISH_SWEEP", f"expected newest BULLISH, got {sw}"


def test_swings_no_lookahead_on_prefix():
    """
    نبود نشتی آینده: swings روی prefix با swings روی کل داده
    (فقط ایندکس‌های تأییدشدهِ prefix) یکسان است.
    """
    rng = np.random.default_rng(42)
    n = 80
    base = np.cumsum(rng.normal(0, 0.3, n)) + 50
    highs = base + rng.uniform(0.1, 0.5, n)
    lows = base - rng.uniform(0.1, 0.5, n)
    df_full = _ohlc(highs, lows=lows)
    left, right = 3, 3
    cut = 50
    df_prefix = df_full.iloc[:cut].reset_index(drop=True)

    h_pref, l_pref = smc.swings(df_prefix, left=left, right=right)
    h_full, l_full = smc.swings(df_full, left=left, right=right)

    max_confirmed = cut - 1 - right
    h_full_conf = [(i, p) for i, p in h_full if i <= max_confirmed]
    l_full_conf = [(i, p) for i, p in l_full if i <= max_confirmed]

    assert h_pref == h_full_conf, f"highs mismatch:\nprefix={h_pref}\nfull_conf={h_full_conf}"
    assert l_pref == l_full_conf, f"lows mismatch:\nprefix={l_pref}\nfull_conf={l_full_conf}"
