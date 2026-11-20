# Ledger Event Schema Contract — PositionOpened / Sizing Authorization

Status: design record only. This document does not change runtime behavior or the current event schema.

## Scope

Define a backward-compatible validation boundary between the Risk Agent's immutable `SizingResult`, the `PositionOpened` event written by Paper/Backtest, and the open-position projection reconstructed by SQLite Ledger.

The ledger is the accounting source of truth. A sizing snapshot is an authorization record, not merely display metadata. It must remain attributable to the exact position that was opened.

## Verified current behavior

- `engine/mas/paper.py` and `engine/mas/backtest.py` call Risk Agent again at the simulated fill and serialize the approved `SizingResult` into `PositionOpened.payload.sizing_result`.
- `engine/mas/ledger.py` stores the event payload as canonical JSON and copies `sizing_result` into the replayed open-position projection.
- `SQLiteLedger._validate_input` currently validates the event envelope, JSON serialization, and positive schema version. It does not validate the nested sizing snapshot or compare it with the enclosing position fields.
- Existing ledger tests intentionally construct legacy `PositionOpened` events without a sizing snapshot. Those events must remain replayable.

## Compatibility strategy

1. Preserve schema-version-1 behavior for historical events: an absent `sizing_result` remains accepted and is explicitly classified as legacy/unverified.
2. Introduce a new event schema version only when the validator and all Paper/Backtest producers are ready together.
3. For the new version, require a complete, approved sizing snapshot. Do not silently synthesize missing authorization fields from the position payload.
4. Validate at append time and again during replay. Append-time validation prevents new malformed events; replay validation detects invalid historical bytes or unsupported event versions.
5. Do not rewrite existing append-only events. Any migration must be additive and retain original event identity, payload, and idempotency semantics.

## Required invariants for a versioned sizing snapshot

All financial quantities remain canonical decimal strings; parsing and comparisons use `Decimal`, never binary float.

- Required identity: `sizing_id`, `approved`, `symbol`, `direction`, `spec_version`, `risk_config_hash`.
- Authorization: `approved` must be exactly true; symbol and direction must equal the enclosing `PositionOpened` fields.
- Quantity/price binding: snapshot `lots`, `entry_price`, and `stop_loss` must equal the enclosing position's lots, entry price, and stop-loss fields after canonical Decimal normalization.
- Positive finite values: lots, entry price, stop loss, requested risk, actual risk, per-lot risk, lot step, minimum lot, and maximum lot must be finite and positive.
- Risk arithmetic: `actual_risk == per_lot_risk * lots` under the sizing precision contract; `actual_risk <= requested_risk` because sizing rounds down conservatively.
- Lot constraints: lots must be within min/max bounds and aligned to lot_step. Define the precision/quantization rule explicitly before enforcement; do not use a float tolerance.
- Directional stop: BUY requires stop_loss below entry_price; SELL requires stop_loss above entry_price.
- Fingerprints: `spec_version` and `risk_config_hash` must be non-empty. Their source payloads should be retained or deterministically reconstructable if later audits need to reproduce the hash.
- Unknown fields: preserve them for forward compatibility only if the event schema version explicitly allows them; reject unknown required-version changes rather than silently interpreting them.

## FX metadata boundary

Current implementation records the caller's supplied `price` as `fx_rate` for non-USD quote symbols. The timestamp is also caller supplied; neither value proves an independently sourced, fresh conversion quote.

Do not promote these fields to a certified FX quote. A future FX quote contract must identify:
- base currency and quote currency;
- pair and conversion direction;
- rate and source/provider identity;
- observed timestamp and timezone normalization;
- maximum permitted age and stale/missing-data policy;
- whether the rate is entry-time sizing conversion or exit-time realized-PnL conversion.

Do not add a freshness rejection gate until the source and policy are defined and tested against supported symbols.

## Required tests before implementation is accepted

- Valid Paper and Backtest snapshots pass and survive close/reopen/replay unchanged.
- Missing snapshot remains accepted only for the explicitly supported legacy event version.
- New-version event with missing, unapproved, malformed, non-finite, zero, or negative snapshot fields is rejected.
- Mismatched symbol, direction, lots, entry price, or stop-loss is rejected.
- Decimal arithmetic mismatch, over-request risk, invalid lot-step alignment, and out-of-bounds lots are rejected.
- Idempotent retry with byte-equivalent canonical payload is accepted; reuse of the same key with a changed snapshot is a conflict.
- Replay fails closed on an invalid new-version snapshot and remains deterministic across restart.
- Existing Paper/Backtest parity, ledger rollback, append-only, crash-recovery, and risk-boundary suites remain green on Python 3.11 and 3.13.

## Non-goals and safety

- No broker, Live execution, order routing, or network quote retrieval.
- No automatic FX-rate inference or synthetic conversion metadata.
- No change to risk percentages, strategy logic, TP1/TP2 behavior, spread, commission, slippage, or swap.
- No main-branch change and no merge as part of this design record.

## Exit criteria

Implementation is eligible for review only after the versioned contract, legacy policy, append/replay validators, negative tests, and exact-head CI evidence are all present. Independent review and all other PR #49 acceptance gates remain mandatory.

## Version-2 PositionOpened identity and validation order

A schema-version-2 `PositionOpened` event must carry a non-empty event-level `trade_id`. The Risk Agent sizing snapshot must agree exactly with the immutable position payload for symbol, direction, lots, entry price, and stop loss before the ledger accepts the event. After identity consistency is established, the ledger validates risk arithmetic, lot bounds and step alignment, and directional stop placement. Negative tests that target a later invariant must keep the corresponding outer position fields aligned with the snapshot so they exercise that invariant rather than failing the earlier identity check.
