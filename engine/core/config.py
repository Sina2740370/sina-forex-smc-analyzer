"""
تنظیمات موتور + مشخصات نمادها.
همه‌ی پارامترهای استراتژی اینجاست و hash دارد؛ هر سیگنال/بک‌تست با این hash ثبت
می‌شود تا نتیجه‌ها قابل مقایسه و تکرارپذیر باشند.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EngineConfig:
    # --- آستانه و ریسک ---
    min_score: float = 0.60          # آستانه‌ی تک‌مقیاسی (قبلاً دو آستانه روی دو مقیاس بود)
    risk_reward: float = 2.0
    sl_atr1h_mult: float = 0.5       # بافر SL بر حسب ATR تایم 1h
    sl_ob_buffer: float = 0.3        # بافر SL نسبت به اندازه OB
    min_risk_atr1h: float = 0.3      # حداقل فاصله SL (ضریب ATR 1h)
    max_risk_atr4h: float = 1.5      # سقف فاصله SL (ضریب ATR 4h)  [جدید]
    ob_gate_atr4h: float = 0.75      # فاصله‌ی مجاز تا OB برای پاس گیت (بر حسب ATR 4h)

    # --- پنجره‌ها ---
    sweep_lookback: int = 96         # ۲۴ ساعت روی 15m (قبلاً ۳۰ کندل = ۷.۵ ساعت)
    break_lookback: int = 40
    fvg_lookback: int = 15
    fvg_min_gap_ratio: float = 0.15
    ob_lookback: int = 60
    pd_lookback: int = 60

    # --- وزن ویژگی‌ها (هر کدام فقط یک‌بار شمرده می‌شود) ---
    w_htf_full: float = 0.15
    w_htf_soft: float = 0.05
    w_sweep: float = 0.25
    w_sweep_against: float = 0.08
    w_choch: float = 0.18
    w_bos: float = 0.08
    w_ob_in: float = 0.30
    w_ob_near: float = 0.20
    w_ob_mid: float = 0.10
    w_pd: float = 0.12
    w_fvg: float = 0.12
    w_candle: float = 0.10
    w_killzone: float = 0.05
    w_confluence: float = 0.10       # Sweep + CHOCH + (FVG یا OB) با هم

    # --- مدیریت معامله ---
    max_hold_hours: int = 72
    cooldown_hours: float = 4.0
    scan_every_bars: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    def hash(self) -> str:
        raw = json.dumps(asdict(self), sort_keys=True).encode()
        return hashlib.sha1(raw).hexdigest()[:10]


@dataclass(frozen=True)
class Instrument:
    name: str
    pip: float
    spread_pips: float   # اسپرد معمول؛ با اسپرد واقعی بروکرت جایگزین کن
    yf: str              # نماد yfinance


# نام‌ها استاندارد (بدون پسوند بروکر)
INSTRUMENTS = {
    "EURUSD": Instrument("EURUSD", 0.0001, 0.8, "EURUSD=X"),
    "GBPUSD": Instrument("GBPUSD", 0.0001, 1.2, "GBPUSD=X"),
    "USDJPY": Instrument("USDJPY", 0.01,   1.0, "USDJPY=X"),
    "AUDUSD": Instrument("AUDUSD", 0.0001, 1.2, "AUDUSD=X"),
    "USDCAD": Instrument("USDCAD", 0.0001, 1.5, "USDCAD=X"),
    "NZDUSD": Instrument("NZDUSD", 0.0001, 1.8, "NZDUSD=X"),
    "USDCHF": Instrument("USDCHF", 0.0001, 1.5, "USDCHF=X"),
    "EURGBP": Instrument("EURGBP", 0.0001, 1.5, "EURGBP=X"),
    "EURJPY": Instrument("EURJPY", 0.01,   1.8, "EURJPY=X"),
    "GBPJPY": Instrument("GBPJPY", 0.01,   2.5, "GBPJPY=X"),
    # توجه: yfinance برای طلا فقط فیوچرز (GC=F) دارد، نه اسپات XAUUSD.
    # اسپرد قبلی (۲.۵ دلار) غیرواقعی بود؛ اینجا ~۰.۳۰ دلار. با بروکر خودت چک کن.
    "XAUUSD": Instrument("XAUUSD", 0.1,    3.0, "GC=F"),
}


def spread_price(symbol: str) -> float:
    ins = INSTRUMENTS[symbol]
    return ins.spread_pips * ins.pip
