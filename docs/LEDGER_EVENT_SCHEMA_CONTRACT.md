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

## Runtime event registry and schema allowlist

The runtime now maintains an explicit event registry in `engine/mas/ledger.py`.
Projection-affecting event types are enumerated separately from stored-only
event types (`FillRecorded` and `AuditMarker`). Stored-only events are retained
in the immutable log but intentionally do not mutate the account projection.

Append validation rejects event types outside the registry. It also rejects
non-integer schema versions, versions outside the event-specific allowlist,
and schema version 2 on event types other than `PositionOpened`. Replay repeats
the registry and version checks so unsupported persisted records fail closed.

The initial registry-only change deliberately left lifecycle, identity, and
migration recovery as separate acceptance gates. Those gates are now addressed
in the following implementation sections; each remains subject to CI and
integration validation on the final branch head.


## Lifetime trade identity and settlement cost invariants

Implemented on the ledger hardening branch:
- A `PositionOpened` event must carry a non-empty `trade_id`; the value is normalized into the immutable event envelope.
- An account may use a given `trade_id` for only one opening event over its entire event history. Closing a position does not make that identifier reusable.
- The append transaction checks persisted openings and earlier openings in the same batch. Idempotent retries of the original opening event remain valid because the idempotency match is resolved first.
- Replay independently rejects a repeated `trade_id`, including reuse after a prior position was fully closed.
- Commission and slippage must be finite decimal strings greater than or equal to zero. Swap must be finite but may be positive or negative because rollover credits and debits are both possible.
- These checks apply at append and replay boundaries. They do not introduce assumed costs or change Paper/Backtest producer values.

Validation status: regression tests have been added for lifetime identity, same-batch rollback, idempotent retry, persisted-history rejection, negative/non-finite commission and slippage, and signed swap. They remain unexecuted until a CI run reports results for the exact branch head.


## Pre-commit position lifecycle validation

The append transaction now validates the complete ordered position lifecycle for every affected account after inserting the candidate batch but before committing:
- Position openings require a unique lifetime trade identity, finite positive lots, and a finite positive entry price.
- Marks and close events must reference a currently active position.
- Close quantities must be positive and cannot exceed the remaining quantity.
- A full close must settle exactly the remaining quantity; a partial close must leave a positive remainder.
- Gross PnL and settlement costs must be finite decimal strings; commission and slippage must be non-negative while swap remains signed.
- The validation runs against the transaction's own database view under `BEGIN IMMEDIATE`. If any event fails, the entire batch is rolled back.
- Idempotent retries are resolved against their original stored event before lifecycle validation, preserving retry behavior without applying the event twice.

The same lifecycle rules remain independently enforced during replay. This is defense in depth: invalid history cannot be newly committed through the supported append API, and persisted corruption is still rejected during projection rebuild.


## Crash-safe legacy Paper migration

The Paper importer now uses immutable migration intent/completion metadata:
- A pending marker is written before importing legacy state or journal records.
- Seed events are appended as one atomic batch using deterministic legacy idempotency keys.
- The immutable initial-balance baseline is written only after the seed batch is durable.
- A completed marker is written last. On restart, a pending but incomplete migration re-reads the legacy source and retries the same event batch; idempotency prevents duplicate import.
- The importer recognizes the former implementation's metadata-only crash window (initial balance present, no ledger events, no migration completion marker) and completes the import from the still-authoritative legacy state/journal.
- An account with existing non-migration events, no baseline, and no pending migration still fails closed rather than guessing its opening balance.

Crash-injection tests cover interruption before baseline persistence, interruption before the completion marker, and recovery of the older metadata-only failure state.


## Version-2 RiskLockReleased audit contract

New `RiskLockReleased` appends now require schema version 2 and non-empty
`reviewed_by`, `reason`, and `approval_reference` fields. Historical
schema-version-1 release events remain replayable for backward compatibility,
but new v1 releases are rejected. The Paper release API requires an explicit
approval reference and writes the same immutable metadata on the event.

Important security boundary: these fields provide audit attribution only.
They do not authenticate the reviewer, prove that an approval ticket exists,
or establish separation of duties. No identity provider or authorization
service is currently integrated; the release operation must not be described
as authenticated approval. A future authenticated workflow must verify the
actor and approval reference against a trusted authorization system before
appending the event.
