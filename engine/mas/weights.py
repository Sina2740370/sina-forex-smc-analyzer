"""ماتریس وزن وابسته به رژیم — رفع علت شکست نسخه قبلی (وزن‌های ثابت).
هر سطر جمعاً 1.00 است؛ در رژیم high_vol باقی‌مانده به ضریب پاداش ریسک منتقل می‌شود."""
from __future__ import annotations

WEIGHTS = {
    "strong_trend": {"technical": 0.35, "regime": 0.20, "liquidity": 0.15,
                     "session": 0.10, "fundamental": 0.10, "sentiment": 0.10},
    "range":        {"technical": 0.20, "regime": 0.10, "liquidity": 0.25,
                     "session": 0.15, "fundamental": 0.15, "sentiment": 0.15},
    "high_vol":     {"technical": 0.25, "regime": 0.15, "liquidity": 0.20,
                     "session": 0.10, "fundamental": 0.15, "sentiment": 0.15},
    "transition":   {"technical": 0.25, "regime": 0.15, "liquidity": 0.20,
                     "session": 0.15, "fundamental": 0.15, "sentiment": 0.10},
    "news":         {"technical": 0.15, "regime": 0.10, "liquidity": 0.10,
                     "session": 0.10, "fundamental": 0.35, "sentiment": 0.20},
}
DEFAULT = "transition"
THRESHOLD = {"strong_trend": 65.0, "range": 70.0, "high_vol": 75.0,
             "transition": 70.0, "news": 75.0}
CONSENSUS_MARGIN = 8.0     # اگر |bull−bear| کمتر از این باشد → WAIT
HIGH_VOL_RISK_DISCOUNT = 0.85   # در نوسان بالا امتیاز نهایی جریمه می‌شود (سنگینی ریسک ATR)

# نقد کردن وزن سشن داخل وزن نقدینگی (A6 هر دو را می‌سنجد؛ جلوگیری از شمارش دوباره)
MERGE_SESSION_INTO_LIQUIDITY = True


def regime_key(regime_ctx: dict) -> str:
    r = regime_ctx.get("regime", DEFAULT)
    return r if r in WEIGHTS else DEFAULT


def get_weights(regime_ctx: dict) -> dict:
    w = dict(WEIGHTS[regime_key(regime_ctx)])
    if MERGE_SESSION_INTO_LIQUIDITY:
        w["liquidity"] = round(w["liquidity"] + w.pop("session"), 4)
    return w


def get_threshold(regime_ctx: dict) -> float:
    return THRESHOLD[regime_key(regime_ctx)]
