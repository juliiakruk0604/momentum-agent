# Momentum Research Agent v3.1

Research-only crypto momentum scanner and dataset builder.

## Safety state

- `MODE=shadow`
- Live order submission is disabled in code.
- No exchange API key is required for the first stage.
- Bybit OHLCV, open interest and funding endpoints are public market-data endpoints.

## Pipeline

1. Scan liquid Bybit USDT linear perpetuals.
2. Detect a 15m Early Impulse.
3. Wait until the signal candle is fully closed.
4. Observe the next 30 minutes using exact 5m candles.
5. Mark continuation as `CONFIRMED`, `STRONG` or `REJECTED`.
6. Add Bybit OI and funding context.
7. Persist every event in PostgreSQL.
8. Revisit the event after 1h / 3h / 6h / 12h / 24h and store MFE, MAE and +5/+10/+20/+30 labels before -4% invalidation.
9. Use `research_backtest.py` for historical point-in-time / rolling-OOS cohort comparison.

## Railway deployment

1. Deploy this GitHub repository as a Railway service.
2. Add Railway PostgreSQL to the same project.
3. In the application service, add a reference variable:
   - `DATABASE_URL=${{Postgres.DATABASE_URL}}`
   - If your database service has a different name, replace `Postgres` with that exact Railway service name.
4. Add application variables:
   - `MODE=shadow`
   - `SERVICE_MODE=all`
   - `UNIVERSE_LIMIT=150`
   - `WORKER_SLEEP_SECONDS=60`
5. Deploy or redeploy.
6. Generate a public domain for the application service and open `/health`.
7. Verify:
   - `status = ok`
   - `database = postgres`
   - a recent `worker` heartbeat appears after the first scan.

One container intentionally runs API + worker for the initial deployment. Later they can be split by setting `SERVICE_MODE=api` or `SERVICE_MODE=worker` in two services.

## Endpoints

- `GET /health` — database + worker status
- `GET /events?limit=100` — recent detected events
- `GET /stats` — event counts by state

## Historical research

```bash
python research_backtest.py --days 30 --max-symbols 100 --out results/research_30d_100
```

The research runner compares Impulse, Continuation, and Continuation + OI cohorts using rolling out-of-sample folds.

## Current research status

The exact 5m validation set is still too small to establish a tradable edge. Live execution remains disabled until historical + shadow + paper gates pass.


## v3.2 forward-shadow observability

The service now stores the raw Bybit derivatives snapshot with each finalized event and exposes live research cohorts.

- \`GET /research-status\` — Impulse / Confirmed / Strong / Confirmed+OI cohort sizes, 24h hit rates, MFE/MAE, invalidation rate, uplift and the research gate.
- \`GET /research-snapshots?limit=30\` — one longitudinal research snapshot per UTC day.
- \`GET /health\` — database state plus worker freshness, cycle errors and last worker error.
- \`WORKER_STALE_SECONDS=300\` — default watchdog threshold.

The forward-shadow gate remains closed until at least 100 completed 24h impulses, 30 confirmed continuations and 20 confirmed continuations with OI rising are collected.


## v3.2.1 runtime efficiency

The worker performs the expensive 150-symbol market scan only once per new 15m candle. Between market scans it still wakes every minute to process due continuation checks and future labels, and it updates the health heartbeat each loop.

- \`GET /candidates?limit=50\` — recent continuation-confirmed forward-shadow candidates with derivatives context.
- \`scan_performed\` in \`/health\` heartbeat shows whether the current loop included a full market scan.
- \`last_market_scan_symbols\` confirms the active universe size even on lightweight loops.


## v3.3 autonomous historical OOS backfill

The live shadow worker now also builds a separate historical OOS dataset incrementally, without a second Railway service.

Defaults:
- \`HISTORICAL_BACKFILL_ENABLED=true\`
- \`HISTORICAL_BACKFILL_DAYS=60\`
- \`HISTORICAL_HOLDOUT_DAYS=10\`
- \`HISTORICAL_BACKFILL_SYMBOLS_PER_SCAN=1\`

The first dataset intentionally ends 10 days before the current time. That keeps the late-August examples used to formulate v3.1 out of the initial autonomous validation window.

Each new 15m market bucket processes one historical contract:
1. point-in-time eligible Bybit USDT linear perpetual universe, including Closed/Settling when exposed by the venue;
2. historical 15m OHLCV;
3. exact 5m continuation only around detected impulses;
4. historical 15m open interest with publication lag;
5. historical funding;
6. 1h/3h/6h/12h/24h future labels;
7. rolling walk-forward test-fold assignment;
8. persistent PostgreSQL storage and resumable cursor.

Use \`GET /historical-status\` for progress and OOS cohort metrics. The historical research gate remains closed until it has at least 100 OOS impulses, 30 confirmed continuations, and 20 confirmed continuations with OI rising.
