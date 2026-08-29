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
2. Add Railway PostgreSQL to the same project. Railway injects `DATABASE_URL` automatically.
3. Add variables:
   - `MODE=shadow`
   - `SERVICE_MODE=all`
   - `UNIVERSE_LIMIT=150`
   - `WORKER_SLEEP_SECONDS=60`
4. Deploy.
5. Open `/health` and verify:
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
