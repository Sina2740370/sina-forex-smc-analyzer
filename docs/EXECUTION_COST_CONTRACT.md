# Execution Cost and Price-Basis Contract (Proposal)

Status: **Design proposal — not implemented, not broker-certified**

This document records the minimum evidence and invariants required before Paper or Backtest may claim net-of-cost performance. It intentionally does not choose broker-specific values or infer a Bid/Ask convention from OHLC bars.

## 1. Current verified limitations

The current CSV ingestion path normalizes bars to `time/open/high/low/close/volume`. It does not preserve an explicit price-basis field (Bid, Ask, Mid, or Last), nor does the inspected schema carry synchronized bid/ask quotes.

Paper and Backtest currently:
- apply a configured typical spread adjustment to modeled entry price;
- derive exits from OHLC stop/target logic;
- write commission, slippage, and swap as zero in close events.

The symbol spread table describes typical estimated spreads, not a time-synchronized executable quote. Consequently, current results must not be described as broker-realistic net performance.

## 2. Required price-basis metadata

Every market-data batch used for execution simulation should declare:
- `price_basis`: one of `BID`, `ASK`, `MID`, or `LAST`;
- `source` and `instrument`;
- `timestamp_timezone` and normalized UTC timestamps;
- `bar_interval` and whether timestamps label bar open or close;
- `quote_completeness`: whether synchronized Bid and Ask are available;
- provenance/version or content hash for reproducibility.

Unknown or mixed price basis must remain explicitly unknown. It must not silently default to Mid or Bid.

## 3. Execution-side rules

For a conventional OTC FX quote:
- BUY entry executes on Ask and BUY exit executes on Bid.
- SELL entry executes on Bid and SELL exit executes on Ask.

These rules may only be applied when the input quote basis and spread representation are known. A spread adjustment must be applied exactly once. If OHLC is already Ask- or Bid-side, do not add the same spread again to that side.

Mid-only OHLC plus a scalar spread is insufficient to reconstruct the true intrabar Bid/Ask path. Any approximation must be labeled, versioned, and tested as an approximation; it must not be presented as tick-accurate execution.

## 4. Cost model contract

A future cost model should be explicit and immutable per simulated fill:
- commission amount, currency, charging basis (per side / round turn / per lot / notional), and schedule version;
- slippage amount, unit, side, and fill-model version;
- swap amount, currency, rollover timestamp, direction, and applicable trading-day multiplier;
- spread source, observed timestamp, bid, ask, and quote age where available;
- conversion rate, source, timestamp, and maximum permitted age for non-account-currency costs.

All monetary values should enter the accounting boundary as validated decimal strings or Decimal values. Missing required inputs must produce an explicit unavailable/uncertified result, not a fabricated zero-cost estimate. A zero value is valid only when supported by an explicit configuration or source observation.

## 5. FX conversion requirements

For non-USD quote currencies or non-USD cost currencies, conversion must identify:
- source pair and direction;
- rate and timestamp;
- source and quality/provenance;
- maximum age and stale-rate policy;
- conversion path when a direct pair is unavailable.

Using the exit price as a conversion proxy is a documented approximation, not a certified independent FX conversion. Cross-pair conversion and freshness validation remain open requirements.

## 6. Ledger invariants

Each fill/settlement event should preserve enough immutable detail to reproduce:
- gross PnL;
- spread impact (if modeled separately);
- commission;
- slippage;
- swap;
- conversion inputs and outputs;
- net PnL and account-currency amount;
- execution-model and instrument-spec versions.

A retry with the same idempotency key but changed cost inputs must conflict. A correction must be represented by a new auditable event, not mutation of a historical event.

## 7. Validation gates before enabling costs

1. Establish and test the actual price basis for every supported data provider and file format.
2. Define per-instrument pip/tick/contract specifications and account-currency conversion.
3. Unit-test BUY/SELL entry and exit side selection, spread counted once, gaps, stop/target collisions, and rounding.
4. Test commission charging schedules, rollover/swap boundaries, slippage units, and stale/missing quotes.
5. Verify deterministic replay and Paper/Backtest event parity for the same execution inputs.
6. Publish gross and net metrics separately, with model version and data provenance.
7. Keep unsupported instruments or missing cost inputs explicitly uncertified.

## 8. Explicit non-goals

This proposal does not:
- change strategy, signal generation, risk thresholds, or lot sizing;
- introduce assumed broker fees, spreads, swap rates, or slippage values;
- connect to a broker or market-data service;
- enable Live trading;
- certify the current backtest as executable performance.

## 9. Acceptance status

All sections above are design requirements only. Implementation should proceed test-first, in isolated changes, after the relevant source conventions and instrument specifications are established. Until then, current zero-cost settlement fields are a known limitation, not evidence that execution costs are zero.
