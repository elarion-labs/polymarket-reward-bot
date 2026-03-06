"""
risk.py — RiskManager: guards the bot against adverse market conditions.

States:
  SELECTING  — choosing a market
  RUNNING    — actively quoting
  COOLDOWN   — waiting after a risk event before re-entering
  UNHEDGED   — got a fill, trying to hedge; time-limited
  PAUSED     — manual or hard kill switch engaged

Risk triggers:
  • Jump detector      — mid moves > JUMP_THRESHOLD in JUMP_WINDOW_SECONDS
  • Reprice threshold  — mid moves > REPRICE_THRESHOLD since last quote
  • Kill switch        — time to market close < SAFE_CLOSE_HOURS
  • Position cap       — net position value > MAX_POSITION_USD
  • Unhedged timeout   — UNHEDGED state exceeds UNHEDGED_MAX_SECONDS
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

from config import Config
from models import BotState, Fill, Position

logger = logging.getLogger("reward_bot")


class RiskManager:
    """
    Evaluates risk conditions and manages bot state transitions.

    Call check_* methods after each market update; they mutate self.state.
    """

    def __init__(self, config: Config):
        self._cfg = config
        self.state: BotState = BotState.SELECTING

        self._mid_history: deque[tuple[float, float]] = deque()  # (timestamp, mid)
        self._last_quote_mid: Optional[float] = None

        self._cooldown_until: float = 0.0

        self._unhedged_since: Optional[float] = None

        # Debounce: multiple fills can arrive quickly; avoid flapping timers.
        self._last_fill_at: Optional[float] = None
        self._fill_debounce_seconds: float = 1.0  # simple, conservative

        self._paused: bool = False

    # ------------------------------------------------------------------
    # State accessors
    # ------------------------------------------------------------------

    def is_running(self) -> bool:
        return self.state == BotState.RUNNING

    def is_cooldown_expired(self) -> bool:
        return time.monotonic() >= self._cooldown_until

    def set_running(self) -> None:
        self.state = BotState.RUNNING
        logger.info("RiskManager: state → RUNNING")

    def set_selecting(self) -> None:
        self.state = BotState.SELECTING
        self._last_quote_mid = None
        self._unhedged_since = None
        logger.info("RiskManager: state → SELECTING")

    def set_paused(self) -> None:
        self.state = BotState.PAUSED
        self._paused = True
        logger.warning("RiskManager: state → PAUSED (manual)")

    def enter_cooldown(self, reason: str) -> None:
        self.state = BotState.COOLDOWN
        self._cooldown_until = time.monotonic() + self._cfg.cooldown_seconds
        self._unhedged_since = None
        logger.warning("RiskManager: state → COOLDOWN for %ds — %s",
                       self._cfg.cooldown_seconds, reason)

    def enter_unhedged(self) -> None:
        """
        Enter UNHEDGED state if not already there.
        Do not reset the timer if we are already UNHEDGED.
        """
        if self.state == BotState.UNHEDGED:
            return
        self.state = BotState.UNHEDGED
        self._unhedged_since = time.monotonic()
        logger.warning("RiskManager: state → UNHEDGED (fill detected, attempting hedge)")

    def exit_unhedged_success(self) -> None:
        """Call when hedge succeeded."""
        self._unhedged_since = None
        self.state = BotState.RUNNING
        logger.info("RiskManager: UNHEDGED → RUNNING (hedge successful)")

    # ------------------------------------------------------------------
    # Check methods — call after each update tick
    # ------------------------------------------------------------------

    def check_kill_switch(self, seconds_to_close: Optional[float]) -> bool:
        """
        If market is closing within SAFE_CLOSE_HOURS, cancel all and pause.
        Returns True if kill switch fired.
        """
        if seconds_to_close is None:
            return False
        if seconds_to_close <= self._cfg.safe_close_seconds:
            logger.warning(
                "RiskManager: kill switch — market closes in %.0fs (< %.0fs threshold)",
                seconds_to_close, self._cfg.safe_close_seconds,
            )
            self.state = BotState.PAUSED
            return True
        return False

    def check_jump(self, mid: float) -> bool:
        """
        Detect large price jumps.  Returns True if a jump was detected.
        Cleans up stale history entries outside the window.
        """
        now = time.monotonic()
        self._mid_history.append((now, mid))
        # Prune old entries
        cutoff = now - self._cfg.jump_window_seconds
        while self._mid_history and self._mid_history[0][0] < cutoff:
            self._mid_history.popleft()

        if len(self._mid_history) < 2:
            return False

        oldest_mid = self._mid_history[0][1]
        delta = abs(mid - oldest_mid)
        if delta >= self._cfg.jump_threshold:
            logger.warning(
                "RiskManager: jump detected Δ=%.4f >= threshold=%.4f",
                delta, self._cfg.jump_threshold,
            )
            self.enter_cooldown(f"jump Δ={delta:.4f}")
            return True
        return False

    def check_reprice(self, current_mid: float) -> bool:
        """
        Detect if mid has drifted enough to require repricing.
        Returns True if reprice is needed (caller should re-sync orders).
        Does NOT change state — just signals repricing.
        """
        if self._last_quote_mid is None:
            self._last_quote_mid = current_mid
            return False

        delta = abs(current_mid - self._last_quote_mid)
        if delta >= self._cfg.reprice_threshold:
            logger.info(
                "RiskManager: reprice triggered Δ=%.4f (threshold=%.4f)",
                delta, self._cfg.reprice_threshold,
            )
            self._last_quote_mid = current_mid
            return True
        return False

    def update_quote_mid(self, mid: float) -> None:
        """Update the reference mid after placing orders."""
        self._last_quote_mid = mid

    def check_position_cap(self, positions: list[Position]) -> bool:
        """
        Check if any position exceeds MAX_POSITION_USD.
        Returns True if cap breached (caller should cancel + reduce).
        """
        for pos in positions:
            notional = pos.size * pos.avg_price
            if abs(notional) > self._cfg.max_position_usd:
                logger.warning(
                    "RiskManager: position cap breached token=%s notional=%.2f > max=%.2f",
                    pos.token_id, abs(notional), self._cfg.max_position_usd,
                )
                self.enter_cooldown("position cap breached")
                return True
        return False

    def check_unhedged_timeout(self) -> bool:
        """
        If in UNHEDGED state for too long, enter COOLDOWN.
        Returns True if timeout fired.
        """
        if self.state != BotState.UNHEDGED:
            return False
        if self._unhedged_since is None:
            self._unhedged_since = time.monotonic()

        elapsed = time.monotonic() - self._unhedged_since
        if elapsed >= self._cfg.unhedged_max_seconds:
            logger.error(
                "RiskManager: unhedged timeout (%.0fs >= %ds) → COOLDOWN",
                elapsed, self._cfg.unhedged_max_seconds,
            )
            self._unhedged_since = None
            self.enter_cooldown("unhedged timeout")
            return True
        return False

    def check_cooldown_expired(self) -> bool:
        """
        If in COOLDOWN and timeout expired, transition to SELECTING.
        Returns True if we just exited cooldown.
        """
        if self.state != BotState.COOLDOWN:
            return False
        if self.is_cooldown_expired():
            logger.info("RiskManager: cooldown expired → SELECTING")
            self.set_selecting()
            return True
        return False

    def on_fill(self, fill: Fill, current_positions: list[Position]) -> None:
        """
        Called when a fill event is received.

        IMPORTANT:
          - Positions are often stale at the exact moment the fill arrives.
          - Therefore, treat ANY fill as a critical event: enter UNHEDGED immediately.
          - The main loop will poll positions and attempt hedge; UNHEDGED timeout
            remains the safety valve.
        """
        now = time.monotonic()
        logger.warning(
            "RiskManager: fill received order_id=%s token=%s side=%s size=%.2f price=%.4f",
            fill.order_id, fill.token_id, fill.side, fill.size, fill.price,
        )

        # Do not override more restrictive states
        if self.state in (BotState.PAUSED, BotState.COOLDOWN):
            return

        # Debounce to avoid flapping if many fill messages arrive quickly
        if self._last_fill_at is not None and (now - self._last_fill_at) < self._fill_debounce_seconds:
            # Still ensure we're in UNHEDGED (without resetting timer)
            if self.state != BotState.UNHEDGED:
                self.enter_unhedged()
            return

        self._last_fill_at = now
        self.enter_unhedged()