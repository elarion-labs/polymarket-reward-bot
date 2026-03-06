"""
rewards.py — RewardEstimator: implements the official Polymarket CLOB
Liquidity Rewards scoring methodology.

References:
  • Quadratic scoring:  S(v, s) = ((v - s) / v)^2 * b
  • Q_one / Q_two as defined in official docs
  • Q_min with single-sided rule and c=3.0
  • Payout sampled per-minute, settled daily at midnight UTC

APPROXIMATIONS (documented):
  1. "adjusted midpoint": Polymarket's official spec references a
     size-cutoff-adjusted midpoint that requires private per-maker data.
     We approximate it as the simple (best_bid + best_ask) / 2 midpoint.
  2. "competition": true per-maker Q_min is private.  We estimate total
     competition by summing S(v, s) * size for all eligible public book
     levels (CompetitionUtilityDepth proxy).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from models import RewardEstimate, RewardParams

logger = logging.getLogger("reward_bot")

C_DEFAULT: float = 3.0   # penalty divisor for single-sided quote


# ---------------------------------------------------------------------------
# Core scoring
# ---------------------------------------------------------------------------

def compute_adjusted_midpoint(
    best_bid: float,
    best_ask: float,
) -> float:
    """
    APPROXIMATION: Compute midpoint from best bid/ask.
    
    The official spec uses a "size-cutoff-adjusted midpoint" that requires
    per-maker order data not available via public API.  We use the simple
    arithmetic midpoint as a conservative approximation.
    """
    return (best_bid + best_ask) / 2.0


def compute_distance_s(order_price: float, midpoint: float, side: str) -> float:
    """
    Distance s of an order from the midpoint.
    
    For a BID: s = midpoint - order_price   (order is below mid)
    For an ASK: s = order_price - midpoint  (order is above mid)
    
    Returns 0 if the order is at or crosses the midpoint (would be ineligible).
    """
    if side == "BUY":
        return max(0.0, midpoint - order_price)
    else:
        return max(0.0, order_price - midpoint)


def score_s(v: float, s: float, b: float = 1.0) -> float:
    """
    Quadratic score S(v, s) = ((v - s) / v)^2 * b.
    
    Args:
        v: max_incentive_spread (price units)
        s: distance to midpoint (price units)
        b: in-game multiplier (default 1.0)
    
    Returns 0 if s >= v (order outside eligible spread).
    """
    if v <= 0 or s >= v:
        return 0.0
    return ((v - s) / v) ** 2 * b


# ---------------------------------------------------------------------------
# Q_one / Q_two
# ---------------------------------------------------------------------------

def compute_q_one_q_two(
    bid_size_m: float,
    ask_size_m_prime: float,
    ask_size_m: float,
    bid_size_m_prime: float,
    score_bid: float,
    score_ask: float,
    score_bid_m_prime: float,
    score_ask_m_prime: float,
) -> tuple[float, float]:
    """
    Compute Q_one and Q_two per official Polymarket formulas.

    Q_one: bid contribution from market m + ask contribution from market m'
      Q_one = score_bid * bid_size_m + score_ask_m_prime * ask_size_m_prime

    Q_two: ask contribution from market m + bid contribution from market m'
      Q_two = score_ask * ask_size_m + score_bid_m_prime * bid_size_m_prime

    In practice for a YES/NO paired binary market:
      m  = YES token book
      m' = NO  token book
    
    If we only operate on one side (SINGLE_SIDED_SAFE), the missing terms
    are 0.
    """
    q_one = score_bid * bid_size_m + score_ask_m_prime * ask_size_m_prime
    q_two = score_ask * ask_size_m + score_bid_m_prime * bid_size_m_prime
    return q_one, q_two


def compute_q_min(
    midpoint: float,
    q_one: float,
    q_two: float,
    c: float = C_DEFAULT,
) -> float:
    """
    Compute Q_min with the single-sided penalty rule.

    If midpoint ∈ [0.10, 0.90]:
        Q_min = max(min(Q_one, Q_two), max(Q_one/c, Q_two/c))
        (two-sided boost; single-sided gets penalised by /c)

    If midpoint ∈ [0, 0.10) or (0.90, 1.0]:
        Q_min = min(Q_one, Q_two)
        (extreme probability markets: no penalty applied)
    """
    if 0.10 <= midpoint <= 0.90:
        two_sided = min(q_one, q_two)
        single_sided = max(q_one / c, q_two / c)
        return max(two_sided, single_sided)
    else:
        return min(q_one, q_two)


# ---------------------------------------------------------------------------
# Competition proxy
# ---------------------------------------------------------------------------

def compute_competition_depth(
    book_levels: list[tuple[float, float]],
    midpoint: float,
    v: float,
    b: float = 1.0,
) -> float:
    """
    PROXY: Estimate total CompetitionUtilityDepth from public order book.

    Iterates over eligible book levels (bid or ask, within spread v) and
    sums S(v, s) * size as a proxy for total competition Q_min in the market.

    This is an approximation because:
      - True per-maker Q_min requires private data.
      - The public book aggregates all makers; we cannot disaggregate.

    Args:
        book_levels: list of (price, size) tuples (bids OR asks)
        midpoint: current midpoint price
        v: max_incentive_spread
        b: in-game multiplier
    """
    total = 0.0
    for price, size in book_levels:
        if price <= 0 or size <= 0:
            continue
        # Determine side based on position relative to mid
        if price < midpoint:
            s = compute_distance_s(price, midpoint, "BUY")
        else:
            s = compute_distance_s(price, midpoint, "SELL")
        sc = score_s(v, s, b)
        if sc > 0:
            total += sc * size
    return total


# ---------------------------------------------------------------------------
# RewardEstimator
# ---------------------------------------------------------------------------

class RewardEstimator:
    """
    Estimates expected rewards for a given order setup and market book.

    Usage:
        estimator = RewardEstimator(reward_params)
        estimate = estimator.estimate(
            book=orderbook_dict,
            our_bid_price=0.49,
            our_bid_size=10.0,
            our_ask_price=None,   # single-sided: no ask
            our_ask_size=0.0,
            book_no=orderbook_no_dict,  # None if unavailable
        )
    """

    def __init__(self, reward_params: RewardParams):
        self.rp = reward_params
        self.v = reward_params.max_incentive_spread   # already in price units
        self.b = reward_params.in_game_multiplier
        self.daily_budget = reward_params.reward_epoch_daily_budget
        self.min_size = reward_params.min_incentive_size

    def _parse_book(self, book: dict) -> tuple[list, list, float]:
        """Extract bids, asks, midpoint from raw book dict."""
        bids = [(float(p), float(s)) for p, s in book.get("bids", [])]
        asks = [(float(p), float(s)) for p, s in book.get("asks", [])]
        best_bid = bids[0][0] if bids else 0.49
        best_ask = asks[0][0] if asks else 0.51
        mid = compute_adjusted_midpoint(best_bid, best_ask)
        return bids, asks, mid

    def estimate(
        self,
        book: dict,
        our_bid_price: Optional[float],
        our_bid_size: float,
        our_ask_price: Optional[float],
        our_ask_size: float,
        book_no: Optional[dict] = None,
        capital_required: float = 10.0,
    ) -> RewardEstimate:
        """
        Full reward estimation pipeline.

        Returns a RewardEstimate with share_est, daily_reward_est,
        and reward_per_capital.
        """
        bids, asks, mid = self._parse_book(book)
        logger.debug("RewardEstimator: mid=%.4f v=%.4f b=%.2f", mid, self.v, self.b)

        # --- Our scores ---
        bid_s = compute_distance_s(our_bid_price, mid, "BUY") if our_bid_price else 0.0
        ask_s = compute_distance_s(our_ask_price, mid, "SELL") if our_ask_price else 0.0
        score_bid = score_s(self.v, bid_s, self.b) if our_bid_price else 0.0
        score_ask = score_s(self.v, ask_s, self.b) if our_ask_price else 0.0

        # --- Paired market (NO token) scores if available ---
        if book_no:
            bids_no, asks_no, mid_no = self._parse_book(book_no)
            # For NO book, complement: bid_no corresponds to ask_yes conceptually
            bid_s_no = compute_distance_s(bids_no[0][0], mid_no, "BUY") if bids_no else 0.0
            ask_s_no = compute_distance_s(asks_no[0][0], mid_no, "SELL") if asks_no else 0.0
            score_bid_no = score_s(self.v, bid_s_no, self.b)
            score_ask_no = score_s(self.v, ask_s_no, self.b)
            size_bid_no = bids_no[0][1] if bids_no else 0.0
            size_ask_no = asks_no[0][1] if asks_no else 0.0
        else:
            score_bid_no = score_ask_no = size_bid_no = size_ask_no = 0.0

        # --- Q_one, Q_two ---
        q_one, q_two = compute_q_one_q_two(
            bid_size_m=our_bid_size if our_bid_price else 0.0,
            ask_size_m_prime=size_ask_no,
            ask_size_m=our_ask_size if our_ask_price else 0.0,
            bid_size_m_prime=size_bid_no,
            score_bid=score_bid,
            score_ask=score_ask,
            score_bid_m_prime=score_bid_no,
            score_ask_m_prime=score_ask_no,
        )

        # --- Q_min ---
        q_min = compute_q_min(mid, q_one, q_two)

        # --- Competition proxy ---
        all_levels = bids + asks
        competition = compute_competition_depth(all_levels, mid, self.v, self.b)

        # --- Share estimate ---
        total_q = competition + q_min
        share_est = q_min / total_q if total_q > 0 else 0.0

        daily_reward_est = share_est * self.daily_budget
        reward_per_capital = daily_reward_est / capital_required if capital_required > 0 else 0.0

        return RewardEstimate(
            token_id=self.rp.token_id,
            s_score=score_bid if score_bid >= score_ask else score_ask,
            q_one=q_one,
            q_two=q_two,
            q_min=q_min,
            competition_depth=competition,
            share_est=share_est,
            daily_reward_est=daily_reward_est,
            reward_per_capital=reward_per_capital,
        )
