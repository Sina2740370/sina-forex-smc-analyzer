"""
استراتژی Top-Down SMC روی هسته‌ی خالص — نسخه 5.1.1 (کیفیت‌محور — آستانه‌های نرم‌تر)

هدف معماری:
- افزایش چگالی کیفیت سیگنال (نه تعداد)
- جداسازی تمیز کیفیت از حضور صرف ویژگی
- حفظ قابلیت بک‌تست بدون lookahead
- Decision غنی‌تر برای تحلیل بعدی و مدیریت حجم

تغییرات کلیدی v5.1 / v5.1.1:
1. Displacement به‌صورت امتیاز کیفیت (۰–۱) نه باینری
2. کیفیت ترکیبی (Quality Density) برای سخت‌گیری روی سیگنال‌های ضعیف
3. گیت چندلایه: حضور + حداقل کیفیت (v5.1.1: 0.42)
4. بونوس ساختاری فقط وقتی CHOCH تازه + Displacement قوی هم‌زمان باشند
5. آستانه‌های گرید کمی نرم‌تر شدند تا تعداد سیگنال قابل استفاده افزایش یابد
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from ..core import smc
from ..core.config import EngineConfig

MIN_LEN = {"1wk": 20, "1d": 20, "4h": 30, "1h": 60, "15m": 120}


@dataclass
class Decision:
    direction: Optional[str] = None
    score: float = 0.0
    grade: str = "-"
    power: float = 0.0
    quality: float = 0.0
    entry: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    bias: str = "-"
    blocked: Optional[str] = None
    reasons: list = field(default_factory=list)
    features: dict = field(default_factory=dict)
    config_hash: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def kill_zone(now: pd.Timestamp) -> bool:
    t = now.tz_localize("UTC") if now.tzinfo is None else now
    ldn = t.tz_convert("Europe/London").hour
    ny = t.tz_convert("America/New_York").hour
    return 7 <= ldn < 10 or 7 <= ny < 10


def in_active_session(now: pd.Timestamp) -> bool:
    t = now.tz_localize("UTC") if now.tzinfo is None else now
    ldn = t.tz_convert("Europe/London").hour
    ny = t.tz_convert("America/New_York").hour
    return 7 <= ldn < 16 or 7 <= ny < 16


def top_down_bias(df_wk: pd.DataFrame, df_d: pd.DataFrame):
    wk, d = smc.structure(df_wk), smc.structure(df_d)
    ema = smc.ema_slope(df_wk, 10)
    if (wk == "BULLISH" and ema == "BEARISH") or (wk == "BEARISH" and ema == "BULLISH"):
        wk = "RANGING"
    sign = {"BULLISH": 1, "BEARISH": -1}
    if wk in sign and wk == d:
        return sign[wk], "full", f"weekly+daily {wk}"
    if wk == "RANGING" and d in sign:
        return sign[d], "soft", f"weekly RANGING / daily {d}"
    if d == "RANGING" and wk in sign:
        return sign[wk], "soft", f"daily RANGING / weekly {wk}"
    if wk in sign and d in sign:
        return None, "none", f"CONFLICT weekly {wk} / daily {d}"
    return None, "none", f"NEUTRAL weekly {wk} / daily {d}"


def _displacement_quality(df: pd.DataFrame, side: int, atr_ref: float) -> float:
    if len(df) < 3 or atr_ref <= 0:
        return 0.0
    last = df.iloc[-1]
    body = abs(float(last["close"]) - float(last["open"]))
    rng = float(last["high"]) - float(last["low"])
    if rng <= 0:
        return 0.0
    if side > 0 and last["close"] <= last["open"]:
        return 0.0
    if side < 0 and last["close"] >= last["open"]:
        return 0.0
    body_atr = body / atr_ref
    body_ratio = body / rng
    if body_atr < 1.2 or body_ratio < 0.65:
        return 0.0
    atr_score = min(1.0, (body_atr - 1.2) / 0.8)
    ratio_score = min(1.0, (body_ratio - 0.65) / 0.25)
    return round(0.55 * atr_score + 0.45 * ratio_score, 3)


def _sweep_quality(sw: dict | None, atr_ref: float) -> float:
    if not sw or atr_ref <= 0:
        return 0.0
    size = float(sw.get("size", 0.0))
    ratio = size / atr_ref
    return round(max(0.0, min(1.0, (ratio - 0.12) / 0.55)), 3)


def analyze(data: dict, cfg: EngineConfig, now: pd.Timestamp) -> Decision:
    dec = Decision(config_hash=cfg.hash())
    for tf, n in MIN_LEN.items():
        if tf not in data or len(data[tf]) < n:
            dec.blocked = f"insufficient data: {tf}"
            return dec

    side, strength, reason = top_down_bias(data["1wk"], data["1d"])
    dec.bias = reason
    if side is None:
        dec.blocked = reason
        return dec

    df15, df1h, df4h = data["15m"], data["1h"], data["4h"]
    price = float(df15["close"].iloc[-1])
    atr15, atr1h, atr4h = smc.atr(df15), smc.atr(df1h), smc.atr(df4h)
    if min(atr15, atr1h, atr4h) <= 0:
        dec.blocked = "invalid ATR"
        return dec

    c: dict = {}
    why: list = [f"bias: {reason}"]
    power_points = 0.0
    quality_components: list[float] = []

    c["htf"] = cfg.w_htf_full if strength == "full" else cfg.w_htf_soft
    if strength == "full":
        power_points += 1.5
        quality_components.append(0.85)
    else:
        quality_components.append(0.45)

    sw = smc.liquidity_sweep(df15, cfg.sweep_lookback)
    want = "BULLISH_SWEEP" if side > 0 else "BEARISH_SWEEP"
    mult = smc.sweep_multiplier(sw, atr15)
    sweep_ok = bool(sw and sw["type"] == want)
    sw_q = _sweep_quality(sw, atr15) if sweep_ok else 0.0

    if sweep_ok:
        c["sweep"] = cfg.w_sweep * mult * (0.70 + 0.30 * sw_q)
        why.append(f"sweep x{mult:.2f} q={sw_q:.2f}")
        power_points += 1.8 * mult * (0.75 + 0.25 * sw_q)
        quality_components.append(0.55 + 0.45 * sw_q)
    elif sw:
        c["sweep_against"] = -cfg.w_sweep_against * mult
        why.append("sweep against")
        quality_components.append(0.15)

    brk = smc.structure_break(df1h, cfg.break_lookback)
    want_dir = "BULL" if side > 0 else "BEAR"
    choch_ok = bool(brk and brk["dir"] == want_dir and brk["kind"] == "CHOCH")
    if brk and brk["dir"] == want_dir:
        c["choch" if choch_ok else "bos"] = cfg.w_choch if choch_ok else cfg.w_bos
        why.append(brk["kind"])
        power_points += 1.6 if choch_ok else 0.7
        quality_components.append(0.80 if choch_ok else 0.40)

    ob = smc.order_blocks(df4h, cfg.ob_lookback)["bullish" if side > 0 else "bearish"]
    dist = smc.ob_distance(price, ob, atr4h) if ob else None
    near_ob = False
    if dist is not None:
        if dist == 0.0:
            c["ob"] = cfg.w_ob_in
            power_points += 2.0
            quality_components.append(0.90)
            near_ob = True
        elif dist <= 0.5:
            c["ob"] = cfg.w_ob_near
            power_points += 1.4
            quality_components.append(0.70)
            near_ob = dist <= cfg.ob_gate_atr4h
        elif dist <= 1.0:
            c["ob"] = cfg.w_ob_mid
            power_points += 0.7
            quality_components.append(0.40)
            near_ob = dist <= cfg.ob_gate_atr4h
        why.append(f"OB dist {dist:.2f} ATR4h")
    else:
        quality_components.append(0.20)

    zone = smc.premium_discount(df4h, cfg.pd_lookback)
    good = "DISCOUNT" if side > 0 else "PREMIUM"
    c["pd"] = cfg.w_pd if zone == good else -cfg.w_pd
    why.append(zone)
    if zone == good:
        power_points += 0.8
        quality_components.append(0.65)
    else:
        quality_components.append(0.25)

    fvg = smc.fvgs(df15, cfg.fvg_lookback, cfg.fvg_min_gap_ratio)["BULLISH" if side > 0 else "BEARISH"]
    if fvg:
        c["fvg"] = cfg.w_fvg
        why.append("FVG")
        power_points += 1.2
        quality_components.append(0.70)
    else:
        quality_components.append(0.25)

    if smc.candle_confirmation(df15, side):
        c["candle"] = cfg.w_candle
        why.append("candle")
        power_points += 0.9
        quality_components.append(0.60)

    disp_q = _displacement_quality(df15, side, atr15)
    if disp_q > 0:
        c["displacement"] = 0.18 * (0.60 + 0.40 * disp_q)
        why.append(f"DISPLACEMENT q={disp_q:.2f}")
        power_points += 2.2 * (0.65 + 0.35 * disp_q)
        quality_components.append(0.70 + 0.30 * disp_q)

    if kill_zone(now):
        c["killzone"] = cfg.w_killzone
        why.append("killzone")
        power_points += 0.6
        quality_components.append(0.55)

    if not in_active_session(now):
        c["off_hours"] = -cfg.w_off_hours
        why.append("off hours")
        quality_components.append(0.20)

    regime = smc.market_regime(df15)
    why.append(f"regime: {regime}")
    if strength == "full" and regime.startswith("TRENDING"):
        c["regime"] = cfg.w_regime_align
        why.append("regime align")
        power_points += 1.0
        quality_components.append(0.75)
    if regime == "RANGING_HIGH_VOL":
        c["regime_chop"] = -cfg.w_regime_chop
        why.append("regime chop")
        quality_components.append(0.15)

    strong_structure = sweep_ok and choch_ok and (disp_q >= 0.55 or near_ob or bool(fvg))
    if strong_structure:
        c["confluence"] = cfg.w_confluence + 0.06
        why.append("STRONG CONFLUENCE")
        power_points += 1.6
        quality_components.append(0.90)

    score = round(sum(c.values()), 4)
    dec.score = score
    dec.features = {k: round(v, 4) for k, v in c.items()}
    dec.reasons = why
    dec.power = round(power_points, 2)

    if quality_components:
        dec.quality = round(sum(quality_components) / len(quality_components), 3)
    else:
        dec.quality = 0.0

    abs_score = abs(score)
    if abs_score >= 1.15 and power_points >= 7.8 and dec.quality >= 0.68:
        dec.grade = "A++"
    elif abs_score >= 0.95 and power_points >= 5.8 and dec.quality >= 0.55:
        dec.grade = "A+"
    elif abs_score >= 0.78 and dec.quality >= 0.45:
        dec.grade = "A"
    else:
        dec.grade = "B"

    structural_gate = near_ob or (sweep_ok and choch_ok) or (sweep_ok and disp_q >= 0.50)
    quality_gate = dec.quality >= 0.42
    if score < cfg.min_score or not structural_gate or not quality_gate:
        dec.blocked = "score/gate/quality"
        return dec

    buf_atr = cfg.sl_atr1h_mult * atr1h
    if ob and near_ob:
        buf = max(buf_atr, (ob["high"] - ob["low"]) * cfg.sl_ob_buffer)
        sl = (ob["low"] - buf) if side > 0 else (ob["high"] + buf)
    elif sweep_ok:
        sl = sw["extreme"] - side * buf_atr
    else:
        sl = price - side * 1.5 * atr1h

    risk = (price - sl) * side
    if risk <= 0:
        dec.blocked = "invalid risk"
        return dec
    if risk < cfg.min_risk_atr1h * atr1h:
        dec.blocked = "risk too small"
        return dec
    if risk > cfg.max_risk_atr4h * atr4h:
        dec.blocked = "risk too large"
        return dec

    rr = cfg.risk_reward
    if dec.grade == "A++":
        rr = min(3.0, cfg.risk_reward + 0.80)
    elif dec.grade == "A+":
        rr = min(2.80, cfg.risk_reward + 0.55)
    elif dec.grade == "A":
        rr = min(2.50, cfg.risk_reward + 0.30)
    if dec.quality >= 0.75:
        rr = min(3.0, rr + 0.15)

    dec.direction = "BUY" if side > 0 else "SELL"
    entry_val, sl_val = price, round(sl, 6)

    ltf_df = data.get("5m") if "5m" in data else data.get("1m")
    if ltf_df is not None and len(ltf_df) >= 10:
        e2, s2, refined = smc.refine_ltf_entry(ltf_df, side, entry_val, sl_val)
        if refined:
            r2 = (e2 - s2) * side
            if cfg.min_risk_atr1h * atr1h <= r2 <= cfg.max_risk_atr4h * atr4h:
                entry_val, sl_val, risk = e2, s2, r2
                why.append("LTF refined entry")

    dec.entry, dec.sl = entry_val, sl_val
    dec.tp = round(entry_val + side * risk * rr, 6)
    return dec
