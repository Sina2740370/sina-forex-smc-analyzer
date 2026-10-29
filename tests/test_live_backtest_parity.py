import pytest
import pandas as pd
from datetime import datetime, timezone
from engine.core.config import EngineConfig
from engine.strategy.smc_topdown import analyze as smc_analyze
from backend.analysis_engine import analyze_technical_topdown


def test_live_backtest_technical_parity():
    """تست هم‌سویی ۱۰۰٪ خروجی تحلیل تکنیکال زنده با استراتژی موتور بک‌تست"""
    times = pd.date_range("2026-01-01", periods=200, freq="15min", tz="UTC")
    df15 = pd.DataFrame({
        "time": times,
        "open": 1.1000 + pd.Series(range(200)) * 0.0001,
        "high": 1.1010 + pd.Series(range(200)) * 0.0001,
        "low": 1.0990 + pd.Series(range(200)) * 0.0001,
        "close": 1.1005 + pd.Series(range(200)) * 0.0001,
        "volume": 1000,
    })

    df1h = df15.iloc[::4].reset_index(drop=True)
    df4h = df15.iloc[::16].reset_index(drop=True)
    df1d = df15.iloc[::96].reset_index(drop=True)
    df1wk = df15.iloc[::672].reset_index(drop=True)

    data_dict = {
        "15m": df15,
        "1h": df1h,
        "4h": df4h,
        "1d": df1d,
        "1wk": df1wk,
    }

    now = times[-1]
    cfg = EngineConfig()

    dec_backtest = smc_analyze(data_dict, cfg, pd.Timestamp(now))
    res_live = analyze_technical_topdown(data_dict, symbol="EURUSD=X", now=now)

    # مقایسه جهت معامله، حد ضرر، حد سود و امتیاز
    assert res_live["direction"] == dec_backtest.direction
    assert res_live["suggested_sl"] == dec_backtest.sl
    assert res_live["suggested_tp"] == dec_backtest.tp
    assert res_live["score"] == round(dec_backtest.score, 2)
