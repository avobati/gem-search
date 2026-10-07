# Validation status — 2026-10-07

This is a tested discovery/research foundation. The complete production mission
has **not** met all acceptance criteria.

## Observed validation

- 55 Python tests pass (existing workbench plus radar).
- 18 JavaScript tests pass; radar JavaScript syntax check passes.
- Live first two collection cycles discovered 59 distinct mints: 36 first seen
  through GeckoTerminal new pools and 23 through DexScreener profiles.
- 97 provider market observations persisted; 31 initial frozen score decisions.
  These counts describe the initial probe, not predictive performance.
- RugCheck returned live authority/concentration/risk evidence. GeckoTerminal
  also returned HTTP 429; cooldowns and explicit provider gaps were observed.
- Dashboard, token dossier and baseline report load in a real browser.
  Mobile tested at 390×844: no page overflow; ranked table scrolls horizontally.
- Local HTTP authentication, read-only behavior and secret boundaries tested.
  Database triggers reject evidence mutation. Future-feature/capture exclusion,
  missing prices, liquidity removal, duplicate pools, provider disagreement,
  outage fallback and forward label idempotence are covered.
- PostgreSQL adapter and container build are supplied but not exercised on a
  running PostgreSQL/container host in this environment (Docker unavailable).
- Production URL/health checks: **not available**. Railway browser account has
  a limited $5 / 30-day trial and no connected repository. Browser GitHub account
  `avobatistuta-ui` differs from repository/CLI account `avobati`. No GitHub App
  installation or paid upgrade was authorized/performed.

## Measured performance

Backtest: unavailable; no historical point-in-time token dataset supplied.
Out-of-sample: unavailable; no mature independent folds yet.
Forward testing: collecting; no mature return claims in this initial probe.
Hit rate, median return and winner recall: **unknown**, not zero.

`python -m radar.evaluate` exports current baseline metrics and walk-forward
experiments. Folds require chronological train/validation/test periods with
seven-day embargoes. Weight alternatives are predeclared, selected on validation,
and frozen before test evaluation. No experiment changes production weights.

## Remaining acceptance work

1. Connect the correct hosting/GitHub identity, provision durable PostgreSQL,
   build the container, deploy and pass live authenticated health checks.
2. Confirm ongoing free-resource capacity; trial credit does not prove perpetual
   24/7 operation. Validate backups, workload, database sizes and provider budgets.
3. Add transaction-level wallet/holder history and creator attribution. Current
   smart-wallet score, whale accumulation and holder velocity are unknown.
4. Add launch/migration subscriptions for Pump, Raydium and Meteora. Current
   discovery is aggregator pool/profile coverage, not a complete on-chain stream.
5. Connect a licensed unattended social source. Existing captures/JEV integration
   works where captures are available, but browser collection requires a browser.
6. Validate sell restrictions, Token-2022 extensions, LP owner/lock conditions,
   creator history and suspicious transfer clusters with independent on-chain data.
7. Accumulate mature forward cohorts; measure baselines, missing-exit sensitivity,
   unique-token cohort metrics, drawdown and observed-universe recall.
8. Calibrate horizon models and assess walk-forward performance before any
   predictive confidence or trading claim.

![Initial desktop live probe](verification/radar-desktop.jpg)
![Mobile live probe](verification/radar-mobile.jpg)
