"""
حافظه بازار: یادگیری از عملکرد گذشته
- آمار برد/باخت هر نماد
- آمار بر اساس سشن معاملاتی
- وزن‌دهی پویا
"""
import logging
from datetime import datetime, timezone

from .database import get_all_signals

logger = logging.getLogger(__name__)

MIN_TRADES_FOR_MEMORY = 10


def get_symbol_performance() -> dict:
    """آمار عملکرد هر نماد"""
    signals = get_all_signals(limit=1000)
    closed = [s for s in signals if s["status"] in ("WIN", "LOSS")]

    perf = {}
    for s in closed:
        sym = s["symbol"]
        if sym not in perf:
            perf[sym] = {"wins": 0, "losses": 0, "total": 0, "pips": 0.0}

        perf[sym]["total"] += 1
        perf[sym]["pips"] += (s["pnl_pips"] or 0)

        if s["status"] == "WIN":
            perf[sym]["wins"] += 1
        else:
            perf[sym]["losses"] += 1

    for sym, d in perf.items():
        d["win_rate"] = d["wins"] / d["total"] if d["total"] > 0 else 0.0

    return perf


def get_session_performance() -> dict:
    """آمار بر اساس سشن معاملاتی"""
    signals = get_all_signals(limit=1000)
    closed = [s for s in signals if s["status"] in ("WIN", "LOSS")]

    sessions = {
        "asian": {"wins": 0, "total": 0},
        "london": {"wins": 0, "total": 0},
        "overlap": {"wins": 0, "total": 0},
        "ny": {"wins": 0, "total": 0},
        "late": {"wins": 0, "total": 0},
    }

    for s in closed:
        try:
            t = datetime.fromisoformat(s["created_at"].replace("Z", "+00:00"))
            h = t.hour

            if 0 <= h < 8:
                key = "asian"
            elif 8 <= h < 13:
                key = "london"
            elif 13 <= h < 17:
                key = "overlap"
            elif 17 <= h < 21:
                key = "ny"
            else:
                key = "late"

            sessions[key]["total"] += 1
            if s["status"] == "WIN":
                sessions[key]["wins"] += 1
        except Exception:
            continue

    for key, d in sessions.items():
        d["win_rate"] = d["wins"] / d["total"] if d["total"] > 0 else 0.5

    return sessions


def calculate_memory_score(symbol: str) -> dict:
    """امتیاز مبتنی بر حافظه برای یه نماد"""
    from .config import MEMORY_ENABLED
    if not MEMORY_ENABLED:
        return {"score": 0.0, "reasons": [], "enabled": False}
    perf = get_symbol_performance()
    total_trades = sum(d["total"] for d in perf.values())

    if total_trades < MIN_TRADES_FOR_MEMORY:
        return {
            "score": 0.0,
            "reasons": [f"حافظه غیرفعال (نیاز به {MIN_TRADES_FOR_MEMORY - total_trades} معامله بیشتر)"],
            "enabled": False,
        }

    symbol_perf = perf.get(symbol, {"total": 0, "win_rate": 0.5})
    session_perf = get_session_performance()
    h = datetime.now(timezone.utc).hour

    if 0 <= h < 8:
        cur = "asian"
    elif 8 <= h < 13:
        cur = "london"
    elif 13 <= h < 17:
        cur = "overlap"
    elif 17 <= h < 21:
        cur = "ny"
    else:
        cur = "late"

    reasons = []
    score = 0.0

    if symbol_perf["total"] >= 3:
        wr = symbol_perf["win_rate"]
        if wr >= 0.70:
            score += 0.4
            reasons.append(f"سابقه خوب نماد ({wr*100:.0f}% برد)")
        elif wr <= 0.35:
            score -= 0.4
            reasons.append(f"سابقه ضعیف نماد ({wr*100:.0f}% برد)")

    cur_data = session_perf.get(cur, {"win_rate": 0.5, "total": 0})
    if cur_data["total"] >= 5:
        wr = cur_data["win_rate"]
        if wr >= 0.65:
            score += 0.3
            reasons.append(f"سشن فعلی مناسب ({wr*100:.0f}% برد)")
        elif wr <= 0.35:
            score -= 0.3
            reasons.append(f"سشن فعلی ضعیف ({wr*100:.0f}% برد)")

    score = max(-1.0, min(1.0, score))
    return {"score": round(score, 2), "reasons": reasons, "enabled": True}


def get_memory_weights() -> dict:
    """وزن پیشنهادی هر نماد"""
    perf = get_symbol_performance()
    total = sum(d["total"] for d in perf.values())

    if total < MIN_TRADES_FOR_MEMORY:
        return {}

    weights = {}
    for sym, d in perf.items():
        if d["total"] >= 3:
            weights[sym] = round(0.5 + d["win_rate"], 2)

    return weights
