# Running the Solana radar

The original localhost research workbench remains `python app.py`. The new
read-only service is separate and never exposes token launch controls.

```sh
python -m pip install -r requirements-radar.txt
python -m radar.worker --once
python -m radar.server --with-worker
```

Open `http://127.0.0.1:8790`. For split processes, run `python -m radar.worker`
and `python -m radar.server`. Worker cycles run every 30 seconds, with
age-dependent market refresh and a five-minute frozen ranking cohort. Provider
budgets and cooldowns can delay collection. One-worker locks prevent overlap.

## Environment

| Variable | Purpose |
|---|---|
| RADAR_DATABASE_URL | PostgreSQL DSN in hosted deployments; SQLite path locally |
| RADAR_USERNAME / RADAR_PASSWORD | Private dashboard sign-in behind hosting TLS; password >=20 characters; explicit Basic API headers supported |
| PORT | Host-provided listener port (local default 8790) |
| RADAR_MARKETS_PER_CYCLE | Bounded mint enrichment budget, default 15 |
| RADAR_ONCHAIN_ENABLED / RADAR_RPC_URL | Optional finalized RPC checks; RPC URL remains server-side |
| GEM_CAPTURE_DATABASE | Optional original Spider SQLite database; captures require browser activity |
| GROK_ENABLED / XAI_API_KEY / GROK_MODEL / GROK_DAILY_CALLS | Existing optional four-reviewer settings; keys remain server-side |
| TELEGRAM_ALERTS_ENABLED / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID | Optional configured research alerts; disabled by default |

Deploy the Dockerfile on a persistent container host with a durable PostgreSQL
database. Set secrets in the host dashboard, never a Git file. TLS termination
is mandatory for hosted authentication. Do not deploy `app.py` publicly.
`/healthz` tests database liveness without exposing evidence. Authenticated
`/api/health` checks a completed worker cycle and recent healthy provider.

Free-host trials do not establish permanent 24/7 availability. Check credit
limits, egress, database expiry and idle suspension. Never use fake traffic to
evade a host's free-service restrictions. Account/payment requirements can block
deployment independently of application readiness.

Railway trial deployment (2026-10-08): repository `avobati/gem-search`, branch
`feat/solana-discovery`, Dockerfile builder/path `Dockerfile`, one replica,
serverless disabled, health path `/healthz`, On Failure restart (ten retries).
`RADAR_DATABASE_URL` references `${{Postgres.DATABASE_URL}}`; the Postgres service
has its own persistent volume. Domain target is 8080, matching the injected PORT.
New services cannot enable legacy `railway.json` config-as-code; these settings
were applied through the dashboard. The checked-in file is a legacy example.
See [Railway IaC documentation](https://docs.railway.com/infrastructure-as-code).

Sign in at `/login`. Passwords remain in the host environment; opaque sessions
last eight hours, use HttpOnly/SameSite=Strict/Secure cookies on public domains,
and expire when the single replica restarts. Login challenges expire after ten
minutes and are single-use; POST login/logout require a same-origin request.
Thirty failed sign-ins in five minutes temporarily stop sign-ins. Login cookies
do not carry credentials. Basic Authorization remains available to API clients.

## Logical evidence backups

Run exports in the host container or a trusted environment with the database
connection set through `RADAR_DATABASE_URL`:

```sh
python -m radar.backup export data/backups/radar-2026-10-08.jsonl
python -m radar.backup validate data/backups/radar-2026-10-08.jsonl
python -m radar.backup restore data/backups/radar-2026-10-08.jsonl --database data/restored.sqlite
```

Exports read one consistent database snapshot and stream rows rather than load
all raw research into memory. The final manifest verifies SHA-256 and entity
counts. Exports refuse to overwrite a file. Restore requires an empty evidence
database, locks it for the import transaction, and preserves record IDs and
timestamps. Operational caches and credentials are excluded; only the latest
ranking pointer is reconstructed. Evidence remains append-only after restore.
Keep backups on independent private storage: a file inside an ephemeral service
container is not a durable off-host backup. Scheduled off-host backups and a
real PostgreSQL recovery drill remain operational acceptance work.

## Methodology and limitations

Default weights: momentum 25, liquidity 15, wallets 15, distribution 10,
narrative 10, project 10, market structure 10, risk 5. Unknown components earn
no points and reduce evidence coverage; known components are not renormalized.
Weights are accepted by the pure scoring function and versioned in each
decision. Production weights require a separately reviewed change.

Momentum measures sampled same-provider/same-pool changes and acceleration of
rolling hourly volume, not transaction-derived disjoint interval flow. The
dashboard displays pool age; true token creation time remains unknown. Multiple
provider prices and overlapping liquidity are never summed. FDV is distinct
from market cap. Holders/unique buyers are unknown unless supplied explicitly.

Risk points: active mint authority 25; active freeze authority 35; reported top
ten account concentration >=70% adds 35, >=40% adds 20; liquidity <$10K adds
40, <$50K adds 15; provider danger adds 20; liquid-market price disagreement
>30% adds 20. Rugged flag forces 100. Levels: <30 LOW, <60 MEDIUM, <90 HIGH,
otherwise EXTREME. Missing critical checks yield UNKNOWN and hold. Active
freeze authority, provider danger, <$10K liquidity or score >=60 reject.
Pool accounts can inflate concentration; these conservative holds need future
owner attribution. None of these checks certifies sellability or safety.

Candidates require risk eligibility and Gem >=50. Smart wallet classifications
are deliberately unavailable until historical realized wallet evidence exists.
Horizon outputs use different research recipes and are never labeled calibrated
probabilities. Four AI reviewers cannot change the risk gate.

Each five-minute cohort freezes the observed universe, evidence, feature values,
score version and six baseline ranks. Forward labels cover 1h/3h/6h/12h/24h/
48h/3d/7d, using the first same-pool/provider observation within a bounded
post-target tolerance. Missing outcomes remain missing. Max/min returns and
drawdown are sampled and may miss intrainterval extremes. Reports disclose
outcome coverage. Repeated token/cohort observations are correlated; headline
metrics are descriptive and cannot establish significance.
Reports separately count settled missing exits and outcomes whose horizons have
not matured. A conservative sensitivity scenario assigns -100% only to settled
missing exits; it is an assumption rather than a measured loss or executable
return. Unmatured outcomes are not classified as failures.

Backtest, out-of-sample and forward results begin as unavailable/collecting;
there is no preexisting token history. The validation module defines
walk-forward periods with a seven-day embargo for seven-day labels. Parameter
selection uses mature chronological training/validation periods and freezes
weights before test evaluation. Repeated recommendations are deduplicated by
mint within each fold. No mature folds exist yet. Probability calibration
remains future research. Daily reports never change production weights.

## Acceptance status

Optional independent RPC evidence (`RADAR_ONCHAIN_ENABLED=1`) reads finalized
mint authority, freeze authority, token program/extensions and the twenty largest
token accounts. It uses two calls per mint, at most two mints per ranking cycle,
with a thirty-minute cache and five-minute outage cooldown. Slots and availability
timestamps are frozen with decisions; calls are not an atomic snapshot. RPC keys
and endpoint URLs are never included in browser evidence. Largest-account amounts
are divided by raw integer mint supply; pool attribution and total holder count
remain unknown. When concentration sources disagree, the larger observed top-ten
percentage is used conservatively. Any fresh active freeze authority rejects.

Unreviewed Token-2022 transfer semantics hold eligibility. Observed transfer hooks,
nontransferable tokens, permanent delegates or confidential-transfer mints add
60 risk points and reject. Other extensions remain a hold rather than receiving
a safety assertion. This does not simulate a sell transaction. Missing independent
RPC evidence does not manufacture observations or replace unavailable RugCheck
fields. Rule version is `gem-v1.1-onchain`.

RPC reference: [getAccountInfo](https://solana.com/docs/rpc/http/getaccountinfo),
[largest token accounts](https://solana.com/docs/rpc/http/gettokenlargestaccounts),
[extensions](https://solana.com/docs/tokens/extensions).

Implemented: public multi-source discovery, historical observations, deterministic
risk gate, explainable scores, immutable ranked cohorts, responsive research
dossiers, crawler/capture/JEV integration, optional Grok adapter, forward labels,
baseline reports and health endpoints. Live provider verification and deployment
status are recorded separately in the validation report.

Not established: profitable predictive performance, calibrated models, global
Solana recall, transaction-level wallet intelligence, creator clusters, complete
LP/sellability checks, licensed unattended social collection,
historical trained walk-forward results. These gaps are acceptance work, not
features represented by invented data.
