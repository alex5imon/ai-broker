# ai-broker

Autonomous adaptive US equity trading bot. Runs as a stateless tick on a
GitHub Actions cron (every 5 min during NYSE hours) and persists state
in SQLite across runs. Same codebase runs locally for paper/live and for
backtesting.

Targets commission-free US equities on the Alpaca Trading API, starting
on a paper account and graduating through phased risk profiles (Phase 1
→ Phase 2 → Phase 3) as equity and win-rate thresholds are met.

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
scripts/                  dev tooling (smoke tests, evaluators)
backtest_data/            offline datasets (see CLAUDE.md)
```

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # fill in Alpaca creds, FINNHUB_API_KEY, NTFY_TOPIC, NTFY_KILL_TOPIC

# One tick (what GHA runs)
python -m trading_bot.main --mode normal

# Dry-run (no orders placed)
python -m trading_bot.main --mode normal --dry-run

# Pre-flight checks + launch
bash start_bot.sh

# Tests
pytest trading_bot/tests/
```

## Trading Strategies

The bot runs multiple strategy sleeves in parallel, each with its own
virtual sub-portfolio and independent entry/exit logic. Trades are
consolidated at the portfolio level for risk enforcement. All allocations
and parameters are tunable under `multi_strategy.strategies.*` in
[config.yaml](config.yaml).

Live watchlist (Phase 1): SPY + QQQ + the 11 SPDR sector ETFs (XLK, XLF,
XLV, XLY, XLP, XLE, XLI, XLB, XLU, XLRE, XLC) — pure ETFs only, to avoid
earnings-gap risk.

| Sleeve | Status | Allocation | Max Positions | Edge |
|---|---|---|---|---|
| **Mean Reversion** | Primary (validated) | $1,500 | 3 | RSI(14) oversold bounce on liquid ETFs with VIX-adaptive thresholds, ATR stops/targets, let-winners-run trailing. PF 1.54 on 13y SPY 5-min. |
| **Breakout** | Active | $1,500 | 1 | 20-day high breakout with volume confirmation; exit at 10-day low or fixed stop. Highest PF (2.94) sleeve in the 13y SPY backtest. |
| **Overnight Drift** | Active | $1,000 | 1 | Buy on the last 5-min bar of the session, sell on the first bar of the next session. Captures the overnight equity premium with a 3% disaster stop. |
| **Trend Following** | Deprioritized | $1,000 | 1 | EMA 9/21 crossover + SMA(50) trend filter + volume. Retained for backtest comparisons; not intended for live capital. |
| **Sentiment Combo** | Disabled | $0 | — | Finnhub sentiment + technical signal. Disabled 2026-04-24 after two tuning iterations failed to find an edge on ETFs. |

Shared portfolio-level filters apply on top of each sleeve: market regime
filter (no new entries when SPY is below its 50-day SMA), ATR-percentile
volatility gate, earnings blackout, post-exit cooldown, settled-cash
check, spread check, max positions, sector exposure, and (in daily mode)
overnight gap filter.

See [SPEC.md](SPEC.md) Sections 6 and 7 for the full entry / exit logic.

### Risk gates layered on top

These are book-wide protections that apply across all sleeves. Each is
config-driven and can be disabled in [config.yaml](config.yaml).

| Gate | Where | Config | What it does |
|---|---|---|---|
| **Per-symbol allocation cap** | `strategy_manager._enforce_symbol_cap` | `watchlist_caps.per_symbol[ticker]` | Caps total exposure to any single ticker (across all sleeves) at a fraction of the multi-strategy book. Shrinks oversized entries instead of skipping when possible. |
| **Entry limit slop clamp** | `strategy_manager._clamp_limit_price` | `entry.limit_slop_pct` (default `0.002` = 0.2%) | Bounds entry limit prices to within 0.2% of NBBO ask (buys) or bid (sells). Prevents thin-spread chasing when the strategy reference price has drifted past the inside market. |
| **Consecutive-loss cooldown** | `execution.loss_cooldown` | `risk.consecutive_loss_cooldown.{enabled,threshold_losses,cooldown_minutes}` | After N losing trades in a row, pauses ONLY the offending sleeve for M minutes. A profitable trade resets immediately. State persists in `risk_circuit_state` so it survives across stateless ticks. |
| **Macro-event gate (FOMC)** | `data.event_calendar` | `event_gate.{enabled,fomc_action,fomc_size_multiplier,fomc_dates_<year>}` | Reduces or skips new entries on Fed announcement days. `fomc_action: "skip"` blocks all new entries; `"reduce"` scales share counts by `fomc_size_multiplier`. Existing positions keep being managed normally. Update `fomc_dates_<year>` annually from federalreserve.gov. |
| **ATR-anchored stops** | each strategy's `evaluate_entry` | `<strategy>.{use_atr_stops,atr_period,atr_stop_mult,atr_trail_mult}` | Sizes stop and trail distance from realized volatility (ATR) instead of fixed percentages. Mean-reversion uses 2.0×ATR stop / 5.0×ATR target; breakout and trend-following use 1.5×ATR stop / 2.0×ATR trail. Falls back to fixed pct when ATR can't be computed. |

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
via `actions/cache` and uploaded as an artifact on every run so logs,
state, and the DB are inspectable after the fact.

### Required repo secrets

| Secret | Purpose |
|---|---|
| `ALPACA_PAPER_KEY_ID` / `ALPACA_PAPER_SECRET` | Paper trading credentials |
| `ALPACA_LIVE_KEY_ID` / `ALPACA_LIVE_SECRET` | Live trading credentials (only used when `ALPACA_ENV=live`) |
| `FINNHUB_API_KEY` | Market news + sentiment data |
| `NTFY_TOPIC` | ntfy.sh topic for trade alerts. Optional — if unset, push notifications are disabled and the bot still trades. |
| `NTFY_KILL_TOPIC` | ntfy.sh topic the bot subscribes to for the kill switch. Optional. |

> **Important:** ntfy.sh free-tier topics have no auth — anyone with the
> topic name can subscribe (read every alert) or publish (including to
> the kill switch). Generate long random topic names locally before
> setting them as repo secrets, e.g.:
> ```bash
> python -c "import secrets; print('bot-alerts-' + secrets.token_urlsafe(24))"
> ```

### Repo variable

- `ALPACA_ENV` — defaults to `paper`. Set to `live` to flip to live
  trading. The workflow selects the matching key pair automatically.

### Pulling artifacts back locally

```bash
gh run list --workflow=bot.yml --limit 20
gh run download <run-id> --name bot-logs-<run-id> --dir ./logs
gh run download <run-id> --name bot-db-<run-id> --dir ./trading_bot/data
```

### Scheduling via GCP Cloud Scheduler

GitHub Actions throttles `schedule` events under load — sub-15-minute
crons routinely fire only every 60–90 minutes. For a strategy that
trades on 5-minute bars that's unacceptable, so the bot is triggered
from an external cron via `workflow_dispatch`, which fires on demand
without throttling.

The chosen scheduler is **Google Cloud Scheduler** (free tier covers up
to 3 jobs/month, runs from Google's always-on infrastructure).

**Setup recap:**

1. **GitHub fine-grained PAT** — Settings → Developer settings → Fine-grained
   tokens. Scope: this repo only, `Actions: Read and write`. 1-year
   expiration. Set a calendar reminder to rotate.
2. **GCP project** with billing enabled and the Cloud Scheduler API on.
3. **Cloud Scheduler job** in any region:

   | Field | Value |
   |---|---|
   | Frequency | `*/5 13-21 * * 1-5` |
   | Timezone | UTC |
   | Target | HTTP POST |
   | URL | `https://api.github.com/repos/alex5imon/ai-broker/actions/workflows/bot.yml/dispatches` |
   | Body | `{"ref":"main"}` |
   | Headers | `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Authorization: Bearer <PAT>` |
   | Max retry attempts | 0 |
   | Attempt deadline | 30s |

The native GHA `schedule:` block in `bot.yml` is intentionally left in
place as a fallback — if the GCP job ever stalls, the GHA cron will
still fire (slowly) and the heartbeat workdog will notice within 20 min.
The bot's `concurrency: bot-run` group serializes any overlap between
the two trigger paths and ticks are idempotent, so duplicate firings are
harmless.

**Rotation procedure** when the PAT nears expiry:

1. Generate a new fine-grained PAT in GitHub.
2. Update the `Authorization` header on the Cloud Scheduler job.
3. Force-run the job once to confirm it works.
4. Revoke the old PAT.

**Debugging a missed tick:**

- Cloud Scheduler job → Logs tab shows each attempt's HTTP response.
  Common errors: `401 Bad credentials` (PAT expired/wrong),
  `404 Not Found` (PAT lacks `Actions: write` on this repo).
- If the Scheduler call succeeds but no GHA run appears, check
  https://github.com/alex5imon/ai-broker/actions/workflows/bot.yml — a
  `workflow_dispatch` event there means the trigger fired and the bot
  itself is at fault.

## Tick model

Each invocation runs one `tick()` and exits:

1. Trading-day + operating-hours gate.
2. Connect to Alpaca (validate creds).
3. Refresh FX rate.
4. Reconcile broker state with SQLite.
5. Poll outstanding order statuses.
6. Pre-market scan / entry scan / exit check / wind-down —
   whichever windows are active, gated by day-scoped flags.
7. Phase-transition + daily-summary checks once per day.

Per-strategy state (day flags, spread-defer timers, strategy sleeves,
loss-cooldown counters) and global risk state (pause window, drawdown
breaker, daily-loss-limit hit, commission stop, recent rejections) live
in the `tick_state` / `risk_circuit_state` SQLite tables so the next
cron invocation picks up cleanly. There is no long-running process, no
WebSocket stream, and no heartbeat loop.

## Heartbeat

[.github/workflows/heartbeat.yml](.github/workflows/heartbeat.yml) runs
every 30 min during NYSE hours and fails if the last successful `bot`
run is older than 20 min. GitHub emails the repo admin on workflow
failure. Adjust `STALE_MINUTES` in the workflow to taste.
