"""
حافظه بازار: یادگیری فقط از نتایجی که در زمان snapshot واقعاً معلوم بوده‌اند.
- آمار برد/باخت هر نماد
- آمار بر اساس سشن زمان ایجاد سیگنال
"""
import logging
from datetime import datetime, timezone

from .database import get_closed_signals

logger = logging.getLogger(__name__)

MIN_TRADES_FOR_MEMORY = 10
MEMORY_SIGNAL_LIMIT = 1000


def _parse_utc_timestamp(value):
    """Parse timestamps consistently; naive legacy values are interpreted as UTC."""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _normalize_as_of(as_of):
    if as_of is None:
        return datetime.now(timezone.utc)
    return _parse_utc_timestamp(as_of)


def _eligible_closed_signals(as_of):
    """Defensive PIT filter; malformed or temporally inconsistent rows are excluded."""
    rows = get_closed_signals(as_of=as_of, limit=MEMORY_SIGNAL_LIMIT)
    eligible = []
    for signal in rows:
        try:
            if signal.get("status") not in ("WIN", "LOSS"):
                continue
            created_at = _parse_utc_timestamp(signal.get("created_at"))
            closed_at = _parse_utc_timestamp(signal.get("closed_at"))
            if created_at > as_of or closed_at > as_of or closed_at < created_at:
                continue
            eligible.append(signal)
        except (TypeError, ValueError, OverflowError):
            # Unknown timestamps cannot establish that the outcome was knowable.
            continue
    return eligible


def get_symbol_performance(signals=None) -> dict:
    """آمار عملکرد هر نماد از مجموعه‌ی معاملات بسته‌شده‌ی ورودی"""
    if signals is None:
        signals = _eligible_closed_signals(_normalize_as_of(None))
    perf = {}
    for signal in signals:
        sym = signal["symbol"]
        if sym not in perf:
            perf[sym] = {"wins": 0, "losses": 0, "total": 0, "pips": 0.0}

        perf[sym]["total"] += 1
        perf[sym]["pips"] += (signal.get("pnl_pips") or 0)

        if signal["status"] == "WIN":
            perf[sym]["wins"] += 1
        else:
            perf[sym]["losses"] += 1

    for stats in perf.values():
        stats["win_rate"] = stats["wins"] / stats["total"] if stats["total"] > 0 else 0.0
    return perf


def get_session_performance(signals=None) -> dict:
    """آمار سشن بر اساس زمان ایجاد سیگنال، فقط با نتایج معلوم تا snapshot"""
    if signals is None:
        signals = _eligible_closed_signals(_normalize_as_of(None))

    sessions = {
        "asian": {"wins": 0, "total": 0},
        "london": {"wins": 0, "total": 0},
        "overlap": {"wins": 0, "total": 0},
        "ny": {"wins": 0, "total": 0},
        "late": {"wins": 0, "total": 0},
    }

    for signal in signals:
        try:
            hour = _parse_utc_timestamp(signal["created_at"]).hour
            if 0 <= hour < 8:
                key = "asian"
            elif 8 <= hour < 13:
                key = "london"
            elif 13 <= hour < 17:
                key = "overlap"
            elif 17 <= hour < 21:
                key = "ny"
            else:
                key = "late"

            sessions[key]["total"] += 1
            if signal["status"] == "WIN":
                sessions[key]["wins"] += 1
        except (KeyError, TypeError, ValueError, OverflowError):
            continue

    for stats in sessions.values():
        stats["win_rate"] = stats["wins"] / stats["total"] if stats["total"] else 0.5
    return sessions


def calculate_memory_score(symbol: str, as_of=None) -> dict:
    """امتیاز حافظه با قرارداد point-in-time؛ as_of زمان مرجع تحلیل است."""
    from .config import MEMORY_ENABLED

    if not MEMORY_ENABLED:
        return {"score": 0.0, "reasons": [], "enabled": False}

    try:
        snapshot = _normalize_as_of(as_of)
    except (TypeError, ValueError, OverflowError):
        logger.warning("Market memory disabled: invalid as_of timestamp")
        return {
            "score": 0.0,
            "reasons": ["حافظه غیرفعال: زمان مرجع نامعتبر است"],
            "enabled": False,
        }

    signals = _eligible_closed_signals(snapshot)
    perf = get_symbol_performance(signals)
    total_trades = sum(stats["total"] for stats in perf.values())

    if total_trades < MIN_TRADES_FOR_MEMORY:
        return {
            "score": 0.0,
            "reasons": [f"حافظه غیرفعال (نیاز به {MIN_TRADES_FOR_MEMORY - total_trades} معامله بیشتر)"],
            "enabled": False,
        }

    symbol_perf = perf.get(symbol, {"total": 0, "win_rate": 0.5})
    session_perf = get_session_performance(signals)
    hour = snapshot.hour

    if 0 <= hour < 8:
        current_session = "asian"
    elif 8 <= hour < 13:
        current_session = "london"
    elif 13 <= hour < 17:
        current_session = "overlap"
    elif 17 <= hour < 21:
        current_session = "ny"
    else:
        current_session = "late"

    reasons = []
    score = 0.0

    if symbol_perf["total"] >= 3:
        win_rate = symbol_perf["win_rate"]
        if win_rate >= 0.70:
            score += 0.4
            reasons.append(f"سابقه خوب نماد ({win_rate*100:.0f}% برد)")
        elif win_rate <= 0.35:
            score -= 0.4
            reasons.append(f"سابقه ضعیف نماد ({win_rate*100:.0f}% برد)")

    current_data = session_perf.get(current_session, {"win_rate": 0.5, "total": 0})
    if current_data["total"] >= 5:
        win_rate = current_data["win_rate"]
        if win_rate >= 0.65:
            score += 0.3
            reasons.append(f"سشن فعلی مناسب ({win_rate*100:.0f}% برد)")
        elif win_rate <= 0.35:
            score -= 0.3
            reasons.append(f"سشن فعلی ضعیف ({win_rate*100:.0f}% برد)")

    score = max(-1.0, min(1.0, score))
    return {"score": round(score, 2), "reasons": reasons, "enabled": True}
