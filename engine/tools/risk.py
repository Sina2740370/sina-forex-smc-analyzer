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
SPECS = {
    "EURUSD": {"pip": 0.0001, "contract": 100_000, "quote": "USD"},
    "GBPUSD": {"pip": 0.0001, "contract": 100_000, "quote": "USD"},
    "AUDUSD": {"pip": 0.0001, "contract": 100_000, "quote": "USD"},
    "NZDUSD": {"pip": 0.0001, "contract": 100_000, "quote": "USD"},
    "USDJPY": {"pip": 0.01, "contract": 100_000, "quote": "JPY"},
    "USDCAD": {"pip": 0.0001, "contract": 100_000, "quote": "CAD"},
    "USDCHF": {"pip": 0.0001, "contract": 100_000, "quote": "CHF"},
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


def loss_per_lot_per_unit(symbol: str, price: float, contract: Optional[float] = None) -> float:
    """زیان یک لات برای حرکت یک واحد قیمت، به دلار."""
    sp = SPECS[symbol]
    c = contract if contract is not None else sp["contract"]
    if sp["quote"] == "USD":
        return c
    if symbol.startswith("USD"):          # USDJPY/USDCAD/USDCHF: ارزش به ارز دوم است
        return c / price
    raise ValueError(f"نماد پشتیبانی نمی‌شود: {symbol}")


def margin_per_lot(symbol: str, price: float, leverage: float,
                   contract: Optional[float] = None) -> float:
    """مارجین یک لات به دلار: ارزش اسمی موقعیت ÷ اهرم."""
    if symbol not in SPECS:
        raise ValueError(f"نماد پشتیبانی نمی‌شود: {symbol}")
    if leverage <= 0:
        raise ValueError("leverage باید مثبت باشد")
    c = contract if contract is not None else SPECS[symbol]["contract"]
    notional = c if symbol.startswith("USD") else c * price   # USD-base: ارزش اسمی خودش دلار است
    return notional / leverage


def position_size(symbol: str, balance: float, risk_pct: float, entry: float, sl: float,
                  tp: Optional[float] = None, lot_step: float = 0.01, min_lot: float = 0.01,
                  max_lot: float = 100.0, contract: Optional[float] = None,
                  leverage: Optional[float] = None, max_margin_pct: float = 50.0) -> SizeResult:
    """
    حجم معامله برای ریسک درصدی ثابت. همیشه به پایین رُند می‌شود (هرگز ریسک بیشتر از خواسته).
    اگر leverage داده شود، مارجین هم حساب می‌شود و حجم طوری سقف می‌خورد که مارجین از
    max_margin_pct درصد موجودی بیشتر نشود (با SL خیلی نزدیک، حجم ریسکی ممکن است بیش از توان مارجین باشد).
    """
    if symbol not in SPECS:
        raise ValueError(f"نماد پشتیبانی نمی‌شود: {symbol}")
    if balance <= 0 or not (0 < risk_pct <= 100):
        raise ValueError("balance باید مثبت و risk_pct بین 0 و 100 باشد")
    dist = abs(entry - sl)
    if dist <= 0:
        raise ValueError("entry و sl نباید برابر باشند")
    risk_amount = balance * risk_pct / 100.0
    per_lot = loss_per_lot_per_unit(symbol, entry, contract) * dist
    raw = risk_amount / per_lot
    lots = math.floor(raw / lot_step + 1e-9) * lot_step
    lots = round(min(lots, max_lot), 8)
    margin_limited = False
    if leverage is not None:
        mpl = margin_per_lot(symbol, entry, leverage, contract)
        cap = math.floor((balance * max_margin_pct / 100.0) / mpl / lot_step + 1e-9) * lot_step
        if lots > cap:
            lots, margin_limited = round(cap, 8), True
    rr = None
    if tp is not None:
        rr = round(abs(tp - entry) / dist, 2)
    pips = dist / SPECS[symbol]["pip"]
    if lots < min_lot:
        why = ("مارجین/اهرم اجازه‌ی حداقل لات را نمی‌دهد؛ اهرم یا موجودی را بالا ببر یا SL را دورتر بگذار"
               if margin_limited else
               "حجم کمتر از حداقل لات است؛ SL را دورتر بگذار یا ریسک/موجودی را بالا ببر")
        return SizeResult(False, 0.0, risk_amount, 0.0, dist, round(pips, 1), rr, why)
    actual = lots * per_lot
    notes = []
    if margin_limited:
        notes.append("حجم به‌خاطر سقف مارجین کم شد؛ ریسک واقعی کمتر از ریسک خواسته است")
    if actual > risk_amount * 1.0001:
        notes.append("ریسک واقعی از خواسته بیشتر شد")
    margin = margin_pct = free = None
    if leverage is not None:
        margin = lots * margin_per_lot(symbol, entry, leverage, contract)
        margin_pct = margin / balance * 100.0
        free = balance - margin
        if margin_pct > 30 and not margin_limited:
            notes.append("بیش از ۳۰٪ موجودی درگیر مارجین می‌شود؛ جا برای افت کم است")
    return SizeResult(True, round(lots, 2), round(risk_amount, 2), round(actual, 2), dist,
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
