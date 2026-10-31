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

# pip و ارزش هر پیپ در یک لات استاندارد (100k واحد). quote در USD.
# USDJPY: پیپ 0.01 و ارزش = 1000 JPY → /نرخ → دلار.
SYM_SPEC = {
    "EURUSD": {"pip": 0.0001, "pip_value_quote_per_lot": 10.0, "quote_is_usd": True},
    "GBPUSD": {"pip": 0.0001, "pip_value_quote_per_lot": 10.0, "quote_is_usd": True},
    "XAUUSD": {"pip": 0.1, "pip_value_quote_per_lot": 1.0, "quote_is_usd": True},  # 100oz
    "USDJPY": {"pip": 0.01, "pip_value_quote_per_lot": 1000.0, "quote_is_usd": False},
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

    risk_dist = abs(entry - sl)
    rr = abs(tp2 - entry) / risk_dist if risk_dist > 0 else 0.0
    risk_usd_full = acct.equity * RISK_RULES["risk_per_trade_pct"] / 100.0
    pip_val = _pip_value_usd(symbol, price)
    pips = risk_dist / SYM_SPEC[symbol]["pip"]
    lots_raw = (risk_usd_full / max(1e-9, pips * pip_val)) * max(0.1, size_factor)
    lots = round(min(lots_raw, 50.0), 2)
    risk_amount = lots * pips * pip_val

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
    g("R-SL", risk_dist > 0 and pips > 0, f"فاصله SL نامعتبر ({risk_dist:.6f})")

    if acct.imminent_news_minutes >= 0:
        g("R-NEWS", acct.imminent_news_minutes > RISK_RULES["news_buffer_minutes"],
          f"خبر پرتأثیر در {acct.imminent_news_minutes:.0f} دقیقه‌ای (پنجره ±{RISK_RULES['news_buffer_minutes']} دقیقه)")
    # اگر تقویم نبود R-NEWS با «نامعلوم» ثبت می‌شود؛ در گیت G6 کل سیستم با هشدار مدیریت می‌کند
    else:
        gates.append(GateResult("R-NEWS", True, "تقویم خبری در دسترس نیست — کنترل در G6 با هشدار"))

    approved = not rej
    return RiskVerdict(approved, lots if approved else 0.0, risk_amount if approved else 0.0,
                       gates, rej)
