"""
استراتژی Top-Down SMC روی هسته‌ی خالص.

فرق‌های مهم با analysis_engine قبلی:
- زمان از پارامتر `now` می‌آید (نه ساعت سیستم) → بک‌تست و live یک کد دارند
- هر ویژگی فقط یک‌بار امتیاز می‌گیرد (Unicorn دیگر Sweep/CHOCH را دوباره نمی‌شمارد)
- فاصله تا OB بر حسب ATR تایم 4h است (نه 15m)
- سقف ریسک (max_risk_atr4h) دارد
- خروجی شامل بردار ویژگی‌ها و hash کانفیگ است
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
    direction: Optional[str] = None      # "BUY" | "SELL" | None
    score: float = 0.0
    grade: str = "-"                     # کیفیت ستاپ: A+ | A | B (برای مدیریت حجم)
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
    """لندن 07-10 به وقت Europe/London و نیویورک 07-10 به وقت America/New_York (با DST)."""
    t = now.tz_localize("UTC") if now.tzinfo is None else now
    ldn = t.tz_convert("Europe/London").hour
    ny = t.tz_convert("America/New_York").hour
    return 7 <= ldn < 10 or 7 <= ny < 10


def in_active_session(now: pd.Timestamp) -> bool:
    """سشن فعال لندن یا نیویورک (07-16 محلی، با DST).
    خارج از این سشن‌ها اسپرد پهن و نقدشوندگی کم است (منبع اصلی استاپ‌های کاذب)."""
    t = now.tz_localize("UTC") if now.tzinfo is None else now
    ldn = t.tz_convert("Europe/London").hour
    ny = t.tz_convert("America/New_York").hour
    return 7 <= ldn < 16 or 7 <= ny < 16


def top_down_bias(df_wk: pd.DataFrame, df_d: pd.DataFrame):
    """(side, strength, reason). side: +1 / -1 / None."""
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


def analyze(data: dict, cfg: EngineConfig, now: pd.Timestamp) -> Decision:
    """data: {tf: DataFrame کندل‌های بسته‌شده}. هیچ I/O ندارد."""
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

    c: dict = {}          # سهم هر ویژگی در امتیاز
    why: list = [f"bias: {reason}"]

    c["htf"] = cfg.w_htf_full if strength == "full" else cfg.w_htf_soft

    # --- Sweep ---
    sw = smc.liquidity_sweep(df15, cfg.sweep_lookback)
    want = "BULLISH_SWEEP" if side > 0 else "BEARISH_SWEEP"
    mult = smc.sweep_multiplier(sw, atr15)
    sweep_ok = bool(sw and sw["type"] == want)
    if sweep_ok:
        c["sweep"] = cfg.w_sweep * mult
        why.append(f"sweep x{mult:.2f}")
    elif sw:
        c["sweep_against"] = -cfg.w_sweep_against * mult
        why.append("sweep against")

    # --- CHOCH / BOS روی 1h ---
    brk = smc.structure_break(df1h, cfg.break_lookback)
    want_dir = "BULL" if side > 0 else "BEAR"
    choch_ok = bool(brk and brk["dir"] == want_dir and brk["kind"] == "CHOCH")
    if brk and brk["dir"] == want_dir:
        c["choch" if choch_ok else "bos"] = cfg.w_choch if choch_ok else cfg.w_bos
        why.append(brk["kind"])

    # --- Order Block 4h ---
    ob = smc.order_blocks(df4h, cfg.ob_lookback)["bullish" if side > 0 else "bearish"]
    dist = smc.ob_distance(price, ob, atr4h) if ob else None
    if dist is not None:
        if dist == 0.0:
            c["ob"] = cfg.w_ob_in
        elif dist <= 0.5:
            c["ob"] = cfg.w_ob_near
        elif dist <= 1.0:
            c["ob"] = cfg.w_ob_mid
        why.append(f"OB dist {dist:.2f} ATR4h")
    near_ob = dist is not None and dist <= cfg.ob_gate_atr4h

    # --- Premium / Discount ---
    zone = smc.premium_discount(df4h, cfg.pd_lookback)
    good = "DISCOUNT" if side > 0 else "PREMIUM"
    c["pd"] = cfg.w_pd if zone == good else -cfg.w_pd
    why.append(zone)

    # --- FVG ---
    fvg = smc.fvgs(df15, cfg.fvg_lookback, cfg.fvg_min_gap_ratio)["BULLISH" if side > 0 else "BEARISH"]
    if fvg:
        c["fvg"] = cfg.w_fvg
        why.append("FVG")

    if smc.candle_confirmation(df15, side):
        c["candle"] = cfg.w_candle
        why.append("candle")

    if kill_zone(now):
        c["killzone"] = cfg.w_killzone
        why.append("killzone")

    # --- Market Regime (دیگر تزئینی نیست: در امتیاز اثر مستقیم دارد) ---
    regime = smc.market_regime(df15)
    why.append(f"regime: {regime}")
    if strength == "full" and regime.startswith("TRENDING"):
        c["regime"] = cfg.w_regime_align
        why.append("regime align")
    if regime == "RANGING_HIGH_VOL":
        c["regime_chop"] = -cfg.w_regime_chop
        why.append("regime chop")

    # --- نقدشوندگی: ورود خارج سشن جریمه می‌شود ---
    if not in_active_session(now):
        c["off_hours"] = -cfg.w_off_hours
        why.append("off hours")

    if sweep_ok and choch_ok and (fvg or near_ob):
        c["confluence"] = cfg.w_confluence
        why.append("confluence")

    score = round(sum(c.values()), 4)
    dec.score, dec.features, dec.reasons = score, {k: round(v, 4) for k, v in c.items()}, why
    abs_score = abs(score)
    dec.grade = "A+" if abs_score >= 1.0 else "A" if abs_score >= 0.8 else "B"

    gate = near_ob or (sweep_ok and choch_ok)
    if score < cfg.min_score or not gate:
        dec.blocked = "score/gate"
        return dec

    # --- SL / TP ---
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

    dec.direction = "BUY" if side > 0 else "SELL"
    entry_val, sl_val = price, round(sl, 6)

    # --- LTF Entry Refinement (اگر دیتاهای 5m یا 1m موجود باشد) ---
    ltf_df = data.get("5m") if "5m" in data else data.get("1m")
    if ltf_df is not None and len(ltf_df) >= 10:
        e2, s2, refined = smc.refine_ltf_entry(ltf_df, side, entry_val, sl_val)
        if refined:
            r2 = (e2 - s2) * side
            # چک‌های min/max ریسک باید روی ورود/SL پالیش‌شده هم اعمال شود،
            # وگرنه refine می‌توانست چک‌های بالاتر را دور بزند.
            if cfg.min_risk_atr1h * atr1h <= r2 <= cfg.max_risk_atr4h * atr4h:
                entry_val, sl_val, risk = e2, s2, r2
                why.append("LTF refined entry")

    dec.entry, dec.sl = entry_val, sl_val
    dec.tp = round(entry_val + side * risk * cfg.risk_reward, 6)
    return dec
