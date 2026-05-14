# Adaptive US Equity Trading Bot — Specification

**Status**: Active
**Account**: Alpaca paper account, USD-denominated
**Repo**: https://github.com/alex5imon/ai-broker

---

## Section 1: Project Overview

### What This Bot Does

This is an **autonomous, adaptive US equity trading bot** that connects to the Alpaca Trading API and runs as a **stateless tick on a GitHub Actions cron** every 5 minutes during NYSE hours. It trades equities on **US exchanges (NYSE/NASDAQ)** commission-free, adapting its strategy as the account grows through defined phases.

This is **not a scalping bot** at the current account size. T+1 cash settlement and modest position values make swing/position trading the only viable approach. As the account grows, the bot automatically shifts toward more active trading via phase transitions.

### Account Details

| Field | Value |
|---|---|
| Broker | Alpaca |
| Account Type | Paper trading (cash, no margin, no PDT rule, T+1 settlement) |
| Currency | USD (trades and reporting) |
| Target Funding | ~$1,000 USD |
| Fractional Shares | Supported (down to 1/1000000) |

### Goal

Steady daily account growth targeting **0.3-0.5% per day**. This sounds modest but compounds to roughly 100% annually. The priority is consistency and capital preservation over aggressive returns.

### Why a Phased Approach

The account size dictates what strategies are viable:

- **Phase 1 (~$1k-$1.5k)**: Small position sizes limit profit potential. Swing/position trading with wide stops and targets is the only viable path.
- **Phase 2 (~$1.5k-$3k)**: Position sizes support more concurrent holdings; shorter holds become viable.
- **Phase 3 (~$3k+)**: Full multi-strategy day trading. Tight stops and rapid turnover.

The bot detects account growth and transitions between phases automatically, adjusting position count, stop distances, hold times, watchlist size, and trade frequency.

### US-Only Rationale

The bot trades exclusively on US exchanges (NYSE/NASDAQ) via Alpaca:
- Commission-free trading eliminates commission drag entirely
- Deep liquidity and tight spreads on US equities
- Single market simplifies scheduling and execution logic
- All trades execute in USD; no FX conversion in P&L

---

## Section 2: Runtime Model

### Stateless GHA Tick

Each `python -m trading_bot.main --mode normal` invocation runs a single `tick()` and exits. Per-tick state (day-scoped flags, spread-defer timers, strategy sleeves, risk circuit breakers) is persisted in the `tick_state` and `risk_circuit_state` SQLite tables, so the next cron invocation resumes where this one left off.

There is **no long-running process**, **no WebSocket stream**, and **no in-process heartbeat loop**. The bot exists only for the duration of one tick.

### Tick Sequence

1. **Trading-day + operating-hours gate** (`config.is_trading_day()` + Alpaca clock).
2. **Connect to Alpaca** and validate credentials.
3. **Reconcile broker state** with the SQLite `positions` and `orders` tables.
4. **Poll outstanding order statuses** (fills, cancels, rejections).
5. **Run the active window**: pre-market scan, entry scan, exit check, or wind-down — gated by day-scoped flags so each window fires at most once per day per phase.
6. **Phase-transition check + daily-summary** once per day.

### Operating Window

| Phase | ET Time | Activity |
|---|---|---|
| Pre-market scan | 09:15 - 09:30 | Scan watchlist, pull sentiment, compute technicals |
| Opening blackout | 09:30 - 09:35 | No trades, opening auction volatility |
| Execution window | 09:35 - 15:50 | Normal entry and exit execution |
| Wind-down | 15:50 - 15:58 | Close intraday-only positions |
| Market close | 16:00 | US closes |

Swing positions (`hold_type = "swing"`) are NOT closed at wind-down — they carry overnight with their stop-loss orders active server-side on Alpaca.

### Holiday & Early Close Handling

The bot reads its own holiday calendar from `config.yaml` (`holidays.us_2026` and `holidays.us_early_close_2026`). On non-trading days, the tick exits cleanly without contacting the broker. On early closes, wind-down runs against the early close time.

### Mid-Day Start Behavior

If a tick fires after the pre-market window:
- **Late start (09:35-15:30 ET)**: skip pre-market scan, run a 15-min indicator warmup, then resume normal entry/exit.
- **Close-only (after 15:30 ET)**: manage existing positions only, no new entries.

---

## Section 3: GitHub Actions Integration

### bot.yml

Runs `python -m trading_bot.main --mode normal` every 5 min on a weekday UTC window covering NYSE 09:30-16:00 ET (`*/5 13-21 * * 1-5`). Real trading-day and market-hour gating happens in code.

State persistence:
- **SQLite DB** (`trading_bot/data/trading_bot.db*`) and **state directory** are restored from `actions/cache@v4` at the start of each run and re-cached at the end.
- Logs, state, and DB are uploaded as `actions/upload-artifact@v4` on every run for inspection.

Concurrency: `concurrency.group: bot-run` with `cancel-in-progress: false` — overlapping cron firings queue rather than cancel.

### heartbeat.yml

Runs every 30 min during NYSE hours and fails if the last successful `bot` run is older than `STALE_MINUTES` (default 20). GitHub emails the repo admin on workflow failure.

### Required Repo Secrets

- `ALPACA_PAPER_KEY_ID`
- `ALPACA_PAPER_SECRET`
- `ALPACA_LIVE_KEY_ID`
- `ALPACA_LIVE_SECRET`

### Repo Variable

- `ALPACA_ENV` — `paper` (default) or `live`. The bot's `trading_bot.env.resolve_alpaca_env()` picks the matching key pair and exports them as `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` for the rest of the codebase.

---

## Section 4: Alpaca Integration

### API Connection

- **Library**: `alpaca-py` (official Alpaca Python SDK)
- **API**: REST via `TradingClient` for orders, positions, and account queries
- **Market Data**: `StockHistoricalDataClient` for OHLCV bars (IEX free feed)
- **Authentication**: env vars `ALPACA_PAPER_KEY_ID` / `ALPACA_PAPER_SECRET` (or LIVE pair), resolved via `trading_bot.env.resolve_alpaca_env()` based on `ALPACA_ENV`.

### Connection Handling

Each tick opens a fresh `TradingClient`. There is no long-running heartbeat — connection failures within a tick log a CRITICAL and exit non-zero, which surfaces as a failed GitHub Actions run (and triggers a heartbeat alert if the failures persist past `STALE_MINUTES`).

### State Recovery on Tick Start

Every tick performs reconciliation:
1. Query Alpaca positions, orders, and account.
2. Read SQLite `positions` (status != CLOSED) and pending orders.
3. Reconcile:
   - Alpaca position not in SQLite: create record, log WARNING, send ntfy alert.
   - SQLite position not in Alpaca: mark CLOSED with reason `reconciliation_mismatch`.
   - Quantity mismatch: trust Alpaca, update SQLite, log discrepancy.
4. Verify protective orders (stop / target / trailing) exist for each open position; replace any that are missing.

Corporate actions (splits, dividends) — Alpaca adjusts positions automatically. Reconciliation logs INFO if notional value is roughly unchanged.

### Order Fill Detection

Each tick polls outstanding order statuses (fills, partial fills, cancels, rejections) once at the top of the tick. There is no in-tick polling loop — fills that happen between ticks are detected on the next tick.

---

## Section 5: Watchlist & Universe

### Phase 1 Live Watchlist (13 symbols)

The live watchlist is **SPY + QQQ + all 11 SPDR sector ETFs**. Pure ETFs only — no individual stocks (avoid earnings-gap risk) and no leveraged ETFs (decay). The set gives enough breadth for sector-rotation filters and stays uniformly liquid on the free IEX data feed.

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

### Watchlist Criteria

- **Average daily volume**: >5M shares
- **Bid-ask spread**: typically <0.05%
- **Price range** (phase-specific, soft filter since fractional shares are supported):
  - Phase 1: ≤ $40
  - Phase 2: ≤ $100
  - Phase 3: no practical limit
- **Market cap**: >$1B
- **Exchange**: NYSE or NASDAQ only. No OTC.
- **Sector diversification**: up to 3 symbols per GICS sector. The risk manager enforces a stricter limit on concurrent positions per sector (1 in Phase 1, 2 in Phase 2, 3 in Phase 3).

### Earnings Blackout

Even though the live universe is pure ETFs, the earnings blackout machinery is retained for Phase 2/3 single-stock additions:
- **Source**: Finnhub `/calendar/earnings`
- **Refresh**: Once per day during the first pre-market scan tick
- **Cache**: `earnings_calendar` SQLite table
- **Window**: 48 hours either side of scheduled earnings

### Phase 2 / Phase 3 Watchlists

Defined in `config.yaml` under `watchlist.us_phase2` and `watchlist.us_phase3`:

- **Phase 2** (6 symbols): SPY, QQQ, XLF, XLE, XLK, XLV
- **Phase 3** (12 symbols): Phase 2 + AAPL, MSFT, NVDA, GOOGL, AMZN, META

Inverse ETFs and options are explicitly out of scope until the multi-strategy framework proves out on ETFs.

---

## Section 6: Strategy Framework

### Overview

Signal generation is a **multi-strategy framework**. Five strategy archetypes are defined; each runs against its own virtual sub-portfolio with independent entry/exit logic. Trades are consolidated at the portfolio level for risk enforcement.

Implementation: `trading_bot/strategy/strategies/` — one module per archetype, each subclassing `StrategyBase`. Configured under `multi_strategy.strategies.*` in `config.yaml`. Total allocation: $5,000 (virtual) across the active sleeves.

### Strategy Roster

| Strategy | Status | Allocation | Max Pos | Rationale |
|---|---|---|---|---|
| Mean Reversion | **Primary (validated)** | $1,500 | 3 | Highest-conviction sleeve; PF 1.54 on 13y SPY 5-min. |
| Breakout | Active | $1,500 | 1 | Highest PF (2.94) on the 13y SPY backtest. |
| Overnight Drift | Active | $1,000 | 1 | Captures the overnight equity premium with a 3% disaster stop. |
| Trend Following | Deprioritized | $1,000 | 1 | Consistent losses in backtests; retained for comparison only. |
| Sentiment Combo | **Disabled (2026-04-24)** | $0 | — | Two tuning iterations failed to find an edge on ETFs. Sentiment signal still consumed by bot-wide entry filters. |

### Strategy 1: Mean Reversion (PRIMARY)

Buy RSI(14) oversold recoveries on highly-liquid ETFs.

**Entry**:
- RSI(14) dipped below the oversold threshold within the last `oversold_lookback` bars (default 5).
- Current RSI has recovered above `rsi_recovery` (default 35).
- Current bar volume > `volume_multiplier` × 20-bar avg (default 1.3×).
- **Optional Bollinger confirmation** (`require_bb_confirm=true`): price touched the lower BB within the last 3 bars and closed back above it.
- **Optional EMA confirm** (`require_ema_confirm=false` by default): close above EMA(9).

**Volatility-adaptive RSI** (`vix_adaptive_rsi=true`): uses 20-day realized vol of daily closes as a free VIX proxy.
- High vol (RV ≥ 25%) → tighten to `rsi_oversold_high_vol` (default 25).
- Low vol (RV ≤ 12%) → loosen to `rsi_oversold_low_vol` (default 30).
- Normal regime → baseline `rsi_oversold` (default 28).

**Exits** (priority order):
1. **ATR stop loss**: `entry − atr_stop_mult × ATR(14)` (default 2× ATR), floored at 3% of entry. Fixed-percent fallback (`stop_loss_pct`) when ATR unavailable.
2. **Trailing stop**: activated at `entry + atr_activation_mult × ATR` (default 2.5× ATR); trails by `atr_trail_mult × ATR` (default 2.5× ATR).
3. **ATR target**: `entry + atr_target_mult × ATR` (default 5× ATR). With `let_winners_run=true` (default), the target becomes the trailing-stop activation trigger rather than a hard exit.
4. **RSI normalization**: exit when RSI crosses above `rsi_exit` (default 55). Disabled once a position is up ≥ `let_winners_run_up_pct` (default 3%).

**Position sizing** (`use_risk_sizing=true`):
- ATR-risk sizing: `shares = (equity × risk_per_trade_pct) / (atr_stop_mult × ATR)`.
- Default `risk_per_trade_pct=0.02` → ~6% max simultaneous risk across 3 positions.
- Clamped by `max_position_pct=0.33` (≈ full deployment with 3 positions).
- Fractional shares enabled — required for SPY/QQQ on $1k capital.

### Strategy 2: Breakout (ACTIVE)

20-day-high breakout with volume; 10-day-low exit.

- **Entry**: close > highest close of last `breakout_period` bars (default 20) AND volume > 1.5× average.
- **Exit**: close < lowest close of last `exit_period` bars (default 10), or 3% stop loss.

### Strategy 3: Overnight Drift (ACTIVE)

Buy on the last 5-min bar of the session, sell on the first bar of the next session.

- **Entry window**: 15:40-15:45 ET (must fire before wind-down at 15:50).
- **Sizing**: `position_pct=0.95` of allocation, fractional shares enabled.
- **Stop**: 3% disaster stop to cap gap-down tail risk.
- **Hold type**: SWING — not auto-closed at wind-down. The strategy's own `evaluate_exit()` closes on the next session's opening bar.

### Strategy 4: Trend Following (DEPRIORITIZED)

EMA(9)/EMA(21) crossover with SMA(50) trend filter, volume confirmation, trailing-stop exit. Backtests consistently show losses; retained for reference and side-by-side comparisons but **NOT intended for live capital**.

- **Entry**: close > SMA(50) AND EMA(9) > EMA(21) (recent crossover) AND volume > 1.5× average.
- **Exit**: trailing stop at `trailing_stop_pct` (default 2.5%) from highest price, or `initial_stop_pct` (3%) initial hard stop.

### Strategy 5: Sentiment Combo (DISABLED)

Disabled 2026-04-24 after two tuning iterations exhausted the parameter search:
- iter 1 baseline: 45 trades, PF 1.05, +2.67% over 13y
- iter 2 tighter: 2 trades, PF 0.79

The hypothesis ("sentiment + 1 technical = tradeable edge on ETFs") didn't validate. Sentiment is still used bot-wide as a risk filter / size modifier (`entry.sentiment_block_threshold`, `sentiment.market_reduce_threshold`). Configuration is left in place for a future re-enable (e.g., archetype switch to earnings-driven single stocks).

### Shared Portfolio-Level Filters

Applied on top of each sleeve before any entry is executed:

1. **Market regime filter** (`multi_strategy.regime_filter`): no new entries when SPY closes below its 50-day SMA. Backtests can disable this with `--no-regime-filter`.
2. **ATR percentile gate**: skip entry when ATR rank > 85th percentile; reduce size by 25% when ATR rank > 70th.
3. **Earnings blackout** (48 hours either side) — applies to Phase 2/3 single-stock additions.
4. **Per-ticker cooldown** (30 min post-exit).
5. **Spread check** (< 0.05%).
6. **Market hours check** (execution window only).
7. **Max positions** (per strategy AND aggregate phase limit).
8. **Sector exposure** (portfolio-level).
9. **Overnight gap filter** (daily mode): skip entries when the overnight gap exceeds a configured threshold.

### Portfolio-Level Position Sizing (Phase 1)

Per-strategy sizing (Mean Reversion uses ATR-risk sizing; others use fixed-percent) runs first. These portfolio-level constraints are applied as an outer bound:

| Parameter | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| Max concurrent positions (aggregate) | 2 | 4 | 8 |
| Max per-position size | 40% | 25% | 15% |
| Risk per trade | 2% | 1.5% | 1% |
| Minimum position value | $50 | $100 | $200 |

When a strategy's own max (e.g., Mean Reversion's `max_positions=3`) exceeds the phase aggregate cap (Phase 1 = 2), the lower number wins.

### Entry Order Type

- **Limit orders only** for entries (never market).
- **Limit price**: ask for liquid names, midpoint for less liquid.
- **Time in force**: DAY.
- **Cancel** if unfilled after 5 minutes.
- **Partial fills**: accept ≥ 50% of intended quantity; cancel remainder.

---

## Section 7: Exit Strategy

### Primary Exit Model (Mean Reversion)

ATR-based stops, targets, and trailing — see Section 6.

### Fixed-Percent Fallback Exits

Used by Trend Following / Breakout / Sentiment Combo, and as fallbacks for Mean Reversion when ATR is unavailable.

### Phase 1 Intraday Exits

For positions with `hold_type = "intraday"`:

| Exit Type | Trigger | Order Type | Priority |
|---|---|---|---|
| Stop loss | -2% from entry | Stop-market | 1 |
| Take profit | +3% from entry | Limit | 2 |
| Trailing stop | Activated at +1.5%; trails at -1% from high | Trailing stop | 3 |
| Time stop | No significant move after 4 hours | Limit at market | 4 |

### Phase 1 Swing Exits

For positions with `hold_type = "swing"`:

| Exit Type | Trigger | Order Type | Priority |
|---|---|---|---|
| Stop loss | -3% from entry | Stop-market (GTC) | 1 |
| Take profit | +5% from entry | Limit (GTC) | 2 |
| Trailing stop | Activated at +2.5%; trails at -1.5% from high | Trailing stop (GTC) | 3 |
| Time stop | Max hold 5 trading days | Limit at market | 4 |

### Exit Priority

1. **Emergency**: kill switch, daily loss limit, drawdown breaker, API outage with open positions → market orders.
2. **Stop loss**: triggered server-side on Alpaca.
3. **Take profit**: triggered server-side on Alpaca.
4. **Trailing stop**: triggered server-side on Alpaca.
5. **Time stop**: bot-initiated.
6. **Wind-down close**: end-of-session forced close for intraday positions.

### Spread-Widening Protection

Before non-emergency exits:
1. If spread > 0.15%: defer up to 2 minutes, rechecking every 15 sec.
2. If spread narrows: exit with limit at midpoint.
3. Otherwise: limit at bid.
4. Emergency exits always use market orders regardless of spread.

The defer state is persisted in `tick_state` so a deferred exit picks up on the next tick.

### Order State Machine

Each position transitions through:

```
SIGNAL_DETECTED → ENTRY_PENDING → POSITION_OPEN → STOP_ACTIVE
                                                      ↓
                                                TRAILING_ACTIVE
                                                      ↓
                                                   CLOSING → CLOSED
```

On any exit trigger, the bot explicitly cancels counterpart orders (stop when target fills, target when stop fills) — Alpaca does not support OCA groups.

---

## Section 8: Risk Management

### Daily Loss Limit

- **Threshold**: -1% of account equity at the start of the trading day.
- **Calculation**: realized + unrealized P&L for positions opened today.
- **When hit**: stop new entries, manage existing positions only, send CRITICAL ntfy, log the event in `daily_summaries`. Persisted in `risk_circuit_state` so subsequent ticks honor close-only mode for the rest of the day.

### Maximum Concurrent Positions

| Phase | Max Positions | Max Per Sector |
|---|---|---|
| Phase 1 | 2 | 1 |
| Phase 2 | 4 | 2 |
| Phase 3 | 8 | 3 |

### Maximum Daily Trades

| Phase | Max Daily Trades |
|---|---|
| Phase 1 | 10 |
| Phase 2 | 25 |
| Phase 3 | 50 |

### Correlation Check

Before entering position B while holding position A:
- Calculate 30-day daily-return correlation between A and B.
- Block entry if correlation > 0.85.

### Kill Switch

- Bot subscribes to a dedicated ntfy topic (the value of `NTFY_KILL_TOPIC`).
- On any message: cancel all pending orders, market-sell all open positions, send confirmation, enter permanent close-only mode (until the kill state is cleared in the DB).

### Drawdown Circuit Breaker

- **Trigger**: account equity drops 5% from its rolling 5-day peak.
- **Action**: close all positions with limit orders, pause trading for 1 trading day, alert via ntfy. Resume with 50% sizing for the first 3 trades after the breaker. State is persisted in `risk_circuit_state` so the pause survives across ticks.

### Order Rejection Handling

- Log rejection reason in the `order_rejections` table.
- Common causes: insufficient funds (settlement), price out of range (adjust to current market), contract not found (remove from watchlist), max order count exceeded (pause 5 min).
- 3+ rejections in 10 minutes → pause new entries for 15 minutes, alert.
- Never retry without modifying the cause.

---

## Section 9: Sentiment & News

### Data Source

All sentiment data comes from the **Finnhub API** (free tier, 60 calls/min rate limit). Even with sentiment_combo disabled, sentiment is still consumed by bot-wide entry filters and as a sizing modifier.

### Individual Stock Sentiment

- **Endpoint**: `GET /news-sentiment?symbol={ticker}`
- **Normalization**: `normalized = (raw_score - 0.5) * 2` → range -1.0 to +1.0
- **Entry threshold**: normalized > 0.1 for longs.
- **Block threshold**: normalized < -0.2 blocks entry entirely.
- **No data**: treated as neutral, proceed with 75% position size.

### Sector / Market Sentiment

- **Sector**: avg normalized sentiment across same-sector watchlist symbols. If avg < -0.1: avoid new entries in that sector.
- **Market**: avg of SPY + QQQ. < -0.2 → halve position sizes; < -0.4 → close-only mode.

### Caching

- Cached in `sentiment_cache` SQLite table (TTL 30 min during market hours).
- On 429: fall back to cache; if no cache, treat as neutral.

---

## Section 10: Notifications (ntfy.sh)

### Configuration

- **Server**: `https://ntfy.sh`
- **Topic**: configurable in `config.yaml`
- **Kill switch topic**: separate

### Notification Events

| Event | Priority |
|---|---|
| Trade entry | Default (3) |
| Position closed | Default (3) |
| Stop loss hit | High (4) |
| Daily loss limit hit | High (4) |
| Trailing stop activated | Low (2) |
| Daily summary | Default (3) |
| Phase transition | High (4) |
| API disconnected | Urgent (5) |
| Mass data staleness | High (4) |
| Kill switch activated | Urgent (5) |
| Drawdown breaker | Urgent (5) |
| Order rejection | Default (3) |
| Excessive rejections | High (4) |
| Bot startup / shutdown | Low (2) |

### Format Example

```
[TRADE ENTRY] SPY @ $512.40
Qty: 0.95 shares ($486.78)
Signals: RSI recovery (28→36) + Volume 1.6x + BB bounce
Sentiment: 0.21 (positive)
Stop: $501.95 (ATR -2x) | Target: $539.46 (ATR +5x)
Hold type: Intraday
```

```
[DAILY SUMMARY] 2026-04-25
Trades: 3 (2W / 1L)
Net P&L: +$5.27
Win rate: 67%
Equity: $1,005.27
Phase: 1
```

---

## Section 11: Database Schema

All data lives in `trading_bot/data/trading_bot.db`. Key tables:

- **trades** — completed trade records (ticker, side, entry/exit price+time, P&L, signals JSON, exit reason, hold type, phase).
- **positions** — currently open positions and their order state machine fields (stop, target, trailing flags, Alpaca order IDs).
- **daily_summaries** — one row per trading day (counts, P&L, win rate, profit factor, phase).
- **sentiment_cache** — per-ticker normalized sentiment with TTL.
- **earnings_calendar** — upcoming earnings dates for blackout management.
- **cooldowns** — per-ticker post-exit cooldown timers.
- **config_snapshots** — full `config.yaml` snapshots when parameters change.
- **order_rejections** — Alpaca rejection log for analysis.
- **phase_transitions** — audit trail for promotions / demotions.
- **backtest_results** — separate from live trades; one row per backtest run.
- **tick_state** — day-scoped flags so a stateless tick knows which windows have already fired today.
- **risk_circuit_state** — circuit-breaker / kill-switch / daily-loss-limit state across ticks.
- **schema_version** — migrations tracking.

See `trading_bot/db/schema.py` for the canonical CREATE TABLE statements and `trading_bot/db/migrations.py` for the version-tracked migration sequence.

---

## Section 12: Reporting

### Daily Report

Generated at the end of each trading day. Rendered as HTML via Jinja2 templates and saved to `~/trading_bot_reports/`.

Contents:
- Header: date, phase, account equity, daily P&L
- Per-trade detail table (ticker, entry/exit, hold time, P&L, exit reason, signals)
- Performance metrics: win rate (today / 7d / 30d rolling), avg win, avg loss, profit factor, expectancy
- Sector breakdown
- Equity curve (last 30 days, inline SVG)
- Phase progress
- Open swing positions

### Weekly / Monthly Reports

Aggregations on top of the daily report — week-over-week comparison, best/worst days, sector rotation, watchlist performance, drawdown analysis, monthly P&L vs the 0.3-0.5%/day target.

### Storage

- Output dir: `~/trading_bot_reports/`
- Naming: `daily_2026-04-25.html`, `weekly_2026-W17.html`, `monthly_2026-04.html`
- Templates: `trading_bot/reporting/templates/`

---

## Section 13: Phase Transitions

### Phase Overview

| Phase | Equity Range | Max Positions | Hold Style | Watchlist |
|---|---|---|---|---|
| 1 | up to ~$1.5k | 2 | Hours to days | 13 ETFs |
| 2 | ~$1.5k - ~$3k | 4 | Minutes to days | 6 ETFs (subset) |
| 3 | ~$3k+ | 8 | Minutes to hours | 12 (ETFs + mega-cap) |

Note: `phases` thresholds in `config.yaml` are USD (the account base currency).

### Phase 1 → Phase 2 Promotion

All criteria must be met simultaneously:

| Criterion | Threshold |
|---|---|
| Account equity | ≥ phase1_to_phase2 threshold |
| Trading days in Phase 1 | ≥ 40 |
| Win rate (last 20 trades) | ≥ 52% |
| Cumulative P&L (last 30 days) | > 0 |
| Daily loss limit breaches (last 20 days) | 0 |

On promotion: log transition, ntfy HIGH alert, apply Phase 2 parameters (4 max positions, 2 max per sector, tighter intraday stops/targets, 25% max position, 1.5% risk per trade, 25 max daily trades), take a config snapshot.

### Phase 2 → Phase 3 Promotion

| Criterion | Threshold |
|---|---|
| Account equity | ≥ phase2_to_phase3 threshold |
| Trading days in Phase 2 | ≥ 60 |
| Win rate (last 40 trades) | ≥ 55% |
| Sharpe ratio (last 60 days) | > 1.0 |
| Cumulative P&L (last 60 days) | > 0 |

On promotion: 8 max positions, 3 max per sector, tight scalping stops, 15% max position, 1% risk per trade, 50 max daily trades, expanded watchlist.

### Phase Demotion

If account equity drops below 80% of the current phase threshold, demote to the previous phase. On demotion: close least-profitable positions until the new phase's max-positions cap is met, apply lower phase parameters, snapshot config, reset the phase timer.

### Phase Detection on Tick Start

Each tick:
1. Check `phase_transitions` for the most recent transition.
2. Verify current equity still qualifies.
3. If equity fell below the demotion threshold, execute demotion.

---

## Section 14: Configuration (config.yaml)

`config.yaml` in the project root is the single source of truth. `config_backtest.yaml` provides backtest-only overrides. The live config wins whenever this spec and the YAML disagree.

Top-level sections:

- `account` — trading mode, phase override
- `alpaca` — paper flag, data feed, retries, reconnect alert threshold
- `market_data` — staleness thresholds, mass-staleness behavior
- `schedule` — pre-market / execution / wind-down windows, timezone, holiday calendar
- `holidays` — annual list of US holidays + early closes
- `watchlist` — `us`, `us_phase2`, `us_phase3`
- `watchlist_criteria` — volume / spread / price / market cap thresholds
- `risk` — daily loss limit, max positions, sector exposure, position sizing, drawdown breaker, order rejection handling
- `strategy` — technical indicators (EMA, Bollinger, volume, ATR)
- `entry` — signal thresholds, sentiment thresholds, spread limits, timeouts, partial-fill behavior, cooldowns
- `exit_intraday` / `exit_swing` / `exit_intraday_phase2` / `exit_intraday_phase3` — fixed-percent exit fallbacks
- `exit_spread_protection` — defer-on-wide-spread parameters
- `notifications` — ntfy topic, priorities, retry behavior
- `phases` — promotion / demotion thresholds
- `reporting` — output dir, daily/weekly/monthly toggles, equity curve window
- `health` — host/port for the health check
- `database` — DB path, backup settings
- `logging` — level, file, rotation
- `backtesting` — slippage bps, default sentiment, bar size
- `multi_strategy` — total allocation, comparison period, regime filter, per-strategy parameters

`phase0` block remains in the YAML for historical reasons but the runtime is a no-op against an empty fresh portfolio.

---

## Section 15: Libraries & Requirements

### Python Requirements (`requirements.txt`)

```
alpaca-py>=0.21.0
pandas>=2.0.0
finnhub-python>=2.4.0
pyyaml>=6.0
requests>=2.31.0
jinja2>=3.1.0
aiohttp>=3.9.0
pytest>=7.0.0
pytest-asyncio>=0.21.0
```

`pandas-ta` was removed in favor of pure-pandas indicator calculations.

### Library Purposes

| Library | Purpose |
|---|---|
| `alpaca-py` | Official Alpaca client. `TradingClient` for REST orders/positions/account; `StockHistoricalDataClient` for OHLCV bars. |
| `pandas` | DataFrame operations for price data, indicator computation, report generation. |
| `finnhub-python` | News sentiment (`/news-sentiment`) and earnings calendar (`/calendar/earnings`). |
| `pyyaml` | Parse `config.yaml`. |
| `requests` | ntfy.sh notifications and synchronous HTTP. |
| `jinja2` | HTML templating for daily/weekly/monthly reports. |
| `aiohttp` | Health-check HTTP server. |
| `pytest`, `pytest-asyncio` | Test framework. |

### Python Version

**Minimum**: Python 3.10. CI / GitHub Actions runs Python 3.12.

---

## Section 16: Architecture (File Tree)

```
trading_bot/
├── __init__.py
├── __main__.py                     # python -m trading_bot entry shim
├── main.py                         # Stateless tick entrypoint, orchestrates US session
├── config.py                       # Load and validate config.yaml
├── constants.py                    # Enums: Phase, HoldType, ExitReason, OrderStatus,
│                                   # MarketSession, Exchange. GICS sector map.
├── env.py                          # resolve_alpaca_env() — picks paper/live key pair
│                                   # based on ALPACA_ENV
├── data_cache.py                   # Parquet cache (load_cached / save_to_cache)
│
├── gateway/
│   ├── connection.py               # Alpaca TradingClient init, retry, reconnect
│   └── recovery.py                 # Tick-start state recovery: query Alpaca,
│                                   # reconcile with SQLite, verify protective orders
│
├── db/
│   ├── schema.py                   # SQLite CREATE TABLE statements
│   ├── migrations.py               # Schema versioning + sequential migrations
│   └── repository.py               # Data access layer
│
├── data/
│   ├── market_data.py              # Historical bars via StockHistoricalDataClient,
│   │                               # bar aggregation, staleness detection
│   ├── sentiment.py                # Finnhub news-sentiment, normalization, caching
│   ├── earnings.py                 # Finnhub earnings calendar + blackout
│   └── alpaca_downloader.py        # Download 1-min + daily bars (Adjustment.ALL)
│
├── strategy/
│   ├── base.py                     # StrategyBase ABC, ExitSignal, StrategyDecision
│   ├── technical.py                # Indicator calcs (RSI, EMA, Bollinger, ATR, SMA, etc.)
│   ├── entry.py                    # Cross-strategy entry evaluation, filter stack
│   ├── exit.py                     # Cross-strategy exit manager
│   ├── strategy_manager.py         # Sleeve registry + multi-strategy orchestration
│   ├── regime_filter.py            # SPY-vs-SMA market regime gate
│   ├── portfolio_assessor.py       # Legacy position-quality scoring (no-op for fresh accounts)
│   └── strategies/                 # Multi-strategy archetypes
│       ├── __init__.py             # create_strategies(cfg) factory
│       ├── mean_reversion.py       # PRIMARY — RSI oversold bounce
│       ├── breakout.py             # ACTIVE — 20-day high breakout
│       ├── overnight_drift.py      # ACTIVE — late-session entry, next-open exit
│       ├── trend_following.py      # DEPRIORITIZED — EMA cross + SMA(50) trend
│       └── sentiment_combo.py      # DISABLED — Finnhub sentiment + technical
│
├── execution/
│   ├── order_manager.py            # Limit orders, stop orders, trailing stops,
│   │                               # state machine, polling-based fill detection,
│   │                               # rejection handling
│   ├── position_sizer.py           # Phase-aware sizing, ATR/sentiment adjustments
│   ├── risk_manager.py             # Daily P&L, sector exposure, correlation,
│   │                               # max positions/trades, drawdown breaker,
│   │                               # kill switch
│   └── virtual_portfolio.py        # Per-strategy virtual sub-portfolios
│
├── notifications/
│   └── notifier.py                 # ntfy.sh POSTs, priority levels, retry,
│                                   # kill switch listener
│
├── health/
│   └── server.py                   # aiohttp /health endpoint (used during local runs)
│
├── reporting/
│   ├── daily_report.py             # Generate daily HTML report
│   ├── performance.py              # Win rate, profit factor, Sharpe, expectancy
│   ├── strategy_comparison.py      # Side-by-side strategy comparison
│   └── templates/                  # Jinja2 templates
│
├── multi_strategy_backtest.py      # CLI backtester with three modes:
│                                   # --daily, --spy, --multi-intraday
├── backtest.py                     # Legacy single-day Alpaca-cache backtester
│
├── data/                           # Runtime data (not Python package)
│   ├── trading_bot.db
│   └── backups/
├── logs/
│   └── bot.log
└── tests/                          # pytest suite
```

---

## Section 17: Local Startup Script (start_bot.sh)

`start_bot.sh` is a manual / local pre-flight + launch script. It is **not** invoked by GitHub Actions — GHA runs `python -m trading_bot.main` directly.

The script:
1. Activates `.venv`.
2. Loads `.env` (if present).
3. Confirms it's a trading day via `Config.is_trading_day()`.
4. Validates `config.yaml` parses and prints a summary (phase, watchlist size).
5. Resolves Alpaca credentials via `trading_bot.env.resolve_alpaca_env()` and pings the REST API.
6. Sends a startup ntfy notification.
7. Launches the bot, tee'ing output to `logs/bot_$(date +%Y-%m-%d).log`.
8. Sends a shutdown notification on exit.

For day-to-day operation, the GitHub Actions cron is the authoritative runtime.

---

## Section 18: Order Execution & Fill Realism

### Order Execution Approach

- **Entries**: limit orders only. Limit price set at current ask (liquid) or midpoint (less liquid). TIF: DAY. Cancel after 5 min unfilled.
- **Stop losses**: stop-market orders, server-side on Alpaca.
- **Take profits**: limit orders, server-side on Alpaca.
- **Trailing stops**: native Alpaca trailing-stop orders (percentage trail).
- **Emergency exits**: market orders. Reserved for kill switch, daily loss limit, drawdown breaker, end-of-day forced close after wind-down.

### Slippage Tracking

For every completed trade:
- `signal_price`, `fill_price`, `slippage = fill_price - signal_price`, `slippage_bps`.
- Rolling avg over the last 20 trades. > 10 bps → ntfy alert.

### Backtesting Slippage Model

- Default 2 bps per side (entry and exit).
- Configurable via `backtesting.slippage_bps_per_side`.
- Applied unfavorably (higher fill on long entries, lower on long exits).

---

## Section 19: Backtesting Module

### Purpose

`trading_bot/multi_strategy_backtest.py` replays historical data through the full strategy logic to evaluate parameter changes before committing capital.

Each enabled sleeve runs independently against its own virtual sub-portfolio. The engine walks bars chronologically, evaluates every strategy's entry/exit logic per bar, tracks per-strategy equity curves, and produces a side-by-side comparison report.

### Backtest Modes

#### `--daily` (S&P 500 daily bars)

- **Data**: `backtest_data/individual_stocks_5yr/` (5y S&P 500 Kaggle dataset).
- **Bar size**: Daily.
- **Filters**: `--min-volume`, `--min-price`, `--max-price`.
- **Use**: Multi-year evaluations across a broad universe of individual stocks.

#### `--spy` (SPY 5-min intraday)

- **Data**: `backtest_data/1_min_SPY_2008-2021/` — 13+ years of 1-min SPY bars, aggregated to 5-min in-engine.
- **Use**: Longest available intraday baseline. The dataset behind the validated Mean Reversion result.

#### `--multi-intraday --tickers SPY,QQQ,XLK,...`

- **Data**: parquet cache at `data/cache/{TICKER}/{DATE}_{type}.parquet`, populated by `trading_bot/data/alpaca_downloader.py`.
- **Critical**: the downloader passes `adjustment=Adjustment.ALL` — the default produces split artifacts.
- **Use**: Multi-ticker intraday on the live 13-ticker watchlist, matching live behavior as closely as backtests can.

### Shared CLI Flags

- `--from YYYY-MM-DD` / `--to YYYY-MM-DD` (required).
- `--strategies mean_reversion,breakout,...` to filter sleeves.
- `--no-regime-filter` to disable the SPY-vs-SMA gate.
- `--config path/to/config.yaml` (`config_backtest.yaml` is committed for backtest-only overrides).
- `--download` to fetch data for `--multi-intraday` before running.

### Replay Engine

For each bar:
1. Update technical indicators per strategy.
2. Evaluate each enabled strategy's entry signal with shared portfolio-level filters.
3. Check exit conditions for each open position.
4. Apply position sizing on the strategy's own equity curve.
5. Model slippage (default 2 bps/side). No commission.

Sentiment: cached values when available; neutral otherwise (flagged as simulated).

### Output

- Daily P&L table
- Win rate, profit factor, Sharpe, max drawdown, expectancy
- Per-trade detail
- Equity curve
- Side-by-side per-strategy comparison
- Marked: "BACKTEST RESULTS - Simulated execution"

Results stored in the `backtest_results` table with backtest UUID, full parameter set, and trades JSON.

### Current Baselines (Mean Reversion, $1k start)

As of 2026-04-20, with `let_winners_run=true`, `vix_adaptive_rsi=true`, regime filter on, `max_positions=3`:

| Dataset | Window | Result |
|---|---|---|
| SPY 1-min (`--spy`) | 13 years | **+76.09%** total, -11.9% max DD, PF 1.54, 60.8% win rate, 102 trades |
| 13-ticker ETF set (`--multi-intraday`) | 2020-07-27 → 2026-04-16 (~6 years) | **+34.37%** total, -13.7% max DD |

These are the post-let-winners-run baselines. Earlier pre-trailing variants produced lower total return and lower profit factor on the same data.

---

## Section 20: Implementation Status

The bot is feature-complete and validated in backtest. The remaining work is:

1. ✅ Migrate to Alpaca, commission-free.
2. ✅ Multi-strategy framework with 5 sleeves (4 active, 1 disabled).
3. ✅ Stateless GHA tick model with persisted state.
4. ✅ Validated Mean Reversion baseline on 13y SPY.
5. ✅ Regime filter bug fixed (was blocking 0 entries; now correctly blocks bearish-period entries).
6. **In progress**: live paper-trading validation against the GHA cron — verify slippage, signal reliability, long holds match backtest behavior.
7. **Pending**: live trading — only after the paper run shows consistent positive P&L.

---

## Appendix A: Alpaca Commission Structure

Alpaca provides **commission-free** trading for US equities. Regulatory fees (SEC, FINRA TAF) are absorbed by Alpaca. There are no per-share, per-order, or percentage commissions. Gross P&L equals net P&L; the bot does not track commission efficiency or budget commissions.

---

## Appendix B: Glossary

| Term | Definition |
|---|---|
| ATR | Average True Range — volatility indicator |
| Bracket order | Entry order with associated stop-loss and take-profit (separate orders in Alpaca) |
| EMA | Exponential Moving Average |
| GICS | Global Industry Classification Standard (sector taxonomy) |
| GTC | Good Till Cancel |
| OCA | One-Cancels-All — not natively supported by Alpaca; managed by the bot |
| OTC | Over The Counter — non-exchange traded stocks |
| RSI | Relative Strength Index |
| SMA | Simple Moving Average |
| T+1 | Trade date plus 1 business day settlement |
| Trailing stop | Stop order that moves with the price, locking in profit |

---

## Appendix C: Risk Acknowledgments

1. **Real money trades when running on the live key pair.** All safeguards (stop losses, daily limits, circuit breakers) limit but cannot eliminate losses.
2. **Cash account limitations**: T+1 settlement restricts capital recycling.
3. **Gap risk**: overnight swing positions can gap through stop losses.
4. **Technology risk**: API outages, GitHub Actions outages, and bugs can cause missed exits. Server-side stop orders on Alpaca mitigate this.
5. **Liquidity risk**: even liquid ETFs can have wide spreads during extreme events. Emergency market orders may experience significant slippage.
6. **Small account limitations**: at ~$1k, position sizes are small and individual trade profits are modest. The bot prioritises capital preservation and steady compounding.

---

*End of specification. The live config wins whenever this document and `config.yaml` disagree. Any deviations from this spec must be documented and justified.*
