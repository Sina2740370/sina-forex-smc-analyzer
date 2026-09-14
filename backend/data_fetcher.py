"""
دریافت داده OHLCV از Yahoo Finance
"""
import warnings
import logging
import pandas as pd
import yfinance as yf

from .config import CANDLE_LIMIT, MIN_CANDLES

# خاموش کردن هشدارهای yfinance
warnings.filterwarnings("ignore")

logger = logging.getLogger(__name__)


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """تمیزکاری ستون‌ها و فرمت دیتا"""
    if df.empty:
        return df

    # اگه MultiIndex بود (بعضی وقتا yfinance اینطور می‌ده)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # rename به حروف کوچیک
    df = df.rename(columns={
        "Open": "open",
        "High": "high",
        "Low": "low",
        "Close": "close",
        "Volume": "volume",
    })

    # ستون زمان
    df = df.reset_index()
    if "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "time"})
    elif "Date" in df.columns:
        df = df.rename(columns={"Date": "time"})
    elif "index" in df.columns:
        df = df.rename(columns={"index": "time"})

    # فقط ستون‌های ضروری
    cols = ["time", "open", "high", "low", "close"]
    if "volume" in df.columns:
        cols.append("volume")
    else:
        df["volume"] = 0
        cols.append("volume")

    df = df[cols].dropna()
    return df


def fetch_ohlcv(symbol: str, timeframe: str = "15m",
                limit: int = CANDLE_LIMIT) -> list:
    """
    دریافت کندل‌های OHLCV از Yahoo Finance

    Args:
        symbol: نماد (مثل EURUSD=X یا GC=F)
        timeframe: تایم‌فریم (15m, 1h, 4h, 1d)
        limit: حداکثر تعداد کندل

    Returns:
        لیست دیکشنری‌های OHLCV
    """
    # تبدیل تایم‌فریم داخلی به yfinance
    interval_map = {
        "5m": "5m",
        "15m": "15m",
        "30m": "30m",
        "1h": "60m",
        "4h": "1h",   # yfinance 4h نداره، از 1h استفاده + resample
        "1d": "1d",
    }
    yf_interval = interval_map.get(timeframe, "15m")

    # بازه زمانی بر اساس تایم‌فریم
    if yf_interval in ("5m", "15m", "30m"):
        period = "5d"
    elif yf_interval == "60m":
        period = "1mo"
    else:
        period = "3mo"

    try:
        df = yf.download(
            symbol,
            interval=yf_interval,
            period=period,
            progress=False,
            auto_adjust=False,
            threads=False,
        )

        if df is None or df.empty:
            logger.warning(f"داده خالی برای {symbol} {timeframe}")
            return []

        df = _clean_dataframe(df)

        # اگه تایم‌فریم 4h بود → resample از 1h
        if timeframe == "4h" and yf_interval == "1h":
            df = _resample_to_4h(df)

        # فقط آخرین N کندل
        df = df.tail(limit)

        # تبدیل به لیست دیکشنری
        records = []
        for _, row in df.iterrows():
            records.append({
                "time": row["time"].isoformat() if hasattr(row["time"], "isoformat")
                        else str(row["time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })

        logger.info(f"✅ {symbol} {timeframe}: {len(records)} کندل")
        return records

    except Exception as e:
        logger.error(f"❌ خطا در fetch {symbol} {timeframe}: {e}")
        return []


def _resample_to_4h(df: pd.DataFrame) -> pd.DataFrame:
    """تبدیل کندل‌های 1h به 4h"""
    df = df.set_index("time")
    resampled = pd.DataFrame({
        "open": df["open"].resample("4h").first(),
        "high": df["high"].resample("4h").max(),
        "low": df["low"].resample("4h").min(),
        "close": df["close"].resample("4h").last(),
        "volume": df["volume"].resample("4h").sum(),
    }).dropna()
    return resampled.reset_index()


def fetch_multi_timeframe(symbol: str, htf: str = "1h",
                           ltf: str = "15m") -> dict:
    """
    دریافت همزمان دو تایم‌فریم

    Returns:
        {"htf": [...], "ltf": [...]} یا {"error": "..."}
    """
    htf_data = fetch_ohlcv(symbol, htf)
    ltf_data = fetch_ohlcv(symbol, ltf)

    if len(htf_data) < MIN_CANDLES:
        return {"error": f"HTF داده کم ({len(htf_data)} < {MIN_CANDLES})"}
    if len(ltf_data) < MIN_CANDLES:
        return {"error": f"LTF داده کم ({len(ltf_data)} < {MIN_CANDLES})"}

    return {"htf": htf_data, "ltf": ltf_data}
