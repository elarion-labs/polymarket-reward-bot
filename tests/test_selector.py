"""
tests/test_selector.py — Unit tests for MarketSelector.

Run with: pytest tests/test_selector.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from unittest.mock import MagicMock, patch

from selector import MarketSelector, MIN_BOOK_DEPTH, MIN_REWARD_BUDGET
from models import MarketInfo, RewardParams


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_config():
    cfg = MagicMock()
    cfg.bankroll_usdc = 100.0
    cfg.free_usdc_buffer_pct = 0.30
    cfg.max_capital_per_market = 40.0
    cfg.max_position_usd = 20.0
    cfg.usable_capital = 70.0
    cfg.target_min_distance = 0.01
    cfg.target_max_distance = 0.02
    cfg.market_reselect_seconds = 1800
    cfg.min_daily_reward_usd = 1.0
    cfg.allow_under_min_payout = False
    return cfg


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.get_markets_with_rewards.return_value = [
        {
            "condition_id": "cond-001",
            "question": "Test market?",
            "tokens": [
                {"token_id": "yes-001", "outcome": "Yes"},
                {"token_id": "no-001",  "outcome": "No"},
            ],
            "rewards": {
                "min_incentive_size": 5.0,
                "max_incentive_spread": 200,
                "daily_budget": 100.0,
            },
        }
    ]
    client.get_orderbook.return_value = {
        "bids": [(0.48, 20.0), (0.47, 30.0)],
        "asks": [(0.52, 20.0), (0.53, 30.0)],
        "mid": 0.50,
    }
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_select_best_market_returns_market(mock_client, mock_config):
    selector = MarketSelector(mock_client, mock_config)
    result = selector.select_best_market(force=True)
    assert result is not None
    assert isinstance(result, MarketInfo)
    assert result.market_id == "cond-001"


def test_select_no_markets_returns_none(mock_client, mock_config):
    mock_client.get_markets_with_rewards.return_value = []
    selector = MarketSelector(mock_client, mock_config)
    result = selector.select_best_market(force=True)
    assert result is None


def test_filters_low_budget_market(mock_client, mock_config):
    """Markets with daily_budget < MIN_REWARD_BUDGET should be filtered out."""
    mock_client.get_markets_with_rewards.return_value = [
        {
            "condition_id": "cond-low",
            "question": "Low budget market",
            "tokens": [
                {"token_id": "yes-low", "outcome": "Yes"},
                {"token_id": "no-low", "outcome": "No"},
            ],
            "rewards": {
                "min_incentive_size": 5.0,
                "max_incentive_spread": 200,
                "daily_budget": 0.50,  # below MIN_REWARD_BUDGET
            },
        }
    ]
    selector = MarketSelector(mock_client, mock_config)
    result = selector.select_best_market(force=True)
    assert result is None


def test_filters_extreme_midpoint(mock_client, mock_config):
    """Markets with midpoint outside [0.05, 0.95] should be filtered."""
    mock_client.get_orderbook.return_value = {
        "bids": [(0.02, 20.0), (0.01, 30.0)],
        "asks": [(0.04, 20.0), (0.05, 30.0)],
        "mid": 0.03,
    }
    selector = MarketSelector(mock_client, mock_config)
    result = selector.select_best_market(force=True)
    assert result is None


def test_filters_insufficient_book_depth(mock_client, mock_config):
    """Books with fewer than MIN_BOOK_DEPTH levels should be filtered."""
    mock_client.get_orderbook.return_value = {
        "bids": [(0.48, 20.0)],  # only 1 bid level
        "asks": [(0.52, 20.0)],
        "mid": 0.50,
    }
    selector = MarketSelector(mock_client, mock_config)
    result = selector.select_best_market(force=True)
    assert result is None


def test_reward_per_capital_positive(mock_client, mock_config):
    selector = MarketSelector(mock_client, mock_config)
    result = selector.select_best_market(force=True)
    assert result is not None
    assert result.reward_per_capital >= 0


def test_should_reselect_respects_timer(mock_client, mock_config):
    """should_reselect() should be False immediately after a selection."""
    mock_config.market_reselect_seconds = 3600
    selector = MarketSelector(mock_client, mock_config)
    selector.select_best_market(force=True)
    # Immediately after selection, should NOT need reselect
    assert not selector.should_reselect()


def test_multiple_markets_picks_best(mock_client, mock_config):
    """When multiple markets exist, selector picks highest reward_per_capital."""
    mock_client.get_markets_with_rewards.return_value = [
        {
            "condition_id": "cond-001",
            "question": "Market A",
            "tokens": [
                {"token_id": "yes-001", "outcome": "Yes"},
                {"token_id": "no-001",  "outcome": "No"},
            ],
            "rewards": {"min_incentive_size": 5.0, "max_incentive_spread": 200, "daily_budget": 50.0},
        },
        {
            "condition_id": "cond-002",
            "question": "Market B",
            "tokens": [
                {"token_id": "yes-002", "outcome": "Yes"},
                {"token_id": "no-002",  "outcome": "No"},
            ],
            "rewards": {"min_incentive_size": 5.0, "max_incentive_spread": 200, "daily_budget": 500.0},
        },
    ]
    # Return same book for all tokens
    mock_client.get_orderbook.return_value = {
        "bids": [(0.48, 20.0), (0.47, 30.0)],
        "asks": [(0.52, 20.0), (0.53, 30.0)],
    }
    selector = MarketSelector(mock_client, mock_config)
    result = selector.select_best_market(force=True)
    # Should pick Market B (higher budget → higher estimated reward)
    assert result is not None
    assert result.market_id == "cond-002"


def test_client_error_returns_cached(mock_client, mock_config):
    """If API call fails and we have a cached selection, return it."""
    selector = MarketSelector(mock_client, mock_config)
    first = selector.select_best_market(force=True)
    assert first is not None

    mock_client.get_markets_with_rewards.side_effect = Exception("API down")
    second = selector.select_best_market(force=True)
    # Should return previously cached selection
    assert second is first
