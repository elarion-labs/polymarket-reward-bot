"""
config.py — Configuration loader and validator.

Reads environment variables (from .env or shell) and exposes a typed Config
singleton. NEVER logs or prints secrets.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

# Load .env from the project root (two levels up from src/)
_ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=False)


class Config(BaseModel):
    # --- Auth (loaded but never logged) ---
    private_key: str = Field(default="", alias="PRIVATE_KEY")
    poly_l2_api_key: str = Field(default="", alias="POLY_L2_API_KEY")
    poly_l2_secret: str = Field(default="", alias="POLY_L2_SECRET")
    poly_l2_passphrase: str = Field(default="", alias="POLY_L2_PASSPHRASE")
    rpc_url: str = Field(default="https://polygon-rpc.com", alias="RPC_URL")
    chain_id: int = Field(default=137, alias="CHAIN_ID")

    # --- Market selection ---
    market_selection_mode: Literal["AUTO", "MANUAL"] = Field(
        default="AUTO", alias="MARKET_SELECTION_MODE"
    )
    manual_market_file: str = Field(
        default="manual_market.json", alias="MANUAL_MARKET_FILE"
    )

    # --- Risk ---
    bankroll_usdc: float = Field(default=100.0, alias="BANKROLL_USDC")
    safe_close_hours: float = Field(default=24.0, alias="SAFE_CLOSE_HOURS")
    max_position_usd: float = Field(default=20.0, alias="MAX_POSITION_USD")
    max_capital_per_market: float = Field(default=40.0, alias="MAX_CAPITAL_PER_MARKET")
    free_usdc_buffer_pct: float = Field(default=0.30, alias="FREE_USDC_BUFFER_PCT")

    # --- Quoting ---
    mode: Literal["SINGLE_SIDED_SAFE", "TWO_SIDED_BALANCED"] = Field(
        default="SINGLE_SIDED_SAFE", alias="MODE"
    )
    target_min_distance: float = Field(default=0.01, alias="TARGET_MIN_DISTANCE")
    target_max_distance: float = Field(default=0.02, alias="TARGET_MAX_DISTANCE")

    # --- Repricing ---
    reprice_threshold: float = Field(default=0.01, alias="REPRICE_THRESHOLD")
    jump_threshold: float = Field(default=0.02, alias="JUMP_THRESHOLD")
    jump_window_seconds: int = Field(default=60, alias="JUMP_WINDOW_SECONDS")
    cooldown_minutes: int = Field(default=10, alias="COOLDOWN_MINUTES")

    # --- Loop ---
    poll_fallback_seconds: int = Field(default=15, alias="POLL_FALLBACK_SECONDS")
    market_reselect_minutes: int = Field(default=30, alias="MARKET_RESELECT_MINUTES")

    # --- Safety ---
    unhedged_max_seconds: int = Field(default=20, alias="UNHEDGED_MAX_SECONDS")

    # --- Rewards ---
    min_daily_reward_usd: float = Field(default=1.00, alias="MIN_DAILY_REWARD_USD")
    allow_under_min_payout: bool = Field(default=False, alias="ALLOW_UNDER_MIN_PAYOUT")

    model_config = {"populate_by_name": True}

    @field_validator(
        "target_min_distance",
        "target_max_distance",
        "bankroll_usdc",
        "safe_close_hours",
        "max_position_usd",
        "max_capital_per_market",
        "free_usdc_buffer_pct",
        "reprice_threshold",
        "jump_threshold",
        "min_daily_reward_usd",
        mode="before",
    )
    @classmethod
    def _parse_float(cls, v):
        return float(v)

    @field_validator(
        "chain_id",
        "jump_window_seconds",
        "cooldown_minutes",
        "poll_fallback_seconds",
        "market_reselect_minutes",
        "unhedged_max_seconds",
        mode="before",
    )
    @classmethod
    def _parse_int(cls, v):
        return int(v)

    @model_validator(mode="after")
    def _validate_distances(self):
        assert 0 < self.target_min_distance, "TARGET_MIN_DISTANCE must be > 0"
        assert self.target_min_distance < self.target_max_distance, (
            "TARGET_MIN_DISTANCE must be < TARGET_MAX_DISTANCE"
        )
        assert self.target_max_distance < 0.05, (
            "TARGET_MAX_DISTANCE must be < 0.05"
        )
        assert 0 < self.free_usdc_buffer_pct < 1, (
            "FREE_USDC_BUFFER_PCT must be between 0 and 1"
        )
        assert self.market_selection_mode in ("AUTO", "MANUAL"), (
            "MARKET_SELECTION_MODE must be AUTO or MANUAL"
        )
        return self

    @property
    def safe_close_seconds(self) -> float:
        return self.safe_close_hours * 3600

    @property
    def cooldown_seconds(self) -> int:
        return self.cooldown_minutes * 60

    @property
    def market_reselect_seconds(self) -> int:
        return self.market_reselect_minutes * 60

    @property
    def usable_capital(self) -> float:
        """Capital available for deployment (after buffer)."""
        return self.bankroll_usdc * (1 - self.free_usdc_buffer_pct)

    def apply_runtime_overrides(self, base: dict[str, Any], overrides: dict[str, Any]) -> None:
        """
        Apply runtime overrides on top of the original base config.
        Mutates the existing config instance so dependent components keep working.

        Supported special values:
          - mode="AUTO" => keep base MODE
          - market_selection_mode="" / None => keep base MARKET_SELECTION_MODE
        """
        merged = dict(base)

        if overrides:
            for k, v in overrides.items():
                if v is None:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue
                merged[k] = v

        if str(merged.get("mode", "")).upper() == "AUTO":
            merged["mode"] = base.get("mode", self.mode)

        if not merged.get("market_selection_mode"):
            merged["market_selection_mode"] = base.get(
                "market_selection_mode",
                self.market_selection_mode,
            )

        validated = Config.model_validate(merged)

        for field_name in self.model_fields.keys():
            setattr(self, field_name, getattr(validated, field_name))


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Return the singleton Config, loaded from environment."""
    raw = {k: v for k, v in os.environ.items()}
    return Config.model_validate(raw)