# CLAUDE.md

## Project Overview

Autonomous adaptive US equity trading bot built with Python 3.10+ and the Alpaca Trading API (`alpaca-py`). Uses an Alpaca paper account with GBP base currency for reporting, starting capital ~£950. The bot follows a phased growth strategy from micro-cap positions through to diversified trading. Commission-free via Alpaca.

**Exercise extreme care with all order logic.**

## Tech Stack

- **Python 3.10+** (minimum required version)
- **alpaca-py** - Alpaca Trading API client (REST + WebSocket)
- **pandas** - data manipulation and technical indicators
- **finnhub-python** - market news and sentiment data
- **sqlite3** - trade persistence and daily reporting
- **requests** - HTTP calls for external APIs
- **aiohttp** - async HTTP for real-time data feeds and FX rates
- **pyyaml** - YAML configuration parsing
- **ntfy.sh** - push notifications for trade alerts and system events
- **jinja2** - HTML report templating
- **pytest** - testing framework

## Project Location

The project lives at `~/Broker/` (previously `/Users/alex/Documents/Claude/Projects/Broker/`, moved on 2026-04-17 for permission reasons). All commands below assume `cd ~/Broker` first.

## Key Commands

```bash
cd ~/Broker

# Run the bot
python -m trading_bot.main

# Run tests
pytest trading_bot/tests/

# Backtest — single-day Alpaca cache (legacy)
python -m trading_bot.backtest --date 2026-04-15

# Multi-strategy backtest — S&P 500 daily bars (505 tickers)
python -m trading_bot.multi_strategy_backtest --from 2017-02-07 --to 2018-02-07 --daily

# Multi-strategy backtest — SPY 5-min intraday (13 years)
python -m trading_bot.multi_strategy_backtest --from 2017-01-01 --to 2018-01-01 --spy

# Multi-strategy backtest — multi-ticker intraday (Alpaca cache)
python -m trading_bot.multi_strategy_backtest --from 2020-07-27 --to 2020-12-31 \
    --multi-intraday --tickers SPY,QQQ,XLF,XLK

# Filter to specific strategies
python -m trading_bot.multi_strategy_backtest ... --strategies mean_reversion,sentiment_combo

# Download Alpaca 1-min + daily bars to cache
python -m trading_bot.data.alpaca_downloader --from 2020-01-01 --to 2020-12-31 \
    --tickers SPY QQQ XLF XLK

# Production start (includes pre-flight checks)
bash start_bot.sh
```

## Code Conventions

- **Type hints everywhere** - all function signatures and meaningful variables must be annotated.
- **Timezone-aware datetimes** - use `zoneinfo.ZoneInfo("US/Eastern")` for US markets. Never use pytz or naive datetimes.
- **Logging via stdlib `logging`** - no `print()` statements anywhere in the codebase.
- **SQLite for persistence** - all trade records, daily summaries, and parameter change history live in the database.
- **No `print()` statements** - use `logger.info()`, `logger.debug()`, etc.
- **FX-aware P&L** - all P&L must be converted to GBP for reporting. USD positions require GBP/USD conversion at the time of calculation. Never mix currencies in aggregations.

## Architecture

- Modular package under `trading_bot/` - each subsystem (strategy, execution, risk, reporting, FX, etc.) is its own module.
- **US-only** - trades US equities via Alpaca (NYSE/NASDAQ), commission-free.
- **Alpaca integration:**
  - `TradingClient` (REST) for orders, positions, account queries
  - `StockDataStream` (WebSocket) for real-time quotes/trades
  - `StockHistoricalDataClient` for historical OHLCV bars
  - Order fill detection via polling (5s interval)
  - FX rate (GBP/USD) via external API (`open.er-api.com`)
- **Phased growth strategy:**
  - **Phase 0** - Assess and clean up existing positions
  - **Phase 1** - Conservative trading, building track record (~£950-£1,500)
  - **Phase 2** - Expanded watchlist with swing holds (~£1,500-£3,000)
  - **Phase 3** - Full adaptive strategy (~£3,000+)

## Testing

```bash
pytest trading_bot/tests/
```

All tests must pass before any changes are deployed. Tests cover strategy logic, risk management, order execution, FX conversion, and reporting.

## Configuration

`config.yaml` is the single source of truth for all tunable parameters (stop loss, profit targets, watchlist, sentiment thresholds, position sizing, phase thresholds, swing parameters, FX settings, etc.). Never hardcode values that belong in config.

## Backtest Datasets

Historical datasets for offline backtesting live outside the package:

- `backtest_data/individual_stocks_5yr/` — S&P 500 daily OHLCV CSVs (2013-2018, ~505 tickers). Loaded via `trading_bot/data/sp500_loader.py`.
- `backtest_data/1_min_SPY_2008-2021/` — SPY 1-minute bars (2008-2021). Loaded via `trading_bot/data/spy_intraday_loader.py`.
- `data/cache/{TICKER}/{DATE}_intraday.parquet` — Alpaca-downloaded 1-min bars. Populated by `trading_bot/data/alpaca_downloader.py`.

## Important

- **Commission-free** - Alpaca charges no commissions on US equities, but still double-check order logic, sizing, and risk limits.
- **Fractional shares supported** - Alpaca allows fractional down to 1/1000000. The backtester's `_size_by_risk()` accepts a `fractional=True` parameter (default true for intraday modes).
- **Cash account settlement** - T+1 for US equities. Track settled vs. unsettled cash to avoid free-riding violations.
- **GBP base currency** - all reporting, risk calculations, and position sizing are denominated in GBP.
- **Compound growth target** - 0.3-0.5% net per trading day. Conservative, consistent returns over aggressive plays.
- **Environment variables** - `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` must be set (via `.env` or shell).
- **Validated strategy** - Mean Reversion on SPY 5-min bars (2008-2021): 66.4% win rate, +6.74% total, -6.3% max DD, PF 1.17 across 292 trades. See `multi_strategy_backtest.py` and the `tune_history` memory for details.
