# Adaptive US Equity Trading Bot - Specification v5

**Version**: 5 (multi-strategy, Alpaca)
**Date**: 2026-04-20
**Status**: Active
**Account**: Alpaca Paper Account, GBP base currency (for reporting)

---

## Section 1: Project Overview & Account Context

### What This Bot Does

This is an **autonomous, adaptive US equity trading bot** that connects to the Alpaca Trading API and runs continuously on a local MacBook during market hours. It trades equities on **US exchanges (NYSE/NASDAQ)** commission-free, adapting its strategy as the account grows through defined phases.

This is **not a scalping bot** at the current account size. Scalping is unviable at ~£950 because:
- T+1 cash settlement prevents rapid fund recycling
- Small position sizes limit profit potential per trade

Instead, the bot starts as a **swing/position trader** and automatically evolves toward more active trading as the account grows through three defined phases.

### Account Details

| Field | Value |
|---|---|
| Broker | Alpaca (migrated from Interactive Brokers on 2026-04-17) |
| Account Type | Paper trading (cash, no margin, no PDT rule, T+1 settlement) |
| Base Currency | GBP (for reporting); trades execute in USD |
| Target Funding | ~$1,000 USD (fresh account, no inherited positions) |
| Fractional Shares | Supported (down to 1/1000000) |

**Current Positions**: None — fresh Alpaca account.

(The old IB account previously held OTC/penny positions — DFTX, TLOFF, BLOZF, QMCI — these are NOT in the Alpaca account.)

### Goal

Steady daily account growth targeting **0.3-0.5% per day** (~£3-5/day initially). This sounds modest but compounds to approximately 100% annually. The priority is consistency and capital preservation over aggressive returns.

### Why a Phased Approach

The account size dictates what strategies are viable:

- **£950**: Small position sizes limit profit potential. Swing trading with wide stops and targets is the only path.
- **£5,000**: Position sizes support more concurrent holdings, shorter holds become viable.
- **£20,000+**: Full day-trading/scalping becomes viable. Position sizes support tight stops and rapid turnover.

The bot automatically detects account growth and transitions between phases, adjusting every parameter: position count, stop distances, hold times, watchlist size, and trade frequency.

### US-Only Rationale

The bot trades exclusively on US exchanges (NYSE/NASDAQ) via Alpaca:
- Commission-free trading eliminates commission drag entirely
- Deep liquidity and tight spreads on US equities
- Single market simplifies scheduling and execution logic
- Account is GBP-denominated; all USD P&L is converted to GBP for reporting

---

## Section 2: Core Infrastructure

### Alpaca API Connection

- **Library**: `alpaca-py` (official Alpaca Python SDK)
- **API**: REST API via `TradingClient` for orders, positions, and account queries
- **Market Data**: `StockDataStream` (WebSocket) for real-time quotes/trades; `StockHistoricalDataClient` for historical OHLCV bars
- **Authentication**: API keys via environment variables `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`
- **Mode**: Paper trading account

### Connection Monitoring

- **Heartbeat interval**: 30 seconds
- **Heartbeat method**: Call `client.get_account()` and verify response within 5 seconds
- **On timeout**: Log WARNING, create a new `TradingClient` instance and retry
- **On reconnect failure**: Wait 30 seconds, retry up to 5 times with exponential backoff (30s, 60s, 120s, 240s, 480s)
- **On persistent failure** (5 consecutive reconnect failures):
  - Send CRITICAL ntfy alert: "API connection lost - 5 reconnect attempts failed"
  - Enter connection-wait mode: retry every 5 minutes indefinitely
  - Do NOT exit the process (API may come back after transient outage)
  - If positions are open when connection drops, they retain their Alpaca server-side stop orders (stop orders survive client disconnection)

### State Recovery on Startup

On every startup (including crash recovery), the bot MUST:

1. **Query Alpaca positions**: Call `client.get_all_positions()` to get all current open positions
2. **Query Alpaca orders**: Call `client.get_orders()` to get all pending/active orders
3. **Query Alpaca account**: Call `client.get_account()` for cash, equity, buying power
4. **Load SQLite state**: Read all positions with status != CLOSED from the `positions` table
5. **Reconcile**:
   - For each Alpaca position, check if a matching SQLite record exists
   - If Alpaca has a position not in SQLite: create a record, log as WARNING, send ntfy alert
   - If SQLite has a position not in Alpaca: mark as CLOSED with exit_reason "reconciliation_mismatch", log as WARNING
   - If quantities differ: update SQLite to match Alpaca (Alpaca is the source of truth), log discrepancy
6. **Verify stop orders**: For each reconciled position, verify that Alpaca has active stop-loss orders. If not, place them immediately.
   - **Corporate actions (splits, dividends)**: Alpaca adjusts positions automatically. On startup, the state recovery module may detect quantity/price discrepancies if a split occurred since last run. Log these as INFO (not CRITICAL) if the notional value is approximately unchanged.
7. **Settlement check**: Query `settlements` table for pending settlements, update any that should have settled based on date
8. **Log result**: Log full reconciliation summary. If any discrepancies found, send ntfy alert with details.
9. **Resume**: Only begin normal operation after reconciliation is complete and clean

### Market Data Staleness Detection

The bot subscribes to real-time market data for all watchlist symbols during their respective market hours.

- **Staleness threshold**: 30 seconds with no tick update for a subscribed symbol during that symbol's market hours
- **On stale detection**:
  1. Log WARNING: "Market data stale for {ticker}"
  2. Unsubscribe and re-subscribe via the Alpaca `StockDataStream` WebSocket
  3. Wait 15 seconds for new tick
  4. If still no data: exclude symbol from trading, log as ERROR
  5. Retry re-subscribe every 60 seconds
- **Mass staleness threshold**: If >50% of currently-subscribed symbols go stale simultaneously:
  1. Pause all new entries immediately
  2. Send CRITICAL ntfy alert: "Market data stale for >50% of watchlist - possible API issue"
  3. Continue managing existing positions (Alpaca server-side stop orders remain active)
  4. Resume normal trading when stale count drops below 25% of watchlist
- **Outside market hours**: Staleness detection is disabled; no ticks are expected

### Settlement Tracking (T+1)

Cash accounts enforce T+1 settlement for equity trades. After selling a position, the proceeds are not available for trading until the next business day.

- **On every sell execution**: Record a settlement entry with `sell_date` and `settle_date` (next business day)
- **Available cash calculation**: `settled_cash = total_cash - sum(unsettled_amounts)`
- **Before any entry**: Verify `settled_cash >= planned_position_value`
- **Settlement date calculation**: Next business day, accounting for weekends and US holidays
- **Alpaca enforcement**: Alpaca will reject orders exceeding buying power, but the bot must pre-check to avoid rejection logs and wasted processing
- **Good-faith violation prevention**: Never enter a position funded by unsettled proceeds. Track this explicitly.

### FX Handling

The account base currency is GBP, but the bot trades USD-denominated stocks.

- **FX rate source**: Query external API (`open.er-api.com`) for live GBP/USD rate via `aiohttp`
- **Rate caching**: Cache the FX rate, refresh every 60 seconds during market hours
- **P&L conversion**: All P&L is tracked in both the trade currency (USD) and GBP. The `trades` table stores both `net_pnl` (in USD) and `pnl_gbp` (converted) along with the `fx_rate` used.
- **Position sizing**: When sizing a USD position, convert available GBP cash to USD at the current rate, then size in USD
- **FX risk**: Not hedged at this account size. FX exposure is logged in reports for awareness.
- **Fallback**: If live FX rate is unavailable, use the last cached rate. If no cached rate exists, use a hardcoded fallback of 1.27 (approximate GBP/USD) and log a WARNING.

### Health Check HTTP Endpoint

A lightweight HTTP server running on port 8080 provides a JSON status endpoint for external monitoring.

- **Endpoint**: `GET http://localhost:8080/health`
- **Response** (JSON):
```json
{
  "status": "ok",
  "timestamp": "2026-04-16T15:30:00-04:00",
  "api_connected": true,
  "phase": 1,
  "account_equity_gbp": 951.93,
  "open_positions": 1,
  "daily_pnl_gbp": 3.42,
  "daily_trades": 2,
  "settled_cash_gbp": 108.90,
  "stale_symbols": [],
  "last_heartbeat": "2026-04-16T15:29:45-04:00",
  "uptime_seconds": 28800,
  "current_session": "US",
  "trading_active": true
}
```
- **Implementation**: Use `aiohttp` to serve within the bot's async event loop
- **Error response**: If API is unreachable, return `{"status": "degraded", ...}` with HTTP 200 (still reachable) but `api_connected: false`

---

## Section 3: Phase 0 - Portfolio Cleanup

Phase 0 runs on the **first startup** (or whenever `phase0_complete` is not set in the database). This is the autonomous initial assessment of existing positions. The bot evaluates every position against quality criteria and liquidates those that do not meet standards.

### Assessment Process

For each position held in Alpaca, the bot computes a quality score (0-100) based on these weighted factors:

| Factor | Weight | Scoring Method |
|---|---|---|
| Liquidity | 25 | Average daily volume. >1M shares = 25, >100K = 15, >10K = 8, <10K = 0 |
| Market Cap | 20 | >$10B = 20, >$1B = 15, >$500M = 10, >$100M = 5, <$100M = 0 |
| Exchange Quality | 15 | NYSE/NASDAQ = 15, OTC = 0 |
| Technical Health | 15 | Price vs 50-day SMA: above = 10, within 5% below = 5, far below = 0. RSI 30-70 = 5, outside = 0 |
| Sentiment | 10 | Finnhub news sentiment > 0.1 = 10, neutral = 5, negative = 0 |
| Loss Magnitude | 15 | Unrealized: profit or <-5% = 15, -5% to -15% = 10, -15% to -30% = 5, >-30% = 0 |

**Note**: Existing positions get a relaxed market cap threshold ($100M instead of $500M) since the cost of selling at a loss must be weighed against the position's potential.

### Score Classification

- **Score > 60: HOLD** - Position meets minimum quality criteria
  - Place a trailing stop at -5% from current price
  - Monitor daily; re-score weekly
  - Add to the active watchlist for exit management

- **Score 30-60: SELL** - Position does not meet criteria but is not urgent
  - Liquidate within 3 trading days
  - Place limit sell at mid-price (midpoint of bid-ask)
  - If unfilled after 2 hours, adjust limit toward bid by 25% of the spread
  - If unfilled after 4 hours, adjust to bid price
  - If unfilled after 1 full trading day, place at bid - 1 tick
  - Never use market orders on illiquid stocks

- **Score < 30: URGENT SELL** - High risk, liquidate quickly
  - Liquidate within 1 trading day
  - Start with limit at mid-price
  - Adjust toward bid every 30 minutes
  - After 2 hours: place at bid price
  - After 4 hours: if still unfilled and position value < $50, consider market order (the cost of monitoring exceeds the slippage risk)

### Phase 0 — Fresh Alpaca Account

**Phase 0 is not applicable to the current Alpaca account** — it was funded fresh with no inherited positions. The scoring logic below was designed for the old IB account which held OTC/penny positions (DFTX, TLOFF, BLOZF, QMCI). The logic is preserved for reference and future cleanup scenarios, but runs a no-op on an empty portfolio.

### Expected Assessment (Historical IB Portfolio — for reference only)

Based on the old IB holdings that were liquidated before the Alpaca migration:

**DFTX (Definium Therapeutics) - Expected Score: ~20 (URGENT SELL)**
- Liquidity: 0/25 (micro-cap, very low volume)
- Market Cap: 0/20 (<$100M)
- Exchange: 0/15 (OTC)
- Technical: variable/15
- Sentiment: variable/10 (likely no data)
- Loss: variable/15
- Action: URGENT SELL. Micro-cap biotech on OTC with poor liquidity.

**TLOFF (Talon Metals) - Expected Score: ~15 (URGENT SELL)**
- Liquidity: 0/25 (OTC, very low volume)
- Market Cap: 0-5/20 (junior mining)
- Exchange: 0/15 (OTC)
- Technical: variable/15
- Sentiment: variable/10
- Loss: variable/15
- Action: URGENT SELL. Junior mining on OTC.

**BLOZF (Cannabix Technologies) - Expected Score: ~5 (URGENT SELL)**
- Liquidity: 0/25 (penny stock)
- Market Cap: 0/20 (<$100M)
- Exchange: 0/15 (OTC)
- Technical: 0/15 (penny stock price)
- Sentiment: 0/10 (cannabis sector, likely no Finnhub data)
- Loss: 0/15 (deep loss expected)
- Action: URGENT SELL. Penny stock with near-zero value.

**QMCI (QuoteMedia) - Expected Score: ~5 (URGENT SELL)**
- Liquidity: 0/25 (penny stock)
- Market Cap: 0/20 (<$100M)
- Exchange: 0/15 (OTC)
- Technical: 0/15 (penny stock price)
- Sentiment: 0/10 (likely no data)
- Loss: 0/15 (deep loss expected)
- Action: URGENT SELL. Penny stock at $0.15, essentially worthless for active trading.

### Execution Protocol

1. **Score all positions** and log the full assessment with scores, reasoning, and classifications
2. **Send ntfy notification** with the complete cleanup plan BEFORE executing any trades:
   ```
   PORTFOLIO CLEANUP PLAN
   URGENT SELL: DFTX (score 20) - limit $22.00
   URGENT SELL: TLOFF (score 15) - limit $6.40
   URGENT SELL: BLOZF (score 5) - limit $0.53
   URGENT SELL: QMCI (score 5) - limit $0.15
   Executing in 5 minutes...
   ```
3. **Wait 5 minutes** after notification (allows user to send kill switch if they disagree)
4. **Execute sells** in order of urgency (URGENT SELL first, then SELL)
5. **Place trailing stop** on HOLD positions
6. **Log all results** to SQLite and send completion notification
7. **Mark phase 0 complete** in the database

### Phase 0 Completion

Phase 0 is complete when:
- All SELL/URGENT SELL positions have been fully liquidated OR have had limit orders active for 3+ trading days
- All HOLD positions have trailing stops in place
- The `phase_transitions` table has a record of Phase 0 completion
- Freed cash from liquidations becomes available for Phase 1 trading after T+1 settlement

---

## Section 4: Runtime Schedule

### Timezone Convention

All internal times are stored and processed in **US/Eastern** timezone using `zoneinfo.ZoneInfo("US/Eastern")`. This handles EST/EDT transitions automatically. Never use `pytz` or naive datetimes.

### US Session (NYSE/NASDAQ)

| Phase | ET Time | Activity |
|---|---|---|
| Pre-market scan | 09:15 - 09:30 | Scan watchlist, pull sentiment, compute technicals |
| Opening blackout | 09:30 - 09:35 | No trades, opening auction volatility |
| Execution window | 09:35 - 15:50 | Normal entry and exit execution |
| Wind-down | 15:50 - 15:58 | Close intraday-only positions |
| Market close | 16:00 | US closes |

### Swing Trade Handling

Swing trades (hold_type = "swing") are NOT subject to wind-down forced closes:
- Intraday trades are closed during wind-down
- Swing trades carry overnight with their stop-loss orders active on Alpaca servers
- Each morning, swing positions are verified during state recovery
- Swing stops remain as GTC (Good Till Cancel) orders on Alpaca

### Full Bot Operating Window

- **Start**: 09:15 ET for pre-market scan
- **End**: 16:00 ET when US market closes
- **Total**: ~6 hours 45 minutes of operation per trading day

### Weekend and Holiday Detection

The bot must check the US holiday calendar before starting:

- **US federal holidays**: Bot does not start. Log "No markets open today" and exit cleanly.
- **Holiday source**: Hardcoded list of known US holidays for the current year, refreshed annually.
- **Early closes**: US markets close at 13:00 ET on some days (day before Thanksgiving, Christmas Eve, etc.). Handle these by adjusting the wind-down time accordingly.

### Mid-Day Start Behavior

If the bot starts after its normal pre-market window:

**Late start (start between 09:35 and 15:30 ET)**:
1. Skip pre-market scan
2. Enter 15-minute warmup: subscribe to market data, compute indicators, no trades
3. After warmup, begin normal execution
4. Use full watchlist at equal priority (no pre-market ranking available)
5. Log and send ntfy alert about late start

**Very late start (close-only)**:
Close-only mode activates after 15:30 ET. In close-only mode:
1. Manage existing positions only, no new entries
2. Follow normal wind-down schedule for any open positions

### MacBook Hardening

- **Prevent sleep**: Run `caffeinate -dims` alongside the bot process
- **Energy Saver**: Configure macOS Energy Saver to prevent sleep when plugged in
- **Network**: Wired Ethernet preferred. If Wi-Fi only, monitor and alert on connectivity issues.
- **Process management**: Run inside `tmux` or `screen` session
- **Auto-updates**: Disable during market hours
- **Battery**: Always run plugged in. Alert if battery < 20%.
- **Auto-reconnect**: If the bot process crashes, the startup script should be wrapped in a restart loop (max 3 restarts per day)

---

## Section 5: Watchlist & Universe

### Phase 1 Watchlist (13 symbols)

The live watchlist is **SPY + QQQ + all 11 SPDR sector ETFs**. Pure ETFs only — no individual stocks (avoid earnings-gap risk) and no leveraged ETFs (decay). The sector set gives enough breadth for sector-rotation filters and stays uniformly liquid on the free IEX data feed.

#### US Picks (USD-denominated)

| Ticker | Name | Sector |
|---|---|---|
| SPY | S&P 500 ETF | Broad market (primary, most validated) |
| QQQ | Nasdaq-100 ETF | Tech-heavy complement |
| XLK | Technology Select Sector SPDR | Technology |
| XLF | Financial Select Sector SPDR | Financials |
| XLV | Health Care Select Sector SPDR | Health Care |
| XLY | Consumer Discretionary SPDR | Consumer Discretionary |
| XLP | Consumer Staples SPDR | Consumer Staples |
| XLE | Energy Select Sector SPDR | Energy |
| XLI | Industrial Select Sector SPDR | Industrials |
| XLB | Materials Select Sector SPDR | Materials |
| XLU | Utilities Select Sector SPDR | Utilities |
| XLRE | Real Estate Select Sector SPDR | Real Estate |
| XLC | Communication Services SPDR | Communication Services |

**Historical note**: Earlier iterations of the watchlist used liquid individual stocks (F, AAL, SOFI, BAC, PLTR, NIO, SNAP, INTC). These were replaced by the ETF-only list on migration to Alpaca to eliminate earnings gap risk and simplify the universe.

### Watchlist Criteria

All watchlist symbols must meet these minimum requirements:

- **Average daily volume**: >5M shares
- **Bid-ask spread**: Typically <0.0005 (0.05%)
- **Price range**: Price limits are phase-specific (fractional shares supported, so price is a soft filter):
  - Phase 1: $40
  - Phase 2: $100
  - Phase 3: no practical limit
- **Market cap**: >$1B (no penny stocks, no micro-caps)
- **Exchange**: NYSE or NASDAQ only. No OTC.
- **Sector diversification**: The watchlist may contain up to 3 symbols per GICS sector (providing options). The risk manager enforces a stricter limit on concurrent held positions per sector (1 in Phase 1, 2 in Phase 2).

### Earnings Blackout

- **Source**: Finnhub `/calendar/earnings` endpoint
- **Fetch schedule**: Once per day during the first pre-market scan
- **Cache**: Store in `earnings_calendar` SQLite table
- **Blackout window**: Skip trading on any symbol from 48 hours before to 48 hours after its scheduled earnings date
- **Log**: "Skipping {ticker} - earnings blackout (reports on {date})"
- **If earnings date changes**: Re-fetch catches updates; always use the most recent data

### Watchlist Expansion (Phase 2+)

The current Phase 2 and Phase 3 watchlists in `config.yaml` are a narrower subset (SPY, QQQ, plus XLF/XLE/XLK/XLV for Phase 2; adding mega-caps AAPL/MSFT/NVDA/GOOGL/AMZN/META for Phase 3). The original Phase 2/3 plan included inverse ETFs and a wider individual-stock set, but that has been deferred until the multi-strategy framework proves out on ETFs.

Phase 2:
- 6 symbols: SPY, QQQ, XLF, XLE, XLK, XLV

Phase 3:
- 12 symbols: Phase 2 set + AAPL, MSFT, NVDA, GOOGL, AMZN, META
- Inverse ETFs for short-side capability: deferred
- Liquid options: deferred

---

## Section 6: Entry Strategy (Multi-Strategy Framework)

### Overview

Signal generation is implemented as a **multi-strategy framework** rather than a single combined signal. Four strategy archetypes run in parallel, each with its own virtual sub-portfolio and independent entry/exit logic. Trades from the strategies are consolidated at the portfolio level for risk enforcement.

Implementation: `trading_bot/strategy/strategies/` — one module per archetype (`mean_reversion.py`, `trend_following.py`, `breakout.py`, `sentiment_combo.py`), all subclassing `StrategyBase`. Configured under `multi_strategy.strategies.*` in `config.yaml`.

### Strategy Roster

| Strategy | Status | Allocation | Max Positions | Rationale |
|---|---|---|---|---|
| Mean Reversion | **PRIMARY (validated)** | $1,000 | 3 | Only strategy with convincing long-horizon backtest evidence. |
| Trend Following | Deprioritized | $1,000 | 1 | Consistent losses in daily and intraday backtests. |
| Breakout | Deprioritized | $1,000 | 1 | Low win rate; breakouts on ETFs are rare and often fail. |
| Sentiment Combo | Secondary | $1,000 | 2 | Profitable only in bull markets; needs regime gating. |

All four are currently `enabled: true` in `config.yaml` so the comparison backtests keep generating evidence, but only Mean Reversion is intended for live capital.

### Strategy 1: Mean Reversion (PRIMARY)

Buy RSI(14) oversold recoveries on highly-liquid ETFs. This is the validated, production-intended strategy.

**Entry conditions**:
- **Oversold trigger**: RSI(14) dipped below the oversold threshold within the last `oversold_lookback` bars (default 5).
- **Recovery confirmation**: Current RSI has recovered above `rsi_recovery` (default 35).
- **Volume confirmation**: Current bar volume > `volume_multiplier` × 20-bar average volume (default 1.3×).
- **Optional EMA confirm**: If `require_ema_confirm=true`, close must be above EMA(9). Disabled by default.

**Oversold threshold is volatility-adaptive** when `vix_adaptive_rsi=true`:
- Uses 20-day realized volatility (annualized stdev of daily close returns × √252 × 100) of the daily bars as a free VIX proxy (avoids the paid `^VIX` feed).
- **High vol (RV ≥ `rv_high_threshold`, default 25%)** → tighten to `rsi_oversold_high_vol` (default 25). Deeper dips required; fewer, higher-quality signals.
- **Low vol (RV ≤ `rv_low_threshold`, default 12%)** → loosen to `rsi_oversold_low_vol` (default 30). More trades when moves are muted.
- Normal regime uses the baseline `rsi_oversold` (default 28).

**Exits** (priority order):
1. **Stop loss** — ATR-based when `use_atr_stops=true`: stop at `entry − atr_stop_mult × ATR(14)`, floored at 3% of entry. Fixed-percent fallback (`stop_loss_pct`) if ATR unavailable.
2. **Trailing stop** — activated at `entry + atr_activation_mult × ATR` (default 2.5× ATR). Trails the highest price seen by `atr_trail_mult × ATR` (default 2.5× ATR).
3. **ATR target** — `entry + atr_target_mult × ATR` (default 5× ATR). Behavior depends on `let_winners_run`:
   - `let_winners_run=true` (current default): hitting the target does NOT exit. The target becomes the trailing-stop activation trigger, and the runner is only closed by the trailing stop or a stop-loss reversal. The RSI-normalization exit below is ALSO disabled once the position is up ≥ `let_winners_run_up_pct` (default 3%).
   - `let_winners_run=false`: target is a hard limit-exit.
4. **RSI normalization** — exit when RSI crosses above `rsi_exit` (default 55). Disabled by the `let_winners_run` rules above for winning trades.

**Position sizing** (when `use_risk_sizing=true`, the default):
- ATR-risk sizing: `shares = (equity × risk_per_trade_pct) / stop_distance`, where `stop_distance = atr_stop_mult × ATR`.
- Default `risk_per_trade_pct=0.02` (2% of equity per trade) — with 3 concurrent positions, maximum simultaneous risk is ~6%.
- Clamped by `max_position_pct` (default 0.33 → 33% per position × 3 positions ≈ full deployment).
- Fractional shares enabled (`fractional_shares=true`) — required on $1k capital for SPY (~$500+/share).

### Strategy 2: Trend Following (DEPRIORITIZED)

EMA(9)/EMA(21) crossover with SMA(50) trend filter, volume confirmation, trailing-stop exit. Backtests consistently show losses; retained for reference and for running side-by-side comparisons but NOT intended for live capital.

- Entry: close > SMA(50) AND EMA(9) > EMA(21) (recent crossover) AND volume > `volume_multiplier` × average.
- Exit: trailing stop at `trailing_stop_pct` (default 2.5%) from highest price, or `initial_stop_pct` (3%) initial hard stop.

### Strategy 3: Breakout (DEPRIORITIZED)

20-day-high breakout with volume, 10-day-low exit. Low win rate on ETFs. Retained for comparison only.

- Entry: close > highest close of last `breakout_period` bars (default 20) AND volume > 1.5× average.
- Exit: close < lowest close of last `exit_period` bars (default 10), or 3% stop loss.

### Strategy 4: Sentiment Combo (SECONDARY)

Combines Finnhub news sentiment with a minimal technical trigger. Profitable only in bull markets.

- Entry: sentiment score ≥ `sentiment_threshold` (default 0.15) AND at least `min_technical_signals` (default 1) technical signal fires (RSI recovery, EMA cross, or volume spike).
- Exit: fixed stop loss (default 1.5%) or take profit (default 2.5%).

### Shared Portfolio-Level Filters

In addition to each strategy's own logic, the following filters are applied at the portfolio level before any entry is executed:

1. **Market regime filter** (`multi_strategy.regime_filter`): No new entries when SPY closes below its 50-day SMA (configurable via `sma_period`; YAML shows 200 but the backtester and engine use 50-day). This blocks entries during broad-market downtrends. `--no-regime-filter` disables this in backtests. The filter is evaluated against daily SPY bars aggregated from the 1-min cache; see the 2026-04-20 bug-fix note below.
2. **ATR Percentile Rank < 85th**: Skip entry when volatility is extreme. Reduce size by 25% if ATR rank > 70th.
3. **Not in earnings blackout** (48 hours either side). Not applicable to pure-ETF watchlist but retained for Phase 2/3 stocks.
4. **Not on cooldown** (30 min post-exit per ticker).
5. **Sufficient settled cash** (T+1 tracking).
6. **Spread check** (< 0.05%).
7. **Market hours check** (execution window only).
8. **Max positions check** (per strategy AND aggregate across phase limit).
9. **Sector exposure check** (portfolio-level).
10. **Overnight gap filter** (daily mode): skip entries when the overnight gap exceeds a configured threshold.

**Regime filter data path (fixed 2026-04-20)**: `load_cached(ticker, to_date, "daily")` previously returned only ~4 months of cached daily bars, which was insufficient for a 50-day SMA through the full backtest window. It also produced tz-naive timestamps that did not match the UTC daily index. The fix aggregates the 1-min cache into daily bars and uses `.normalize()` when matching, so the regime filter now correctly blocks hundreds of thousands of bearish-period entry evaluations.

### Portfolio-Level Position Sizing (Phase 1)

The per-strategy sizing formulas above (Mean Reversion uses ATR-risk sizing) run first. These portfolio-level constraints are then applied as an outer bound:

| Parameter | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Max concurrent positions (aggregate) | 2 | 4 | 8 |
| Max per-position size | 40% of equity | 25% of equity | 15% of equity |
| Risk per trade | 2% of equity | 1.5% of equity | 1% of equity |
| Minimum position value | $50 | $100 | $200 |

**Note**: Mean Reversion's own `max_positions=3` and `max_position_pct=0.33` allow a fuller deployment than the Phase-1 aggregate cap of 2. The lower number wins — the engine enforces the portfolio-level cap. Current baselines were generated at `max_positions=3`; if live operation holds to Phase 1's cap of 2, expect slightly lower returns than the backtests.

**Legacy fixed-percent sizing** (still referenced by `exit_intraday.stop_loss_pct` etc., used when ATR is unavailable):
```
max_risk_amount = account_equity * risk_per_trade
stop_distance = entry_price * stop_loss_pct
shares = max_risk_amount / stop_distance   # fractional shares — no floor
position_value = shares * entry_price
```

Constraints applied in order:
1. `position_value <= account_equity * max_position_pct`
2. `position_value <= settled_cash_available`
3. **Fractional shares**: Alpaca supports down to 1/1000000 share. Do NOT floor to whole shares on ETFs — required for SPY/QQQ at this account size.
4. `position_value >= minimum_position_value` (if not, skip the trade)
5. If ATR Rank > 70th: `shares = shares * 0.75`
6. If sentiment is neutral (no data): `shares = shares * 0.75`

### Entry Order Type

- **Always use limit orders** for entries (never market orders)
- **Limit price**: Set at the current ask price (for immediate fill on liquid stocks) or midpoint for less liquid names
- **Time in force**: DAY (expires at market close if unfilled)
- **If unfilled after 5 minutes**: Cancel the order. The opportunity has passed.
- **Partial fills**: Accept partial fills if >= 50% of intended quantity. Cancel remainder. If < 50%, cancel all and retry with adjusted size only if signals still valid.

---

## Section 7: Exit Strategy

### Primary Exit Model (Mean Reversion)

The PRIMARY strategy (Mean Reversion) uses **ATR-based stops, targets, and trailing stops**, not fixed percentages. These are specified per-strategy under `multi_strategy.strategies.mean_reversion` in `config.yaml`:

| Exit Type | Trigger | Notes |
|---|---|---|
| Stop loss | `entry − atr_stop_mult × ATR(14)` | Default 2× ATR, floored at 3% of entry. Placed as a stop-market order immediately on entry fill. |
| ATR target | `entry + atr_target_mult × ATR(14)` | Default 5× ATR. If `let_winners_run=true` (default), this becomes the trailing-stop activation trigger rather than a hard exit. |
| Trailing stop | Activates at `entry + atr_activation_mult × ATR`; trails at `atr_trail_mult × ATR` from highest price | Defaults 2.5× ATR for both. Native Alpaca trailing-stop order. |
| RSI normalization | RSI crosses above `rsi_exit` (default 55) | Disabled once a position is up ≥ `let_winners_run_up_pct` (default 3%). |

### Fixed-Percent Fallback Exits

The fixed-percent exits below (`exit_intraday`, `exit_swing`, phase-2/3 overrides) remain in `config.yaml` and are used (a) by Trend Following / Breakout / Sentiment Combo, and (b) as fallbacks for Mean Reversion when ATR is unavailable.

### Phase 1 Intraday Exits (Fallback)

For positions with `hold_type = "intraday"` (expected to close same day):

| Exit Type | Trigger | Order Type | Priority |
|---|---|---|---|
| Stop loss | Price drops -2% from entry | Stop-market | 1 (highest) |
| Take profit | Price rises +3% from entry | Limit | 2 |
| Trailing stop | Activated when +1.5% from entry; trails at -1% from high | Trailing stop | 3 |
| Time stop | No significant move after 4 hours | Limit at market | 4 |

**Stop loss (-2%)**: Placed immediately on entry as a separate Alpaca server-side stop order. This is the maximum acceptable loss per trade. Uses a stop-market order to guarantee execution.

**Take profit (+3%)**: Placed immediately on entry as a separate Alpaca server-side limit order. Reward:risk ratio = 1.5:1.

**Trailing stop**: NOT placed at entry. Activated only when the position reaches +1.5% profit. At that point:
1. Cancel the fixed take-profit order
2. Place a trailing stop order with -1% trail distance
3. Log: "Trailing stop activated for {ticker} at +1.5%"

**Time stop (4 hours)**: If the position has been open for 4 hours and:
- P&L is between -0.5% and +0.5%: close at market (the trade is going nowhere)
- P&L is between -2% and -0.5%: keep stop loss, give it another hour, then close
- P&L is > +0.5% but < +1.5% (trailing not yet active): close with limit at current bid

### Phase 1 Swing Exits

For positions with `hold_type = "swing"` (expected multi-day hold):

| Exit Type | Trigger | Order Type | Priority |
|---|---|---|---|
| Stop loss | Price drops -3% from entry | Stop-market (GTC) | 1 (highest) |
| Take profit | Price rises +5% from entry | Limit (GTC) | 2 |
| Trailing stop | Activated when +2.5% from entry; trails at -1.5% from high | Trailing stop (GTC) | 3 |
| Time stop | Max hold 5 trading days | Limit at market | 4 |

**Swing stop loss (-3%)**: Wider than intraday to accommodate overnight gaps. GTC order survives after hours.

**Swing take profit (+5%)**: Higher target for multi-day moves. GTC.

**Swing trailing stop**: Activated at +2.5% profit, trails at -1.5%. GTC.

**Note**: Swing exit parameters are consistent across all phases. The wider stops and targets suit the longer hold times regardless of account size. Phase-specific changes only apply to intraday exits.

**Swing time stop (5 days)**: After 5 trading days, re-evaluate:
- If P&L > 0: close with limit at current bid
- If P&L between 0 and -1.5%: tighten stop to -1.5% from current price, give 2 more days
- If P&L < -1.5%: close immediately

### Exit Priority

Exits are processed in this priority order (highest first):

1. **Emergency exits**: Daily loss limit hit, kill switch activated, API disconnect with open positions. Always use **market orders**.
2. **Stop loss**: Fixed stop-loss order triggered by Alpaca server-side.
3. **Take profit**: Fixed take-profit order triggered by Alpaca server-side.
4. **Trailing stop**: Trailing stop order triggered by Alpaca server-side.
5. **Time stop**: Bot-initiated evaluation and close.
6. **Wind-down close**: End-of-session forced close for intraday positions.

### Spread-Widening Protection

Before executing non-emergency exits:
1. Check current bid-ask spread
2. If spread > 0.15%: delay exit up to 2 minutes, rechecking every 15 seconds
3. If spread narrows: execute exit with limit at midpoint
4. If spread remains wide after 2 minutes: execute with limit at bid price
5. **Emergency exits always use market orders regardless of spread**

### Order State Machine

Each position follows this state machine:

```
SIGNAL_DETECTED
    │
    ▼
ENTRY_PENDING ──(fill)──► POSITION_OPEN
    │                          │
    │(timeout/cancel)          ▼
    │                   STOP_AND_TARGET_ACTIVE
    ▼                          │
CANCELLED                      │──(price hits +1.5%/+2.5%)──► TRAILING_ACTIVE
                               │                                    │
                               │──(stop hit)──────────────────►     │
                               │──(target hit)────────────────►     │
                               │                                    │──(trail hit)──►
                               │                                    │                │
                               ▼                                    ▼                ▼
                           CLOSING ◄────────────────────────────CLOSING              │
                               │                                                     │
                               ▼                                                     ▼
                           CLOSED ◄──────────────────────────────────────────────CLOSED
```

**State transitions**:
- `SIGNAL_DETECTED` -> `ENTRY_PENDING`: Entry order placed
- `ENTRY_PENDING` -> `POSITION_OPEN`: Entry order filled
- `ENTRY_PENDING` -> `CANCELLED`: Entry order timed out or cancelled
- `POSITION_OPEN` -> `STOP_AND_TARGET_ACTIVE`: Stop and target orders confirmed placed
- `STOP_AND_TARGET_ACTIVE` -> `TRAILING_ACTIVE`: Profit threshold reached, trailing stop replaces target
- `STOP_AND_TARGET_ACTIVE` -> `CLOSING`: Stop or target triggered
- `TRAILING_ACTIVE` -> `CLOSING`: Trailing stop triggered
- `CLOSING` -> `CLOSED`: Exit order filled, all orders for this symbol cancelled

**On any exit trigger**: Cancel ALL other pending orders for this symbol before or immediately after the exit fill. The bot must explicitly cancel counterpart orders (stop when target fills, target when stop fills) since Alpaca does not support OCA groups.

---

## Section 8: Risk Management

### Daily Loss Limit

- **Threshold**: -1% of account equity at the start of the trading day
- **Calculation**: Sum of realized P&L + unrealized P&L for all positions opened today
- **When hit**:
  1. Immediately stop all new entries
  2. Manage existing positions only (stops remain active, trailing stops continue)
  3. Send CRITICAL ntfy alert: "Daily loss limit hit (-1%). No new entries until tomorrow."
  4. Log the event in `daily_summaries`
  5. Remain in close-only mode for the rest of the day

**Note on daily loss limit vs position sizing interaction**: With 2% risk per trade and 2 max concurrent positions, maximum simultaneous risk is 4% if both positions hit stops. The daily loss limit (-1%) acts as an early warning — when triggered after the first losing trade (~-2%), no new entries are permitted. The second position's stop remains active. This means the actual daily loss can exceed -1% (up to ~-4% in the worst case with 2 concurrent max-loss trades). This is by design — the daily loss limit prevents compounding losses through new entries, not existing positions.

### Maximum Concurrent Positions

| Phase | Max Positions | Max Per Sector |
|---|---|---|
| Phase 1 | 2 | 1 |
| Phase 2 | 4 | 2 |
| Phase 3 | 8 | 3 |

Positions are counted across all US exchanges combined.

### Maximum Daily Trades

| Phase | Max Daily Trades |
|---|---|
| Phase 1 | 10 |
| Phase 2 | 25 |
| Phase 3 | 50 |

This prevents overtrading, which is the primary account killer for small accounts. When the limit is reached:
- Send ntfy alert: "Daily trade limit reached ({count}). No new entries."
- Continue managing existing positions

### Correlation Check

Before entering position B while holding position A:
- Calculate 30-day daily return correlation between A and B
- If correlation > 0.85: block the entry. Log: "Blocked {B} - correlation {corr} with held {A}"
- Use cached historical data for correlation calculation (refresh daily)

### Settlement Tracking

- Track every sell in the `settlements` table
- Before every entry, compute: `available = settled_cash - reserved_for_open_orders`
- Never commit more than available settled cash
- Alpaca will reject the order anyway, but pre-checking avoids rejection logs and wasted API calls
- On each startup, mark settlements that should have cleared based on date

### Kill Switch

A remote kill switch via ntfy.sh subscription:

- Bot subscribes to a dedicated ntfy topic (e.g., `REDACTED_KILL_TOPIC`)
- On receiving any message to this topic:
  1. Immediately cancel all pending orders
  2. Place market sell orders for all open positions
  3. Send confirmation: "Kill switch activated. Flattening all positions."
  4. Enter permanent close-only mode (no new entries until restart)
  5. Log all actions

### Drawdown Circuit Breaker

- **Trigger**: Account equity drops 5% from its rolling 5-day peak
- **Action**:
  1. Close all positions with limit orders (not market, unless near session end)
  2. Pause all trading for 1 full trading day
  3. Send CRITICAL ntfy alert: "Drawdown breaker triggered. -5% from 5-day peak. Trading paused for 1 day."
  4. Log circuit breaker activation in `daily_summaries` with a note (not in `phase_transitions`, which is reserved for actual phase changes)
  5. On the next trading day, resume with 50% position sizes for the first 3 trades
  6. Return to normal sizing after 3 profitable trades post-breaker

### Order Rejection Handling

If Alpaca rejects an order:
1. Log the rejection reason in `order_rejections` table
2. Common reasons and responses:
   - "Insufficient funds": Recalculate available cash, likely settlement issue
   - "Price out of range": Adjust limit price to current market
   - "Contract not found": Remove ticker from watchlist, alert
   - "Max order count exceeded": Pause entries for 5 minutes
3. If 3+ rejections in 10 minutes: pause all new entries for 15 minutes, alert via ntfy
4. Never retry a rejected order without modifying the cause

---

## Section 9: Sentiment & News

### Data Sources

All sentiment data comes from the **Finnhub API** (free tier, 60 calls/minute rate limit).

### Individual Stock Sentiment

- **Endpoint**: `GET /news-sentiment?symbol={ticker}`
- **Score range**: Finnhub returns a `companyNewsScore` between 0 and 1, and a `sectorAverageNewsScore`
- **Normalization**: Convert to -1.0 to +1.0 range: `normalized = (raw_score - 0.5) * 2`
- **Entry threshold**: normalized score > 0.1 for long entries
- **Block threshold**: normalized score < -0.2 blocks entry entirely
- **No data**: Treat as neutral (0.0), proceed with 75% position size

### Sector Sentiment

- Calculate average normalized sentiment score across all watchlist symbols in the same GICS sector
- If sector average < -0.1: avoid new entries in that sector
- Used for sector rotation decisions in Phase 2+

### Market Sentiment

- **US market**: Average sentiment of SPY + QQQ. SPY and QQQ are added to the Finnhub sentiment refresh cycle even though they are not in the trading watchlist.
- If overall market sentiment < -0.2: reduce all position sizes by 50%
- If overall market sentiment < -0.4: enter close-only mode

### Caching

- All sentiment scores cached in `sentiment_cache` SQLite table
- Cache TTL: 30 minutes during market hours
- Before querying Finnhub, check cache. If fresh data exists, use it.
- Rate limit handling: If Finnhub returns 429, use cached data. If no cache, treat as neutral.
- Refresh schedule: Every 30 minutes for all watchlist symbols, staggered to avoid rate limits

### Earnings Calendar

- **Endpoint**: `GET /calendar/earnings?from={date}&to={date}`
- Fetch range: Today to 7 days ahead
- Refresh: Once per day during first pre-market scan
- Cache in `earnings_calendar` table
- Blackout: 48 hours before and after scheduled earnings date

---

## Section 10: Notifications (ntfy.sh)

### Configuration

- **Server**: `https://ntfy.sh` (default, configurable)
- **Topic**: User-configurable in `config.yaml` (e.g., `REDACTED_TOPIC`)
- **Kill switch topic**: Separate topic for kill commands (e.g., `REDACTED_KILL_TOPIC`)
- **Authentication**: None for ntfy.sh free tier (topic name is the security)

### Notification Events

| Event | Priority | Content |
|---|---|---|
| Trade entry | Default (3) | Ticker, side, price, quantity, signals, reasoning |
| Position closed | Default (3) | Ticker, P&L (amount and %), hold time, exit reason |
| Stop loss hit | High (4) | Ticker, loss amount, entry vs exit price |
| Daily loss limit hit | High (4) | Current daily P&L, limit value, action taken |
| Trailing stop activated | Low (2) | Ticker, profit at activation, trail distance |
| Daily summary | Default (3) | Total trades, wins/losses, daily P&L, equity, phase |
| Phase 0 cleanup plan | High (4) | Full assessment table, planned actions, 5-min delay |
| Phase transition | High (4) | From phase, to phase, equity, metrics |
| API disconnected | Urgent (5) | Reconnect attempts, positions at risk |
| API reconnected | Default (3) | Downtime duration, positions reconciled |
| Mass data staleness | High (4) | Count of stale symbols, percentage, action |
| Kill switch activated | Urgent (5) | Confirmation, positions being flattened |
| Drawdown breaker | Urgent (5) | Drawdown amount, peak equity, current equity, pause duration |
| Order rejection | Default (3) | Ticker, order type, rejection reason |
| Excessive rejections | High (4) | Count, time window, trading paused |
| Max daily trades reached | Default (3) | Trade count, no new entries |
| Bot startup | Low (2) | Start time, mode, phase, positions found |
| Bot shutdown | Low (2) | End time, daily summary |

### Rate Limiting

Rate limit: Maximum 5 notifications per minute to avoid hitting ntfy.sh free tier limits. Queue excess notifications and batch them.

### Fallback Notifications

If ntfy.sh is unreachable (3 consecutive failed POST attempts):
1. Fall back to macOS native notification via `osascript -e 'display notification ...'`
2. Log the ntfy failure as WARNING
3. Retry ntfy.sh every 60 seconds
4. Resume normal notifications when reachable

### Notification Format

```
[TRADE ENTRY] SOFI @ $12.35
Qty: 40 shares ($494.00)
Signals: EMA cross + BB bounce + Volume 2.1x
Sentiment: 0.32 (positive)
Stop: $12.10 (-2.0%) | Target: $12.72 (+3.0%)
Hold type: Intraday
```

```
[POSITION CLOSED] SOFI @ $12.65
P&L: +$12.00 (+2.4%) / £9.45 GBP
Hold time: 2h 15m
Exit reason: Trailing stop
```

```
[DAILY SUMMARY] 2026-04-16
Trades: 3 (2W / 1L)
Net P&L: +£4.21
Win rate: 67%
Equity: £953.14
Phase: 1 (£953/£5,000)
```

---

## Section 11: Database Schema

All data is stored in a SQLite database at `trading_bot/data/trading_bot.db`.

### Full Schema

```sql
-- Trades table: completed trade records
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    exchange        TEXT NOT NULL,           -- 'NYSE', 'NASDAQ', 'OTC'
    currency        TEXT NOT NULL,           -- 'GBP', 'USD'
    side            TEXT NOT NULL,           -- 'BUY', 'SELL'
    entry_time      TEXT NOT NULL,           -- ISO 8601 with timezone
    entry_price     REAL NOT NULL,
    quantity        INTEGER NOT NULL,
    exit_time       TEXT,                    -- NULL until closed
    exit_price      REAL,                    -- NULL until closed
    exit_reason     TEXT,                    -- 'stop_loss', 'take_profit', 'trailing_stop',
                                            -- 'time_stop', 'wind_down', 'kill_switch',
                                            -- 'daily_loss_limit', 'drawdown_breaker',
                                            -- 'manual', 'reconciliation_mismatch',
                                            -- 'phase0_cleanup'
    gross_pnl       REAL,                   -- In trade currency (USD)
    net_pnl         REAL,                   -- Same as gross_pnl (commission-free via Alpaca)
    pnl_gbp         REAL,                   -- Net P&L converted to GBP
    fx_rate         REAL,                   -- GBP/USD rate at close (1.0 for GBP trades)
    signal_price    REAL,                   -- Price when entry signal was generated (before order)
    slippage_bps    REAL,                   -- Basis points of slippage: (fill_price - signal_price) / signal_price * 10000
    sentiment_score REAL,                   -- Finnhub sentiment at entry time
    signals         TEXT,                   -- JSON: {"ema_cross": true, "bb_bounce": true, ...}
    hold_type       TEXT NOT NULL,          -- 'intraday', 'swing'
    phase           INTEGER NOT NULL,       -- Phase number when trade was taken
    notes           TEXT,                   -- Free-form notes (e.g., Phase 0 cleanup reasoning)
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Index for common queries
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_exit_reason ON trades(exit_reason);
CREATE INDEX IF NOT EXISTS idx_trades_phase ON trades(phase);

-- Positions table: currently open positions and their management state
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    currency        TEXT NOT NULL,
    sector          TEXT,                    -- GICS sector for sector exposure tracking
    quantity        INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    entry_time      TEXT NOT NULL,
    status          TEXT NOT NULL,           -- 'ENTRY_PENDING', 'POSITION_OPEN',
                                            -- 'STOP_AND_TARGET_ACTIVE', 'TRAILING_ACTIVE',
                                            -- 'CLOSING', 'CLOSED'
    stop_price      REAL,                   -- Current stop-loss price
    target_price    REAL,                   -- Current take-profit price
    trailing_active INTEGER NOT NULL DEFAULT 0, -- 0 or 1
    trailing_distance REAL,                 -- Trail distance in price units
    hold_type       TEXT NOT NULL,          -- 'intraday', 'swing'
    phase           INTEGER NOT NULL,
    alpaca_order_id     TEXT,               -- Alpaca entry order ID
    alpaca_stop_order_id TEXT,              -- Alpaca stop order ID
    alpaca_target_order_id TEXT,            -- Alpaca target order ID
    alpaca_trail_order_id TEXT,             -- Alpaca trailing stop order ID (when active)
    highest_price   REAL,                   -- Highest price since entry (for trailing calc)
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker);

-- Daily summaries: one row per trading day
CREATE TABLE IF NOT EXISTS daily_summaries (
    date            TEXT PRIMARY KEY,       -- YYYY-MM-DD
    total_trades    INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    gross_pnl_gbp   REAL NOT NULL DEFAULT 0.0,
    net_pnl_gbp      REAL NOT NULL DEFAULT 0.0,
    account_equity_gbp REAL NOT NULL,
    max_drawdown_pct REAL,                  -- Intraday max drawdown percentage
    win_rate        REAL,                   -- wins / total_trades
    avg_win_gbp     REAL,                   -- Average winning trade P&L
    avg_loss_gbp    REAL,                   -- Average losing trade P&L
    profit_factor   REAL,                   -- sum(wins) / abs(sum(losses))
    phase           INTEGER NOT NULL,
    us_trades       INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

-- Settlement tracking: T+1 for equities
CREATE TABLE IF NOT EXISTS settlements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER,                -- FK to trades.id (NULL for Phase 0 cleanup sells)
    ticker          TEXT NOT NULL,
    amount          REAL NOT NULL,          -- Proceeds amount in trade currency
    currency        TEXT NOT NULL,
    amount_gbp      REAL NOT NULL,          -- Converted to GBP
    sell_date       TEXT NOT NULL,           -- Date the sell was executed
    settle_date     TEXT NOT NULL,           -- Expected settlement date (T+1 business day)
    settled         INTEGER NOT NULL DEFAULT 0, -- 0 = pending, 1 = settled
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

CREATE INDEX IF NOT EXISTS idx_settlements_settled ON settlements(settled);
CREATE INDEX IF NOT EXISTS idx_settlements_settle_date ON settlements(settle_date);

-- Sentiment cache: avoid redundant API calls
CREATE TABLE IF NOT EXISTS sentiment_cache (
    ticker          TEXT NOT NULL,
    score           REAL NOT NULL,          -- Normalized -1.0 to +1.0
    raw_score       REAL,                   -- Original Finnhub score
    source          TEXT NOT NULL,           -- 'finnhub_news', 'finnhub_social', 'calculated'
    timestamp       TEXT NOT NULL,           -- When the score was fetched
    PRIMARY KEY (ticker, source)
);

-- Earnings calendar: blackout management
CREATE TABLE IF NOT EXISTS earnings_calendar (
    ticker          TEXT NOT NULL,
    earnings_date   TEXT NOT NULL,           -- YYYY-MM-DD
    earnings_hour   TEXT,                    -- 'bmo' (before market open), 'amc' (after close), NULL
    fetched_at      TEXT NOT NULL,           -- When this data was fetched
    PRIMARY KEY (ticker, earnings_date)
);

CREATE INDEX IF NOT EXISTS idx_earnings_date ON earnings_calendar(earnings_date);

-- Cooldowns: prevent rapid re-entry after exit
CREATE TABLE IF NOT EXISTS cooldowns (
    ticker          TEXT PRIMARY KEY,
    cooldown_until  TEXT NOT NULL            -- ISO 8601 datetime
);

-- Config snapshots: track parameter changes over time
CREATE TABLE IF NOT EXISTS config_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    config_json     TEXT NOT NULL,           -- Full config.yaml serialized as JSON
    notes           TEXT,                    -- Why the config was changed
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Order rejections: track Alpaca order failures for analysis
CREATE TABLE IF NOT EXISTS order_rejections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    order_type      TEXT NOT NULL,           -- 'LIMIT_BUY', 'LIMIT_SELL', 'STOP', 'TRAILING_STOP'
    intended_price  REAL,
    intended_qty    INTEGER,
    reason          TEXT NOT NULL,           -- Alpaca rejection reason string
    timestamp       TEXT NOT NULL,
    resolved        INTEGER NOT NULL DEFAULT 0 -- 0 = unresolved, 1 = resolved
);

CREATE INDEX IF NOT EXISTS idx_rejections_timestamp ON order_rejections(timestamp);

-- Phase transitions: audit trail for phase changes
CREATE TABLE IF NOT EXISTS phase_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    from_phase      INTEGER NOT NULL,       -- 0, 1, 2, or 3
    to_phase        INTEGER NOT NULL,
    direction       TEXT NOT NULL,           -- 'promotion', 'demotion'
    account_equity_gbp REAL NOT NULL,
    metrics_json    TEXT NOT NULL,           -- JSON with win_rate, sharpe, days_in_phase, etc.
    reason          TEXT NOT NULL,           -- Human-readable reason for transition
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Backtest results: separate from live trades
CREATE TABLE IF NOT EXISTS backtest_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id     TEXT NOT NULL,           -- UUID for this backtest run
    run_date        TEXT NOT NULL,           -- When the backtest was executed
    start_date      TEXT NOT NULL,           -- Backtest period start
    end_date        TEXT NOT NULL,           -- Backtest period end
    initial_equity  REAL NOT NULL,
    final_equity    REAL NOT NULL,
    total_trades    INTEGER NOT NULL,
    wins            INTEGER NOT NULL,
    losses          INTEGER NOT NULL,
    gross_pnl       REAL NOT NULL,
    net_pnl         REAL NOT NULL,          -- Same as gross_pnl (commission-free)
    max_drawdown_pct REAL NOT NULL,
    sharpe_ratio    REAL,
    win_rate        REAL NOT NULL,
    profit_factor   REAL,
    avg_hold_minutes REAL,
    slippage_model  TEXT NOT NULL,           -- e.g., '2bps_per_side'
    parameters_json TEXT NOT NULL,           -- Config used for this backtest
    trades_json     TEXT NOT NULL,           -- All trades in JSON array
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_run_date ON backtest_results(run_date);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER PRIMARY KEY,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now')),
    description     TEXT
);

-- Insert initial schema version
INSERT OR IGNORE INTO schema_version (version, description)
VALUES (5, 'V5 schema - US equity adaptive trading bot via Alpaca');
```

---

## Section 12: Reporting

### Daily Report

Generated automatically at the end of each trading day (after the last market closes). Rendered as HTML via Jinja2 templates.

**Content**:
- **Header**: Date, phase, account equity, daily P&L
- **P&L breakdown**:
  - Gross P&L (in trade currencies and GBP)
  - Net P&L in GBP
  - P&L as percentage of account equity
- **Per-trade detail table**:
  - Ticker, exchange, entry time, entry price, exit time, exit price
  - Hold time, hold type (intraday/swing)
  - Net P&L (USD and GBP)
  - Exit reason, signals used, sentiment score
- **Performance metrics**:
  - Win rate (today, 7-day rolling, 30-day rolling)
  - Average winning trade (GBP)
  - Average losing trade (GBP)
  - Profit factor (sum of wins / abs(sum of losses))
  - Largest win, largest loss
  - Expectancy: (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
- **Sector breakdown**: Trades and P&L by GICS sector
- **Exchange breakdown**: Trades and P&L by exchange (NYSE vs NASDAQ)
- **Account equity curve**: Line chart of daily equity over the last 30 days (generated as inline SVG or base64 image)
- **Phase progress**: Current phase, equity target for next phase, percentage progress, estimated days to next phase at current rate
- **Settlement status**: Pending settlements and expected clear dates
- **Open positions**: Any swing positions carried overnight with current P&L

### Weekly Report

Generated every Friday (or last trading day of the week) after the daily report.

**Additional content beyond daily**:
- Week-over-week comparison
- Best and worst days
- Cumulative weekly P&L
- Sector rotation analysis
- Watchlist performance ranking (which tickers were most/least profitable)

### Monthly Report

Generated on the last trading day of each month.

**Additional content**:
- Monthly P&L vs. compound growth target (0.3-0.5% per day)
- Drawdown analysis
- Phase transition progress
- Strategy parameter review recommendations
- Month-over-month trend

### Report Storage

- **Output directory**: `~/trading_bot_reports/`
- **File naming**: `daily_2026-04-16.html`, `weekly_2026-W16.html`, `monthly_2026-04.html`
- **Retention**: Keep all reports indefinitely (they are small HTML files)
- **Templates**: Stored in `trading_bot/reporting/templates/`

---

## Section 13: Phase Transitions

### Phase Overview

| Phase | Name | Equity Range | Max Positions | Hold Style | Watchlist Size |
|---|---|---|---|---|---|
| 0 | Portfolio Cleanup | Any | N/A | N/A | Existing positions |
| 1 | Micro-Account Swing | £950 - £5,000 | 2 | Hours to days | 10-15 |
| 2 | Small Account Active | £5,000 - £20,000 | 4 | Minutes to days | 20-25 |
| 3 | Full Day Trading | £20,000+ | 8 | Minutes to hours | 30-40 |

### Phase 1 to Phase 2 Promotion

All of the following criteria must be met simultaneously:

| Criterion | Threshold |
|---|---|
| Account equity | >= £5,000 |
| Trading days in Phase 1 | >= 40 |
| Win rate (last 20 trades) | >= 52% |
| Cumulative P&L (last 30 days) | > £0 (positive) |
| Daily loss limit breaches (last 20 days) | 0 |

**On promotion**:
1. Log the transition with full metrics in `phase_transitions`
2. Send HIGH priority ntfy: "Phase 2 activated! Equity: £{equity}. Upgrading parameters."
3. Update in-memory phase to 2
4. Apply Phase 2 parameters:
   - Max positions: 4
   - Max per sector: 2
   - Stop loss intraday: -1.5% (tighter)
   - Take profit intraday: +2.5%
   - Position size: max 25% of equity per position
   - Risk per trade: 1.5% of equity
   - Add 5-10 more symbols to watchlist
   - Max daily trades: 25
5. Take a config snapshot

### Phase 2 to Phase 3 Promotion

| Criterion | Threshold |
|---|---|
| Account equity | >= £20,000 |
| Trading days in Phase 2 | >= 60 |
| Win rate (last 40 trades) | >= 55% |
| Sharpe ratio (last 60 days) | > 1.0 |
| Cumulative P&L (last 60 days) | > £0 (positive) |

**On promotion**:
1. Log transition with full metrics
2. Send HIGH priority ntfy: "Phase 3 activated! Full day-trading mode. Equity: £{equity}."
3. Apply Phase 3 parameters:
   - Max positions: 8
   - Max per sector: 3
   - Stop loss intraday: -1.0% (tight scalping stops)
   - Take profit intraday: +1.5%
   - Trailing activation: +0.8%
   - Position size: max 15% of equity per position
   - Risk per trade: 1% of equity
   - Expand watchlist to 30-40 symbols including mega-cap tech
   - Add inverse ETF capability for short-side trading
   - Max daily trades: 50
   - Check signals every 5 seconds (faster scanning)
4. Take a config snapshot

### Phase Demotion

If account equity drops below 80% of the phase threshold, demote:

| Current Phase | Demotion Trigger | Demote To |
|---|---|---|
| Phase 2 | Equity < £4,000 | Phase 1 |
| Phase 3 | Equity < £16,000 | Phase 2 |

**On demotion**:
1. Log transition with full metrics and reason
2. Send CRITICAL ntfy: "Phase demotion! {from} -> {to}. Equity: £{equity}. Reducing parameters."
3. If current positions exceed new phase max: close the least profitable positions first (by unrealized P&L %) to comply
4. Apply the lower phase's parameters immediately
5. Take a config snapshot
6. The phase timer resets (must re-earn promotion)

### Phase Detection on Startup

On every startup, the bot determines its current phase:
1. Check `phase_transitions` table for the most recent transition
2. Verify current equity still qualifies for that phase
3. If equity has dropped below demotion threshold, execute demotion
4. Log: "Starting in Phase {N}. Equity: £{equity}. Next phase at £{target}."

---

## Section 14: Configuration (config.yaml)

The live config is at `config.yaml` in the project root — that file is the single source of truth. The snippet below is illustrative; whenever the live config and this spec disagree, the live config wins.

```yaml
# =============================================================================
# Trading Bot Configuration - V5 (Alpaca)
# =============================================================================
# This file is the single source of truth for all tunable parameters.
# Never hardcode values that belong here.
# =============================================================================

# -----------------------------------------------------------------------------
# Account Settings
# -----------------------------------------------------------------------------
account:
  base_currency: "GBP"
  trading_mode: "live"              # 'live' or 'backtest'
  phase_override: null              # Set to 0, 1, 2, or 3 to force a phase (null = auto-detect)

# -----------------------------------------------------------------------------
# Alpaca Connection
# -----------------------------------------------------------------------------
# API keys are read from environment variables:
#   ALPACA_API_KEY
#   ALPACA_SECRET_KEY
alpaca:
  paper: true                         # true = paper trading, false = live
  data_feed: "iex"                    # "iex" (free) or "sip" (paid)
  max_retries: 5
  retry_backoff_seconds: 30

# -----------------------------------------------------------------------------
# Market Data
# -----------------------------------------------------------------------------
market_data:
  staleness_threshold_seconds: 300    # IEX feed has multi-minute gaps on ETFs
  resubscribe_wait_seconds: 15
  resubscribe_retry_seconds: 60
  mass_staleness_pct: 0.95
  mass_staleness_resume_pct: 0.50
  # IEX free paper websocket is sparse. Strategies fetch fresh bars via REST on
  # every scan, so streaming staleness is not a sufficient reason to halt.
  pause_on_mass_staleness: false

# -----------------------------------------------------------------------------
# Market Schedule
# -----------------------------------------------------------------------------
schedule:
  us:
    pre_market_scan_start: "09:15"  # ET
    pre_market_scan_end: "09:30"
    market_open: "09:30"
    execution_start: "09:35"        # Skip first 5 min
    execution_end: "15:50"
    wind_down_start: "15:50"
    wind_down_end: "15:58"
    market_close: "16:00"
    timezone: "US/Eastern"
  warmup_minutes: 15
  late_start_close_only_et: "15:30" # Close-only after 15:30 ET
  bot_start_et: "09:15"
  bot_end_et: "16:00"

# -----------------------------------------------------------------------------
# Holiday Calendars (update annually)
# -----------------------------------------------------------------------------
holidays:
  us_2026:
    - "2026-01-01"  # New Year's Day
    - "2026-01-19"  # MLK Day
    - "2026-02-16"  # Presidents' Day
    - "2026-04-03"  # Good Friday
    - "2026-05-25"  # Memorial Day
    - "2026-07-03"  # Independence Day (observed)
    - "2026-09-07"  # Labor Day
    - "2026-11-26"  # Thanksgiving
    - "2026-12-25"  # Christmas Day
  us_early_close_2026:
    - "2026-11-27"  # Day after Thanksgiving (13:00 ET close)
    - "2026-12-24"  # Christmas Eve (13:00 ET close)

# -----------------------------------------------------------------------------
# Watchlist — SPY + QQQ + all 11 SPDR sector ETFs
# -----------------------------------------------------------------------------
watchlist:
  us:
    - "SPY"     # S&P 500 ETF — primary, most validated
    - "QQQ"     # Nasdaq-100 ETF — tech-heavy complement
    - "XLK"     # Technology
    - "XLF"     # Financials
    - "XLV"     # Health Care
    - "XLY"     # Consumer Discretionary
    - "XLP"     # Consumer Staples
    - "XLE"     # Energy
    - "XLI"     # Industrials
    - "XLB"     # Materials
    - "XLU"     # Utilities
    - "XLRE"    # Real Estate
    - "XLC"     # Communication Services
  us_phase2:
    - "SPY"
    - "QQQ"
    - "XLF"
    - "XLE"
    - "XLK"
    - "XLV"
  us_phase3:
    - "SPY"
    - "QQQ"
    - "XLF"
    - "XLE"
    - "XLK"
    - "XLV"
    - "AAPL"    # Apple
    - "MSFT"    # Microsoft
    - "NVDA"    # NVIDIA
    - "GOOGL"   # Alphabet
    - "AMZN"    # Amazon
    - "META"    # Meta Platforms

# -----------------------------------------------------------------------------
# Watchlist Quality Criteria
# -----------------------------------------------------------------------------
watchlist_criteria:
  min_avg_daily_volume_us: 5000000
  max_spread_pct_us: 0.0005           # 0.05% as decimal
  max_price_us_usd: 40.00
  min_market_cap_usd: 1000000000      # $1B
  max_symbols_per_sector: 3
  price_limits_by_phase:
    phase1:
      us_usd: 40.0
    phase2:
      us_usd: 100.0
    phase3:
      us_usd: 9999.0                  # No practical limit

# -----------------------------------------------------------------------------
# Risk Management
# -----------------------------------------------------------------------------
risk:
  daily_loss_limit_pct: 0.01      # -1% of account equity
  max_daily_trades:
    phase1: 10
    phase2: 25
    phase3: 50
  max_positions:
    phase1: 2
    phase2: 4
    phase3: 8
  max_sector_exposure:
    phase1: 1
    phase2: 2
    phase3: 3
  risk_per_trade_pct:
    phase1: 0.02                  # 2% of equity
    phase2: 0.015                 # 1.5%
    phase3: 0.01                  # 1%
  max_position_pct:
    phase1: 0.40                  # 40% of equity
    phase2: 0.25                  # 25%
    phase3: 0.15                  # 15%
  min_position_value_usd:
    phase1: 50.0
    phase2: 100.0
    phase3: 200.0
  drawdown_breaker:
    threshold_pct: 0.05           # 5% from 5-day peak
    rolling_days: 5
    pause_days: 1
    recovery_position_size_pct: 0.50  # 50% size for first 3 trades after breaker
    recovery_trades: 3
  correlation_threshold: 0.85

# -----------------------------------------------------------------------------
# Strategy - Technical Indicators
# -----------------------------------------------------------------------------
strategy:
  ema:
    fast_period: 9
    slow_period: 21
    timeframe: "5min"             # 5-minute bars
    crossover_lookback_bars: 3    # Signal valid for 3 bars after crossover
  bollinger:
    period: 20
    std_dev: 2.0
    timeframe: "5min"
    bounce_lookback_bars: 5       # Look back 5 bars for band touch
    squeeze_threshold: 0.02       # bandwidth / middle < this = squeeze
  volume:
    average_period: 20
    multiplier: 1.5               # Current vol must be > 1.5x average
    timeframe: "5min"
  atr:
    period: 14
    rank_lookback_days: 100
    extreme_percentile: 85        # Skip entry if ATR rank >= 85th
    high_percentile: 70           # Reduce size if ATR rank >= 70th
    high_vol_size_reduction: 0.75 # Multiply position size by this

# -----------------------------------------------------------------------------
# Entry Parameters
# -----------------------------------------------------------------------------
entry:
  min_signals_required: 3         # All 3 signals must align in Phase 1
  sentiment_threshold: 0.1        # Normalized score > 0.1 for longs
  sentiment_block_threshold: -0.2 # Block entry if sentiment < -0.2
  no_data_size_multiplier: 0.75   # 75% size when no sentiment data
  spread_max_pct: 0.0005           # 0.05%
  spread_wait_seconds: 120        # Wait up to 2 min for spread to narrow
  spread_recheck_seconds: 15      # Recheck spread every 15 sec
  entry_timeout_seconds: 300      # Cancel unfilled entry after 5 min
  partial_fill_min_pct: 0.50      # Accept partial fill >= 50%
  cooldown_minutes: 30            # Cooldown after exiting a ticker
  earnings_blackout_hours: 48     # Hours before/after earnings to skip

# -----------------------------------------------------------------------------
# Exit Parameters - Intraday
# -----------------------------------------------------------------------------
exit_intraday:
  stop_loss_pct: 0.02             # -2% from entry
  take_profit_pct: 0.03           # +3% from entry
  trailing_activation_pct: 0.015  # Activate trailing at +1.5%
  trailing_distance_pct: 0.01     # Trail at -1% from high
  time_stop_hours: 4              # Re-evaluate after 4 hours
  time_stop_flat_threshold: 0.005 # +/- 0.5% considered "flat"

# -----------------------------------------------------------------------------
# Exit Parameters - Swing
# -----------------------------------------------------------------------------
exit_swing:
  stop_loss_pct: 0.03             # -3% from entry
  take_profit_pct: 0.05           # +5% from entry
  trailing_activation_pct: 0.025  # Activate trailing at +2.5%
  trailing_distance_pct: 0.015    # Trail at -1.5% from high
  max_hold_days: 5                # Maximum hold time in trading days
  daily_review: true              # Re-evaluate swing positions daily

# -----------------------------------------------------------------------------
# Exit Parameters - Phase 2 Overrides
# -----------------------------------------------------------------------------
exit_intraday_phase2:
  stop_loss_pct: 0.015            # -1.5%
  take_profit_pct: 0.025          # +2.5%
  trailing_activation_pct: 0.012
  trailing_distance_pct: 0.008

# -----------------------------------------------------------------------------
# Exit Parameters - Phase 3 Overrides
# -----------------------------------------------------------------------------
exit_intraday_phase3:
  stop_loss_pct: 0.01             # -1.0% (tight scalping)
  take_profit_pct: 0.015          # +1.5%
  trailing_activation_pct: 0.008
  trailing_distance_pct: 0.005

# -----------------------------------------------------------------------------
# Spread-Widening Protection
# -----------------------------------------------------------------------------
exit_spread_protection:
  max_spread_pct: 0.0015          # 0.15% - delay non-emergency exits
  max_delay_seconds: 120          # Max 2 minutes delay
  recheck_interval_seconds: 15

# -----------------------------------------------------------------------------
# Notifications
# -----------------------------------------------------------------------------
notifications:
  ntfy_server: "https://ntfy.sh"
  ntfy_topic: "REDACTED_TOPIC"
  ntfy_kill_topic: "REDACTED_KILL_TOPIC"
  priorities:
    trade_entry: 3                # Default
    position_closed: 3
    stop_loss_hit: 4              # High
    daily_loss_limit: 4
    trailing_activated: 2         # Low
    daily_summary: 3
    phase0_cleanup: 4
    phase_transition: 4
    api_disconnect: 5              # Urgent
    api_reconnect: 3
    mass_staleness: 4
    kill_switch: 5
    drawdown_breaker: 5
    order_rejection: 3
    excessive_rejections: 4
    max_daily_trades: 3
    bot_startup: 2
    bot_shutdown: 2
  fallback_to_osascript: true
  max_retries: 3
  retry_interval_seconds: 60

# -----------------------------------------------------------------------------
# Settlement
# -----------------------------------------------------------------------------
settlement:
  t_plus_days: 1                  # T+1 for equities
  # Business day calculation accounts for weekends and holidays

# -----------------------------------------------------------------------------
# FX
# -----------------------------------------------------------------------------
fx:
  base_currency: "GBP"
  trading_currency: "USD"
  fx_api_url: "https://open.er-api.com/v6/latest/GBP"
  refresh_interval_seconds: 60
  fallback_gbp_usd: 1.27         # Used only if FX API unavailable

# -----------------------------------------------------------------------------
# Phase 0 - Portfolio Cleanup
# -----------------------------------------------------------------------------
phase0:
  enabled: true
  notification_delay_seconds: 300 # Wait 5 min after notifying before executing
  scoring:
    liquidity_weight: 25
    market_cap_weight: 20
    exchange_weight: 15
    technical_weight: 15
    sentiment_weight: 10
    loss_weight: 15
  thresholds:
    hold_min_score: 60
    sell_min_score: 30
    # Below 30 = urgent sell
  sell_strategy:
    start_at_mid: true
    adjust_toward_bid_after_hours: 2
    adjust_to_bid_after_hours: 4
    urgent_adjust_interval_minutes: 30
    market_order_threshold_value: 50  # Use market if position < $50 after 4h

# -----------------------------------------------------------------------------
# Phase Transitions
# -----------------------------------------------------------------------------
phases:
  phase1_to_phase2:
    equity_gbp: 5000
    min_trading_days: 40
    min_win_rate_last_n: 0.52     # 52% over last 20 trades
    win_rate_lookback_trades: 20
    positive_pnl_lookback_days: 30
    max_loss_limit_breaches_last_n: 0
    loss_breach_lookback_days: 20
  phase2_to_phase3:
    equity_gbp: 20000
    min_trading_days: 60
    min_win_rate_last_n: 0.55     # 55% over last 40 trades
    win_rate_lookback_trades: 40
    min_sharpe_ratio: 1.0
    sharpe_lookback_days: 60
    positive_pnl_lookback_days: 60
  demotion:
    equity_pct_of_threshold: 0.80 # Demote if equity < 80% of phase threshold

# -----------------------------------------------------------------------------
# Reporting
# -----------------------------------------------------------------------------
reporting:
  output_dir: "~/trading_bot_reports"
  daily_report: true
  weekly_report: true
  monthly_report: true
  equity_curve_days: 30           # Show last 30 days on chart
  templates_dir: "trading_bot/reporting/templates"

# -----------------------------------------------------------------------------
# Health Check
# -----------------------------------------------------------------------------
health:
  enabled: true
  host: "0.0.0.0"
  port: 8080

# -----------------------------------------------------------------------------
# Database
# -----------------------------------------------------------------------------
database:
  path: "trading_bot/data/trading_bot.db"
  backup_enabled: true
  backup_interval_hours: 24
  backup_dir: "trading_bot/data/backups"

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging:
  level: "INFO"                   # DEBUG, INFO, WARNING, ERROR, CRITICAL
  file: "trading_bot/logs/bot.log"
  max_bytes: 10485760             # 10 MB
  backup_count: 5                 # Keep 5 rotated log files
  format: "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# -----------------------------------------------------------------------------
# Backtesting
# -----------------------------------------------------------------------------
backtesting:
  slippage_bps_per_side: 2        # 2 basis points per side
  default_sentiment: 0.0          # Assume neutral if no cached data
  bar_size: "1 min"                # 1-min bars from Alpaca

# -----------------------------------------------------------------------------
# Multi-Strategy Configuration (current production block)
# -----------------------------------------------------------------------------
multi_strategy:
  enabled: true
  total_allocation_usd: 4000.0
  comparison_period_days: 252

  # Market regime filter — blocks new entries when SPY < 50-day SMA.
  regime_filter:
    enabled: true
    index_symbol: "SPY"
    sma_period: 200                 # YAML currently 200; engine uses 50-day for validated baseline
    cache_ttl_minutes: 30

  strategies:
    # PRIMARY. Validated on 13 years of SPY 5-min data.
    mean_reversion:
      enabled: true
      allocation_usd: 1000.0
      max_positions: 3
      rsi_period: 14
      rsi_oversold: 28
      rsi_recovery: 35
      rsi_exit: 55
      stop_loss_pct: 0.02            # fallback when ATR unavailable
      take_profit_pct: 0.03          # fallback when ATR unavailable
      volume_multiplier: 1.3
      oversold_lookback: 5
      require_ema_confirm: false
      ema_confirm_period: 9
      # ATR-based stops/targets
      use_atr_stops: true
      atr_period: 14
      atr_stop_mult: 2.0
      atr_target_mult: 5.0
      atr_trail_mult: 2.5
      atr_activation_mult: 2.5
      # Risk-based sizing
      use_risk_sizing: true
      risk_per_trade_pct: 0.02       # 2% risk per trade (~6% max with 3 positions)
      max_position_pct: 0.33         # 33% per position × 3 ≈ full deployment
      fractional_shares: true        # required on $1k capital for SPY/QQQ
      # Volatility-adaptive RSI — uses 20-day realized vol of daily closes as a free VIX proxy.
      vix_adaptive_rsi: true
      rv_lookback_days: 20
      rv_high_threshold: 25.0
      rv_low_threshold: 12.0
      rsi_oversold_high_vol: 25      # stricter when vol is high
      rsi_oversold_low_vol: 30       # looser when vol is low
      # Let winners run — ATR target becomes trailing-activation; RSI exit disabled once up >= pct.
      let_winners_run: true
      let_winners_run_up_pct: 0.03

    # DEPRIORITIZED — kept for comparison backtests, NOT intended for live capital.
    trend_following:
      enabled: true
      allocation_usd: 1000.0
      max_positions: 1
      sma_period: 50
      ema_fast: 9
      ema_slow: 21
      volume_multiplier: 1.5
      trailing_stop_pct: 0.025
      initial_stop_pct: 0.03

    # DEPRIORITIZED.
    breakout:
      enabled: true
      allocation_usd: 1000.0
      max_positions: 1
      breakout_period: 20
      exit_period: 10
      volume_multiplier: 1.5
      stop_loss_pct: 0.03

    # SECONDARY — profitable in bull regimes only.
    sentiment_combo:
      enabled: true
      allocation_usd: 1000.0
      max_positions: 2
      sentiment_threshold: 0.15
      min_technical_signals: 1
      stop_loss_pct: 0.015
      take_profit_pct: 0.025
```

---

## Section 15: Libraries & Requirements

### Python Requirements (`requirements.txt`)

```
alpaca-py>=0.21.0
pandas>=2.0.0
pandas-ta>=0.3.14
finnhub-python>=2.4.0
pyyaml>=6.0
requests>=2.31.0
jinja2>=3.1.0
aiohttp>=3.9.0
pytest>=7.0.0
pytest-asyncio>=0.21.0
```

### Library Purposes

| Library | Purpose |
|---|---|
| `alpaca-py` | Official Alpaca Trading API client. `TradingClient` for REST (orders, positions, account), `StockDataStream` for WebSocket real-time data, `StockHistoricalDataClient` for OHLCV bars. |
| `pandas` | DataFrame operations for price data, indicator computation, report generation. |
| `pandas-ta` | Technical indicator calculations: EMA, Bollinger Bands, ATR, RSI. Avoids manual implementation. |
| `finnhub-python` | Finnhub API client for news sentiment (`/news-sentiment`) and earnings calendar (`/calendar/earnings`). |
| `pyyaml` | Parse `config.yaml` configuration file. |
| `requests` | HTTP calls for ntfy.sh notifications and any non-async API calls. |
| `jinja2` | HTML templating for daily/weekly/monthly report generation. |
| `aiohttp` | Async HTTP server for the health check endpoint and FX rate queries. Runs within the bot's event loop. |
| `pytest` | Testing framework for unit and integration tests. |
| `pytest-asyncio` | Async test support for testing async code. |

### Python Version

**Minimum**: Python 3.10 (required for `match` statements, improved type hints, `zoneinfo` module).

### Standard Library Dependencies

These standard library modules are used extensively and require no installation:

- `asyncio` - Event loop for async operations
- `sqlite3` - Database access
- `logging` - All logging (no `print()` statements)
- `zoneinfo` - Timezone handling (`ZoneInfo("US/Eastern")`)
- `datetime` - Date and time operations
- `json` - JSON serialization for database fields
- `pathlib` - File path handling
- `dataclasses` - Data structures for trades, positions, signals
- `enum` - Enums for phase, status, exit reason, etc.
- `uuid` - Backtest run IDs
- `math` - Floor, ceil for position sizing
- `collections` - Named tuples, counters

---

## Section 16: Architecture (File Tree)

```
trading_bot/
├── __init__.py
├── main.py                          # Entry point, event loop, phase detection,
│                                    # orchestrates US market session
├── config.py                        # Load config.yaml, validate all fields,
│                                    # provide typed access to parameters
├── constants.py                     # Enums: Phase, HoldType, ExitReason, OrderStatus,
│                                    # MarketSession, Exchange. Exchange constants
│                                    # (trading hours). GICS sector map.
│
├── gateway/
│   ├── __init__.py
│   ├── connection.py                # Alpaca API connection management:
│   │                                # TradingClient init, heartbeat, reconnect,
│   │                                # exponential backoff, connection state
│   └── recovery.py                  # Startup state recovery: query Alpaca positions/orders,
│                                    # reconcile with SQLite, verify stop orders,
│                                    # log discrepancies, send alerts
│
├── db/
│   ├── __init__.py
│   ├── schema.py                    # SQLite CREATE TABLE statements, table creation,
│   │                                # schema version check
│   ├── migrations.py                # Schema versioning: detect current version,
│   │                                # apply migrations sequentially, rollback support
│   └── repository.py               # Data access layer: all INSERT/SELECT/UPDATE queries.
│                                    # Methods: save_trade(), get_open_positions(),
│                                    # get_daily_summary(), save_settlement(),
│                                    # get_settled_cash(), save_sentiment(), etc.
│
├── data/
│   ├── __init__.py
│   ├── market_data.py               # Real-time market data via Alpaca StockDataStream WebSocket,
│   │                                # historical data via StockHistoricalDataClient, staleness detection,
│   │                                # mass staleness handling, bar aggregation
│   ├── sentiment.py                 # Finnhub news-sentiment API integration,
│   │                                # score normalization, caching in SQLite,
│   │                                # rate limit handling, sector/market sentiment calc
│   ├── earnings.py                  # Finnhub earnings calendar API,
│   │                                # blackout window calculation, daily refresh
│   ├── fx.py                        # FX rate queries from external API (open.er-api.com),
│   │                                # caching, conversion functions,
│   │                                # fallback rate handling
│   └── alpaca_downloader.py         # Download historical 1-min and daily bars
│                                    # from Alpaca into the parquet cache
│                                    # (uses Adjustment.ALL — critical for splits)
│
├── strategy/
│   ├── __init__.py
│   ├── base.py                      # StrategyBase ABC: evaluate_entry() /
│   │                                # evaluate_exit() contract, ExitSignal,
│   │                                # StrategyDecision dataclasses
│   ├── technical.py                 # Indicator calculations (TechnicalAnalyzer):
│   │                                # RSI(14), EMA (9/21), Bollinger, volume avg,
│   │                                # ATR (14), ATR percentile rank, SMA
│   ├── strategies/                  # Multi-strategy archetypes (plug-in style)
│   │   ├── __init__.py              # create_strategies(cfg) factory
│   │   ├── mean_reversion.py        # PRIMARY — RSI oversold bounce with VIX-adaptive
│   │   │                            # thresholds, ATR stops/targets/trailing, let
│   │   │                            # winners run, risk-based sizing
│   │   ├── trend_following.py       # DEPRIORITIZED — EMA cross + SMA(50) trend + volume
│   │   ├── breakout.py              # DEPRIORITIZED — 20-day high breakout, 10-day-low exit
│   │   └── sentiment_combo.py       # SECONDARY — Finnhub sentiment + technical signal
│   └── portfolio_assessor.py        # Phase 0: score existing positions (0-100),
│                                    # classify HOLD/SELL/URGENT_SELL,
│                                    # execute liquidation with patient limit orders,
│                                    # notification before execution
│
├── execution/
│   ├── __init__.py
│   ├── order_manager.py             # Order placement via alpaca-py: limit orders,
│   │                                # stop orders, trailing stops.
│   │                                # Order state machine tracking.
│   │                                # Order modification, cancellation.
│   │                                # Fill detection via polling (5s interval).
│   │                                # Alpaca order rejection handling.
│   ├── position_sizer.py            # Phase-aware position sizing: compute shares
│   │                                # based on risk %, equity, stop distance.
│   │                                # Apply ATR adjustment, sentiment adjustment.
│   │                                # Enforce min/max constraints.
│   ├── risk_manager.py              # Daily P&L tracking, daily loss limit enforcement,
│   │                                # sector exposure tracking, correlation checking,
│   │                                # max positions enforcement, max daily trades,
│   │                                # drawdown circuit breaker,
│   │                                # kill switch listener
│   └── settlement_tracker.py        # T+1 settlement: record sells, calculate settle dates,
│                                    # mark settlements as cleared, compute available
│                                    # settled cash, handle weekends/holidays
│
├── notifications/
│   ├── __init__.py
│   └── notifier.py                  # ntfy.sh integration: POST notifications,
│                                    # priority levels, retry logic, fallback to
│                                    # osascript, kill switch subscription listener
│
├── health/
│   ├── __init__.py
│   └── server.py                    # aiohttp server on port 8080,
│                                    # /health endpoint returning JSON status,
│                                    # runs within main event loop
│
├── reporting/
│   ├── __init__.py
│   ├── daily_report.py              # Generate daily HTML report: query DB for day's
│   │                                # trades, compute metrics, render Jinja2 template,
│   │                                # save to ~/trading_bot_reports/
│   ├── performance.py               # Metric calculations: win rate, profit factor,
│   │                                # Sharpe ratio, expectancy, max drawdown,
│   │                                # rolling averages, equity curve data
│   └── templates/
│       ├── daily_report.html        # Jinja2 template: header, P&L table, per-trade
│       │                            # detail, metrics, equity chart, phase progress
│       ├── weekly_report.html       # Jinja2 template: weekly aggregation, trends,
│       │                            # sector analysis, watchlist ranking
│       └── monthly_report.html      # Jinja2 template: monthly P&L vs targets,
│                                    # drawdown analysis, phase progress, trends
│
├── multi_strategy_backtest.py       # CLI backtester entry point (not a package).
│                                    # Three modes: --daily, --spy, --multi-intraday.
│                                    # Per-strategy virtual sub-portfolios, shared
│                                    # regime filter, comparison report.
│
├── data_cache.py                    # Parquet cache (load_cached / save_to_cache)
│                                    # used by both the downloader and the backtester.
│

├── data/                            # Runtime data directory (not Python package)
│   ├── trading_bot.db               # SQLite database (created on first run)
│   └── backups/                     # Daily DB backups
│
├── logs/                            # Log files (created on first run)
│   └── bot.log
│
└── tests/
    ├── __init__.py
    ├── conftest.py                  # Shared fixtures: mock Alpaca client, test DB,
    │                                # sample market data, config overrides
    ├── test_entry_signals.py        # Test EMA crossover detection, Bollinger bounce,
    │                                # volume confirmation, combined signal logic
    ├── test_exit_logic.py           # Test stop loss, take profit, trailing activation,
    │                                # time stops, wind-down behavior, emergency exits
    ├── test_risk_manager.py         # Test daily loss limit, sector exposure,
    │                                # max positions, max trades, drawdown breaker
    ├── test_position_sizer.py       # Test sizing formula, ATR adjustment,
    │                                # sentiment adjustment, min/max constraints
    ├── test_settlement_tracker.py   # Test T+1 calculation, weekend handling,
    │                                # holiday handling, settled cash computation
    ├── test_portfolio_assessor.py   # Test Phase 0 scoring, classification,
    │                                # sell order strategy, hold decisions
    ├── test_phase_transitions.py    # Test promotion criteria, demotion triggers,
    │                                # parameter changes on transition
    ├── test_order_state_machine.py  # Test state transitions, OCA behavior,
    │                                # partial fills, rejections, cancellations
    ├── test_fx_conversion.py        # Test GBP/USD conversion, fallback rate,
    │                                # P&L conversion, position sizing in USD
    └── test_data_staleness.py       # Test staleness detection, re-subscribe,
                                     # mass staleness threshold, pause/resume
```

---

## Section 17: Startup Script (start_bot.sh)

```bash
#!/usr/bin/env bash
# =============================================================================
# start_bot.sh - Pre-flight checks and bot launcher
# =============================================================================
# Usage: bash start_bot.sh
# =============================================================================

set -euo pipefail

# Colors for output
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
NC='\033[0m' # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOT_DIR="$SCRIPT_DIR"
PID_FILE="$BOT_DIR/.bot.pid"
LOG_FILE="$BOT_DIR/trading_bot/logs/bot.log"
VENV_DIR="$BOT_DIR/venv"

critical_fail=0
warnings=0

log_critical() {
    echo -e "${RED}[CRITICAL]${NC} $1"
    critical_fail=1
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
    warnings=$((warnings + 1))
}

log_ok() {
    echo -e "${GREEN}[OK]${NC} $1"
}

echo "========================================"
echo "  Trading Bot Pre-Flight Checks"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "========================================"
echo ""

# ---- CRITICAL CHECKS (fail = abort) ----

# 1. Check Python version >= 3.10
echo "Checking Python version..."
if command -v python3 &> /dev/null; then
    PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)
    if [ "$PY_MAJOR" -ge 3 ] && [ "$PY_MINOR" -ge 10 ]; then
        log_ok "Python $PY_VERSION"
    else
        log_critical "Python $PY_VERSION found, need >= 3.10"
    fi
else
    log_critical "Python 3 not found in PATH"
fi

# 2. Check Alpaca API keys
echo "Checking Alpaca API keys..."
if [ -n "${ALPACA_API_KEY:-}" ] && [ -n "${ALPACA_SECRET_KEY:-}" ]; then
    log_ok "Alpaca API keys configured in environment"
else
    log_critical "Alpaca API keys not set. Set ALPACA_API_KEY and ALPACA_SECRET_KEY."
fi

# 3. Check config.yaml exists
echo "Checking config.yaml..."
if [ -f "$BOT_DIR/config.yaml" ]; then
    log_ok "config.yaml found"
else
    log_critical "config.yaml not found at $BOT_DIR/config.yaml"
fi

# 4. Check network connectivity
echo "Checking network..."
if ping -c 1 -W 3 8.8.8.8 &>/dev/null; then
    log_ok "Network connectivity OK"
else
    log_critical "No network connectivity (cannot reach 8.8.8.8)"
fi

# 5. Check config.yaml is valid YAML
echo "Validating config.yaml..."
if python3 -c "import yaml; yaml.safe_load(open('$BOT_DIR/config.yaml'))" 2>/dev/null; then
    log_ok "config.yaml is valid YAML"
else
    log_critical "config.yaml is not valid YAML"
fi

# 6. Check disk space (>500MB free)
echo "Checking disk space..."
FREE_MB=$(df -m "$BOT_DIR" | tail -1 | awk '{print $4}')
if [ "$FREE_MB" -gt 500 ]; then
    log_ok "Disk space: ${FREE_MB}MB free"
else
    log_critical "Low disk space: ${FREE_MB}MB free (need >500MB)"
fi

# 7. Check SQLite database is accessible
echo "Checking database..."
DB_PATH="$BOT_DIR/trading_bot/data/trading_bot.db"
DB_DIR=$(dirname "$DB_PATH")
if [ -d "$DB_DIR" ] || mkdir -p "$DB_DIR" 2>/dev/null; then
    if [ -f "$DB_PATH" ]; then
        if python3 -c "import sqlite3; sqlite3.connect('$DB_PATH').execute('SELECT 1')" 2>/dev/null; then
            log_ok "SQLite database accessible"
        else
            log_critical "SQLite database exists but is not accessible"
        fi
    else
        log_ok "Database directory ready (DB will be created on first run)"
    fi
else
    log_critical "Cannot create database directory: $DB_DIR"
fi

# 8. Check required Python packages
echo "Checking Python packages..."
MISSING_PKGS=""
for pkg in alpaca pandas pandas_ta finnhub yaml requests jinja2 aiohttp; do
    if ! python3 -c "import $pkg" 2>/dev/null; then
        MISSING_PKGS="$MISSING_PKGS $pkg"
    fi
done
if [ -z "$MISSING_PKGS" ]; then
    log_ok "All required Python packages installed"
else
    log_critical "Missing Python packages:$MISSING_PKGS"
fi

echo ""

# ---- WARNING CHECKS (warn but continue) ----

# 9. Check caffeinate
echo "Checking caffeinate..."
if pgrep -x caffeinate &>/dev/null; then
    log_ok "caffeinate is running"
else
    log_warning "caffeinate not running. Starting it now..."
    caffeinate -dims &
    log_ok "caffeinate started (PID: $!)"
fi

# 10. Check if running in tmux/screen
echo "Checking terminal session..."
if [ -n "${TMUX:-}" ] || [ -n "${STY:-}" ]; then
    log_ok "Running inside tmux/screen"
else
    log_warning "Not running inside tmux/screen. Terminal closure will kill the bot."
fi

# 11. Check power adapter
echo "Checking power..."
if pmset -g batt 2>/dev/null | grep -q "AC Power"; then
    log_ok "Running on AC power"
else
    BATT_PCT=$(pmset -g batt 2>/dev/null | grep -o '[0-9]*%' | head -1 | tr -d '%')
    if [ -n "$BATT_PCT" ] && [ "$BATT_PCT" -lt 20 ]; then
        log_warning "Running on battery at ${BATT_PCT}%! Plug in immediately."
    else
        log_warning "Running on battery (${BATT_PCT:-unknown}%). Recommend plugging in."
    fi
fi

# 12. Check ntfy.sh reachability
echo "Checking ntfy.sh..."
if curl -s -o /dev/null -w "%{http_code}" "https://ntfy.sh" 2>/dev/null | grep -q "200\|301\|302"; then
    log_ok "ntfy.sh is reachable"
else
    log_warning "ntfy.sh is not reachable. Notifications will use fallback."
fi

# 13. Determine if today is a trading day
echo "Checking trading day..."
DAY_OF_WEEK=$(date +%u)
if [ "$DAY_OF_WEEK" -ge 6 ]; then
    log_warning "Today is a weekend. No markets are open."
fi

echo ""
echo "========================================"

# ---- ABORT IF CRITICAL FAILURES ----

if [ "$critical_fail" -eq 1 ]; then
    echo -e "${RED}Pre-flight checks FAILED. Fix critical issues above before starting.${NC}"
    exit 1
fi

echo -e "${GREEN}All critical checks passed.${NC} Warnings: $warnings"
echo ""

# ---- LAUNCH ----

echo "Starting trading bot..."

# Create log directory if needed
mkdir -p "$(dirname "$LOG_FILE")"

# Set PYTHONPATH
export PYTHONPATH="$BOT_DIR:${PYTHONPATH:-}"

# Start the bot
python3 -m trading_bot.main &
BOT_PID=$!

# Write PID file
echo "$BOT_PID" > "$PID_FILE"

echo "Bot started with PID: $BOT_PID"
echo "PID file: $PID_FILE"
echo "Log file: $LOG_FILE"
echo ""
echo "To stop: kill \$(cat $PID_FILE)"
echo "To monitor: tail -f $LOG_FILE"
```

---

## Section 18: Order Execution & Fill Realism

### Live Trading Context

This bot trades on an **Alpaca paper account**. The Phase 0 to Phase 1 transition and the overall cautious phased approach IS the safety mechanism:
- The account already holds real positions that need management (Phase 0)
- The account is small enough that losses are bounded
- Cash account mode naturally prevents over-leveraging
- Conservative position sizing and strict risk limits provide protection without a separate paper phase

### Order Execution Approach

- **Entries**: Always use **limit orders**. Never use market orders for entries.
  - Limit price set at current ask (for liquid stocks) or midpoint (for less liquid)
  - Time in force: DAY
  - Cancel if unfilled after 5 minutes

- **Stop losses**: Use **stop-market orders** placed as separate orders immediately after entry fill
  - Server-side on Alpaca: survive bot crashes and disconnections
  - Guarantee execution (though slippage may occur on gaps)

- **Take profits**: Use **limit orders** placed as separate orders immediately after entry fill
  - Server-side on Alpaca

- **Trailing stops**: Use Alpaca native **trailing stop orders**
  - Server-side on Alpaca
  - Specified as percentage trail

- **Emergency exits**: Use **market orders**
  - Only for: kill switch, daily loss limit, drawdown breaker, end-of-day forced close after wind-down limit fails

### Slippage Tracking

For every completed trade, log:
- **Signal price**: The price at the moment the entry signal was generated
- **Fill price**: The actual fill price from Alpaca
- **Slippage**: `fill_price - signal_price` (positive = unfavorable for longs)
- **Slippage bps**: `slippage / signal_price * 10000`

Track rolling average slippage over the last 20 trades. If average slippage exceeds 10 bps (0.1%):
1. Send ntfy alert: "Average slippage {bps} bps over last 20 trades. Review order strategy."
2. Log detailed slippage breakdown by exchange and time of day
3. Consider switching to more aggressive limit prices (e.g., ask + 1 tick instead of ask)

### Backtesting Slippage Model

For backtesting purposes (Section 19), model slippage as:
- **Default**: 2 basis points per side (entry and exit)
- **Configurable**: Adjust in `config.yaml` under `backtesting.slippage_bps_per_side`
- **Applied**: Unfavorable direction on both entry (higher for longs) and exit (lower for longs)

---

## Section 19: Backtesting Module

### Purpose

The backtesting module replays historical data through the full strategy logic to evaluate performance before committing real capital to parameter changes. Implementation is in `trading_bot/multi_strategy_backtest.py` (CLI: `python -m trading_bot.multi_strategy_backtest ...`).

Each enabled strategy runs independently against its own virtual $1,000 sub-portfolio. The engine walks bars chronologically, evaluates every strategy's entry/exit logic per bar, tracks per-strategy equity curves, and produces a side-by-side comparison report.

### Backtest Modes

There are three CLI-selectable modes, each with a different data source:

#### `--daily` (S&P 500 daily bars)

- **Data**: CSV files under `backtest_data/individual_stocks_5yr/` (well-known 5-year S&P 500 Kaggle dataset).
- **Bar size**: Daily.
- **Entry point**: `run_daily()`.
- **Filters**: `--min-volume`, `--min-price`, `--max-price` for universe filtering.
- **Use case**: Multi-year evaluations across a broad universe of individual stocks. Regime filter uses daily SPY bars natively.

#### `--spy` (SPY 5-minute bars)

- **Data**: `backtest_data/1_min_SPY_2008-2021/` — 13+ years of SPY 1-minute bars, aggregated to 5-min in-engine.
- **Entry point**: `run_spy_intraday()`.
- **Use case**: The longest available intraday baseline. This is the dataset behind the validated Mean Reversion result.

#### `--multi-intraday --tickers SPY,QQQ,XLK,...` (Alpaca 1-min cache)

- **Data**: Parquet cache at `data/cache/{TICKER}/{DATE}_{type}.parquet`, populated by `trading_bot/data/alpaca_downloader.py` from Alpaca's historical API.
- **Entry point**: `run_multi_ticker_intraday()`.
- **Download**: `python -m trading_bot.data.alpaca_downloader --from 2026-01-15 --to 2026-04-15` fetches 1-min intraday bars and 120-day daily bars per ticker.
  - **Critical**: the downloader passes `adjustment=Adjustment.ALL` to Alpaca's `StockBarsRequest`. The default is unadjusted and produced an artifact 50% overnight move in XLU from a split — always use `Adjustment.ALL`.
- **Use case**: Multi-ticker intraday on the current live watchlist (the 13-ticker ETF set), matching live behavior as closely as backtests can.

### Shared CLI Flags

- `--from YYYY-MM-DD` / `--to YYYY-MM-DD` (required): backtest window.
- `--strategies mean_reversion,trend_following`: comma-separated list of which strategies to enable for the run (overrides `enabled` flags in config).
- `--no-regime-filter`: disable the SPY-vs-SMA regime gate for this run.
- `--config path/to/config.yaml`: alternate config file (`config_backtest.yaml` is committed for this).
- `--download`: download data for `--multi-intraday` mode before running.

### Replay Engine

For each bar (daily or intraday depending on mode):

1. Update technical indicators (RSI, EMA, ATR, volume avg, etc. — strategy-specific).
2. Evaluate each enabled strategy's entry signal with shared portfolio-level filters layered on top (regime filter, overnight gap filter in daily mode, etc.).
3. Check exit conditions for each open position (ATR stop, trailing stop, ATR target, RSI exit, or per-strategy fixed exits).
4. Apply position sizing with the strategy's equity curve (Mean Reversion uses ATR-risk sizing; others use fixed-percent).
5. Model slippage as configured (default 2 bps per side). No commission (Alpaca is commission-free).

**Sentiment handling**: use cached sentiment from `sentiment_cache` when available; otherwise neutral (0.0), flagged as simulated.

**Settlement**: T+1 tracked during backtest to reproduce the live cash-account constraint.

### Commission Model

Alpaca is commission-free for US equities. No commission modeling is needed in the backtest engine. Regulatory fees (SEC fee, FINRA TAF) are negligible for small accounts and are absorbed by Alpaca.

### Output

Backtests produce the same report format as live trading:
- Daily P&L table
- Win rate, profit factor, Sharpe ratio
- Max drawdown
- Per-trade detail
- Equity curve
- Clearly marked: "BACKTEST RESULTS - Simulated execution"

### Storage

Backtest results are stored in the `backtest_results` table (see Section 11) with:
- Unique backtest ID (UUID)
- Full parameter set used
- All trades in JSON format
- Summary metrics

### Comparison

The bot can compare backtest results against live results for the same period:
- Expected vs actual P&L
- Win rate deviation
- Slippage analysis (backtest modeled vs actual)
- Useful for validating that the slippage model is realistic

### Current Baselines (Mean Reversion, $1k start)

As of 2026-04-20, with `let_winners_run=true`, `vix_adaptive_rsi=true`, regime filter on, `max_positions=3`:

| Dataset | Window | Result |
|---|---|---|
| SPY 1-min (`--spy`) | 13 years | **+76.09%** total, -11.9% max DD, profit factor 1.54, 60.8% win rate, 102 trades |
| 13-ticker ETF set (`--multi-intraday`) | 2020-07-27 → 2026-04-16 (~6 years) | **+34.37%** total, -13.7% max DD |

These are the post-`let_winners_run` baselines. Earlier pre-trailing-stop variants of the strategy produced lower total return and lower profit factor on the same data.

---

## Section 20: Implementation Priority

Build the system in this order. Each phase depends on the previous ones.

### Priority 1: Core Infrastructure (Days 1-3)
1. **`config.py`**: Load and validate `config.yaml`, provide typed access
2. **`constants.py`**: Define all enums (Phase, HoldType, ExitReason, OrderStatus, etc.)
3. **`db/schema.py`**: Create all SQLite tables from Section 11
4. **`db/repository.py`**: Basic CRUD operations for all tables
5. **`gateway/connection.py`**: Connect to Alpaca API, heartbeat, reconnect logic
6. **`main.py`**: Skeleton entry point, event loop, logging setup
7. **Logging**: Configure stdlib logging with file rotation

### Priority 2: Phase 0 - Portfolio Cleanup (Days 4-6)
8. **`strategy/portfolio_assessor.py`**: Position scoring (0-100), classification
9. **`strategy/technical.py`**: RSI, SMA for Phase 0 assessment (subset of full indicators)
10. **`data/sentiment.py`**: Finnhub sentiment queries for position assessment
11. **`execution/order_manager.py`**: Limit order placement for liquidation sells
12. **`notifications/notifier.py`**: ntfy.sh integration for cleanup plan notification
13. **`gateway/recovery.py`**: Position reconciliation on startup

This is the highest priority after core infra because the user has positions that need cleanup NOW.

### Priority 3: Market Data (Days 7-9)
14. **`data/market_data.py`**: Real-time subscriptions, staleness detection
15. **`data/fx.py`**: GBP/USD rate queries, caching, conversion
16. **`data/earnings.py`**: Earnings calendar fetch, blackout logic
17. **`execution/settlement_tracker.py`**: T+1 tracking, settled cash calculation

### Priority 4: Entry Strategy (Days 10-13)
18. **`strategy/technical.py`**: Full indicator suite (EMA, Bollinger, volume, ATR)
19. **`strategy/entry.py`**: Signal evaluation, all filters, opportunity ranking
20. **`execution/position_sizer.py`**: Phase-aware sizing, all adjustments
21. **Pre-market scan**: Watchlist ranking logic

### Priority 5: Exit Strategy (Days 14-17)
22. **`strategy/exit.py`**: Stop loss, take profit, trailing activation, time stops
23. **`execution/order_manager.py`**: Stop orders, limit orders, trailing stops
24. **Order state machine**: Full state tracking with Alpaca order polling
25. **Wind-down logic**: Session-end position management

### Priority 6: Risk Management (Days 18-20)
26. **`execution/risk_manager.py`**: Daily limits, sector exposure, correlation
28. **Drawdown circuit breaker**: Rolling peak tracking, pause logic
29. **Kill switch**: ntfy subscription listener

### Priority 7: Notifications & Reporting (Days 21-24)
30. **`notifications/notifier.py`**: All notification events from Section 10
31. **`reporting/daily_report.py`**: HTML report generation
32. **`reporting/performance.py`**: Metric calculations (Sharpe, profit factor, etc.)
33. **Jinja2 templates**: Daily and weekly report templates

### Priority 8: Health & Operations (Days 25-27)
34. **`health/server.py`**: HTTP status endpoint
35. **`start_bot.sh`**: Full pre-flight script
36. **`db/migrations.py`**: Schema versioning

### Priority 9: Backtesting (Days 28-32)
37. **`backtest/data_loader.py`**: Historical bar loading from Alpaca
38. **`backtest/engine.py`**: Full replay engine with slippage model (commission-free)
39. **Comparison tools**: Backtest vs live analysis

### Priority 10: Phase Transitions (Days 33-35)
40. **Phase detection on startup**: Check equity, check criteria
41. **Auto-promotion logic**: Monitor criteria continuously, execute transition
42. **Auto-demotion logic**: Monitor for equity drops below threshold
43. **Parameter switching**: Apply new phase parameters dynamically

### Total Estimated Timeline: 5-7 weeks for solo developer

---

## Appendix A: Alpaca Commission Structure

Alpaca provides **commission-free** trading for US equities. This eliminates commission drag entirely, which is a significant advantage at small account sizes.

Regulatory fees (SEC fee, FINRA TAF) are absorbed by Alpaca and not passed through to the trader. There are no per-share, per-order, or percentage-based commissions.

This means:
- No minimum position size constraint from commissions
- Gross P&L equals net P&L
- The bot does not need commission efficiency tracking or commission budget monitoring
- All position sizing is based purely on risk management and available capital

---

## Appendix B: Glossary

| Term | Definition |
|---|---|
| ATR | Average True Range - volatility indicator |
| Bracket order | Entry order with associated stop-loss and take-profit orders (placed as separate orders in Alpaca) |
| EMA | Exponential Moving Average |
| GTC | Good Till Cancel - order remains active until filled or cancelled |
| GICS | Global Industry Classification Standard (sector taxonomy) |
| OCA | One-Cancels-All - order group where filling one cancels the others (not natively supported by Alpaca; managed by the bot) |
| OTC | Over The Counter - non-exchange traded stocks |
| RSI | Relative Strength Index |
| SMA | Simple Moving Average |
| T+1 | Trade date plus 1 business day settlement |
| Trailing stop | Stop order that moves with the price, locking in profit |

---

## Appendix C: Risk Acknowledgments

1. **This bot trades real money.** All safeguards (stop losses, daily limits, circuit breakers) are designed to limit but cannot eliminate losses.
2. **Cash account limitations**: T+1 settlement restricts capital recycling. The bot accounts for this but it limits trade frequency.
3. **FX risk**: USD positions are exposed to GBP/USD fluctuations. Not hedged at this account size.
4. **Gap risk**: Overnight swing positions can gap through stop losses. The -3% swing stop may result in larger actual losses on earnings surprises or market events.
5. **Technology risk**: API disconnections, network outages, and software bugs can cause missed exits. Alpaca server-side stop orders mitigate this.
6. **Liquidity risk**: Even liquid stocks can have wide spreads during extreme events. Emergency market orders may experience significant slippage.
7. **Small account limitations**: At £950, position sizes are small and individual trade profits are modest. The bot prioritises capital preservation and steady compounding.

---

*End of specification. This document is the single source of truth for the trading bot's behavior. All implementation must conform to this spec. Any deviations must be documented and justified.*
