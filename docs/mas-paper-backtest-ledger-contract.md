# MAS Paper / Backtest Shared-Ledger Integration Contract

Status: design contract only. This document does not change runtime behavior.

## 1. Objective and non-goals

Use one deterministic portfolio-accounting contract for Paper and Backtest. The same ordered domain events must produce the same ledger snapshot when both modes receive equivalent executions and marks.

The ledger is the sole authority for cash balance, realized PnL, financing, fees, open quantity, equity, open risk, daily/weekly loss, drawdown, and consecutive-loss state. Strategy analysis may consume a read-only AccountState projection, but must not mutate account state.

Out of scope: broker connectivity, live orders, Telegram delivery, strategy/SMC rule changes, forced regime filters, and claims of profitability.

## 2. Canonical identity and ordering

- A portfolio has one stable ledger_id across all symbols and process restarts. Do not create one ledger per symbol.
- A position_id is deterministic and persisted at the first accepted entry. It must not depend on random UUIDs, process-local counters, or wall-clock time alone. Recommended composition: account namespace + strategy run namespace + symbol + direction + signal decision timestamp + stable signal/candidate identity. Reject collisions rather than silently reusing an ID.
- An execution_id identifies one immutable fill/settlement. Re-delivery of the same execution must be an exact idempotent retry; same ID with different payload is a hard error.
- Event sequence is assigned by the ledger, contiguous and strictly increasing. Events are applied in deterministic event-time order; ties require a stable secondary key.
- All timestamps must be timezone-aware and normalized to UTC at the accounting boundary. New York 17:00 daily and Sunday 17:00 weekly rollovers are emitted before any trade event belonging to the new period.

## 3. Domain event mapping

Minimum event lifecycle:

1. `PositionOpened`: stable position_id, symbol, side, original quantity, entry price, stop, approved risk, actual risk, timestamp, currency.
2. `PositionPartiallyClosed`: position_id, execution_id, close quantity, gross realized PnL, commission, signed swap, other fees, timestamp.
3. `PositionClosed`: same settlement fields; quantity must equal the remaining open quantity.
4. `MarkToMarketUpdated`: aggregate unrealized PnL for every remaining open position from one coherent quote/mark snapshot. No position may be omitted because its symbol lacks a fresh mark; missing/stale marks must cause an explicit fail-closed outcome or a separately specified stale-mark policy.
5. `PeriodRolled`: daily/weekly period IDs, emitted before first event in the new period.

A close and the subsequent portfolio MTM update must be committed as one atomic batch. After settlement, recompute unrealized PnL across all positions still open; do not subtract the closed position twice or carry its prior unrealized amount.

## 4. Pricing, PnL and precision boundary

- Side-aware executable marks are mandatory: long positions are valued on the liquidation bid; short positions on the liquidation ask. Entry and exit execution prices must include the modeled spread exactly once.
- Paper and Backtest must call the same execution-cost/PnL calculation contract for equivalent inputs. Spread, slippage, commission, signed swap/financing, and other fees must be explicit fields, not hidden adjustments.
- Intrabar ambiguity remains conservative: if stop and target are both touched in the same bar, apply the documented adverse-first rule consistently in both modes.
- Decimal Ledger values are authoritative. Do not calculate PnL in binary float and then imply that converting the result to Decimal restores precision. Define an explicit ingestion/quantization policy for source prices and quantities, then calculate money and fees in Decimal.
- A missing quote, invalid price, non-finite number, negative quantity, or unquantifiable execution must be rejected before ledger mutation.

## 5. Paper state and migration policy

The current Paper state is JSON with equity, open_positions, consecutive_losses, daily_loss_pct and run metadata. It does not prove a complete ledger history or a trustworthy daily/weekly equity baseline.

Migration must be versioned and conservative:

- Preserve the original JSON and journal as immutable migration evidence; never overwrite them before a verified ledger commit.
- If legacy state has no open positions, migration may proceed only when initial/current balance and required period baselines can be established from authoritative evidence. Do not invent missing realized PnL or fee history.
- If open positions exist, migration requires every position's stable identity, side, symbol, remaining quantity, entry, risk fields, timestamp and a defensible current mark, plus reconciliation of account equity and costs. If any required field is absent or totals do not reconcile within an explicitly defined currency precision, stop migration and require manual reconciliation.
- Do not silently approximate missing history from the current equity field.
- Store a migration version and source fingerprint so a restart cannot apply the same migration twice.
- After cutover, SQLite event log + snapshot are authoritative. JSON may remain a read-only compatibility/export projection during a bounded transition; it must not be a second writable source of truth.
- Crash recovery must cover interruption before migration, during ledger creation, after ledger commit but before compatibility export, and after export. Every case must converge idempotently.

## 6. Paper adapter responsibilities

- Load or initialize the one portfolio ledger and verify its identity/schema before analysis.
- Reconcile persisted open positions against available market data before allowing new decisions.
- Emit rollover before trade events.
- Settle all triggered positions; batch each close with a coherent post-close portfolio MTM.
- Compute a complete portfolio AccountState projection from the ledger for signal gates. Projection is read-only.
- Create new positions only after the strategy/risk gate approves; persist the deterministic PositionOpened event before publishing the position to the Paper state view.
- Never write the legacy JSON state first and then attempt ledger persistence.

## 7. Backtest portfolio orchestrator responsibilities

- Replace per-symbol independent account balances with one portfolio ledger shared across all symbols in a chronological event loop.
- Preserve per-symbol strategy calculations and symbol-specific market data, but merge candidate decisions and executions into one deterministic portfolio event stream.
- At each decision timestamp, construct AccountState from the same ledger snapshot; apply portfolio concurrency, daily/weekly loss and symbol exposure gates consistently.
- Do not let iteration order of the symbols list decide which simultaneous signal receives scarce portfolio capacity. Define deterministic timestamp + priority + symbol tie-breaking and test permutation invariance.
- Keep each symbol's market-data visibility bounded by the decision timestamp. Precomputed caches must be proven not to expose future bars.
- Define end-of-test treatment explicitly: either mark all remaining positions at the common terminal timestamp or force-close them with the same executable-price/cost rules. Do not compare Paper marked equity with Backtest forced-close equity as if equivalent.
- Backtest output should include one portfolio-level ledger snapshot/curve plus per-symbol attribution derived from the shared event stream.

## 8. Paper / Backtest parity acceptance

A parity fixture supplies the same timestamped bars/quotes, strategy decisions, fills, costs, rollover schedule and terminal policy to both adapters. Compare:

- canonical event type, event_id, sequence, timestamp and payload;
- cash balance, realized net PnL, fees/financing, unrealized PnL and equity;
- remaining positions and quantities, open risk, daily/weekly loss, consecutive losses;
- portfolio drawdown and terminal state.

Required deterministic scenarios:

1. One open and one full close.
2. Two simultaneous positions on different symbols sharing one equity/risk budget.
3. Partial close followed by final close.
4. Close one position while another remains open; verify aggregate MTM excludes the closed position and includes the remaining one.
5. Long liquidation at bid and short liquidation at ask.
6. Positive and negative swap; commission and other fees.
7. Daily and weekly rollover with positions carried open.
8. Duplicate exact execution retry and divergent same-ID rejection.
9. Restart/reload between every lifecycle boundary.
10. SQLite failure during a close+MTM batch; neither persisted nor in-memory state may partially publish.
11. Missing/stale quote and malformed/non-finite values fail closed.
12. Permuting input symbol order yields the same event stream and final snapshot.
13. End-of-test open positions follow the selected common terminal policy.
14. Lookahead sentinel: modifying bars strictly after a decision timestamp must not change that decision or its emitted event.

Acceptance requires all targeted tests and full CI on the exact head SHA, plus explicit reconciliation fixtures. A passing unit suite alone does not establish strategy edge, statistical robustness, or live-market execution quality.

## 9. Cutover gates

Do not enable Paper ledger cutover until:
- legacy migration decision matrix and versioned schema are implemented;
- deterministic IDs and event ordering are tested;
- atomic close + aggregate MTM is used by the adapter;
- restart, duplicate, rollback and stale-mark tests pass;
- no hidden float-based money calculations remain in the accounting path.

Do not enable shared Backtest until:
- portfolio event loop is independent of symbol iteration order;
- all symbols share one account/risk state;
- execution costs and terminal policy are explicit;
- lookahead and event-level parity tests pass.

Do not claim Paper/Backtest parity until canonical event streams and resulting ledger snapshots match on the same fixtures.

## 10. Current known gaps (observed before implementation)

- Paper currently persists a mutable JSON state and updates daily loss by accumulating risk_usd for losing trades; it does not derive the metric from the equity baseline.
- Paper currently uses float prices/PnL and fixed symbol spread; its module documentation says slippage and swap are not modeled.
- Backtest currently returns a per-symbol BTResult and maintains a local float equity/AccountState; it processes one open position per symbol in that function.
- Backtest currently force-closes a remaining position at the final available close, while Paper carries positions forward. This terminal-policy mismatch must be resolved before parity claims.
- The shared Decimal ledger and atomic batch API exist on the parent PR chain, but neither Paper nor Backtest currently consumes them.

