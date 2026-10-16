"""
Risk and position-sizing helpers.

Defaults assume a USD-denominated account. SPECS are generic defaults, not
broker-authoritative metadata. Fees, slippage, stop gaps, and execution-price
differences are excluded from risk estimates.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_FLOOR
from typing import Optional

D = Decimal
ZERO = D("0")
ONE = D("1")
HUNDRED = D("100")
CENT = D("0.01")

# Defaults only. Live trading must use the broker's actual symbol metadata.
SPECS = {
    "EURUSD": {"pip": D("0.0001"), "contract": D("100000"), "base": "EUR", "quote": "USD"},
    "GBPUSD": {"pip": D("0.0001"), "contract": D("100000"), "base": "GBP", "quote": "USD"},
    "AUDUSD": {"pip": D("0.0001"), "contract": D("100000"), "base": "AUD", "quote": "USD"},
    "NZDUSD": {"pip": D("0.0001"), "contract": D("100000"), "base": "NZD", "quote": "USD"},
    "USDJPY": {"pip": D("0.01"), "contract": D("100000"), "base": "USD", "quote": "JPY"},
    "USDCAD": {"pip": D("0.0001"), "contract": D("100000"), "base": "USD", "quote": "CAD"},
    "USDCHF": {"pip": D("0.0001"), "contract": D("100000"), "base": "USD", "quote": "CHF"},
    "XAUUSD": {"pip": D("0.1"), "contract": D("100"), "base": "XAU", "quote": "USD"},
}


def _decimal(value, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} باید عددی معتبر باشد")
    try:
        result = value if isinstance(value, Decimal) else D(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"{name} باید عددی معتبر باشد") from None
    if not result.is_finite():
        raise ValueError(f"{name} باید متناهی باشد")
    return result


def _positive(value, name: str) -> Decimal:
    result = _decimal(value, name)
    if result <= ZERO:
        raise ValueError(f"{name} باید مثبت باشد")
    return result


def _money(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    if value <= ZERO:
        return ZERO
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _step_decimals(step: Decimal) -> int:
    return max(0, -step.normalize().as_tuple().exponent)


def _instrument(symbol: str) -> dict:
    if not isinstance(symbol, str) or symbol not in SPECS:
        raise ValueError(f"نماد پشتیبانی نمی‌شود: {symbol}")
    return SPECS[symbol]


def _contract_for(symbol: str, contract) -> Decimal:
    spec = _instrument(symbol)
    return _positive(spec["contract"] if contract is None else contract, "contract")


def _quote_to_usd_rate(symbol: str, price: Decimal, supplied_rate=None) -> Decimal:
    """USD value of one quote-currency unit for a USD account."""
    spec = _instrument(symbol)
    if spec["quote"] == "USD":
        if supplied_rate is not None and _positive(supplied_rate, "quote_to_account_rate") != ONE:
            raise ValueError("برای نمادهای با مظنه USD و حساب USD، نرخ تبدیل باید 1 باشد")
        return ONE
    if supplied_rate is not None:
        return _positive(supplied_rate, "quote_to_account_rate")
    if spec["base"] == "USD":
        return ONE / price
    raise ValueError(
        f"برای تبدیل ارز مظنه {spec['quote']} به USD باید quote_to_account_rate ارائه شود"
    )


@dataclass(frozen=True)
class SizeResult:
    ok: bool
    lots: float
    risk_amount: float
    actual_risk: float
    sl_distance: float
    sl_pips: float
    rr: Optional[float]
    note: str = ""
    margin: Optional[float] = None
    margin_pct: Optional[float] = None
    free_margin: Optional[float] = None
    margin_limited: bool = False


def loss_per_lot_per_unit(
    symbol: str,
    price: float,
    contract: Optional[float] = None,
    quote_to_account_rate: Optional[float] = None,
) -> float:
    """USD loss for one lot per 1.0 price-unit move, before trading costs."""
    spec = _instrument(symbol)
    p = _positive(price, "price")
    c = _contract_for(symbol, contract)
    return float(c * _quote_to_usd_rate(symbol, p, quote_to_account_rate))


def _margin_per_lot_decimal(
    symbol: str, price: Decimal, leverage: Decimal, contract: Decimal
) -> Decimal:
    spec = _instrument(symbol)
    if spec["quote"] != "USD" and spec["base"] != "USD":
        raise ValueError("برای محاسبه مارجین این نماد، تبدیل ارز و مشخصات بروکر لازم است")
    notional_usd = contract if spec["base"] == "USD" else contract * price
    return notional_usd / leverage


def margin_per_lot(
    symbol: str,
    price: float,
    leverage: float,
    contract: Optional[float] = None,
) -> float:
    """Approximate USD margin per lot; broker-specific adjustments are excluded."""
    p = _positive(price, "price")
    lev = _positive(leverage, "leverage")
    c = _contract_for(symbol, contract)
    return float(_margin_per_lot_decimal(symbol, p, lev, c))


def position_size(
    symbol: str,
    balance: float,
    risk_pct: float,
    entry: float,
    sl: float,
    tp: Optional[float] = None,
    lot_step: float = 0.01,
    min_lot: float = 0.01,
    max_lot: float = 100.0,
    contract: Optional[float] = None,
    leverage: Optional[float] = None,
    max_margin_pct: float = 50.0,
    quote_to_account_rate: Optional[float] = None,
) -> SizeResult:
    """Size a trade for a USD account, flooring volume to the broker lot step."""
    spec = _instrument(symbol)
    bal = _positive(balance, "balance")
    rp = _decimal(risk_pct, "risk_pct")
    if not ZERO < rp <= HUNDRED:
        raise ValueError("risk_pct باید بزرگ‌تر از صفر و حداکثر 100 باشد")

    ent = _positive(entry, "entry")
    stop = _positive(sl, "sl")
    if ent == stop:
        raise ValueError("entry و sl نباید برابر باشند")

    step = _positive(lot_step, "lot_step")
    minimum = _positive(min_lot, "min_lot")
    maximum = _positive(max_lot, "max_lot")
    if maximum < minimum:
        raise ValueError("max_lot نباید از min_lot کمتر باشد")
    if _floor_to_step(minimum, step) != minimum:
        raise ValueError("min_lot باید مضرب دقیق lot_step باشد")

    contract_size = _contract_for(symbol, contract)
    distance = abs(ent - stop)
    conversion = _quote_to_usd_rate(symbol, ent, quote_to_account_rate)
    risk_budget = bal * rp / HUNDRED
    loss_per_lot = distance * contract_size * conversion
    if not loss_per_lot.is_finite() or loss_per_lot <= ZERO:
        raise ValueError("زیان هر لات باید مثبت و متناهی باشد")

    raw_lots = risk_budget / loss_per_lot
    lots = min(_floor_to_step(raw_lots, step), _floor_to_step(maximum, step))
    margin_limited = False
    lev = None
    margin_per = None

    if leverage is not None:
        lev = _positive(leverage, "leverage")
        margin_pct_limit = _decimal(max_margin_pct, "max_margin_pct")
        if not ZERO < margin_pct_limit <= HUNDRED:
            raise ValueError("max_margin_pct باید بزرگ‌تر از صفر و حداکثر 100 باشد")
        margin_per = _margin_per_lot_decimal(symbol, ent, lev, contract_size)
        allowed_margin = bal * margin_pct_limit / HUNDRED
        margin_lots = _floor_to_step(allowed_margin / margin_per, step)
        if lots > margin_lots:
            lots = margin_lots
            margin_limited = True

    rr = None
    notes = []
    if tp is not None:
        target = _positive(tp, "tp")
        if (target - ent) * (ent - stop) > ZERO:
            rr = float((abs(target - ent) / distance).quantize(CENT, rounding=ROUND_HALF_UP))
        else:
            notes.append("TP در سمت اشتباه است؛ R:R محاسبه نشد")

    pips = distance / spec["pip"]
    if lots < minimum:
        reason = (
            "مارجین/اهرم اجازه حداقل لات را نمی‌دهد"
            if margin_limited
            else "حجم کمتر از حداقل لات است؛ ریسک، فاصله SL یا موجودی را بازبینی کن"
        )
        return SizeResult(
            False, 0.0, _money(risk_budget), 0.0, float(distance),
            float(pips.quantize(D("0.1"), rounding=ROUND_HALF_UP)), rr, reason,
            margin_limited=margin_limited,
        )

    actual_risk_exact = lots * loss_per_lot
    if actual_risk_exact > risk_budget:
        raise ArithmeticError("Invariant violated: actual risk exceeds risk budget")

    margin = margin_pct = free_margin = None
    if lev is not None and margin_per is not None:
        margin_exact = lots * margin_per
        margin_pct_exact = margin_exact / bal * HUNDRED
        margin = _money(margin_exact)
        margin_pct = float(margin_pct_exact.quantize(D("0.1"), rounding=ROUND_HALF_UP))
        free_margin = _money(bal - margin_exact)
        if margin_limited:
            notes.append("حجم به‌خاطر سقف مارجین کاهش یافت")
        elif margin_pct_exact > D("30"):
            notes.append("بیش از 30٪ موجودی درگیر مارجین می‌شود")

    lots_display = lots.quantize(D(1).scaleb(-_step_decimals(step)))
    return SizeResult(
        True, float(lots_display), _money(risk_budget), _money(actual_risk_exact),
        float(distance), float(pips.quantize(D("0.1"), rounding=ROUND_HALF_UP)),
        rr, "؛ ".join(notes), margin, margin_pct, free_margin, margin_limited,
    )


def breakeven_winrate(rr: float) -> float:
    """Break-even win rate before fees, spread, and slippage."""
    ratio = _positive(rr, "rr")
    return float(ONE / (ONE + ratio))


def trade_r(side: str, entry: float, sl: float, exit_price: float) -> float:
    """Realized R multiple before trading costs."""
    if not isinstance(side, str):
        raise ValueError("side باید BUY یا SELL باشد")
    normalized = side.upper()
    direction = ONE if normalized == "BUY" else D("-1") if normalized == "SELL" else ZERO
    if direction == ZERO:
        raise ValueError("side باید BUY یا SELL باشد")

    ent = _positive(entry, "entry")
    stop = _positive(sl, "sl")
    exit_ = _positive(exit_price, "exit_price")
    initial_risk = (ent - stop) * direction
    if initial_risk <= ZERO:
        raise ValueError("SL باید در سمت زیان باشد (BUY: زیر ورود، SELL: بالای ورود)")
    return float((exit_ - ent) * direction / initial_risk)
