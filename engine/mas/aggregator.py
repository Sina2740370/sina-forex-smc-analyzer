"""
A0 — Coordinator: اجرای ایجنت‌ها، Debate، تجمیع رژیم‌محور، ۷ گیت تصمیم، Risk Agent.
هر WAIT با شناسه گیت شکست‌خورده + «چه چیزی باید رخ دهد تا مجدد بررسی شود» ذخیره می‌شود.
"""
from __future__ import annotations

import pandas as pd

from ..data.providers import build_topdown
from .agents import run_data_agent, run_liquidity_agent, run_regime_agent, run_technical_agent
from .contracts import BUY, SELL, AgentReport, GateResult, Signal, data_hash, missing_report
from .debate import run_debate
from .risk_agent import AccountState, RiskVerdict, evaluate_risk
from .weights import (DEFAULT, HIGH_VOL_RISK_DISCOUNT, get_threshold, get_weights,
                      regime_key)


def analyze_mas(df15: pd.DataFrame, symbol: str, now: pd.Timestamp,
                spread_price: float | None = None, acct: AccountState | None = None,
                top: dict | None = None, feeds=None, top_cache: dict | None = None,
                cfg: dict | None = None) -> Signal:
    """تحلیل چندایجنتی در یک نقطه زمانی (بدون lookahead: فقط داده ≤ now).
    spread_price خالی = اسپرد معمول نماد از engine/mas/symbols.py.
    feeds (engine/mas/datafeeds.py: MASFeeds) = داده بیرونی فاز ۳؛ خالی → A4/A5 stub صادقانه.
    top_cache = topdown پیش‌محاسبهٔ کل تاریخچه (بهینه‌سازی بک‌تست)؛ در هر نقطه فقط slice ≤ now
    گرفته می‌شود — دقیقاً معادل build_topdown روی گذشته (پاک‌سازی سطر-به-سطر، resample سبدی).
    cfg = پارامترهای قابل‌تنظیم OAT/walk-forward: g1_delta، ext_weight_scale، min_rr؛
    خالی = مقادیر پایه (بدون تغییر رفتار)."""
    now = pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    if spread_price is None:
        from .symbols import get_spec
        spread_price = get_spec(symbol).spread_price()

    # --- A1: داده (فقط گذشته) ---
    past = df15[df15["time"] <= now]
    data = run_data_agent(past)
    df = data["df"]
    if top is None and top_cache is not None:
        top = _top_from_cache(top_cache, df, now)
    else:
        top = top or build_topdown(df)

    sig = Signal(symbol=symbol, session="", market_regime="")
    warnings = list(data["warnings"])
    missing = []
    if data["quality"] < 0.90:
        sig.gate_results.append(GateResult("G7", False, f"کیفیت داده {data['quality']:.2f} < 0.90"))
        sig.wait_reason = "G7: کیفیت داده پایین"
        sig.warnings = warnings
        return sig
    if len(df) < 6000:  # ~۶۲ روز M15؛ کمتر از این H4/D1 بی‌اعتبارند
        sig.gate_results.append(GateResult("G7", False, f"تاریخچه کوتاه ({len(df)} کندل)"))
        sig.wait_reason = "G7: تاریخچه ناکافی برای تحلیل چندتایم‌فریم"
        sig.warnings = warnings
        return sig
    sig.gate_results.append(GateResult("G7", True, f"کیفیت داده {data['quality']:.2f}"))

    # --- A2..A6 موازی (اینجا ترتیبی؛ I/O ندارد) ---
    rep_regime = run_regime_agent(top, now)
    rep_tech = run_technical_agent(top, now)
    rep_liq = run_liquidity_agent(top, now, spread_price)
    # فاز ۳: A4/A5 با فید واقعی اگر داده شد؛ وگرنه stub صادقانه (داده غایب رأی نمی‌دهد)
    if feeds is not None:
        from .fundamental import run_fundamental_agent
        from .sentiment import run_sentiment_agent
        rep_fund = run_fundamental_agent(symbol, now, feeds)
        rep_sent = run_sentiment_agent(symbol, now, feeds)
    else:
        rep_fund = missing_report("fundamental", ["نرخ بهره/CPI/NFP/GDP/PMI", "تقویم اقتصادی بلادرنگ"])
        rep_sent = missing_report("sentiment", ["COT CFTC", "سنتیمنت خرده‌فروشی"])
    reports = [rep_regime, rep_tech, rep_fund, rep_sent, rep_liq]

    regime_ctx = dict(rep_regime.context)
    regime_ctx["htf_bias"] = _htf_bias(top)
    rep_debate = run_debate(reports, regime_ctx)
    reports.append(rep_debate)
    sig.market_regime = regime_key(regime_ctx)
    sig.session = regime_ctx.get("session", "")
    missing = sum([r.missing_data for r in reports], [])
    for r in reports:
        errs = r.validate()
        if errs:
            raise ValueError(f"قرارداد ایجنت {r.agent} نقض شد: {errs}")

    # --- تجمیع رژیم‌محور ---
    # ایجنت غایب رأی نمی‌دهد: وزنش بین ایجنت‌های فعال بازتوزیع می‌شود و
    # اطمینان نهایی با «پوشش وزنی» جریمه می‌شود (صادقانه، نه حدس).
    w = get_weights(regime_ctx, cfg)
    active = [rep for rep in reports if rep.agent in w and not rep.is_stub]
    wsum = sum(w[rep.agent] for rep in active)
    if wsum <= 0:
        wsum = 1.0
    final = sum(rep.score * w[rep.agent] for rep in active) / wsum
    if regime_key(regime_ctx) == "high_vol":
        final = 50 + (final - 50) * HIGH_VOL_RISK_DISCOUNT
    wconf = sum(rep.confidence * w[rep.agent] for rep in active) / wsum
    debate_conf = rep_debate.confidence
    sig.final_score = final
    sig.confidence = round(min(100.0, wconf * (0.5 + debate_conf / 100.0) * wsum), 1)
    sig.agent_scores = {rep.agent: {"direction": rep.direction, "score": round(rep.score, 1),
                                    "confidence": round(rep.confidence, 1)} for rep in reports}

    # --- گیت‌ها G1..G6 ---
    threshold = get_threshold(regime_ctx, cfg)
    direction = BUY if final >= threshold else (SELL if final <= 100 - threshold else None)
    if direction is None:
        sig.gate_results.append(GateResult("G1", False,
                                           f"امتیاز {final:.1f} بین آستانه‌های {threshold}/{100 - threshold}"))
        return _finish_wait(sig, "G1: امتیاز نهایی زیر آستانه رژیم", reports, warnings, missing,
                            tip=f"بررسی مجدد اگر امتیاز به {threshold:.0f} برسد")
    sig.gate_results.append(GateResult("G1", True,
                                       f"{final:.1f} {'≥' if direction == BUY else '≤'} "
                                       f"{threshold if direction == BUY else 100 - threshold} ({direction})"))
    sig.signal = direction

    # G2: حداقل ۳ ایجنت مستقل هم‌جهت (در فاز ۱ فقط ۲ ایجنت جهت‌دار داریم → دقت پایین
    # fundamental/sentiment را نمی‌شمارد؛ پس گیت واقعی: ۲ جهت‌دار + debate هم‌جهت)
    aligned = [r.agent for r in reports if r.direction == direction]
    if len(aligned) < 3:
        sig.gate_results.append(GateResult("G2", False, f"ایجنت‌های هم‌جهت: {aligned}"))
        return _finish_wait(sig, "G2: کمتر از ۳ ایجنت مستقل هم‌جهت", reports, warnings, missing,
                            tip="ورود جدید ایجنت فاندامنتال/سنتیمنت هم‌جهت (فاز ۳) یا تقویت تکنیکال")
    sig.gate_results.append(GateResult("G2", True, str(aligned)))

    # G3: هم‌خوانی رژیم با جهت
    htb = regime_ctx.get("htf_bias", 0)
    # فاکتور تجربی w1_bias: هم‌خوانی با روند هفتگی (EMA8 روی W1) — فقط با cfg فعال می‌شود
    w1b = _w1_bias(top) if (cfg or {}).get("w1_bias") else 0
    w1_ok = bool(not (cfg or {}).get("w1_bias")
                 or (htb > 0 and w1b > 0) or (htb < 0 and w1b < 0)
                 or sig.market_regime in ("range",))
    regime_ok = bool(((htb > 0 and direction == BUY) or (htb < 0 and direction == SELL)
                      or (sig.market_regime in ("range",)))  # در رنج، برگشت از سطح مجاز است
                     and w1_ok)
    sig.gate_results.append(GateResult("G3", regime_ok,
                                       f"htf_bias={htb:+d}, regime={sig.market_regime}, w1={w1b or 'off'}"))
    if not regime_ok:
        return _finish_wait(sig, "G3: رژیم/ساختار بلندمدت با جهت سیگنال هم‌خوان نیست",
                            reports, warnings, missing, tip="تغییر ساختار H4/D1 به هم‌جهت سیگنال")

    # G4: RR≥2 و SL معتبر — از کاندید تکنیکال
    cand = rep_tech.context.get("candidate", {})
    if not cand:
        sig.gate_results.append(GateResult("G4", False, "کاندید معامله ساخته نشد"))
        return _finish_wait(sig, "G4: ستاپ معتبر با SL ساختاری و RR≥2 پیدا نشد",
                            reports, warnings, missing,
                            tip="تشکیل ستاپ در ناحیه فیبو/ساختار با تأیید مومنتوم")
    # فاکتور تجربی sl_mode=atr: جایگزینی SL ساختاری با k×ATR1h (SL تنگ‌تر → RR بالاتر).
    # ادعایی درباره کیفیت نیست؛ فقط آزمون تجربی که خروج آن در بک‌تست سنجیده می‌شود.
    if (cfg or {}).get("sl_mode") == "atr" and cand.get("risk_atr1h"):
        d2 = 1 if direction == BUY else -1
        atr1h = abs(cand["entry"] - cand["sl"]) / max(1e-9, cand["risk_atr1h"])
        k = float(cfg.get("atr_k", 1.5))
        sl_new = cand["entry"] - d2 * k * atr1h
        dist = abs(cand["entry"] - sl_new)
        if dist > 0:
            cand = {**cand, "sl": sl_new, "tp1": cand["entry"] + d2 * dist,
                    "tp2": cand["entry"] + d2 * 2.0 * dist, "rr": 2.0}
    rr = cand["rr"]
    ok4 = bool(rr >= float((cfg or {}).get("min_rr", 2.0)))
    sig.gate_results.append(GateResult("G4", ok4, f"RR={rr:.2f}, SL_dist/cand={cand['risk_atr1h']:.2f}×ATR1h"))
    if not ok4:
        return _finish_wait(sig, "G4: نسبت ریسک/ریوارد کمتر از 1:2", reports, warnings, missing)
    sig.cand = cand

    # G5: Risk Agent
    verdict = evaluate_risk(symbol, direction, cand["entry"], cand["sl"], cand["tp2"],
                            float(df["close"].iloc[-1]), acct or AccountState())
    sig.gate_results.append(GateResult("G5", verdict.approved,
                                       "تأیید ریسک" if verdict.approved else "; ".join(verdict.rejection_reasons)))
    sig.risk_approved = verdict.approved
    if not verdict.approved:
        return _finish_wait(sig, "G5: رد توسط ایجنت ریسک — " + "; ".join(verdict.rejection_reasons),
                            reports, warnings, missing)

    # G6: پنجره خبری — در فاز ۱ تقویم نداریم؛ صادقانه هشدار می‌دهیم و پاس موقت ثبت می‌شود
    if (acct or AccountState()).imminent_news_minutes < 0:
        sig.warnings.append("G6: تقویم اقتصادی متصل نیست؛ پنجره خبر ±۱۵ دقیقه تأیید نشد (فاز ۳)")
    sig.gate_results.append(GateResult("G6", True, "بدون داده تقویم؛ هشدار ثبت شد"))

    # --- سیگنال نهایی ---
    from .symbols import get_spec
    spec = get_spec(symbol)
    f = lambda x: spec.fmt(x)
    sig.entry_zone = f(cand["entry"])
    sig.entry_trigger = f"بسته‌شدن M15 {'بالای' if direction == BUY else 'زیر'} {f(cand['entry'])}"
    sig.stop_loss = f(cand["sl"])
    sig.take_profit_1 = f(cand["tp1"])
    sig.take_profit_2 = f(cand["tp2"])
    sig.risk_reward = f"1:{cand['rr']:.1f}"
    sig.lot_size = f"{verdict.lots:.2f}"
    sig.risk_amount = f"{verdict.risk_amount:.0f} USD"
    sig.invalidation = sum([r.invalidation for r in reports if r.agent != "fundamental"], [])[:6]
    sig.missing_data = missing
    sig.warnings = warnings
    return sig


def _top_from_cache(top_cache: dict, df_past: pd.DataFrame, now: pd.Timestamp) -> dict:
    """topdown از کش پیش‌محاسبه + بازسازی سبد ناقص انتهایی از دادهٔ گذشته.

    سبدِ در حال شکل‌گیری (مثلاً کندل D1 امروز) در کش با کل روز تجمیع شده؛ برش با
    «زمان ≤ now» نشت آینده می‌داد. راه درست: سندهای کاملِ قبل از سبد جاری از کش +
    بازسازی سبد جاری فقط از ردیف‌های ≤ now (مطابق build_topdown روی گذشته)."""
    out = {"15m": df_past}
    for tf, fr in top_cache.items():
        if tf == "15m":
            continue
        bs = fr.loc[fr["time"] <= now, "time"].max() if (fr["time"] <= now).any() else None
        if bs is None:
            out[tf] = fr.iloc[0:0]
            continue
        head = fr[fr["time"] < bs]
        tail_rows = df_past[df_past["time"] >= bs]
        from ..data.providers import resample_ohlc
        tail = resample_ohlc(tail_rows, tf) if len(tail_rows) else fr.iloc[0:0]
        out[tf] = pd.concat([head, tail], ignore_index=True) if len(tail) else head
    return out


def _w1_bias(top: dict) -> int:
    """جهت روند هفتگی: کلوز W1 نسبت به EMA8 هفتگی (۱=بالای EMA، −۱=زیر)."""
    w = top.get("1wk")
    if w is None or len(w) < 10:
        return 0
    from .indicators import ema
    c = w["close"]
    e8 = ema(c, 8).iloc[-1]
    px = float(c.iloc[-1])
    return 1 if px > e8 else -1


def _htf_bias(top: dict) -> int:
    """جهت ساختار بلندمدت از EMAهای D1 (زمینه رژیم برای Debate و G3)."""
    d = top["1d"]
    c = d["close"]
    from .indicators import ema
    if len(d) < 60:
        return 0
    e20, e50 = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1]
    return 1 if e20 > e50 else -1


def _finish_wait(sig: Signal, reason: str, reports: list, warnings: list,
                 missing: list, tip: str = "") -> Signal:
    sig.signal = "WAIT"
    sig.wait_reason = reason + (f" | برای بازبینی مجدد: {tip}" if tip else "")
    sig.agent_reports = [r.to_dict() for r in reports]
    sig.warnings = warnings
    sig.missing_data = missing
    sig.risk_approved = False
    return sig
