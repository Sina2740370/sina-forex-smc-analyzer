"""
A8 — ایجنت ریسک: حق وتوی مطلق. هر قاعده یک گیت باینری است؛ رد = نهایی و غیرقابل برگشت.
حجم لات از pip value واقعی جفت‌ارز محاسبه می‌شود (بدون حدس).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import GateResult

RISK_RULES = {
    "risk_per_trade_pct": 1.0,
    "max_concurrent_risk_pct": 3.0,
    "max_daily_loss_pct": 3.0,
    "max_weekly_loss_pct": 6.0,
    "max_open_trades": 3,
    "max_per_symbol": 1,
    "consecutive_losses_half": 2,
    "consecutive_losses_stop": 3,
    "news_buffer_minutes": 15,
    "min_sl_atr_mult": 1.5,
    "min_rr": 2.0,
    "equity_default": 10_000.0,
}



@dataclass
class AccountState:
    equity: float = RISK_RULES["equity_default"]
    open_trades: int = 0
    open_risk_pct: float = 0.0
    daily_loss_pct: float = 0.0
    weekly_loss_pct: float = 0.0
    consecutive_losses: int = 0
    per_symbol_open: dict = field(default_factory=dict)
    imminent_news_minutes: float = -1.0     # -1 = داده تقویم موجود نیست


@dataclass
class RiskVerdict:
    approved: bool
    lots: float = 0.0
    risk_amount: float = 0.0
    gates: list = field(default_factory=list)
    rejection_reasons: list = field(default_factory=list)


# مشخصات نمادها به engine/mas/symbols.py منتقل شد (تک منبع حقیقت برای چندارزی).
# SYM_SPEC اینجا برای سازگاری قبلی به شکل دیکشنری ساده نگه داشته می‌شود.
from .symbols import SYM_SPECS as _SYM_SPECS, size_lots_for_risk

SYM_SPEC = {sym: {"pip": s.pip, "pip_value_quote_per_lot": s.pip_value_quote_per_lot,
                  "quote_is_usd": s.quote_is_usd} for sym, s in _SYM_SPECS.items()}


def _pip_value_usd(symbol: str, price: float) -> float:
    spec = SYM_SPEC[symbol]
    v = spec["pip_value_quote_per_lot"]
    return v if spec["quote_is_usd"] else v / price


def evaluate_risk(symbol: str, direction: str, entry: float, sl: float, tp2: float,
                  price: float, acct: AccountState | None = None,
                  size_factor: float = 1.0) -> RiskVerdict:
    """سیگنال کاندید را مستقل می‌سنجد؛ خروجی تأیید/رد + حجم لات."""
    acct = acct or AccountState()
    gates, rej = [], []

    def g(name: str, ok: bool, detail: str = ""):
        gates.append(GateResult(name, ok, detail))
        if not ok:
            rej.append(f"{name}: {detail}")
        return ok

    import math
    inputs_valid = all(math.isfinite(float(v)) for v in (entry, sl, tp2, price, acct.equity, size_factor))
    inputs_valid = (inputs_valid and entry > 0 and sl > 0 and tp2 > 0
                    and price > 0 and acct.equity > 0 and 0.1 <= size_factor <= 1.0)
    direction_valid = direction in ("BUY", "SELL")
    risk_dist = abs(entry - sl) if inputs_valid else 0.0
    stop_valid = direction_valid and ((sl < entry) if direction == "BUY" else (sl > entry))
    target_valid = direction_valid and ((tp2 > entry) if direction == "BUY" else (tp2 < entry))
    rr = abs(tp2 - entry) / risk_dist if risk_dist > 0 else 0.0
    risk_usd_full = acct.equity * RISK_RULES["risk_per_trade_pct"] / 100.0 if inputs_valid else 0.0
    lots, risk_amount = 0.0, 0.0
    pips = risk_dist / SYM_SPEC[symbol]["pip"] if symbol in SYM_SPEC else 0.0
    sizing_error = ""
    if inputs_valid and direction_valid and stop_valid and target_valid and symbol in SYM_SPEC:
        try:
            requested_risk = risk_usd_full * max(0.1, size_factor)
            lots, risk_amount = size_lots_for_risk(symbol, entry, sl, requested_risk)
        except (KeyError, ValueError, ArithmeticError) as exc:
            sizing_error = str(exc)
    else:
        sizing_error = "ورودی، نماد، جهت، SL، TP یا ضریب حجم نامعتبر است (0.1 تا 1.0)"

    g("R-RR", rr >= RISK_RULES["min_rr"] - 1e-9, f"RR={rr:.2f} < {RISK_RULES['min_rr']}")
    g("R-OPEN", acct.open_trades < RISK_RULES["max_open_trades"],
      f"معاملات باز {acct.open_trades} ≥ {RISK_RULES['max_open_trades']}")
    g("R-CONC", acct.open_risk_pct + RISK_RULES["risk_per_trade_pct"] <= RISK_RULES["max_concurrent_risk_pct"],
      f"ریسک همزمان {acct.open_risk_pct:.1f}% > {RISK_RULES['max_concurrent_risk_pct']}%")
    g("R-DAY", acct.daily_loss_pct < RISK_RULES["max_daily_loss_pct"],
      f"ضرر روزانه {acct.daily_loss_pct:.1f}% ≥ سقف")
    g("R-WK", acct.weekly_loss_pct < RISK_RULES["max_weekly_loss_pct"],
      f"ضرر هفتگی {acct.weekly_loss_pct:.1f}% ≥ سقف")
    g("R-SYM", acct.per_symbol_open.get(symbol, 0) < RISK_RULES["max_per_symbol"],
      "یک معامله باز در همین جفت‌ارز وجود دارد")
    g("R-ADDTOLOSS", acct.consecutive_losses < RISK_RULES["consecutive_losses_stop"] or size_factor < 1.0,
      "سه ضرر متوالی → توقف تا بازبینی")
    g("R-DIR", direction_valid and stop_valid and target_valid,
      "جهت معامله یا سمت SL/TP با BUY/SELL سازگار نیست")
    g("R-SL", inputs_valid and risk_dist > 0 and pips > 0,
      f"فاصله SL یا ورودی نامعتبر ({risk_dist:.6f})")
    g("R-SIZE", lots > 0 and risk_amount > 0,
      sizing_error or "حجم پس از گرد کردن محافظه‌کارانه کمتر از حداقل لات است")

    if acct.imminent_news_minutes >= 0:
        g("R-NEWS", acct.imminent_news_minutes > RISK_RULES["news_buffer_minutes"],
          f"خبر پرتأثیر در {acct.imminent_news_minutes:.0f} دقیقه‌ای (پنجره ±{RISK_RULES['news_buffer_minutes']} دقیقه)")
    # اگر تقویم نبود R-NEWS با «نامعلوم» ثبت می‌شود؛ در گیت G6 کل سیستم با هشدار مدیریت می‌کند
    else:
        gates.append(GateResult("R-NEWS", True, "تقویم خبری در دسترس نیست — کنترل در G6 با هشدار"))

    approved = not rej
    return RiskVerdict(approved, lots if approved else 0.0, risk_amount if approved else 0.0,
                       gates, rej)
