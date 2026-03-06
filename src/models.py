"""
models.py — Shared data models (Pydantic) for the Polymarket Reward Bot.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class BotState(str, Enum):
    SELECTING = "SELECTING"
    RUNNING = "RUNNING"
    COOLDOWN = "COOLDOWN"
    UNHEDGED = "UNHEDGED"
    PAUSED = "PAUSED"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Mode(str, Enum):
    SINGLE_SIDED_SAFE = "SINGLE_SIDED_SAFE"
    TWO_SIDED_BALANCED = "TWO_SIDED_BALANCED"


# ---------------------------------------------------------------------------
# Market / Rewards
# ---------------------------------------------------------------------------

class RewardParams(BaseModel):
    """Official reward parameters for a market token."""
    token_id: str
    min_incentive_size: float            # minimum order size to qualify
    max_incentive_spread: float          # maximum spread in price units (converted from cents)
    reward_epoch_daily_budget: float     # daily USDC budget for this market
    in_game_multiplier: float = 1.0      # b factor; default 1.0 if not available


class MarketInfo(BaseModel):
    """Selected market snapshot used throughout the bot lifecycle."""
    market_id: str
    condition_id: str
    question: str
    yes_token_id: str
    no_token_id: str
    reward_params_yes: RewardParams
    reward_params_no: Optional[RewardParams] = None
    midpoint: float = 0.5
    # Estimated metrics (filled by RewardEstimator)
    share_est: float = 0.0
    daily_reward_est: float = 0.0
    reward_per_capital: float = 0.0
    capital_required: float = 0.0


# ---------------------------------------------------------------------------
# Order / Position
# ---------------------------------------------------------------------------

class DesiredOrder(BaseModel):
    """Represents the order the bot *wants* to have in the book."""
    token_id: str
    side: Side
    price: float
    size: float
    tag: str = ""    # e.g. "YES_BID", "NO_BID"


class OpenOrder(BaseModel):
    """Live order returned by the exchange."""
    order_id: str
    token_id: str
    side: Side
    price: float
    size_remaining: float
    created_at: Optional[str] = None


class Position(BaseModel):
    """Current token position held by the bot."""
    token_id: str
    size: float        # positive = long
    avg_price: float


class Fill(BaseModel):
    """A recorded fill event."""
    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    timestamp: str


# ---------------------------------------------------------------------------
# Reward estimation outputs
# ---------------------------------------------------------------------------

class RewardEstimate(BaseModel):
    """Output from RewardEstimator for a given set of orders + book."""
    token_id: str
    s_score: float            # S(v, s) for our order
    q_one: float
    q_two: float
    q_min: float
    competition_depth: float  # proxy: sum S(v,s)*size for eligible book levels
    share_est: float          # q_min / (competition_depth + q_min)  — proxy
    daily_reward_est: float   # share_est * daily_budget
    reward_per_capital: float # daily_reward_est / capital_required


# ---------------------------------------------------------------------------
# Dashboard snapshot
# ---------------------------------------------------------------------------

class DashboardSnapshot(BaseModel):
    state: BotState
    market: Optional[MarketInfo] = None
    open_orders: list[OpenOrder] = Field(default_factory=list)
    positions: list[Position] = Field(default_factory=list)
    recent_fills: list[Fill] = Field(default_factory=list)
    reward_estimate: Optional[RewardEstimate] = None
    last_updated: str = ""
    log_tail: list[str] = Field(default_factory=list)
