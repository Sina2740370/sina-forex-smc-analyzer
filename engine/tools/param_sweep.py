"""
سوییپ و تست حساسیت پارامترها — سپر ضد overfitting (v1)

ایده: پارامتری که با کمی جابه‌جایی، امید ریاضی را وارونه کند «شکننده» است؛
لبه‌ی واقعی باید در ±۲۰٪ پارامترها زنده بماند.

کاربرد:
  # حساسیت One-At-a-Time (هر پارامتر جداگانه ±pct٪ حول پیش‌فرض):
  python -m engine.cli sweep --source csv --csv-dir engine/data --symbols EURUSD XAUUSD --mode oat --pct 20

  # همه‌ی پارامترهای عددی (کندتر):
  python -m engine.cli sweep --symbols EURUSD --mode oat --pct 20 --all-params

  # شبکه‌ی صریح (محصول دکارتی):
  python -m engine.cli sweep --symbols EURUSD --mode grid \
      --set min_score=0.5,0.55,0.6,0.65,0.7 --set risk_reward=1.5,2,2.5

خروجی: out/sweep_<hash>.csv + out/sweep_<hash>.md + حکم پایداری هر پارامتر.
دیتا یک‌بار لود و بین همه‌ی ترکیب‌ها مشترک می‌شود؛ بک‌تست‌ها موازی اجرا می‌شوند.
"""
from __future__ import annotations

import itertools
import json
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import fields, replace

import pandas as pd

from ..backtest.runner import evaluate_gate, run_backtest, summarize
from ..core.config import EngineConfig

# پارامترهای کلیدی برای OAT پیش‌فرض (پرمتأثرترین‌ها؛ با --all-params همه سنجیده می‌شوند)
CORE_PARAMS = [
    "min_score", "risk_reward", "sl_atr1h_mult", "sl_ob_buffer",
    "ob_gate_atr4h", "sweep_lookback", "max_hold_hours", "cooldown_hours",
    "w_sweep", "w_choch", "w_ob_in", "w_confluence", "w_regime_chop", "w_off_hours",
]

_WEIGHTS = {"w_htf_full", "w_htf_soft", "w_sweep", "w_sweep_against", "w_choch",
            "w_bos", "w_ob_in", "w_ob_near", "w_ob_mid", "w_pd", "w_fvg",
            "w_candle", "w_killzone", "w_confluence", "w_regime_align",
            "w_regime_chop", "w_off_hours"}


# ---------------------------------------------------------------- ساخت ترکیب‌ها
def _cast(name: str, val, cfg: EngineConfig):
    """تبدیل مقدار متنی به نوع فیلد + محدوده‌های منطقی."""
    ftype = {f.name: f.type for f in fields(EngineConfig)}.get(name, "float")
    if name in _WEIGHTS:
        return max(0.0, float(val))
    if name == "min_score":
        return min(0.95, max(0.05, float(val)))
    if ftype.startswith("int"):
        return max(1, int(round(float(val))))
    return float(val)


def oat_variants(cfg: EngineConfig, pct: float, all_params: bool = False,
                 only: list[str] | None = None) -> list[dict]:
    """هر پارامتر جداگانه ±pct٪ حول پیش‌فرض. خروجی: [{params, cfg, label}]
    only: فقط این پارامترها (برای اجرای متمرکز/پله‌ای)."""
    names = [f.name for f in fields(EngineConfig)
             if isinstance(getattr(cfg, f.name), (int, float)) and f.name != "scan_every_bars"]
    if only:
        unknown = [n for n in only if n not in names]
        if unknown:
            raise SystemExit(f"پارامتر ناشناخته: {unknown}")
        names = list(only)
    elif not all_params:
        names = [n for n in names if n in CORE_PARAMS]
    out = [{"params": {}, "cfg": cfg, "label": "BASELINE"}]
    for name in names:
        base = getattr(cfg, name)
        for mult in (1 - pct / 100.0, 1 + pct / 100.0):
            val = _cast(name, base * mult, cfg)
            if val == base:
                continue
            out.append({"params": {name: val}, "cfg": replace(cfg, **{name: val}),
                        "label": f"{name}={val:g}"})
    return out


def grid_variants(cfg: EngineConfig, sets: list[str]) -> list[dict]:
    """--set name=v1,v2,...  → محصول دکارتی. خروجی مشابه oat_variants."""
    parsed: dict[str, list] = {}
    for s in sets:
        name, _, vals = s.partition("=")
        name = name.strip()
        if name not in {f.name for f in fields(EngineConfig)}:
            raise SystemExit(f"پارامتر ناشناخته: {name}")
        parsed[name] = [_cast(name, v, cfg) for v in vals.split(",") if v.strip()]
    names = list(parsed)
    out = []
    for combo in itertools.product(*(parsed[n] for n in names)):
        kw = dict(zip(names, combo))
        out.append({"params": kw, "cfg": replace(cfg, **kw),
                    "label": ",".join(f"{n}={kw[n]:g}" for n in names)})
    return out


# ---------------------------------------------------------------- اجرا
def _run_one(task):
    """در پروسه‌ی جدا اجرا می‌شود (fork → دیتا copy-on-write است)."""
    (symbol, data, cfg, start, end, strategy_name, label, params) = task
    from ..strategy.smc_topdown import analyze
    from ..strategy import baselines
    fn = analyze if strategy_name == "smc" else {
        "s1": baselines.analyze_s1, "s2": baselines.analyze_s2}[strategy_name]
    trades = run_backtest(symbol, data, cfg, start, end, analyze_fn=fn)
    s = summarize(trades)
    s["expectancy_ci95"] = list(s.get("expectancy_ci95", (0, 0)))
    s["win_rate_ci95"] = list(s.get("win_rate_ci95", (0, 0)))
    return {"label": label, "params": json.dumps(params, ensure_ascii=False),
            "symbol": symbol, **{k: s.get(k) for k in
            ("n", "win_rate", "breakeven_win_rate", "expectancy_r", "profit_factor",
             "total_r", "max_drawdown_r", "expired")}, **evaluate_gate(s)}


def run_sweep(data_map: dict, variants: list[dict], symbols: list[str],
              start=None, end=None, strategy: str = "smc", workers: int = 0) -> pd.DataFrame:
    tasks = []
    for v in variants:
        for sym in symbols:
            tasks.append((sym, data_map[sym], v["cfg"], start, end, strategy,
                          v["label"], v["params"]))
    if workers <= 0:
        workers = min(len(tasks), os.cpu_count() or 1)
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            rows = list(ex.map(_run_one, tasks, chunksize=1))
    else:
        rows = [_run_one(t) for t in tasks]
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- حکم پایداری
def stability_verdict(df: pd.DataFrame, oat: bool) -> list[str]:
    """برای هر پارامتر: آیا امید ریاضی با ±pct٪ وارونه یا فروپاشی می‌شود؟"""
    lines = []
    if oat:
        params = sorted({l.split("=")[0] for l in df["label"].unique()
                         if l != "BASELINE"})
        for name in params:
            frag = []
            any_edge = False
            for sym in sorted(df["symbol"].unique()):
                sub = df[df["symbol"] == sym]
                base_rows = sub[sub["label"] == "BASELINE"]
                if base_rows.empty or not base_rows["n"].iloc[0]:
                    continue
                e0 = base_rows["expectancy_r"].iloc[0]
                if e0 is None or e0 <= 0:
                    continue  # اگر پایه خودش لبه ندارد، حکم حساسیت بی‌معناست
                any_edge = True
                sv = sub[sub["label"].str.startswith(name + "=")]
                for _, row in sv.iterrows():
                    e = row["expectancy_r"]
                    if e is None or pd.isna(e):
                        continue
                    if e <= 0:
                        frag.append(f"{sym}: {row['label']} → E[r] وارونه شد ({e:+.4f})")
                    elif e < e0 * 0.5:
                        frag.append(f"{sym}: {row['label']} → افت >۵۰٪ به {e:+.4f}")
            if not any_edge:
                continue
            status = "🔴 شکننده" if frag else "🟢 پایدار"
            lines.append(f"- `{name}`: {status}" + (f" — {'؛ '.join(frag)}" if frag else ""))
    else:
        lines.append("حالت grid: جدول را مستقیم بخوان — ستون expectancy_r نباید "
                     "بین مقدارهای مجاور علامت عوض کند.")
    return lines


def to_markdown(df: pd.DataFrame, verdicts: list[str], args_note: str) -> str:
    cols = ["label", "symbol", "n", "win_rate", "breakeven_win_rate",
            "expectancy_r", "profit_factor", "total_r", "max_drawdown_r",
            "expired", "ready"]
    cols = [c for c in cols if c in df.columns]
    show = df[cols].copy()
    for c in ("win_rate", "breakeven_win_rate"):
        if c in show:
            show[c] = show[c].map(lambda x: f"{x:.3f}" if pd.notna(x) else "-")
    for c in ("expectancy_r",):
        if c in show:
            show[c] = show[c].map(lambda x: f"{x:+.4f}" if pd.notna(x) else "-")
    md = ["# گزارش Sweep پارامترها\n", f"دستور: `{args_note}`\n",
          show.to_markdown(index=False), "\n## حکم پایداری\n", *verdicts, ""]
    return "\n".join(md)
