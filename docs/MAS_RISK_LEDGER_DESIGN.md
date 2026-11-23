# MAS Risk Ledger — Design Contract (Phase 0)

**Status:** design baseline; no runtime behavior changed.  
**Scope:** MAS risk accounting shared by Paper and Backtest, with a future Live adapter.  
**Safety:** this document does not authorize broker connectivity, order placement, or changes to the production signal path.

## 1. Why this contract is required

The current implementations do not share one account ledger:

- `engine/mas/paper.py` persists equity and open positions in `state.json`, but increments `daily_loss_pct` by the position's original `risk_usd` after a losing close rather than by the realized net loss. It does not maintain weekly realized loss.
- `engine/mas/backtest.py` updates equity and consecutive losses on a close, but does not update daily/weekly loss state. Its sizing path independently calculates lots with floating-point arithmetic and `round(..., 2)`.
- `engine/mas/risk_agent.py` receives daily/weekly percentages through `AccountState`, but does not own or reconstruct their source of truth.
- `engine/mas/portfolio.py` accumulates approved open risk as a rounded percentage, without a position/settlement ledger.
- `tests/test_mas_paper.py` contains an assertion ending in `or True`; that assertion cannot detect the regression it appears to test.

These are source-level findings from the current `main` snapshot. They are not claims about live account balances.

## 2. Non-negotiable accounting invariants

1. **One settlement, one effect.** A close event is applied at most once, keyed by a stable `event_id` and `position_id`.
2. **Realized means closed.** Daily/weekly realized PnL changes only when a close or partial-close event is accepted. Opening a position never books a loss.
3. **Net PnL.** Net realized PnL = gross price PnL − commission − swap − other explicitly modeled fees. Missing cost data must be represented as unknown/estimated, never silently treated as observed zero.
4. **Risk reservation.** Open risk is reserved from approved stop risk, not inferred from the eventual outcome. Reservation is released or reduced on close/partial close.
5. **Conservative sizing.** `actual_modeled_risk <= approved_risk_budget` must hold after lot-step quantization and serialization. No nearest rounding that can exceed budget.
6. **Separate balance and equity.** Balance changes on realized settlement. Equity = balance + marked unrealized PnL; drawdown uses a documented equity series.
7. **No implicit recovery.** A missing, corrupt, stale, or incompatible persisted ledger must fail closed for new entries. It must not silently reset daily loss, weekly loss, or open exposure.
8. **Deterministic replay.** Replaying the same ordered event stream produces byte-equivalent normalized state and the same risk verdicts.
9. **No double counting.** Realized PnL is not also counted as open risk; reserved risk is not itself realized loss.
10. **Auditability.** Every state transition records timestamp, event type, event ID, position ID, currency, source, and ledger schema version.

## 3. Canonical state

The canonical ledger state should contain:

- `schema_version`, `account_id`, `base_currency`, `sequence`
- `balance`, `equity`, `realized_pnl_lifetime`, `unrealized_pnl`
- `fees_lifetime`, `open_risk_amount`, `open_risk_pct`
- `daily_period_id`, `daily_start_balance`, `daily_realized_net_pnl`
- `weekly_period_id`, `weekly_start_balance`, `weekly_realized_net_pnl`
- `peak_equity`, `max_drawdown_amount`, `max_drawdown_pct`
- `consecutive_losses`, `open_positions`, `applied_event_ids`
- `last_event_at`, `updated_at`

Use Decimal (or integer minor units where currency precision is fixed) for money, price-distance-derived risk, fees and lot calculations. Convert to float only at presentation/API boundaries, and validate the serialized amount again.

## 4. Event model

Minimum event types:

- `PositionOpened`: approved risk budget, actual modeled risk, quantity, entry, stop, symbol, timestamp and idempotency key.
- `PositionPartiallyClosed`: closed quantity, execution price, gross PnL, fee components, timestamp and idempotency key.
- `PositionClosed`: remaining quantity and final settlement values.
- `MarkToMarketUpdated`: unrealized PnL and mark timestamp; does not change balance or realized PnL.
- `FeeAdjusted`: a separately identified fee correction when the source reports costs after the fill.
- `PeriodRolled`: explicit daily/weekly period boundary; never inferred from local machine time.

Each event is validated before mutation. Reject duplicate IDs, out-of-order sequence numbers, negative remaining quantity, over-closes, currency mismatches, non-finite values and a realized close against an unknown position. Event application must be atomic.

## 5. Period and timezone policy

Proposed default for FX: daily risk period rolls at **17:00 America/New_York**, including DST transitions; weekly period starts at the Sunday 17:00 New York FX session open. Store all event timestamps in UTC and derive period IDs using IANA timezone data, not fixed UTC offsets.

This is a configurable policy, not a hard-coded assumption. Tests must cover both EST and EDT transitions, weekend gaps, year boundaries and events exactly on the rollover instant. The configured policy and timezone database version should be recorded in run metadata.

## 6. Risk metric semantics

- `daily_loss_pct = max(0, -daily_realized_net_pnl / daily_start_balance * 100)`
- `weekly_loss_pct = max(0, -weekly_realized_net_pnl / weekly_start_balance * 100)`
- `open_risk_pct = sum(remaining_position_stop_risk) / current_equity * 100`
- `equity = balance + unrealized_pnl`
- Daily/weekly loss gates use realized net PnL by default. A separate intraday equity/drawdown circuit breaker may include unrealized loss, but must have its own explicit threshold and must not be conflated with realized-loss accounting.
- A loss streak increments only on a fully settled position whose net realized PnL is negative; partial closes do not independently increment the streak. A final net breakeven resets neither side unless policy explicitly says otherwise.

## 7. Integration sequence

1. Implement a pure, deterministic ledger core with no filesystem, database, broker, network or clock access.
2. Add an adapter for Paper's existing JSON state with explicit schema migration and atomic write/replace. Preserve a recoverable backup; reject malformed state.
3. Make Paper close/open flows emit ledger events; derive `AccountState` from the ledger snapshot. Remove the current risk-budget-as-loss update.
4. Integrate the same ledger core into Backtest; keep each backtest run isolated and deterministic. Add daily/weekly period roll events based on the configured calendar.
5. Make portfolio risk aggregation consume the same exposure calculation, without pretending an analysis-only portfolio is a settled account.
6. Add a future Live adapter only after execution/fill events and broker reconciliation are independently specified and tested.

Do not combine this migration with a strategy change, new market filter, or parameter optimization.

## 8. Required tests before runtime integration can be accepted

- profitable close, losing close, breakeven close
- partial close followed by final close
- fees and swap included exactly once
- no PnL booked on open
- duplicate close event is idempotently ignored/rejected
- close for unknown position rejected
- over-close and negative quantity rejected
- daily and weekly realized loss calculated from net realized PnL, not initial risk
- rollover at exact boundary; EST/EDT; Sunday open; weekend gap
- process restart restores identical state and open exposure
- corrupt/old schema fails closed and migration is deterministic
- actual modeled risk never exceeds approved budget after serialization
- same event stream gives identical Paper and Backtest ledger snapshots
- no future mark or event can mutate a historical as-of snapshot

A passing unit suite is necessary but not sufficient: parity fixtures and a reproducible end-to-end simulation are also required.

## 9. Acceptance gates

No runtime integration is mergeable until:

- all invariants above are enforced by tests;
- existing Paper and Backtest behavior is captured in baseline fixtures;
- migration/recovery and rollback are demonstrated;
- Python 3.11 and 3.13 full test suites pass;
- no secrets, network calls, Telegram delivery or broker execution are introduced;
- changed files and risk behavior are reviewed against the diff;
- the owner explicitly approves the final merge.

## 10. Explicitly out of scope

This phase does not claim a profitable strategy, validate a statistical edge, enable live trading, change signal generation, change risk limits, or alter the production database schema. The main database's existing `signals` table is not automatically a suitable account ledger: it lacks authoritative fills, quantities, fee components and idempotent settlement events.
