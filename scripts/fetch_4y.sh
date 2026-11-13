#!/usr/bin/env bash
# دانلود داده ۴ ساله M15 از Dukascopy برای بک‌تست جدی
set -euo pipefail
OUT_DIR="${1:-data_duka_4y}"
SYMBOLS="${2:-EURUSD USDJPY GBPUSD XAUUSD}"

echo ">>> Output directory: $OUT_DIR"
echo ">>> Symbols: $SYMBOLS"
mkdir -p "$OUT_DIR"

python -m engine.cli fetch \
  --symbols $SYMBOLS \
  --years 4 \
  --out "$OUT_DIR"

echo ""
echo ">>> Done. Example backtest command:"
echo "python -m engine.cli report --source csv --csv-dir $OUT_DIR --symbols EURUSD USDJPY GBPUSD --walk-forward"
