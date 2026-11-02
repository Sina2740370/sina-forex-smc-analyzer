"""نمودارهای گزارش ممیزی SINA QUANT (برچسب‌های انگلیسی به‌دلیل محدودیت shaping در matplotlib)."""
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ACCENT = "#C9A84C"
GREY = "#B0B0B0"
DARK = "#1A1A1A"

def pooled_per_symbol(path, key="baseline"):
    r = json.load(open(path))
    out = {}
    for s in sorted(r):
        rs = [t["r"] for t in r[s].get(key, {}).get("trades_detail", [])]
        out[s] = sum(rs) / len(rs) if rs else 0.0
    return out

old = pooled_per_symbol("/home/z/my-project/sweep-dev/out_mas/factor_lab/results.json")
hold = pooled_per_symbol("/home/z/my-project/sweep-dev/out_mas/holdout2019/results.json")
syms = sorted(set(old) | set(hold))

# --- نمودار ۱: E[R] هر نماد در دو نمونهٔ مستقل ---
fig, ax = plt.subplots(figsize=(10, 5.2), constrained_layout=True)
x = np.arange(len(syms))
w = 0.38
v1 = [old.get(s, 0.0) for s in syms]
v2 = [hold.get(s, 0.0) for s in syms]
ax.bar(x - w / 2, v1, w, label="Sample A (2023-07 to 2026-10, n=608)", color=ACCENT)
ax.bar(x + w / 2, v2, w, label="Holdout (2020-03 to 2023-05, n=965)", color="#4A6575")
ax.axhline(0, color=DARK, lw=0.8)
ax.set_ylabel("E[R] per trade")
ax.set_title("Per-symbol expectancy in two independent samples — signs flip, edge is noise")
ax.set_xticks(x)
ax.set_xticklabels(syms, rotation=45, ha="right")
ax.grid(True, axis="y", alpha=0.3)
ax.legend(loc="upper right", bbox_to_anchor=(1.0, 1.02), fontsize=9)
fig.savefig("/tmp/chart_per_symbol.png", dpi=200)
plt.close(fig)

# --- نمودار ۲: سهم هزینهٔ اسپرد از 1R (اندازه‌گیری مستقیم) ---
syms2 = ["EURUSD", "GBPUSD", "XAUUSD", "USDJPY"]
spread_pips = [0.8, 1.1, 2.5, 1.0]
sl_med = [28.4, 40.5, 101.5, 29.4]
cost = [sp / sl * 100 for sp, sl in zip(spread_pips, sl_med)]
fig, ax = plt.subplots(figsize=(8, 4.8), constrained_layout=True)
bars = ax.bar(syms2, cost, color=ACCENT, width=0.55)
for b, c in zip(bars, cost):
    ax.text(b.get_x() + b.get_width() / 2, c + 0.05, f"{c:.1f}%", ha="center", fontsize=10)
ax.set_ylabel("Round-trip spread as % of 1R")
ax.set_title("Execution cost is small (~3% of 1R) — the bottleneck is raw signal edge, not costs")
ax.set_ylim(0, 4.5)
ax.grid(True, axis="y", alpha=0.3)
fig.savefig("/tmp/chart_cost.png", dpi=200)
plt.close(fig)

print("charts saved")
from PIL import Image
for p in ["/tmp/chart_per_symbol.png", "/tmp/chart_cost.png"]:
    im = Image.open(p)
    print(p, im.size)
