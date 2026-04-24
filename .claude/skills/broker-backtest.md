---
description: Backtest strategies against historical data — single-day Alpaca cache, S&P 500 daily, SPY intraday, or multi-ticker intraday
---

# Backtest Strategies Against Historical Data

Replay past market data through the strategy engine to see what trades would have been taken and how they would have performed. The project supports four backtest modes.

## Step 1: Choose a Mode

Ask the user what they want to validate:

| Mode | Dataset | When to use |
|------|---------|-------------|
| **Single-day** | Alpaca cache (parquet) | Debug a specific day of live trading |
| **--daily** | `backtest_data/individual_stocks_5yr/` | Broad S&P 500 daily-bar validation (2013-2018, 505 tickers) |
| **--spy** | `backtest_data/1_min_SPY_2008-2021/` | SPY 5-min intraday over 13 years (most proven, PF 1.17) |
| **--multi-intraday** | `data/cache/{TICKER}/*.parquet` | Multi-ticker 5-min (requires Alpaca downloader to populate cache first) |

## Step 2: Validate Dates

- Single-day: needs a past weekday with no market holiday
- `--daily`: dataset covers 2013-02-08 to 2018-02-07
- `--spy`: dataset covers 2008-01-22 to 2021-05-06
- `--multi-intraday`: whatever range was downloaded via `alpaca_downloader.py` (Alpaca IEX tier limits how far back you can fetch)

## Step 3: Run the Backtest

```bash
cd /Users/alex/Broker

# Single-day (requires ALPACA_API_KEY/SECRET_KEY in .env)
python -m trading_bot.backtest --date YYYY-MM-DD

# S&P 500 daily, all 4 strategies, with regime filter
python -m trading_bot.multi_strategy_backtest --from 2017-02-07 --to 2018-02-07 --daily

# SPY 5-min intraday, just the validated Mean Reversion strategy
python -m trading_bot.multi_strategy_backtest --from 2017-01-01 --to 2018-01-01 \
    --spy --strategies mean_reversion --cash 1000

# Multi-ticker intraday (requires cached 1-min data)
python -m trading_bot.multi_strategy_backtest --from 2020-07-27 --to 2020-12-31 \
    --multi-intraday --tickers SPY,QQQ,XLF,XLK --strategies mean_reversion
```

Common flags:
- `--strategies mean_reversion,sentiment_combo` — filter to specific strategies
- `--no-regime-filter` — disable market-regime filter (default: enabled)
- `--cash 4000` — override per-strategy allocation (default $1000)

If data is missing for `--multi-intraday`, run the downloader first:

```bash
python -m trading_bot.data.alpaca_downloader --from 2020-01-01 --to 2020-12-31 \
    --tickers SPY QQQ XLF XLK
```

## Step 4: Read Results

The backtester produces:
- A console report (strategy comparison table, best/worst trades)
- A JSON file in `backtest_results/multi_strategy_{from}_to_{to}_{ts}.json`
- A log file in `trading_bot/logs/multi_strategy_backtest.log`

Load the JSON to analyze exit reasons, R:R, drawdown, or per-trade details.

## Step 5: Present Results Summary

Report the comparison table and call out:

- **Best/worst strategy** by profit factor and max drawdown
- **Win rate and trade count** per strategy
- **Exit reason distribution** — how many stops vs take-profits vs max-hold-days
- **R:R** = avg win / avg loss (sanity check vs the target ratio)

## Step 6: Compare to Baseline

If the user is tuning, compare against the baseline validated metrics:

**Mean Reversion on SPY 5-min (2008-2021):**
- 292 trades, 66.4% win rate, +6.74%, PF 1.17, -6.3% max DD

Any tune that regresses these materially should be flagged.

## Step 7: Next Steps

Offer:
1. Another date range or mode
2. Parameter tune — edit `config.yaml` under `multi_strategy.strategies.*`
3. Paper-trade validation — the next step after backtest passes
