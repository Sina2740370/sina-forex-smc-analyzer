# Paper / Backtest Lifecycle Parity Matrix

Status: **Audit record — partial parity only**

This matrix describes the currently inspected implementation. It is not a claim of complete engine equivalence.

## Shared behavior verified in source

- Paper imports and calls the same `_advance_position` implementation used by Backtest.
- Both use the same modeled entry convention: candle close plus/minus the configured typical spread.
- Both reauthorize sizing through the Risk Agent at the modeled fill price.
- Both settle full positions through the shared SQLite Ledger event contract.
- Both currently record commission, slippage, and swap as zero; this is a known model limitation, not evidence that those costs are zero.
- The stop/target OHLC resolver prioritizes stop loss when both SL and TP are touched in the same candle and models a stop gap at the worse candle-open price.

## Lifecycle matrix

| Lifecycle path | Paper | Backtest | Evidence / status |
|---|---|---|---|
| Full TP close | Supported | Supported | Deterministic integration parity test exists |
| Full SL close | Supported | Supported | Deterministic integration parity test exists, including risk-state projection comparison |
| Same-candle SL/TP collision | Shared resolver | Shared resolver | Resolver is common; dedicated parity coverage should remain explicit |
| Stop gap | Shared resolver | Shared resolver | BUY/SELL gap tests exist; execution-side quote basis remains unresolved |
| Open position at end of current data batch | Remains open | Not applicable during ordinary steps | Paper dataset-boundary test exists |
| Backtest end-of-dataset settlement | Not performed by Paper | Forced `TIME_EXIT` | Intentional semantic difference; dataset exhaustion is not a Paper execution event |
| Partial close / scale-out | No engine path identified | No engine path identified | Ledger can replay partial-close events, but engine settlement loops inspected here emit full `PositionClosed` events |
| Commission / slippage / swap | Recorded as zero | Recorded as zero | Not modeled; see `EXECUTION_COST_CONTRACT.md` |
| Intrabar Bid/Ask reconstruction | Not established | Not established | Input schema does not preserve price basis or synchronized quotes |
| Cross-pair FX conversion freshness | Not certified | Not certified | Conversion provenance and maximum quote age remain open |

## Important semantic boundary

Paper and Backtest should not be forced into identical behavior at dataset exhaustion. A finite historical sample ending is a backtest boundary condition; it is not proof that a Paper position was closed by the market. Paper therefore leaves an untriggered position open. Backtest may report a separately labeled `TIME_EXIT` for sample accounting, but those results must not be confused with a strategy-generated SL/TP close.

## Partial-close design gate

The Ledger's `PositionPartiallyClosed` replay support is not sufficient to claim engine support. Before implementing scale-outs, define and test:
- which target(s) trigger a partial fill and what fraction of original lots is closed;
- whether remaining stop loss moves, and exactly when;
- whether risk exposure and reserved risk are recalculated after each partial fill;
- how commission, swap, slippage, and conversion are allocated across partial fills;
- how trade-level consecutive-loss state is determined (only after final close);
- idempotency and crash recovery between partial fills and final close;
- identical event generation for Paper and Backtest under the same deterministic market input.

No partial-close behavior should be invented or enabled until these strategy semantics are explicitly specified and covered by deterministic tests.

## Acceptance criteria for full parity

1. Same immutable market input, strategy configuration, symbol specification, and initial account state.
2. Same signal/fill decisions at every eligible decision timestamp.
3. Same ordered lifecycle events and canonical payload fields for all non-boundary paths.
4. Same gross/net PnL, equity, open positions, risk state, and loss streak after replay.
5. Explicitly classified differences for dataset boundary, unavailable data, and unsupported execution-cost inputs.
6. Deterministic tests for TP, SL, gaps, same-bar collisions, partial exits (once specified), retries, and crash recovery.

## Scope and safety

This document changes no runtime behavior, strategy rules, or risk settings. It does not authorize broker connections, Live execution, or trades. PR #49 remains Draft until full acceptance gates and independent review are complete.
