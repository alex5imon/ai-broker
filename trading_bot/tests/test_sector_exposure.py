"""Tests for sector exposure limits and GICS mapping."""

from __future__ import annotations

from typing import Any

import pytest

from trading_bot.config import Config
from trading_bot.constants import GICS_SECTOR, Phase
from trading_bot.execution.risk_manager import RiskManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _position(ticker: str) -> dict[str, Any]:
    return {"ticker": ticker, "sector": GICS_SECTOR.get(ticker, "")}


def _sector_position(sector: str) -> dict[str, Any]:
    return {"ticker": "UNKNOWN", "sector": sector}


# ---------------------------------------------------------------------------
# Phase 1 sector limits
# ---------------------------------------------------------------------------


class TestPhase1SectorExposure:
    @pytest.fixture
    def rm(self, config: Config, tmp_db_path: str, mock_fx, mock_notifier):
        config._phase = Phase.MICRO
        return RiskManager(config, tmp_db_path, mock_fx, mock_notifier)

    def test_phase1_allows_first_per_sector(self, rm: RiskManager) -> None:
        assert rm.check_sector_exposure("Financials", []) is False

    def test_phase1_allows_one_per_sector(self, rm: RiskManager) -> None:
        assert rm.check_sector_exposure("Financials", []) is False

    def test_phase1_blocks_second_financials(self, rm: RiskManager) -> None:
        positions = [_position("SOFI")]
        assert rm.check_sector_exposure("Financials", positions) is True

    def test_phase1_blocks_second_via_sector_field(self, rm: RiskManager) -> None:
        positions = [_sector_position("Financials")]
        assert rm.check_sector_exposure("Financials", positions) is True

    def test_phase1_different_sectors_both_allowed(self, rm: RiskManager) -> None:
        positions = [_position("SOFI")]  # Financials
        assert rm.check_sector_exposure("Energy", positions) is False

    def test_phase1_two_different_sectors_independent(
        self, rm: RiskManager
    ) -> None:
        positions = [
            _position("SOFI"),  # Financials
            _position("AAL"),   # Industrials
        ]
        assert rm.check_sector_exposure("Energy", positions) is False


# ---------------------------------------------------------------------------
# Phase 2 sector limits
# ---------------------------------------------------------------------------


class TestPhase2SectorExposure:
    @pytest.fixture
    def rm(self, raw_config: dict[str, Any], tmp_db_path: str, mock_fx, mock_notifier):
        raw = dict(raw_config)
        raw["account"] = dict(raw["account"])
        raw["account"]["phase_override"] = 2
        cfg = Config(raw)
        return RiskManager(cfg, tmp_db_path, mock_fx, mock_notifier)

    def test_phase2_allows_two_per_sector(self, rm: RiskManager) -> None:
        positions = [_position("SOFI")]
        assert rm.check_sector_exposure("Financials", positions) is False

    def test_phase2_blocks_third_in_sector(self, rm: RiskManager) -> None:
        positions = [_position("SOFI"), _position("BAC")]
        assert rm.check_sector_exposure("Financials", positions) is True

    def test_phase2_two_in_different_sectors(self, rm: RiskManager) -> None:
        positions = [_position("SOFI"), _position("BAC")]  # 2 Financials
        assert rm.check_sector_exposure("Energy", positions) is False


# ---------------------------------------------------------------------------
# GICS sector lookup
# ---------------------------------------------------------------------------


class TestGicsSectorLookup:
    def test_sofi_is_financials(self) -> None:
        assert GICS_SECTOR.get("SOFI") == "Financials"

    def test_pltr_is_information_technology(self) -> None:
        assert GICS_SECTOR.get("PLTR") == "Information Technology"

    def test_f_is_consumer_discretionary(self) -> None:
        assert GICS_SECTOR.get("F") == "Consumer Discretionary"

    def test_snap_is_communication_services(self) -> None:
        assert GICS_SECTOR.get("SNAP") == "Communication Services"

    def test_aal_is_industrials(self) -> None:
        assert GICS_SECTOR.get("AAL") == "Industrials"

    def test_unknown_ticker_missing(self) -> None:
        assert GICS_SECTOR.get("XYZUNKNOWN") is None

    @pytest.mark.parametrize("ticker,expected_sector", [
        ("SOFI", "Financials"),
        ("BAC", "Financials"),
        ("SNAP", "Communication Services"),
        ("AAL", "Industrials"),
        ("F", "Consumer Discretionary"),
        ("NIO", "Consumer Discretionary"),
        ("PLTR", "Information Technology"),
        ("INTC", "Information Technology"),
    ])
    def test_gics_sector_parametrized(self, ticker: str, expected_sector: str) -> None:
        assert GICS_SECTOR.get(ticker) == expected_sector


# ---------------------------------------------------------------------------
# Ticker exchange mapping
# ---------------------------------------------------------------------------


class TestTickerExchangeMapping:
    def test_pltr_on_nasdaq(self) -> None:
        from trading_bot.constants import TICKER_EXCHANGE, Exchange
        assert TICKER_EXCHANGE["PLTR"] == Exchange.NASDAQ

    def test_f_on_nyse(self) -> None:
        from trading_bot.constants import TICKER_EXCHANGE, Exchange
        assert TICKER_EXCHANGE["F"] == Exchange.NYSE

    def test_ticker_currency_is_usd(self) -> None:
        from trading_bot.constants import ticker_currency, Currency
        assert ticker_currency("PLTR") == Currency.USD

    def test_ticker_market_is_us(self) -> None:
        from trading_bot.constants import ticker_market, Market
        assert ticker_market("PLTR") == Market.US
