---
description: Watchlist management - review and adjust LSE and US watchlists with market-specific criteria
---

# Watchlist Management

Review the current watchlists for both LSE and US markets, analyze per-ticker performance, and recommend additions or removals. Each market has different volume, spread, and liquidity criteria.

## Step 1: Read Current Watchlists

Read both watchlists from `config.yaml`:

```bash
cd /Users/alex/Broker
python3 -c "
import yaml
with open('config.yaml') as f:
    config = yaml.safe_load(f)

for market in ['lse', 'us']:
    watchlist = config.get('watchlist', {}).get(market, [])
    print(f'\n{market.upper()} Watchlist ({len(watchlist)} tickers):')
    for ticker in watchlist:
        print(f'  - {ticker}')
"
```

## Step 2: Per-Ticker Performance Analysis

Query the database for per-ticker stats over the last 5-10 trading days, split by market:

```bash
cd /Users/alex/Broker
python3 -c "
import sqlite3
conn = sqlite3.connect('trading_bot/data/trading_bot.db')

for label, where in [('LSE', "exchange = 'LSE'"), ('US', "exchange IN ('NYSE', 'NASDAQ')")]:
    print(f'\n=== {label} Ticker Performance (Last 14 Days) ===')
    cursor = conn.execute(f'''
        SELECT ticker,
               COUNT(*) as total_trades,
               SUM(CASE WHEN pnl_gbp > 0 THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN pnl_gbp <= 0 THEN 1 ELSE 0 END) as losses,
               ROUND(SUM(CASE WHEN pnl_gbp > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*), 1) as win_rate,
               ROUND(SUM(pnl_gbp), 2) as total_pnl_gbp,
               ROUND(SUM(CASE WHEN currency = 'GBP' THEN commission ELSE commission / fx_rate END), 2) as total_comm_gbp,
               ROUND(SUM(pnl_gbp) - SUM(CASE WHEN currency = 'GBP' THEN commission ELSE commission / fx_rate END), 2) as net_pnl_gbp,
               ROUND(AVG(pnl_gbp), 2) as avg_pnl_per_trade
        FROM trades
        WHERE date(entry_time) >= date('now', '-14 days') AND {where}
        GROUP BY ticker
        ORDER BY net_pnl_gbp ASC
    ''')
    print(f'{\"Ticker\":<10} {\"Trades\":>6} {\"Wins\":>5} {\"Losses\":>6} {\"WR%\":>6} {\"Gross\":>9} {\"Comm\":>8} {\"Net\":>9} {\"Avg\":>8}')
    print('-' * 80)
    for row in cursor:
        print(f'{row[0]:<10} {row[1]:>6} {row[2]:>5} {row[3]:>6} {row[4]:>5.1f}% £{row[5]:>8.2f} £{row[6]:>7.2f} £{row[7]:>8.2f} £{row[8]:>7.2f}')

conn.close()
"
```

## Step 3: Identify Tickers to Remove

Flag tickers for removal if they meet ANY of these criteria:

**LSE tickers:**
- Negative net P&L (GBP) over the last 10 trading days
- Win rate below 45% with at least 5 trades
- Average P&L per trade is negative after commissions
- Very low trade count (fewer than 2 trades in 10 days) - poor liquidity or no setups
- Spread consistently too wide (above 1% for micro/small-cap, above 0.3% for large-cap)

**US tickers:**
- Negative net P&L (GBP) over the last 10 trading days
- Win rate below 45% with at least 5 trades
- Average P&L per trade is negative after commissions and FX conversion
- Very low trade count (fewer than 2 trades in 10 days)
- FX conversion consistently eroding profits

Present each removal candidate with its stats and the specific reason.

## Step 4: Identify Candidate Tickers to Add

Suggest potential additions based on market-specific criteria:

**LSE candidates should have:**
- Average daily volume > 500K shares (or appropriate for the price level)
- Spread < 0.5% for small-cap, < 0.2% for FTSE 100/250
- Listed on LSE Main Market only
- GBP-denominated (avoid GBX complexity where possible, or handle pence vs. pounds correctly)
- Good intraday volatility for the strategy type
- Stamp duty consideration: 0.5% SDRT applies to UK shares (factor into profitability)

**US candidates should have:**
- Average daily volume > 1M shares
- Tight spreads (< 0.1% for large-cap)
- In major indices (S&P 500, NASDAQ 100) for reliability
- Good intraday range relative to price
- Consider FX impact: round-trip USD trades have GBP conversion on both legs

Ask the user if they have specific tickers in mind, or suggest well-known liquid names:
- **LSE:** LLOY, BARC, VOD, BP, SHEL, AZN, GSK, RIO, HSBA, LSEG
- **US:** AAPL, MSFT, NVDA, AMD, TSLA, META, AMZN, GOOGL, SPY, QQQ

## Step 5: Present Proposed Changes

Summarize all proposed changes by market:

```
=== LSE Watchlist Changes ===

Removals:
Ticker    Reason                              Net P&L (GBP)  Win Rate
------    ----------------------------------  -------------  --------
XYZ.L     Negative P&L, wide spreads          -£12.30        38.2%

Additions:
Ticker    Reason
------    ----------------------------------
LLOY.L    High liquidity, good volatility, no SDRT issue at this price

Unchanged: [list]

=== US Watchlist Changes ===

Removals:
Ticker    Reason                              Net P&L (GBP)  Win Rate
------    ----------------------------------  -------------  --------
XYZ       FX drag making trades unprofitable  -£8.50         42.0%

Additions:
Ticker    Reason
------    ----------------------------------
NVDA      High volume, tight spreads, good range

Unchanged: [list]
```

## Step 6: Apply Changes (With Approval)

Ask the user to confirm the changes for each market separately. If approved, edit the appropriate watchlist section of `config.yaml` using the Edit tool.

After applying changes, report the new watchlists and their sizes. Remind the user that watchlist changes take effect on the next bot restart or watchlist reload.
