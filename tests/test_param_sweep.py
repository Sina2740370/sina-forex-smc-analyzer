"""تست‌های ماژول sweep پارامترها (سریع، بدون بک‌تست واقعی)."""
from __future__ import annotations

import pandas as pd
import pytest

from engine.tools.param_sweep import (_cast, grid_variants, oat_variants,
                                      stability_verdict)


def _cfg():
    from engine.core.config import EngineConfig
    return EngineConfig()


# ---------------------------------------------------------------- oat_variants
def test_oat_default_covers_core_params():
    variants = oat_variants(_cfg(), pct=20)
    labels = {v["label"] for v in variants}
    assert "BASELINE" in labels
    # پارامترهای کلیدی باید حضور داشته باشند
    assert any(l.startswith("min_score=") for l in labels)
    assert any(l.startswith("risk_reward=") for l in labels)
    assert any(l.startswith("w_sweep=") for l in labels)
    # هر پارامتر core دقیقاً دو واریانت (بالا/پایین) دارد
    for p in ("min_score", "risk_reward", "w_sweep"):
        assert sum(l.startswith(p + "=") for l in labels) == 2


def test_oat_pct_correct():
    cfg = _cfg()
    variants = oat_variants(cfg, pct=20)
    rr = {v["label"]: v["cfg"].risk_reward for v in variants
          if v["label"].startswith("risk_reward=")}
    assert rr["risk_reward=1.6"] == pytest.approx(cfg.risk_reward * 0.8)
    assert rr["risk_reward=2.4"] == pytest.approx(cfg.risk_reward * 1.2)


def test_oat_int_param_stays_int():
    variants = oat_variants(_cfg(), pct=20)
    for v in variants:
        if v["label"].startswith("max_hold_hours="):
            assert isinstance(v["cfg"].max_hold_hours, int)
            assert v["cfg"].max_hold_hours >= 1


def test_oat_all_params_includes_regime_weights():
    labels = {v["label"] for v in oat_variants(_cfg(), pct=20, all_params=True)}
    assert any(l.startswith("w_regime_align=") for l in labels)
    assert any(l.startswith("w_pd=") for l in labels)


# ---------------------------------------------------------------- grid_variants
def test_grid_cartesian_product():
    variants = grid_variants(_cfg(), ["min_score=0.5,0.6", "risk_reward=1.5,2,2.5"])
    assert len(variants) == 6  # 2 × 3
    labels = {v["label"] for v in variants}
    assert "min_score=0.5,risk_reward=1.5" in labels
    assert "min_score=0.6,risk_reward=2.5" in labels


def test_grid_unknown_param_raises():
    with pytest.raises(SystemExit):
        grid_variants(_cfg(), ["not_a_param=1,2"])


# ---------------------------------------------------------------- _cast
def test_cast_clamps():
    cfg = _cfg()
    assert _cast("min_score", 1.5, cfg) == 0.95   # سقف
    assert _cast("min_score", -1, cfg) == 0.05   # کف
    assert _cast("w_sweep", -0.1, cfg) == 0.0    # وزن منفی → صفر
    assert _cast("max_hold_hours", 3.4, cfg) == 3
    assert _cast("max_hold_hours", 0, cfg) == 1  # حداقل ۱


# ---------------------------------------------------------------- stability_verdict
def _mkdf(rows):
    return pd.DataFrame(rows)


def test_verdict_flags_sign_flip():
    df = _mkdf([
        {"label": "BASELINE", "symbol": "EUR", "n": 10, "expectancy_r": 0.2},
        {"label": "p=0.4", "symbol": "EUR", "n": 10, "expectancy_r": -0.1},
        {"label": "p=0.6", "symbol": "EUR", "n": 10, "expectancy_r": 0.25},
    ])
    v = stability_verdict(df, oat=True)
    assert any("p" in line and "شکننده" in line for line in v)


def test_verdict_stable_when_no_flip():
    df = _mkdf([
        {"label": "BASELINE", "symbol": "EUR", "n": 10, "expectancy_r": 0.2},
        {"label": "p=0.4", "symbol": "EUR", "n": 10, "expectancy_r": 0.15},
        {"label": "p=0.6", "symbol": "EUR", "n": 10, "expectancy_r": 0.18},
    ])
    v = stability_verdict(df, oat=True)
    assert all("شکننده" not in line for line in v)


def test_verdict_ignores_nonpositive_baseline():
    """اگر پایه خودش لبه ندارد، حکم شکنندگی بی‌معناست و نباید هشدار بدهد."""
    df = _mkdf([
        {"label": "BASELINE", "symbol": "EUR", "n": 10, "expectancy_r": -0.1},
        {"label": "p=0.4", "symbol": "EUR", "n": 10, "expectancy_r": -0.3},
    ])
    v = stability_verdict(df, oat=True)
    assert not v  # هیچ پارامتری ارزیابی نشده چون پایه لبه ندارد
