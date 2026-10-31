"""
ابزار ریسک برای معامله‌ی دستی (خالص، بدون I/O).

فرض‌ها (قابل تغییر با پارامتر): حساب به دلار؛ لات استاندارد 100,000 واحد؛ طلا 100 اونس.
مشخصات واقعی بروکر (contract size، گام لات، حداقل لات) را از MT5 چک کن؛ این‌جا فقط پیش‌فرض است.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# quote: ارز دوم جفت؛ pip: اندازه‌ی یک پیپ؛ contract: اندازه‌ی لات
# کراس‌ها (EURJPY و…) به نرخ ارز کوت نسبت به دلار نیاز دارند (cross_rates)
SPECS = {
    "EURUSD": {"pip": 0.0001, "contract": 100_000, "quote": "USD"},
    "GBPUSD": {"pip": 0.0001, "contract": 100_000, "quote": "USD"},
    "AUDUSD": {"pip": 0.0001, "contract": 100_000, "quote": "USD"},
    "NZDUSD": {"pip": 0.0001, "contract": 100_000, "quote": "USD"},
    "USDJPY": {"pip": 0.01, "contract": 100_000, "quote": "JPY"},
    "USDCAD": {"pip": 0.0001, "contract": 100_000, "quote": "CAD"},
    "USDCHF": {"pip": 0.0001, "contract": 100_000, "quote": "CHF"},
    "EURJPY": {"pip": 0.01, "contract": 100_000, "quote": "JPY"},
    "GBPJPY": {"pip": 0.01, "contract": 100_000, "quote": "JPY"},
    "AUDJPY": {"pip": 0.01, "contract": 100_000, "quote": "JPY"},
    "EURGBP": {"pip": 0.0001, "contract": 100_000, "quote": "GBP"},
    "XAUUSD": {"pip": 0.1, "contract": 100, "quote": "USD"},
}


@dataclass(frozen=True)
class SizeResult:
    ok: bool
    lots: float
    risk_amount: float          # ریسک درخواستی (دلار)
    actual_risk: float          # ریسک واقعی با لات رُندشده (دلار)
    sl_distance: float          # فاصله‌ی SL به واحد قیمت
    sl_pips: float
    rr: Optional[float]
    note: str = ""
    margin: Optional[float] = None       # مارجین لازم (دلار)، فقط اگر leverage داده شده
    margin_pct: Optional[float] = None   # مارجین به درصد موجودی
    free_margin: Optional[float] = None  # مارجین آزاد پس از ورود
    margin_limited: bool = False         # حجم به‌خاطر سقف مارجین کم شده؟


def loss_per_lot_per_unit(symbol: str, price: float, contract: Optional[float] = None,
                          quote_to_usd: Optional[float] = None) -> float:
    """زیان یک لات برای حرکت یک واحد قیمت، به دلار.

    برای کراس‌ها (مثل EURJPY) ارزش به ارز کوت است و باید با نرخ ارز کوت به دلار
    تقسیم شود (مثلاً برای JPY: نرخ USDJPY). قبلاً کراس‌ها با ValueError رد می‌شدند."""
    sp = SPECS[symbol]
    c = contract if contract is not None else sp["contract"]
    if sp["quote"] == "USD":
        return c
    if symbol.startswith("USD"):          # USDJPY/USDCAD/USDCHF: ارزش به ارز دوم است
        return c / price
    # کراس: EURJPY/GBPJPY/AUDJPY/EURGBP — تبدیل ارزش ارز کوت به دلار
    if not quote_to_usd or quote_to_usd <= 0:
        raise ValueError(
            f"محاسبه‌ی {symbol} به نرخ {sp['quote']}USD نیاز دارد؛ "
            f"cross_rates={{'{sp['quote']}': نرخ}} را بده (مثلاً USDJPY برای جفت‌های ین)"
        )
    return c / quote_to_usd


def _decimals(step: float) -> int:
    """تعداد رقم اعشار گام لات (0.01 → 2، 0.1 → 1، 0.001 → 3)."""
    return max(0, -int(math.floor(math.log10(step) + 1e-9)))


def margin_per_lot(symbol: str, price: float, leverage: float,
                   contract: Optional[float] = None,
                   quote_to_usd: Optional[float] = None) -> float:
    """مارجین یک لات به دلار: ارزش اسمی موقعیت ÷ اهرم.
    برای کراس‌ها ارزش اسمی به ارز کوت است و به دلار تبدیل می‌شود."""
    if symbol not in SPECS:
        raise ValueError(f"نماد پشتیبانی نمی‌شود: {symbol}")
    if leverage <= 0:
        raise ValueError("leverage باید مثبت باشد")
    sp = SPECS[symbol]
    c = contract if contract is not None else sp["contract"]
    if symbol.startswith("USD"):
        notional = c                                     # USD-base: ارزش اسمی خودش دلار است
    elif sp["quote"] == "USD":
        notional = c * price
    else:                                                # کراس: تبدیل به دلار
        if not quote_to_usd or quote_to_usd <= 0:
            raise ValueError(
                f"محاسبه‌ی مارجین {symbol} به نرخ {sp['quote']}USD نیاز دارد (cross_rates)"
            )
        notional = c * price / quote_to_usd
    return notional / leverage


def position_size(symbol: str, balance: float, risk_pct: float, entry: float, sl: float,
                  tp: Optional[float] = None, lot_step: float = 0.01, min_lot: float = 0.01,
                  max_lot: float = 100.0, contract: Optional[float] = None,
                  leverage: Optional[float] = None, max_margin_pct: float = 50.0,
                  cross_rates: Optional[dict] = None) -> SizeResult:
    """
    حجم معامله برای ریسک درصدی ثابت. همیشه به پایین رُند می‌شود (هرگز ریسک بیشتر از خواسته).
    اگر leverage داده شود، مارجین هم حساب می‌شود و حجم طوری سقف می‌خورد که مارجین از
    max_margin_pct درصد موجودی بیشتر نشود (با SL خیلی نزدیک، حجم ریسکی ممکن است بیش از توان مارجین باشد).
    cross_rates: برای کراس‌ها، نرخ ارز کوت به دلار (مثلاً {"JPY": 155.0} برای EURJPY).
    """
    if symbol not in SPECS:
        raise ValueError(f"نماد پشتیبانی نمی‌شود: {symbol}")
    if balance <= 0 or not (0 < risk_pct <= 100):
        raise ValueError("balance باید مثبت و risk_pct بین 0 و 100 باشد")
    dist = abs(entry - sl)
    if dist <= 0:
        raise ValueError("entry و sl نباید برابر باشند")
    sp = SPECS[symbol]
    q2u = (cross_rates or {}).get(sp["quote"]) if sp["quote"] != "USD" else None
    risk_amount = balance * risk_pct / 100.0
    per_lot = loss_per_lot_per_unit(symbol, entry, contract, quote_to_usd=q2u) * dist
    raw = risk_amount / per_lot
    lots = math.floor(raw / lot_step + 1e-9) * lot_step
    lots = round(min(lots, max_lot), 8)
    margin_limited = False
    if leverage is not None:
        mpl = margin_per_lot(symbol, entry, leverage, contract, quote_to_usd=q2u)
        cap = math.floor((balance * max_margin_pct / 100.0) / mpl / lot_step + 1e-9) * lot_step
        if lots > cap:
            lots, margin_limited = round(cap, 8), True
    rr, tp_note = None, ""
    if tp is not None:
        if (tp - entry) * (entry - sl) > 0:
            rr = round(abs(tp - entry) / dist, 2)
        else:
            tp_note = "TP در سمت اشتباه است (باید خلاف جهت SL باشد)؛ R:R محاسبه نشد"
    pips = dist / SPECS[symbol]["pip"]
    if lots < min_lot:
        why = ("مارجین/اهرم اجازه‌ی حداقل لات را نمی‌دهد؛ اهرم یا موجودی را بالا ببر یا SL را دورتر بگذار"
               if margin_limited else
               "حجم کمتر از حداقل لات است؛ SL را دورتر بگذار یا ریسک/موجودی را بالا ببر")
        return SizeResult(False, 0.0, risk_amount, 0.0, dist, round(pips, 1), rr, why)
    actual = lots * per_lot
    notes = [tp_note] if tp_note else []
    if margin_limited:
        notes.append("حجم به‌خاطر سقف مارجین کم شد؛ ریسک واقعی کمتر از ریسک خواسته است")
    if actual > risk_amount * 1.0001:
        notes.append("ریسک واقعی از خواسته بیشتر شد")
    margin = margin_pct = free = None
    if leverage is not None:
        margin = lots * margin_per_lot(symbol, entry, leverage, contract, quote_to_usd=q2u)
        margin_pct = margin / balance * 100.0
        free = balance - margin
        if margin_pct > 30 and not margin_limited:
            notes.append("بیش از ۳۰٪ موجودی درگیر مارجین می‌شود؛ جا برای افت کم است")
    return SizeResult(True, round(lots, _decimals(lot_step)), round(risk_amount, 2), round(actual, 2), dist,
                      round(pips, 1), rr, "؛ ".join(notes),
                      None if margin is None else round(margin, 2),
                      None if margin_pct is None else round(margin_pct, 1),
                      None if free is None else round(free, 2), margin_limited)


def breakeven_winrate(rr: float) -> float:
    """حداقل win rate برای سربه‌سر شدن (بدون هزینه) در نسبت ریسک به ریوارد rr."""
    if rr <= 0:
        raise ValueError("rr باید مثبت باشد")
    return 1.0 / (1.0 + rr)


def trade_r(side: str, entry: float, sl: float, exit_price: float) -> float:
    """R معامله: (خروج-ورود)·جهت / ریسک اولیه. side: BUY یا SELL."""
    s = 1 if side.upper() == "BUY" else -1 if side.upper() == "SELL" else 0
    if s == 0:
        raise ValueError("side باید BUY یا SELL باشد")
    risk = (entry - sl) * s
    if risk <= 0:
        raise ValueError("SL باید در سمت زیان باشد (BUY: زیر ورود، SELL: بالای ورود)")
    return (exit_price - entry) * s / risk


def calculate_position_milestones(direction: str, entry: float, sl: float, tp: float,
                                   be_r_trigger: float = 1.0, partial_r_trigger: float = 1.5) -> dict:
    """
    محاسبه سطوح مدیریت زنده پوزیشن: قیمت فعال‌سازی ریسک‌فری (Break-Even) و خروج پله‌ای (Partial TP).
    """
    side = 1 if direction.upper() == "BUY" else -1
    risk_dist = abs(entry - sl)
    if risk_dist <= 0:
        return {}

    be_trigger = entry + side * risk_dist * be_r_trigger
    partial_tp = entry + side * risk_dist * partial_r_trigger

    return {
        "direction": direction.upper(),
        "entry": round(entry, 6),
        "sl_initial": round(sl, 6),
        "tp_final": round(tp, 6),
        "breakeven_price": round(entry, 6),
        "be_trigger_price": round(be_trigger, 6),
        "partial_tp_price": round(partial_tp, 6),
    }


def fractional_kelly_risk_pct(win_rate: float, win_loss_ratio: float, fraction: float = 0.25,
                              min_risk_pct: float = 0.25, max_risk_pct: float = 2.0) -> float:
    """
    محاسبه درصد ریسک بهینه بر اساس فرمول کلی تعدیل‌شده (Fractional Kelly Criterion).
    win_rate: نرخ برد تاریخی (بین 0 تا 1)
    win_loss_ratio: نسبت میانگین سود به میانگین زیان (b)
    fraction: ضریب ایمنی فرمول کلی (پیش‌فرض 0.25 = Quarter Kelly)
    """
    if win_rate <= 0 or win_loss_ratio <= 0:
        return min_risk_pct
    q = 1.0 - win_rate
    kelly_full = (win_rate * win_loss_ratio - q) / win_loss_ratio
    if kelly_full <= 0:
        return min_risk_pct
    raw_risk = kelly_full * fraction * 100.0
    return max(min_risk_pct, min(max_risk_pct, round(raw_risk, 2)))


def calculate_risk_of_ruin(win_rate: float, loss_rate: float, risk_per_trade_pct: float,
                            capital_loss_threshold_pct: float = 50.0) -> float:
    """
    محاسبه احتمال ریاضی ورشکستگی (Risk of Ruin - Classic Formula):
    win_rate: نرخ برد (بین 0 تا 1)
    loss_rate: نرخ باخت (1 - win_rate)
    risk_per_trade_pct: درصد ریسک در هر معامله
    capital_loss_threshold_pct: آستانه افت سرمایه (مثلا 50٪)
    """
    if win_rate <= 0.50:
        return 100.0  # بدون امید ریاضی مثبت، احتمال ورشکستگی قطعی ۱۰۰٪ است
    if risk_per_trade_pct <= 0:
        return 0.0

    a = (1.0 - win_rate) / win_rate
    units = capital_loss_threshold_pct / risk_per_trade_pct
    ror = (a ** units) * 100.0
    return round(max(0.0, min(100.0, ror)), 2)
