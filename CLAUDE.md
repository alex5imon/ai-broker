# CLAUDE.md

## Project Overview

Autonomous adaptive US equity trading bot built with Python 3.10+ and the Alpaca Trading API (`alpaca-py`). Runs as a stateless tick on a GitHub Actions cron (every 5 min during NYSE hours) against an Alpaca paper account, with state persisted in SQLite across runs. The same codebase runs locally for development, paper/live trading, and backtesting. Commission-free via Alpaca.

GitHub: https://github.com/alex5imon/ai-broker

**Exercise extreme care with all order logic.**

## Tech Stack

- **Python 3.10+** (minimum required version; GitHub Actions runs 3.12)
- **alpaca-py** - Alpaca Trading API client (REST)
- **pandas** - data manipulation and technical indicators
- **finnhub-python** - market news and sentiment data
- **sqlite3** - trade persistence, tick state, daily reporting
- **requests** - HTTP calls for external APIs
- **aiohttp** - async HTTP for FX rates
- **pyyaml** - YAML configuration parsing
- **ntfy.sh** - push notifications for trade alerts and system events
- **jinja2** - HTML report templating
- **pytest** - testing framework

## Project Location

The project lives at `~/Documents/Claude/Projects/Broker/`. All commands below assume `cd ~/Documents/Claude/Projects/Broker` first (or that you're already at the project root).

## Key Commands

```bash
# Run a single tick (what GitHub Actions runs every 5 min)
python -m trading_bot.main --mode normal

# Dry-run (no orders placed)
python -m trading_bot.main --mode normal --dry-run

# Run tests
pytest trading_bot/tests/

# Multi-strategy backtest — S&P 500 daily bars (505 tickers)
python -m trading_bot.multi_strategy_backtest --from 2017-02-07 --to 2018-02-07 --daily

# Multi-strategy backtest — SPY 5-min intraday (13 years)
python -m trading_bot.multi_strategy_backtest --from 2017-01-01 --to 2018-01-01 --spy

# Multi-strategy backtest — multi-ticker intraday (Alpaca cache)
python -m trading_bot.multi_strategy_backtest --from 2020-07-27 --to 2020-12-31 \
    --multi-intraday --tickers SPY,QQQ,XLF,XLK

# Filter to specific strategies
python -m trading_bot.multi_strategy_backtest ... --strategies mean_reversion,breakout

# Download Alpaca 1-min + daily bars to cache
python -m trading_bot.data.alpaca_downloader --from 2020-01-01 --to 2020-12-31 \
    --tickers SPY QQQ XLF XLK

# Local pre-flight + launch (manual run, not used by GHA)
bash start_bot.sh

# Daily self-improvement review — postmortem + rule-based proposals + backtest A/B
python -m trading_bot.self_improve \
    --window-days 20 \
    --bt-from 2026-02-20 --bt-to 2026-04-29 \
    --tickers SPY,QQQ,XLF,XLK,XLE,XLV,XLI,XLY,XLP,XLU,XLB,XLRE,XLC \
    --out self_improve_reports

# Same, but skip the backtest gate (postmortem-only smoke test)
python -m trading_bot.self_improve \
    --window-days 20 --bt-from 2026-04-01 --bt-to 2026-04-29 --dry-run
```

## Self-improvement agent

`trading_bot/self_improve/` is a research-only agent that runs after market
close (via `.github/workflows/daily-review.yml`, 21:30 UTC weekdays). It:

1. Reads recent closed trades from SQLite, computes per-strategy stats.
2. Runs rule-based hypotheses (each requires ≥ 20 trades of evidence and
   proposes a single, bounded ±10% parameter step).
3. Validates each proposal with an in-process A/B backtest. Pass requires
   no Sharpe drop > 0.10, no drawdown increase > 2pp, and ≥ 95% of
   baseline return.
4. Writes a markdown report to `self_improve_reports/YYYY-MM-DD.md` and
   opens a draft PR. **The agent never edits config.yaml** — patches in
   the report are advisory text the human applies by hand.

## Code Conventions

- **Type hints everywhere** - all function signatures and meaningful variables must be annotated.
- **Timezone-aware datetimes** - use `zoneinfo.ZoneInfo("US/Eastern")` for US markets. Never use pytz or naive datetimes.
- **Logging via stdlib `logging`** - no `print()` statements anywhere in the codebase.
- **SQLite for persistence** - all trade records, tick state, daily summaries, and parameter change history live in the database.

## Architecture

- Modular package under `trading_bot/` — each subsystem (strategy, execution, risk, reporting, etc.) is its own module.
- **US-only** — trades US equities via Alpaca (NYSE/NASDAQ), commission-free.
- **Stateless tick model** — each `python -m trading_bot.main` invocation runs one `tick()` and exits. Per-strategy state (day flags, spread-defer timers) is persisted in the `tick_state` / `risk_circuit_state` SQLite tables, so the next cron invocation picks up cleanly. No long-running process, no WebSocket stream, no heartbeat loop.
- **Alpaca integration:**
  - `TradingClient` (REST) for orders, positions, account queries
  - `StockHistoricalDataClient` for historical OHLCV bars
  - Order fill detection via polling
- **Phased growth** (account size → risk profile):
  - **Phase 1** — Conservative trading, 2 max positions
  - **Phase 2** — Expanded watchlist with swing holds, 4 max positions
  - **Phase 3** — Full adaptive strategy, 8 max positions

## GitHub Actions

- [.github/workflows/bot.yml](.github/workflows/bot.yml) runs every 5 min on a weekday UTC window covering NYSE 09:30-16:00 ET. Real trading-day / market-hour gating happens in code (`config.is_trading_day()` + Alpaca clock).
- [.github/workflows/heartbeat.yml](.github/workflows/heartbeat.yml) runs every 30 min and fails if the last successful bot run is older than 20 min — GitHub emails the repo admin on workflow failure.
- SQLite state (`trading_bot/data/trading_bot.db`) is persisted across runs via `actions/cache@v4` and uploaded as an artifact on every run, so logs, state, and the DB are inspectable after the fact.

### Required repo secrets

- `ALPACA_PAPER_KEY_ID`
- `ALPACA_PAPER_SECRET`
- `ALPACA_LIVE_KEY_ID`
- `ALPACA_LIVE_SECRET`

### Repo variable

- `ALPACA_ENV` — defaults to `paper`. Set to `live` to flip to live trading. The workflow selects the matching key pair automatically.

## Testing

```bash
pytest trading_bot/tests/
```

All tests must pass before any changes are deployed. Tests cover strategy logic, risk management, order execution, and reporting.

## Configuration

`config.yaml` is the single source of truth for all tunable parameters (stop loss, profit targets, watchlist, sentiment thresholds, position sizing, phase thresholds, swing parameters, etc.). Never hardcode values that belong in config.

## Backtest Datasets

Historical datasets for offline backtesting live outside the package:

- `backtest_data/individual_stocks_5yr/` — S&P 500 daily OHLCV CSVs (2013-2018, ~505 tickers). Loaded via `trading_bot/data/sp500_loader.py`.
- `backtest_data/1_min_SPY_2008-2021/` — SPY 1-minute bars (2008-2021). Loaded via `trading_bot/data/spy_intraday_loader.py`.
- `data/cache/{TICKER}/{DATE}_intraday.parquet` — Alpaca-downloaded 1-min bars. Populated by `trading_bot/data/alpaca_downloader.py`.

## Important

- **Commission-free** — Alpaca charges no commissions on US equities, but still double-check order logic, sizing, and risk limits.
- **Fractional shares supported** — Alpaca allows fractional down to 1/1000000. The backtester's `_size_by_risk()` accepts a `fractional=True` parameter (default true for intraday modes).
- **Cash account settlement** — T+1 for US equities. Track settled vs. unsettled cash to avoid free-riding violations.
- **Compound growth target** — 0.3-0.5% net per trading day. Conservative, consistent returns over aggressive plays.
- **Environment variables (GHA):** `ALPACA_PAPER_KEY_ID` / `ALPACA_PAPER_SECRET` and `ALPACA_LIVE_KEY_ID` / `ALPACA_LIVE_SECRET`, selected by `ALPACA_ENV` (defaults to `paper`). The internal code also reads `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` if set directly, so a local `.env` can use either pair.
- **Validated strategy** — Mean Reversion on SPY 5-min bars (2008-2021): 60.8% win rate, +76.09% total, -11.9% max DD, PF 1.54 across 102 trades (post let-winners-run + VIX-adaptive RSI). See `multi_strategy_backtest.py` and the `tune_history` memory for details.
