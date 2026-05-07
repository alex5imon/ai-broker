"""Tests for the data layer: market_data, sentiment, earnings.

External APIs (Alpaca, Finnhub) are stubbed; SQLite uses tmp_db_path.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import TZ_EASTERN
from trading_bot.data.earnings import EarningsCalendar
from trading_bot.data.market_data import (
    MarketDataManager,
    MarketDataSubscription,
)
from trading_bot.data.sentiment import SentimentAnalyzer, _RateLimiter

ET = TZ_EASTERN


# ===========================================================================
# MarketDataManager
# ===========================================================================


def _make_md(notifier, monkeypatch=None) -> MarketDataManager:
    if monkeypatch is not None:
        monkeypatch.setenv("ALPACA_API_KEY", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    gateway = MagicMock()
    cfg = {
        "market_data": {
            "staleness_threshold_seconds": 30,
            "pause_on_mass_staleness": True,
            "mass_staleness_pct": 0.5,
            "mass_staleness_resume_pct": 0.25,
        },
        "alpaca": {"data_feed": "iex"},
    }
    return MarketDataManager(gateway, cfg, notifier)


@pytest.mark.asyncio
async def test_subscribe_creates_cache_entry(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    quote = MagicMock()
    quote.bid_price = 9.95
    quote.ask_price = 10.05
    md._historical_client.get_stock_latest_quote = MagicMock(return_value={"SPY": quote})
    await md.subscribe("SPY", "US")
    assert "SPY" in md._subscriptions
    assert md.get_latest_price("SPY") == pytest.approx(10.0)
    assert md.get_bid_ask("SPY") == (9.95, 10.05)


@pytest.mark.asyncio
async def test_subscribe_idempotent(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._historical_client.get_stock_latest_quote = MagicMock(return_value={})
    await md.subscribe("SPY", "US")
    await md.subscribe("SPY", "US")
    # Second call should short-circuit (mock called once via subscribe path).
    assert md._historical_client.get_stock_latest_quote.call_count == 1


@pytest.mark.asyncio
async def test_subscribe_swallows_quote_error(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._historical_client.get_stock_latest_quote = MagicMock(
        side_effect=RuntimeError("403 IEX paper")
    )
    await md.subscribe("SPY", "US")
    # Subscription still created, just no quote.
    assert "SPY" in md._subscriptions
    assert md.get_latest_price("SPY") is None


@pytest.mark.asyncio
async def test_unsubscribe(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._historical_client.get_stock_latest_quote = MagicMock(return_value={})
    await md.subscribe("SPY", "US")
    await md.unsubscribe("SPY")
    assert "SPY" not in md._subscriptions
    # unsubscribe of unknown is a no-op
    await md.unsubscribe("UNKNOWN")


@pytest.mark.asyncio
async def test_unsubscribe_all_clears(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._historical_client.get_stock_latest_quote = MagicMock(return_value={})
    await md.subscribe("SPY", "US")
    await md.subscribe("QQQ", "US")
    await md.unsubscribe_all()
    assert md._subscriptions == {}


@pytest.mark.asyncio
async def test_refresh_quotes_updates_cache(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    spy_q = MagicMock()
    spy_q.bid_price = 100.00
    spy_q.ask_price = 100.10
    qqq_q = MagicMock()
    qqq_q.bid_price = 200.00
    qqq_q.ask_price = 200.20
    md._historical_client.get_stock_latest_quote = MagicMock(
        return_value={"SPY": spy_q, "QQQ": qqq_q}
    )
    await md.refresh_quotes(["SPY", "QQQ"])
    assert md.get_latest_price("SPY") == pytest.approx(100.05)
    assert md.get_latest_price("QQQ") == pytest.approx(200.10)


@pytest.mark.asyncio
async def test_refresh_quotes_empty_list_noops(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._historical_client.get_stock_latest_quote = MagicMock()
    await md.refresh_quotes([])
    md._historical_client.get_stock_latest_quote.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_quotes_swallows_api_error(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._historical_client.get_stock_latest_quote = MagicMock(
        side_effect=RuntimeError("API down")
    )
    # Should not raise.
    await md.refresh_quotes(["SPY"])


@pytest.mark.asyncio
async def test_refresh_quotes_recovers_stale(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._subscriptions["SPY"] = MarketDataSubscription(
        ticker="SPY", exchange="US", is_stale=True, excluded=True,
    )
    q = MagicMock()
    q.bid_price = 100.0
    q.ask_price = 100.10
    md._historical_client.get_stock_latest_quote = MagicMock(return_value={"SPY": q})
    await md.refresh_quotes(["SPY"])
    assert md._subscriptions["SPY"].is_stale is False
    assert md._subscriptions["SPY"].excluded is False


def test_get_bid_ask_missing_returns_none(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    assert md.get_bid_ask("UNKNOWN") is None


def test_get_bid_ask_zero_bid_returns_none(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._subscriptions["SPY"] = MarketDataSubscription(
        ticker="SPY", exchange="US", bid=0.0, ask=10.0,
    )
    assert md.get_bid_ask("SPY") is None


def test_get_spread_pct(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._subscriptions["SPY"] = MarketDataSubscription(
        ticker="SPY", exchange="US", bid=100.0, ask=100.10,
    )
    spread = md.get_spread_pct("SPY")
    assert spread == pytest.approx(0.001, rel=0.05)


def test_get_spread_pct_missing_returns_none(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    assert md.get_spread_pct("UNKNOWN") is None


def test_get_volume(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._subscriptions["SPY"] = MarketDataSubscription(
        ticker="SPY", exchange="US", volume=1000,
    )
    assert md.get_volume("SPY") == 1000


def test_get_volume_zero_returns_none(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._subscriptions["SPY"] = MarketDataSubscription(ticker="SPY", exchange="US")
    assert md.get_volume("SPY") is None
    assert md.get_volume("UNKNOWN") is None


def test_get_ticker_object(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._subscriptions["SPY"] = MarketDataSubscription(ticker="SPY", exchange="US")
    assert md.get_ticker_object("SPY") is not None
    assert md.get_ticker_object("UNKNOWN") is None


def test_is_stale_when_pause_disabled_returns_false(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._pause_on_staleness = False
    md._subscriptions["SPY"] = MarketDataSubscription(
        ticker="SPY", exchange="US", is_stale=True,
    )
    assert md.is_stale("SPY") is False


def test_is_stale_unknown_ticker_returns_false(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    assert md.is_stale("UNKNOWN") is False


def test_get_stale_and_excluded_symbols(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._subscriptions["S1"] = MarketDataSubscription(
        ticker="S1", exchange="US", is_stale=True,
    )
    md._subscriptions["S2"] = MarketDataSubscription(
        ticker="S2", exchange="US", is_stale=True, excluded=True,
    )
    md._subscriptions["S3"] = MarketDataSubscription(ticker="S3", exchange="US")
    assert md.get_stale_symbols() == ["S1"]
    assert md.get_excluded_symbols() == ["S2"]


def test_parse_bar_size_known_values(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    # Just exercise the static method paths
    for bs in ("1 min", "5 mins", "15 mins", "1 hour", "1 day", "unknown"):
        md._parse_bar_size(bs)


def test_parse_duration_known_units(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    for d in ("1 D", "2 W", "1 M", "garbage", "5 X"):
        md._parse_duration(d)


@pytest.mark.asyncio
async def test_get_historical_bars_parses_response(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    bar = MagicMock()
    bar.open = 10.0
    bar.high = 10.5
    bar.low = 9.5
    bar.close = 10.2
    bar.volume = 100_000
    bar.timestamp = datetime(2026, 1, 1, tzinfo=ET)
    response = MagicMock()
    response.data = {"SPY": [bar]}
    md._historical_client.get_stock_bars = MagicMock(return_value=response)
    bars = await md.get_historical_bars("SPY", bar_size="5 mins", duration="2 D")
    assert len(bars) == 1
    assert bars[0]["close"] == 10.2


@pytest.mark.asyncio
async def test_get_historical_bars_empty_returns_empty(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    response = MagicMock()
    response.data = {"SPY": []}
    md._historical_client.get_stock_bars = MagicMock(return_value=response)
    assert await md.get_historical_bars("SPY") == []


@pytest.mark.asyncio
async def test_get_historical_bars_swallows_error(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._historical_client.get_stock_bars = MagicMock(
        side_effect=RuntimeError("API")
    )
    assert await md.get_historical_bars("SPY") == []


@pytest.mark.asyncio
async def test_check_staleness_marks_stale(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    long_ago = datetime.now(tz=ET) - timedelta(seconds=120)
    md._subscriptions["SPY"] = MarketDataSubscription(
        ticker="SPY", exchange="US", last_tick_time=long_ago,
    )
    await md._check_staleness()
    assert md._subscriptions["SPY"].is_stale is True


@pytest.mark.asyncio
async def test_check_staleness_mass_pause_triggers_notification(
    mock_notifier, monkeypatch
):
    md = _make_md(mock_notifier, monkeypatch)
    long_ago = datetime.now(tz=ET) - timedelta(seconds=120)
    for sym in ("A", "B", "C", "D"):
        md._subscriptions[sym] = MarketDataSubscription(
            ticker=sym, exchange="US", last_tick_time=long_ago,
        )
    await md._check_staleness()
    assert md.trading_paused is True
    mock_notifier.send.assert_awaited()


@pytest.mark.asyncio
async def test_check_staleness_resume_after_recovery(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    md._trading_paused = True
    now = datetime.now(tz=ET)
    # Mostly fresh, only one stale → ratio 0.25 < resume_pct 0.25? Use 0.0
    for sym in ("A", "B", "C", "D"):
        md._subscriptions[sym] = MarketDataSubscription(
            ticker=sym, exchange="US", last_tick_time=now,
        )
    await md._check_staleness()
    assert md.trading_paused is False


@pytest.mark.asyncio
async def test_staleness_monitor_is_noop(mock_notifier, monkeypatch):
    md = _make_md(mock_notifier, monkeypatch)
    # Just runs without error.
    await md.staleness_monitor()
    md.stop_monitor()


# ===========================================================================
# SentimentAnalyzer
# ===========================================================================


def _make_sentiment(tmp_db_path: str, monkeypatch, with_key: bool = True) -> SentimentAnalyzer:
    if with_key:
        monkeypatch.setenv("FINNHUB_API_KEY", "fake-key")
    else:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    return SentimentAnalyzer({}, tmp_db_path)


def test_normalize_score_clamps_and_centers():
    assert SentimentAnalyzer._normalize_score(0.5) == 0.0
    assert SentimentAnalyzer._normalize_score(1.0) == 1.0
    assert SentimentAnalyzer._normalize_score(0.0) == -1.0
    # Clamp
    assert SentimentAnalyzer._normalize_score(2.0) == 1.0
    assert SentimentAnalyzer._normalize_score(-1.0) == -1.0


def test_no_finnhub_key_disables_client(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch, with_key=False)
    assert sa._client is None


@pytest.mark.asyncio
async def test_get_sentiment_no_client_returns_none(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch, with_key=False)
    assert await sa.get_sentiment("SPY") is None


@pytest.mark.asyncio
async def test_get_sentiment_caches_after_fetch(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    sa._client.news_sentiment = MagicMock(return_value={
        "sentiment": {"companyNewsScore": 0.75},
    })
    s1 = await sa.get_sentiment("SPY")
    assert s1 == pytest.approx(0.5)  # (0.75 - 0.5) * 2
    # Second call: should hit cache (no new API call)
    s2 = await sa.get_sentiment("SPY")
    assert s2 == pytest.approx(0.5)
    assert sa._client.news_sentiment.call_count == 1


@pytest.mark.asyncio
async def test_get_sentiment_handles_rate_limit(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    sa._client.news_sentiment = MagicMock(side_effect=RuntimeError("429 rate limit"))
    assert await sa.get_sentiment("SPY") is None


@pytest.mark.asyncio
async def test_get_sentiment_handles_generic_api_error(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    sa._client.news_sentiment = MagicMock(side_effect=RuntimeError("500 server error"))
    assert await sa.get_sentiment("SPY") is None


@pytest.mark.asyncio
async def test_get_sentiment_empty_response(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    sa._client.news_sentiment = MagicMock(return_value={})
    assert await sa.get_sentiment("SPY") is None


@pytest.mark.asyncio
async def test_get_sentiment_top_level_score(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    # No nested 'sentiment' key, only top-level companyNewsScore
    sa._client.news_sentiment = MagicMock(return_value={"companyNewsScore": 0.8})
    s = await sa.get_sentiment("SPY")
    # Raw 0.8 normalised → (0.8 - 0.5) * 2 = 0.6
    assert s == pytest.approx(0.6)


@pytest.mark.asyncio
async def test_clear_cache(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    sa._cache_score("SPY", 0.5, 0.75, "finnhub_news")
    sa.clear_cache()
    assert sa._get_cached_score("SPY") is None


def test_market_thresholds(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    sa._market_close_only_threshold = -0.4
    sa._market_reduce_threshold = -0.2
    sa._sector_block_threshold = -0.1
    assert sa.is_market_close_only(-0.5) is True
    assert sa.is_market_close_only(-0.3) is False
    assert sa.is_market_reduced_size(-0.3) is True
    assert sa.is_market_reduced_size(0.0) is False
    assert sa.is_sector_blocked(-0.2) is True
    assert sa.is_sector_blocked(0.0) is False


@pytest.mark.asyncio
async def test_refresh_all_iterates(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    sa._client.news_sentiment = MagicMock(return_value={"companyNewsScore": 0.6})
    await sa.refresh_all(["AAPL", "MSFT"])
    # Each ticker fetched once + market symbols (SPY, QQQ default)
    assert sa._client.news_sentiment.call_count >= 2


@pytest.mark.asyncio
async def test_get_market_sentiment_aggregates(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    sa._client.news_sentiment = MagicMock(
        return_value={"sentiment": {"companyNewsScore": 0.75}},
    )
    avg = await sa.get_market_sentiment()
    assert avg == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_get_sector_sentiment_no_data(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    # No watchlist → 0.0
    assert await sa.get_sector_sentiment("Energy") == 0.0


@pytest.mark.asyncio
async def test_rate_limiter_acquires_immediately_under_quota():
    rl = _RateLimiter(max_calls=2, period_seconds=10.0)
    await rl.acquire()
    await rl.acquire()
    assert len(rl._timestamps) == 2


def test_get_cached_score_handles_invalid_timestamp(
    tmp_db_path: str, monkeypatch
):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    # Insert a row with a garbage timestamp
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO sentiment_cache (ticker, score, raw_score, source, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            ("X", 0.5, 0.75, "finnhub_news", "not-a-date"),
        )
        conn.commit()
    assert sa._get_cached_score("X") is None


def test_get_cached_score_stale_returns_none(tmp_db_path: str, monkeypatch):
    sa = _make_sentiment(tmp_db_path, monkeypatch)
    sa._cache_ttl_minutes = 1
    old_ts = (datetime.now(tz=ET) - timedelta(minutes=60)).isoformat()
    with sqlite3.connect(tmp_db_path) as conn:
        conn.execute(
            "INSERT INTO sentiment_cache (ticker, score, raw_score, source, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            ("X", 0.5, 0.75, "finnhub_news", old_ts),
        )
        conn.commit()
    assert sa._get_cached_score("X") is None


# ===========================================================================
# EarningsCalendar
# ===========================================================================


def _make_earnings(tmp_db_path: str, monkeypatch, with_key: bool = True) -> EarningsCalendar:
    if with_key:
        monkeypatch.setenv("FINNHUB_API_KEY", "fake-key")
    else:
        monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    return EarningsCalendar({"entry": {"earnings_blackout_hours": 48}}, tmp_db_path)


def test_earnings_no_finnhub_key_disables_client(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch, with_key=False)
    assert ec._client is None


def test_is_in_blackout_when_no_record(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch)
    now = datetime.now(tz=ET)
    assert ec.is_in_blackout("AAPL", now) is False


def _insert_earnings(db_path: str, ticker: str, earnings_date: str,
                     earnings_hour: str | None = None) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO earnings_calendar "
            "(ticker, earnings_date, earnings_hour, fetched_at) VALUES (?, ?, ?, ?)",
            (ticker, earnings_date, earnings_hour, datetime.now(tz=ET).isoformat()),
        )
        conn.commit()


def test_is_in_blackout_within_window(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch)
    today = date.today()
    _insert_earnings(tmp_db_path, "AAPL", today.isoformat(), "amc")
    # Now is "today" — definitely within ±48h of today's earnings
    assert ec.is_in_blackout("AAPL", datetime.now(tz=ET)) is True


def test_is_in_blackout_outside_window(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch)
    far_future = (date.today() + timedelta(days=30)).isoformat()
    _insert_earnings(tmp_db_path, "AAPL", far_future, "bmo")
    assert ec.is_in_blackout("AAPL", datetime.now(tz=ET)) is False


def test_is_in_blackout_skips_invalid_date(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch)
    _insert_earnings(tmp_db_path, "AAPL", "not-a-date", "bmo")
    # Bad row is skipped, no other rows → not in blackout
    assert ec.is_in_blackout("AAPL", datetime.now(tz=ET)) is False


def test_get_next_earnings_returns_date(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch)
    future = (date.today() + timedelta(days=5)).isoformat()
    _insert_earnings(tmp_db_path, "AAPL", future, "bmo")
    nxt = ec.get_next_earnings("AAPL")
    assert nxt is not None
    assert nxt.isoformat() == future


def test_get_next_earnings_none_when_no_data(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch)
    assert ec.get_next_earnings("UNKNOWN") is None


def test_get_blackout_tickers_filters(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch)
    today = date.today().isoformat()
    far = (date.today() + timedelta(days=30)).isoformat()
    _insert_earnings(tmp_db_path, "AAPL", today, "amc")
    _insert_earnings(tmp_db_path, "MSFT", far, "bmo")
    blocked = ec.get_blackout_tickers(["AAPL", "MSFT"], datetime.now(tz=ET))
    assert blocked == ["AAPL"]


def test_match_ticker_handles_dot_suffix():
    assert EarningsCalendar._match_ticker("BP", ["BP.", "AAPL"]) == "BP."
    assert EarningsCalendar._match_ticker("AAPL", ["AAPL"]) == "AAPL"
    # No match → returns finnhub symbol
    assert EarningsCalendar._match_ticker("ZZZ", ["AAPL"]) == "ZZZ"


@pytest.mark.asyncio
async def test_refresh_no_client_short_circuits(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch, with_key=False)
    # Should not raise even with no client
    await ec.refresh(["AAPL"])


@pytest.mark.asyncio
async def test_refresh_swallows_api_error(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch)
    ec._client.earnings_calendar = MagicMock(side_effect=RuntimeError("nope"))
    await ec.refresh(["AAPL"])  # Should not raise


@pytest.mark.asyncio
async def test_refresh_empty_response(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch)
    ec._client.earnings_calendar = MagicMock(return_value={})
    await ec.refresh(["AAPL"])


@pytest.mark.asyncio
async def test_refresh_inserts_matched_events(tmp_db_path: str, monkeypatch):
    ec = _make_earnings(tmp_db_path, monkeypatch)
    future_str = (date.today() + timedelta(days=3)).isoformat()
    ec._client.earnings_calendar = MagicMock(return_value={
        "earningsCalendar": [
            {"symbol": "AAPL", "date": future_str, "hour": "bmo"},
            {"symbol": "ZZZ", "date": future_str, "hour": "amc"},  # not in watchlist
        ]
    })
    await ec.refresh(["AAPL"])
    assert ec.get_next_earnings("AAPL") is not None
