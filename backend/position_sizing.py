"""
محاسبه حجم (lot) بر اساس درصد ریسک، و نام نماد در MT5
فرض: حساب دلاری، اندازه قرارداد استاندارد (فارکس ۱۰۰٬۰۰۰ واحد، طلا ۱۰۰ اونس)
"""
import math

from .config import BROKER_SUFFIX, GOLD_SYMBOL

_BASE_NAMES = {
    "EURUSD=X": "EURUSD", "GBPUSD=X": "GBPUSD", "USDJPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD", "USDCAD=X": "USDCAD", "NZDUSD=X": "NZDUSD",
    "USDCHF=X": "USDCHF", "EURGBP=X": "EURGBP", "EURJPY=X": "EURJPY",
    "GBPJPY=X": "GBPJPY",
}


def broker_symbol(symbol: str) -> str:
    """نام نماد تو MT5 (با پسوند بروکر)"""
    base = GOLD_SYMBOL if symbol == "GC=F" else _BASE_NAMES.get(symbol, symbol)
    return f"{base}{BROKER_SUFFIX}"


def _contract_size(symbol: str) -> float:
    return 100.0 if symbol == "GC=F" else 100_000.0


def _quote_to_usd(symbol: str, price: float, rates: dict):
    """۱ واحد ارز دوم (quote) جفت‌ارز = چند دلار؛ اگه نرخ لازم نبود None"""
    if symbol == "GC=F" or symbol.endswith("USD=X"):
        return 1.0
    if symbol in ("USDJPY=X", "USDCAD=X", "USDCHF=X"):
        return 1.0 / price
    if symbol == "EURGBP=X":
        gbpusd = (rates or {}).get("GBPUSD=X")
        return float(gbpusd) if gbpusd else None
    if symbol in ("EURJPY=X", "GBPJPY=X"):
        usdjpy = (rates or {}).get("USDJPY=X")
        return 1.0 / float(usdjpy) if usdjpy else None
    return None


def calc_lots(symbol, entry, sl, balance, risk_pct, rates=None):
    """
    حجم پیشنهادی (گرد به پایین، مضرب ۰.۰۱).
    اگه حداقل لات ۰.۰۱ هم ریسک رو از هدف بیشتر کنه، over=True برمی‌گردونه.
    """
    try:
        dist = abs(float(entry) - float(sl))
        conv = _quote_to_usd(symbol, float(entry), rates)
        if dist <= 0 or not conv or balance <= 0 or risk_pct <= 0:
            return None
        target = balance * risk_pct / 100.0
        loss_per_lot = dist * _contract_size(symbol) * conv
        lots = math.floor(target / loss_per_lot * 100) / 100
        over = lots < 0.01
        if over:
            lots = 0.01
        risk_usd = loss_per_lot * lots
        return {
            "lots": lots,
            "risk_usd": risk_usd,
            "risk_pct": risk_usd / balance * 100.0,
            "target_pct": risk_pct,
            "over": over,
        }
    except Exception:
        return None


def fractional_kelly_risk_pct(win_rate: float, win_loss_ratio: float, fraction: float = 0.25,
                              min_risk_pct: float = 0.25, max_risk_pct: float = 2.0) -> float:
    """محاسبه درصد ریسک بهینه پوزیشن بر اساس فرمول کلی تعدیل‌شده"""
    if win_rate <= 0 or win_loss_ratio <= 0:
        return min_risk_pct
    q = 1.0 - win_rate
    kelly_full = (win_rate * win_loss_ratio - q) / win_loss_ratio
    if kelly_full <= 0:
        return min_risk_pct
    raw_risk = kelly_full * fraction * 100.0
    return max(min_risk_pct, min(max_risk_pct, round(raw_risk, 2)))
