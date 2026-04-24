# ai-broker

Autonomous adaptive US equity trading bot. Runs as a stateless tick on a
GitHub Actions cron (every 5 min during NYSE hours) and persists state
in SQLite across runs. Same codebase runs locally for paper/live and for
backtesting.

Targeting commission-free US equities via the Alpaca Trading API,
starting from a paper account with ~£950 GBP base, then graduating
through phases (micro → small → full) as equity and win-rate thresholds
are met.

See [SPEC.md](SPEC.md) for the full design and [CLAUDE.md](CLAUDE.md) for
agent-facing guidance.

## Layout

```
trading_bot/
  main.py                 tick entrypoint
  config.py               config loader (config.yaml)
  constants.py            phases, markets, enums
  gateway/                Alpaca REST client wrapper
  data/                   market data, FX, sentiment, earnings
  strategy/               entry/exit logic + strategy sleeves
  execution/              order manager, risk manager, sizing
  db/                     SQLite schema + migrations + repository
  reporting/              daily reports, performance metrics
  notifications/          ntfy.sh push notifications
  tests/                  pytest suite

.github/workflows/
  bot.yml                 5-min cron that runs `python -m trading_bot.main`
  heartbeat.yml           30-min watchdog that fails if bot is stale

config.yaml               single source of truth for all tunables
config_backtest.yaml      backtest-only overrides
scripts/                  dev tooling (smoke tests, evaluators)
backtest_data/            offline datasets (see CLAUDE.md)
```

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in ALPACA_API_KEY / ALPACA_SECRET_KEY

# One tick (what GHA runs)
python -m trading_bot.main --mode normal

# Dry-run (no orders placed)
python -m trading_bot.main --mode normal --dry-run

# Pre-flight checks + launch
bash start_bot.sh

# Tests
pytest trading_bot/tests/
```

## Backtesting

```bash
# Multi-strategy backtest — SPY 5-min intraday
python -m trading_bot.multi_strategy_backtest --from 2017-01-01 --to 2018-01-01 --spy

# Multi-ticker intraday (Alpaca cache)
python -m trading_bot.multi_strategy_backtest --from 2020-07-27 --to 2020-12-31 \
    --multi-intraday --tickers SPY,QQQ,XLF,XLK

# Download Alpaca bars to cache
python -m trading_bot.data.alpaca_downloader --from 2020-01-01 --to 2020-12-31 \
    --tickers SPY QQQ XLF XLK
```

See [CLAUDE.md](CLAUDE.md) for the full command surface.

## GitHub Actions

[.github/workflows/bot.yml](.github/workflows/bot.yml) runs every 5 min in
a weekday UTC window that covers NYSE 09:30-16:00 ET. Actual trading-day
and market-hour gating happens in code
([trading_bot/main.py](trading_bot/main.py) + Alpaca clock).

SQLite state (`trading_bot/data/trading_bot.db`) is persisted across runs
via `actions/cache@v4` and uploaded as an artifact on every run so logs,
state, and the DB are inspectable after the fact.

### Required repo secrets

- `ALPACA_PAPER_KEY_ID`
- `ALPACA_PAPER_SECRET`
- `ALPACA_LIVE_KEY_ID`
- `ALPACA_LIVE_SECRET`

### Repo variable

- `ALPACA_ENV` — defaults to `paper`. Set to `live` to flip to live
  trading. The workflow selects the matching key pair automatically.

### Pulling artifacts back locally

```bash
gh run list --workflow=bot.yml --limit 20
gh run download <run-id> --name bot-logs-<run-id> --dir ./logs
gh run download <run-id> --name bot-db-<run-id> --dir ./trading_bot/data
```

## Tick model

Each invocation runs one `tick()` and exits:

1. Trading-day + operating-hours gate.
2. Connect to Alpaca (validate creds).
3. Refresh FX rate (GBP/USD).
4. Reconcile broker state with SQLite.
5. Poll outstanding order statuses.
6. Pre-market scan / entry scan / exit check / wind-down —
   whichever windows are active, gated by day-scoped flags.
7. Phase-transition + daily-summary checks once per day.

Per-strategy state (day flags, spread-defer timers, strategy sleeves)
lives in the `tick_state` / `risk_circuit_state` SQLite tables so the
next cron invocation picks up cleanly. There is no long-running
process, no WebSocket stream, and no heartbeat loop.

## Heartbeat

[.github/workflows/heartbeat.yml](.github/workflows/heartbeat.yml) runs
every 30 min during NYSE hours and fails if the last successful `bot`
run is older than 20 min. GitHub emails the repo admin on workflow
failure. Adjust `STALE_MINUTES` in the workflow to taste.
