"""
محاسبه حجم (lot) بر اساس درصد ریسک، و نام نماد در MT5
فرض: حساب دلاری، اندازه قرارداد استاندارد (فارکس ۱۰۰٬۰۰۰ واحد، طلا ۱۰۰ اونس)
"""
import math

from .config import BROKER_SUFFIX, GOLD_SYMBOL, PIP_SIZE, SPREAD

_BASE_NAMES = {
    "EURUSD=X": "EURUSD", "GBPUSD=X": "GBPUSD", "USDJPY=X": "USDJPY",
    "AUDUSD=X": "AUDUSD", "USDCAD=X": "USDCAD", "NZDUSD=X": "NZDUSD",
    "USDCHF=X": "USDCHF", "EURGBP=X": "EURGBP", "EURJPY=X": "EURJPY",
    "GBPJPY=X": "GBPJPY",
}


def broker_symbol(symbol: str) -> str:
    """نام نماد تو MT5 (با پسوند بروکر)"""
    # GC=F is a futures proxy, not a tradable XAUUSD spot quote.
    # Never silently translate its prices into the broker's spot-gold symbol.
    base = _BASE_NAMES.get(symbol, symbol)
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
        if symbol == "GC=F":
            # Contract specs and basis differ by futures venue; don't invent spot sizing.
            return None
        dist = abs(float(entry) - float(sl))
        # Signal entry/SL are mid-price levels. A market BUY enters at ask and
        # a market SELL at bid, so executable stop risk includes entry half-spread.
        if symbol in PIP_SIZE and symbol in SPREAD:
            dist += PIP_SIZE[symbol] * SPREAD[symbol] / 2.0
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
