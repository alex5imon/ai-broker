# Broker bot

Minimal Alpaca trading bot skeleton. Same code runs locally (paper/live/tuning) and on GitHub Actions cron.

## Layout

```
bot/
  config.py          env loading
  alpaca_client.py   thin Alpaca trading wrapper
  data.py            Alpaca bar-data fetching
  calendar.py        NYSE hours + bar timestamp snapping
  state.py           JSON state persistence
  strategy.py        pure decide() — replace with real logic
  logging_setup.py   JSONL logs
  run.py             live/paper entrypoint
  backtest.py        tuning entrypoint
.github/workflows/bot.yml
.github/workflows/heartbeat.yml
```

## Local setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in paper keys
python -m bot.run      # one tick
python -m bot.backtest --start 2026-01-02T14:30 --end 2026-01-02T20:00
```

`.env` with `ALPACA_ENV=paper` → paper trading. Flip to `live` only when ready.

## GitHub Actions

Runs every 5 min in a weekday UTC window. Code gates actual trading with `calendar.py` + Alpaca clock.

Repo secrets required (both pairs):
- `ALPACA_PAPER_KEY_ID`
- `ALPACA_PAPER_SECRET`
- `ALPACA_LIVE_KEY_ID`
- `ALPACA_LIVE_SECRET`

Repo variable:
- `ALPACA_ENV` — defaults to `paper`. Set to `live` to flip to live trading. The workflow selects the matching key pair automatically.

## Pulling logs back locally

```bash
gh run list --workflow=bot.yml --limit 20
gh run download <run-id> --name bot-logs-<run-id> --dir ./logs
gh run download <run-id> --name bot-state-<run-id> --dir ./state
```

Logs are JSONL — one event per line. Grep, or load into pandas:

```python
import pandas as pd
df = pd.read_json("logs/run-YYYYMMDDTHHMM.jsonl", lines=True)
```

## Caveat mitigations (matches prior discussion)

| Caveat | Mitigation in this skeleton |
|---|---|
| Cron delays/skips | 5-min cadence; `last_bar_timestamp` snaps to 15-min bar so delayed runs still process the right bar |
| No in-memory state | Alpaca is source of truth for positions; `state/*.json` persisted as GHA artifact + cache |
| Cold start | `actions/setup-python` with pip cache |
| Double-firing | Deterministic `client_order_id` per (strategy, symbol, side, bar_ts) — Alpaca rejects duplicates |
| Holidays / DST | `pandas_market_calendars` NYSE calendar + Alpaca `/v2/clock` double-check |

## Heartbeat

[.github/workflows/heartbeat.yml](.github/workflows/heartbeat.yml) runs every 30 min during market hours and fails if no successful `bot` run has occurred in the last 20 min. GitHub emails the repo admin on failure. Adjust `STALE_MINUTES` in the workflow to taste.

## Next steps

1. Implement real logic in [bot/strategy.py](bot/strategy.py) `decide()` — pure function of `(bar_ts, positions, bars)`, must be deterministic.
2. Expand `SYMBOLS` and tune `LOOKBACK_MINUTES` in [bot/strategy.py](bot/strategy.py).
3. Flip `ALPACA_ENV=live` once paper results are convincing.
