# Historical Executable Quote Ingestion Contract

Status: implemented as a strict pure ingestion boundary; no legacy data conversion.

## Required source fields
Each historical bar must provide:
- an ISO-8601 timestamp with explicit timezone (normalized to UTC);
- bid_open, bid_high, bid_low, bid_close;
- ask_open, ask_high, ask_low, ask_close.

All prices must arrive as Decimal, integer, or decimal strings. Binary float
values are rejected at this boundary. Both bid and ask OHLC geometry is
validated by the shared OHLCQuote type, and ask must not be below bid at each
corresponding OHLC field.

## Ordering and missing data
Rows must already be strictly chronological. Duplicate and out-of-order
timestamps are rejected; input is never silently sorted. Missing intervals
remain missing. No interpolation, forward-fill, synthetic candle, or spread
reconstruction is performed.

## Explicit non-support
The existing historical MT5/yfinance OHLCV datasets contain one price series,
not independent bid and ask candles. They cannot be passed through this
contract. The adapter does not treat mid, last, close, or a guessed fixed
spread as executable Bid/Ask history.

## Next integration gate
A provider-specific ingestion path must establish source semantics (bid/ask,
timestamp convention, timezone, precision, corrections and gaps) and emit
the required fields. Only then can the common OHLC stop/target resolver and
Decimal execution model be exercised against historical data. No claim of
Backtest/Paper parity follows from this contract alone.
