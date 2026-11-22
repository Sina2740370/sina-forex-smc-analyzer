# Shared Portfolio Backtest — Validation and Safety Contract

Status: implementation under validation; not a claim of production or Live readiness.

## 1. Multi-symbol account model

`backtest_portfolio` is a separate API from the existing single-symbol `backtest_mas`. It receives a mapping of symbol to M15 DataFrame and uses:
- one overlapping UTC simulation window;
- one master event timeline containing all available symbol candle timestamps;
- one in-memory SQLite Ledger account for the complete portfolio;
- one Risk Agent `AccountState` reconstructed from Ledger after every financial event;
- one active position maximum per symbol, subject to the global max-open-trades, daily-loss, weekly-loss, concurrent-risk and hard-stop gates.

The same-timestamp arbitration contract is deterministic:
1. process existing position exits in lexical symbol order;
2. evaluate new entries in lexical symbol order;
3. append each event synchronously before evaluating the next symbol.

This is a deterministic simulation ordering policy. It is not a claim that independent OTC FX symbols have a truly simultaneous executable market feed.

## 2. Lookahead boundary

Before every analysis call, the engine slices the symbol frame to `time <= decision_timestamp`. The aggregator also retains its own as-of filtering and top-down cache reconstruction. A runtime invariant rejects a frame that contains a timestamp after the decision point.

The master clock includes all candle timestamps, so an open position is advanced at the first event timestamp where its own symbol has a new candle. Decisions are only evaluated when the decision schedule exactly matches a candle timestamp. Missing exact decision bars are skipped, not forward-filled.

## 3. Paper / Backtest parity

The new two-symbol integration test uses identical market frames and deterministic signals in Paper and Portfolio Backtest. It compares immutable Ledger payload fields for:
- PositionOpened: symbol, direction, lots, entry, SL, TP2 and authorized risk;
- PositionClosed: quantity, gross PnL, explicit cost fields, exit price/time and outcome.

Trade identifiers and sizing UUIDs are intentionally not compared because Paper currently generates its sizing identifier randomly. Monetary and execution fields are compared exactly.

Dataset exhaustion remains distinct: Portfolio Backtest can mark remaining positions as `TIME_EXIT`; Paper does not fabricate a market close merely because its current data file ended.

## 4. Execution-cost certification

The current simulator writes commission, slippage and swap as zero placeholders because the input history does not establish a synchronized executable Bid/Ask path or broker fee schedules. The new result explicitly exposes:
- `execution_costs_certified = false`;
- `execution_cost_model = uncertified_zero_cost_placeholder`.

These zero fields are not a claim that real-world costs are zero. Do not compare the resulting equity curve to broker-realizable net performance. A future cost implementation must use documented price basis, spread-once semantics, commission schedule, slippage units, swap rollover and FX conversion provenance. No arbitrary fee assumptions are introduced in this change.

## 5. Lifecycle and recovery

The new engine reuses the existing `_advance_position` OHLC resolver and SQLite Ledger lifecycle contract. SL/TP same-candle collisions remain conservative (SL first); stop gaps retain the shared resolver behavior. Full closes are recorded as immutable PositionClosed events and accounting is read back from Ledger replay.

The Ledger's partial-close replay support does not mean the engines support scale-outs. No partial-close trigger, TP1 fraction, remaining-stop policy or fee allocation is invented here. Engine-level partial exits remain unsupported pending an explicit strategy contract.

The consolidated Ledger PR includes append-only storage, idempotency, lifecycle validation, replay checks, and Paper migration/recovery regression tests. Its full Python 3.11/3.13 suite previously passed on the pre-merge SHA; the post-merge SHA requires fresh validation.

## 6. Acceptance tests

- Shared account: four same-time symbol candidates cannot exceed the configured three-open-trade limit.
- Deterministic arbitration: candidate evaluation order is lexical and stable.
- As-of isolation: analyzer inputs contain no future candle.
- Repeatability: identical data/configuration yields identical portfolio summaries.
- Paper parity: two-symbol open and close accounting payloads match exactly for the tested full-close path.
- Input contract: invalid step size and empty/invalid candle sets fail explicitly.

## 7. Explicit exclusions

No broker connection, Live order routing, real trade, Telegram send, market scan, or remote data retrieval is performed. Strategy thresholds and Risk Agent defaults are not changed by this portfolio-backtest implementation.
