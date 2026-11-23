# SINA QUANT — Manual Trading Intelligence Specification

**Status:** normative architecture contract  
**Execution policy:** `MANUAL_ONLY`  
**Broker connectivity:** prohibited  
**Live order execution:** prohibited

This document defines the target architecture. A requirement marked **planned** is not represented as implemented merely because it appears here. Implementation status must be evidenced by code, tests, and CI.

## 1. Non-negotiable operating boundary

SINA QUANT is a market-analysis and decision-support system. It may ingest public/authorized market data, analyze it, calculate hypothetical risk, publish alerts, and record user-entered trade facts.

It must never:
- connect to a broker account to read account state or submit orders;
- call MT5 trade/order APIs, FIX order-routing endpoints, broker REST/WebSocket trading endpoints, or equivalent execution interfaces;
- create, submit, modify, cancel, close, or partially close a live order or position;
- infer that a signal was traded merely because it was generated or acknowledged;
- represent Paper/Backtest fills as real user fills.

The word “execution” in simulation code refers only to deterministic historical/Paper fill simulation. It must be named and documented as simulation to avoid confusion with broker execution.

Any future request to add broker execution would be a separate architectural change and is outside this product contract.

## 2. Operating modes and data separation

| Mode | Purpose | Permitted data/actions |
|---|---|---|
| Analysis | Live market analysis | Market data only; no account connection |
| Recommendation | Produce BUY/SELL/WAIT plan | Entry zone, invalidation, SL/TP, sizing estimate, expiry |
| Manual journal | Record real user action | User-entered fill, volume, timestamps, costs and exit |
| Paper | Simulated strategy validation | Historical/synthetic fills; isolated state |
| Backtest | Historical research | Point-in-time data and explicit cost model |

The data model and API must label the provenance of every trade record: `MANUAL`, `PAPER`, or `BACKTEST`. Performance reports must not aggregate these sources without an explicit filter.

## 3. Market-data integrity

Required controls:
- Validate schema, OHLC invariants, timezone, duplicate timestamps, gaps, outliers and symbol identity.
- Track provider, retrieval time, source candle time, timeframe, and data-quality status.
- Enforce per-timeframe freshness limits and closed-candle policy.
- Do not construct a supposedly closed higher-timeframe candle from incomplete lower-timeframe bars.
- Detect stale fallback data and surface its age; never silently present stale data as current.
- Empty provider responses and provider exceptions must follow one documented cache/fallback policy.
- A missing/invalid required feed must fail closed to WAIT/NO-TRADE.

Acceptance: tests cover empty responses, exceptions, stale cache, future timestamps, duplicate/gapped bars, timezone/DST, incomplete HTF bars and synchronized portfolio snapshots.

## 4. Analysis and intelligence layers

Required analytical layers:
1. Market structure and price action.
2. SMC/ICT: BOS, CHOCH, OB, FVG, liquidity pools/sweeps, premium/discount and mitigation/invalidations.
3. Multi-timeframe context with explicit candle-close semantics.
4. Volatility and liquidity: ATR, realized volatility, spread proxy/observed spread, session liquidity and abnormal conditions.
5. Market regime as contextual evidence, **not a hard mandatory regime gate**.
6. Cross-asset/currency correlation and exposure clustering.
7. Fundamental macro context and point-in-time economic events.
8. News impact, event time, source timestamp and confidence.
9. Quantitative/statistical layer: conditional expectancy, distributions, uncertainty, calibration and baselines.
10. Debate/ensemble layer that preserves disagreement rather than hiding it.

Every directional recommendation must expose evidence, missing inputs, invalidation conditions, timestamp, expiry, strategy/config version and a stable signal identifier. Confidence is not a calibrated probability unless calibration evidence supports that interpretation.

## 5. Recommendation contract

A recommendation must contain, where applicable:
- symbol, direction, timeframe and generation timestamp;
- source-data timestamp and freshness status;
- entry type/zone and a clearly defined trigger;
- initial stop, targets and invalidation conditions;
- gross and cost-adjusted reward/risk estimates;
- hypothetical size and risk amount based on user-supplied account/contract inputs;
- quality score, uncertainty, supporting evidence and opposing evidence;
- news/event warnings and correlation/exposure warnings;
- expiry time and explicit WAIT/NO-TRADE reason.

Expired recommendations must not remain actionable. A new recommendation must not silently mutate an existing recommendation.

## 6. Manual account and contract inputs

No account data is fetched from a broker. User-supplied settings may include:
- balance and equity, account base currency;
- risk percentage and daily/weekly loss budgets;
- open positions and reserved risk;
- symbol contract size, tick size/value, pip size, volume step/min/max;
- margin assumptions and conversion rates;
- spread, commission, swap and slippage assumptions.

Each value needs provenance, effective timestamp and validation. Unknown contract specifications or FX conversion must produce an explicit uncertainty/blocked sizing result, not a guessed lot size. A minimum lot must never silently exceed the requested risk budget.

## 7. Risk engine

Required guards:
- per-trade risk, aggregate open risk, maximum open positions and per-symbol exposure;
- daily and weekly realized loss limits with explicit timezone and reset semantics;
- consecutive-loss stop that cannot be bypassed by position-size scaling;
- drawdown guard and abnormal-volatility/news protection;
- correlation-aware aggregate exposure;
- cost-aware sizing and conservative rounding to valid volume steps;
- no trade when inputs are missing, contradictory, stale or unverifiable.

Risk checks are advisory gates for the user's manual decision; the system does not control the broker account. The UI must clearly show the timestamp and manual provenance of account inputs.

## 8. Manual trade lifecycle

The manual journal must support:
1. Recommendation created (not a trade).
2. User manually confirms that a real trade was opened and records actual fill time, price, side, volume, initial stop and optional initial target.
3. Optional user-entered changes to SL/TP with an audit trail.
4. Open-position monitoring using market data only; alerts are informational and never execute.
5. User manually records partial/full exit, exit time/price, commissions, swap and other costs.
6. Realized PnL, R multiple and slippage are calculated from actual entered facts.
7. Corrections are append-only/audited; deletion must not silently rewrite performance history.

A signal that was not manually marked as filled is not a real trade. Open, closed, cancelled, expired and rejected recommendations are separate from actual position state.

## 9. Performance, research and learning

Required:
- Backtest/Paper/manual journal separation and source-aware reports.
- Spread, slippage, commission, swap, gaps and conservative intrabar fill assumptions.
- No lookahead, repainting or survivorship leakage; point-in-time news/fundamental inputs.
- Walk-forward and untouched out-of-sample evaluation.
- Confidence intervals, expectancy, profit factor, drawdown, tail loss and calibration against a baseline.
- Per-symbol, timeframe, session, regime, setup and strategy-version attribution.
- Record rejected and expired recommendations for selection-bias analysis.
- No automatic strategy mutation from recent outcomes; candidate changes require reproducible validation and approval.

A win-rate threshold alone is not an acceptance criterion.

## 10. Reliability, security and observability

Required:
- fail-safe WAIT on stale feeds, scheduler failure, DB errors or invalid calculations;
- structured logs, audit events, correlation IDs and strategy/config hashes;
- owner-only authorization for private journal/account settings;
- secret management and rotation; no credentials in source or logs;
- database migrations, tested backup/restore and retention policy;
- health checks that distinguish service, DB, provider and scheduler health;
- duplicate-event/idempotency protection;
- no sensitive account values in public health responses;
- mobile-first dashboard with explicit manual-only status and no order controls.

## 11. Implementation sequence and acceptance gates

1. **Safety contract and inventory:** identify all broker/execution surfaces; prove no live order route exists. Rename simulation-only terminology where misleading.
2. **Manual journal lifecycle:** open/close/partial-close records, actual fill fields, provenance and audit trail.
3. **Risk correctness:** contract metadata, conversion, conservative volume steps, daily/weekly resets, hard-stop invariants and cost model.
4. **Data integrity:** per-timeframe freshness, cache fallback, closed candles and synchronized snapshots.
5. **Analysis evidence:** SMC/ICT, volatility, correlation, fundamentals/news point-in-time, uncertainty and explanations.
6. **Research integrity:** parity, no-lookahead, walk-forward, untouched holdout, calibration and costs.
7. **Operations/UI:** health, backup/restore, security and responsive manual workflow.

No phase is complete without focused unit tests, regression tests, static review, and successful CI on the exact candidate commit. A failed or unavailable check is not a pass. No merge to `main` is implied by this specification.

## 12. Repository inventory and current implementation evidence (2026-11-23)

Baseline `main` snapshot:
- Existing analysis, data fetch, news/fundamental, SMC/ICT, MAS, Backtest and Paper modules.
- Existing risk calculator and owner-protected legacy journal for closed trades.
- Existing Telegram commands for sizing and closed-trade logging.
- Existing simulated pending-order candle fill helper; it is not evidence of broker connectivity.
- Bounded source searches found no matches for `order_send`, `MetaTrader5`, `mt5.order`, `send_order`, or `execute_order`. This is not a proof against indirect/dynamic execution paths.

Implementation added on branch `feat/manual-only-intelligence-platform` (not yet merged):
- Dedicated `manual_positions` and append-only `manual_position_events` tables, separate from legacy closed-trade rows.
- Authenticated endpoints: `POST /api/manual-positions`, `GET /api/manual-positions`, `GET /api/manual-positions/{id}`, and `POST /api/manual-positions/{id}/close`.
- User-entered actual entry/initial stop/optional target/volume; partial and final exits; recorded exit costs and currency; idempotency keys; timestamp ordering checks; over-close prevention.
- Current SL/TP can be updated manually on open positions; each change is append-only and audited separately. Initial SL/TP remain immutable so realized R continues to use the original risk anchor.
- Position history is immutable through this API; no edit/delete route is provided for lifecycle events.
- Closed manual positions are represented in journal analytics using volume-weighted gross R. Recorded costs are disclosed separately and are **not** converted or subtracted from R because the required account-currency risk conversion is not yet implemented.
- Dashboard includes manual open/close forms and event history.
- Focused lifecycle tests cover open, partial/final close, over-close, duplicate requests, conflicting idempotency keys, timestamp order, cost currency, weighted-R aggregation, and audited SL/TP changes.

Not yet proven/complete:
- These commits were created through the GitHub contents API; the latest head has no associated CI check run in the observed GitHub response. The tests have therefore **not been confirmed passing** on this branch.
- Race-condition behavior under concurrent SL/TP updates, PostgreSQL integration, backup/restore, full account-currency net PnL, daily/weekly guard parity, and broader security/indirect-execution review still require validation.
- Risk/accounting, data freshness, cost modeling, calibration, and Paper/Backtest parity remain subject to separate implementation and evidence-based validation.

## 13. Definition of done

The product is accepted only when:
- execution boundary is enforced and tested as manual-only;
- all real trade facts are user-entered and source-labeled;
- no recommendation is mistaken for an actual fill;
- risk calculations are conservative and fail closed;
- market data and research are point-in-time and cost-aware;
- all tests pass on the exact commit in supported Python versions;
- backup/restore and auditability are demonstrated;
- every advertised capability is marked implemented and evidenced, or explicitly marked unavailable/planned.
