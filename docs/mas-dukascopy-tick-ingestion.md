# Dukascopy Historical Tick Ingestion

Status: pure, offline parser and UTC OHLC aggregator; no network client or strategy integration.

## Source contract

The supported export schema is the Dukascopy tick CSV/JSON shape:

- `timestamp`: Unix epoch milliseconds in UTC;
- `askPrice`: exact decimal string;
- `bidPrice`: exact decimal string;
- `askVolume` / `bidVolume`: accepted as source metadata but intentionally not consumed as traded volume.

The upstream downloader documents that ticks contain both best bid and ask, millisecond timestamps, and quote-side volumes. A sample CLI invocation is:

```sh
npx dukascopy-node -i eurusd -from 2020-03-30 -to 2020-03-31 -t tick -f csv
```

The downloader is an external acquisition step. This repository module does not make network requests, invoke Node.js, or download market data.

## Parsing and validation

`dukascopy_tick_from_row` rejects missing fields, invalid/negative/non-integer timestamps, binary floating-point prices, non-positive prices, and crossed quotes. CSV decimal strings are preserved through Python Decimal parsing.

`dukascopy_tick_series` preserves source order, rejects decreasing timestamps, and permits equal millisecond timestamps without reordering. Equal-time input order determines open/close when applicable.

## OHLC aggregation

`aggregate_dukascopy_ticks(rows, timeframe_minutes=15)` groups actual ticks into fixed UTC epoch-aligned, start-inclusive/end-exclusive intervals. Bid and ask OHLC are calculated independently from the same tick observations and emitted as the existing `OHLCQuote` contract.

Only intervals containing ticks are emitted. No sorting, interpolation, forward-fill, synthetic bars, or spread reconstruction occurs. First and last bars may be partial. Gaps remain absent and must be handled explicitly by downstream validation.

The source's askVolume/bidVolume fields are not treated as traded volume or liquidity depth. This adapter currently emits price-only `OHLCQuote` values.

## Boundaries and limitations

- Dukascopy is one provider's feed, not a universal consolidated FX market.
- Historical tick coverage and instrument availability must be checked per symbol/date before any experiment.
- UTC epoch alignment is explicit; it does not reproduce broker-specific candle session boundaries unless a later, separately tested aggregation policy is introduced.
- This adapter is not yet connected to the legacy Backtest, Paper runtime, or a production data store.
- No backtest, Paper/Backtest parity, execution quality, or profitability claim follows from parser/aggregation tests alone.
