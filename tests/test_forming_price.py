import os
os.environ.setdefault("FRED_API_KEY", "x"); os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x"); os.environ.setdefault("TELEGRAM_CHAT_ID", "1")
import pandas as pd, numpy as np
from backend import analysis_engine as ae

def _df(minutes, n, end_open, last_close):
    t = pd.date_range(end=end_open, periods=n, freq=f"{minutes}min", tz="UTC")
    c = np.full(n, 1.0); c[-1] = last_close
    return pd.DataFrame({"time": t, "open": c, "high": c + 0.001, "low": c - 0.001, "close": c, "volume": 1})

def test_price_ignores_forming_candle():
    now = pd.Timestamp.now(tz="UTC")
    open15 = now.floor("15min")                      # کندل 15m جاری هنوز بسته نشده
    data = {"15m": _df(15, 200, open15, 2.0),         # آخرین کندل (در حال تشکیل) close=2.0
            "1h": _df(60, 200, now.floor("1h"), 1.0),
            "4h": _df(240, 100, now.floor("4h"), 1.0),
            "1d": _df(1440, 60, now.floor("1D"), 1.0),
            "1wk": _df(10080, 60, now.floor("1D"), 1.0)}
    out = ae.analyze_technical_topdown(data, "EURUSD")
    assert out["price"] == 1.0, f"price از کندل در حال تشکیل آمده: {out['price']}"
