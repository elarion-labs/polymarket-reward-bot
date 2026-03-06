"""
tests/test_rewards.py — Unit tests for the rewards scoring module.

Run with: pytest tests/test_rewards.py -v
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from rewards import (
    compute_adjusted_midpoint,
    compute_distance_s,
    score_s,
    compute_q_one_q_two,
    compute_q_min,
    compute_competition_depth,
    RewardEstimator,
)
from models import RewardParams


# ---------------------------------------------------------------------------
# compute_adjusted_midpoint
# ---------------------------------------------------------------------------

def test_midpoint_basic():
    assert compute_adjusted_midpoint(0.48, 0.52) == pytest.approx(0.50)

def test_midpoint_asymmetric():
    assert compute_adjusted_midpoint(0.40, 0.60) == pytest.approx(0.50)

def test_midpoint_extreme():
    m = compute_adjusted_midpoint(0.08, 0.12)
    assert m == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# compute_distance_s
# ---------------------------------------------------------------------------

def test_distance_bid_below_mid():
    s = compute_distance_s(0.48, 0.50, "BUY")
    assert s == pytest.approx(0.02)

def test_distance_bid_at_mid():
    s = compute_distance_s(0.50, 0.50, "BUY")
    assert s == pytest.approx(0.00)

def test_distance_ask_above_mid():
    s = compute_distance_s(0.53, 0.50, "SELL")
    assert s == pytest.approx(0.03)

def test_distance_ask_at_mid():
    s = compute_distance_s(0.50, 0.50, "SELL")
    assert s == pytest.approx(0.00)

def test_distance_bid_above_mid_clamps_zero():
    # Order price above midpoint for a bid: s=0 (ineligible anyway)
    s = compute_distance_s(0.55, 0.50, "BUY")
    assert s == pytest.approx(0.00)


# ---------------------------------------------------------------------------
# score_s
# ---------------------------------------------------------------------------

def test_score_at_mid():
    """Order exactly at mid (s=0): score = 1.0."""
    sc = score_s(v=0.02, s=0.0, b=1.0)
    assert sc == pytest.approx(1.0)

def test_score_at_edge():
    """Order exactly at max spread (s=v): score = 0."""
    sc = score_s(v=0.02, s=0.02, b=1.0)
    assert sc == pytest.approx(0.0)

def test_score_outside():
    """Order outside spread: score = 0."""
    sc = score_s(v=0.02, s=0.03, b=1.0)
    assert sc == pytest.approx(0.0)

def test_score_midpoint():
    """s = v/2: score = 0.25."""
    sc = score_s(v=0.02, s=0.01, b=1.0)
    assert sc == pytest.approx(0.25)

def test_score_multiplier():
    """In-game multiplier b scales linearly."""
    sc1 = score_s(v=0.02, s=0.01, b=1.0)
    sc2 = score_s(v=0.02, s=0.01, b=2.0)
    assert sc2 == pytest.approx(sc1 * 2)

def test_score_v_zero():
    """v=0 should return 0, not divide-by-zero."""
    sc = score_s(v=0.0, s=0.01, b=1.0)
    assert sc == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Q_one / Q_two
# ---------------------------------------------------------------------------

def test_q_one_single_sided():
    """Single-sided (ask_m_prime=0, bid_m_prime=0)."""
    q1, q2 = compute_q_one_q_two(
        bid_size_m=10.0,
        ask_size_m_prime=0.0,
        ask_size_m=0.0,
        bid_size_m_prime=0.0,
        score_bid=0.5,
        score_ask=0.0,
        score_bid_m_prime=0.0,
        score_ask_m_prime=0.0,
    )
    assert q1 == pytest.approx(5.0)   # 0.5 * 10
    assert q2 == pytest.approx(0.0)

def test_q_two_sided():
    q1, q2 = compute_q_one_q_two(
        bid_size_m=10.0,
        ask_size_m_prime=8.0,
        ask_size_m=10.0,
        bid_size_m_prime=8.0,
        score_bid=0.5,
        score_ask=0.5,
        score_bid_m_prime=0.4,
        score_ask_m_prime=0.4,
    )
    assert q1 == pytest.approx(0.5*10 + 0.4*8)   # 5 + 3.2 = 8.2
    assert q2 == pytest.approx(0.5*10 + 0.4*8)   # symmetric


# ---------------------------------------------------------------------------
# Q_min
# ---------------------------------------------------------------------------

def test_q_min_mid_range_balanced():
    """Mid in [0.10, 0.90], balanced Q_one=Q_two → Q_min = Q_one."""
    q_min = compute_q_min(midpoint=0.50, q_one=5.0, q_two=5.0, c=3.0)
    assert q_min == pytest.approx(5.0)

def test_q_min_mid_range_single_sided():
    """Mid in [0.10, 0.90], only Q_one > 0 (single-sided) → Q_min = Q_one / c."""
    q_min = compute_q_min(midpoint=0.50, q_one=9.0, q_two=0.0, c=3.0)
    # min(9,0)=0, max(9/3,0/3)=3 → max(0,3) = 3
    assert q_min == pytest.approx(3.0)

def test_q_min_extreme_mid():
    """Mid near 0 (extreme probability) → Q_min = min(Q_one, Q_two)."""
    q_min = compute_q_min(midpoint=0.05, q_one=9.0, q_two=0.0, c=3.0)
    assert q_min == pytest.approx(0.0)

def test_q_min_extreme_mid_high():
    """Mid near 1 → Q_min = min(Q_one, Q_two)."""
    q_min = compute_q_min(midpoint=0.95, q_one=9.0, q_two=3.0, c=3.0)
    assert q_min == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Competition depth proxy
# ---------------------------------------------------------------------------

def test_competition_depth_empty():
    d = compute_competition_depth([], midpoint=0.50, v=0.02)
    assert d == pytest.approx(0.0)

def test_competition_depth_outside_spread():
    """Levels outside v should not contribute."""
    levels = [(0.40, 100.0)]  # s = 0.10, v = 0.02 → outside
    d = compute_competition_depth(levels, midpoint=0.50, v=0.02)
    assert d == pytest.approx(0.0)

def test_competition_depth_inside():
    """Level at mid (s=0): contributes score=1.0 * size."""
    levels = [(0.50, 10.0)]
    # s = 0 for a bid at mid
    d = compute_competition_depth(levels, midpoint=0.50, v=0.02)
    assert d == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# RewardEstimator integration
# ---------------------------------------------------------------------------

@pytest.fixture
def rp():
    return RewardParams(
        token_id="test-token",
        min_incentive_size=5.0,
        max_incentive_spread=0.02,
        reward_epoch_daily_budget=100.0,
        in_game_multiplier=1.0,
    )

@pytest.fixture
def sample_book():
    return {
        "bids": [(0.48, 20.0), (0.47, 30.0)],
        "asks": [(0.52, 20.0), (0.53, 30.0)],
        "mid": 0.50,
    }

def test_estimator_single_sided(rp, sample_book):
    est = RewardEstimator(rp)
    result = est.estimate(
        book=sample_book,
        our_bid_price=0.49,
        our_bid_size=10.0,
        our_ask_price=None,
        our_ask_size=0.0,
        book_no=None,
        capital_required=4.9,
    )
    assert result.share_est > 0
    assert result.daily_reward_est >= 0
    assert result.reward_per_capital >= 0
    assert result.q_two == pytest.approx(0.0)  # no ask placed

def test_estimator_share_between_0_and_1(rp, sample_book):
    est = RewardEstimator(rp)
    result = est.estimate(
        book=sample_book,
        our_bid_price=0.499,  # very close to mid → high score
        our_bid_size=100.0,
        our_ask_price=None,
        our_ask_size=0.0,
        capital_required=50.0,
    )
    assert 0 <= result.share_est <= 1.0

def test_estimator_zero_budget():
    rp_zero = RewardParams(
        token_id="t",
        min_incentive_size=5.0,
        max_incentive_spread=0.02,
        reward_epoch_daily_budget=0.0,
    )
    est = RewardEstimator(rp_zero)
    result = est.estimate(
        book={"bids": [(0.48, 10)], "asks": [(0.52, 10)]},
        our_bid_price=0.49, our_bid_size=10.0,
        our_ask_price=None, our_ask_size=0.0,
        capital_required=5.0,
    )
    assert result.daily_reward_est == pytest.approx(0.0)
