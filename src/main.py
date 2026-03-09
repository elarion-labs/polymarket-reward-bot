"""
main.py — Entry point for the Polymarket Reward-Aware Conservative Liquidity Bot.

Architecture summary:
  Config          → loads env, validates parameters
  PolyClient      → wraps py-clob-client SDK (REST + WS)
  RewardEstimator → S(v,s), Q_one/Q_two, Q_min, share/payout estimates
  MarketSelector  → discovers + ranks markets every MARKET_RESELECT_MINUTES
  OrderManager    → desired vs open order reconciliation
  RiskManager     → jump/reprice/kill-switch/position-cap state machine
  Dashboard       → FastAPI HTTP server (runs in background thread)

Main loop state machine:
  SELECTING → RUNNING → (COOLDOWN | UNHEDGED | PAUSED) → SELECTING

Run:
  python src/main.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import uvicorn

# Ensure src/ is importable
sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))

from config import get_config
from dashboard import (
    app as dashboard_app,
    get_manual_market_ref,
    get_runtime_overrides,
    pop_control_command,
    update_runtime_status,
    update_snapshot,
)
from models import (
    BotState,
    DashboardSnapshot,
    Fill,
    MarketInfo,
    OpenOrder,
    Position,
    RewardParams,
    Side,
)
from orders import OrderManager
from poly_client import PolyClient
from risk import RiskManager
from selector import MarketSelector
from utils import setup_logging, utc_iso

logger = logging.getLogger("reward_bot")

TICK_SIZE = 0.01  # keep consistent with orders.py


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class RewardBot:
    """
    Top-level bot orchestrator. Runs the main async event loop.
    """

    def __init__(self):
        self.cfg = get_config()
        self._base_cfg = self.cfg.model_dump()

        self._client = PolyClient(
            private_key=self.cfg.private_key,
            api_key=self.cfg.poly_l2_api_key,
            api_secret=self.cfg.poly_l2_secret,
            api_passphrase=self.cfg.poly_l2_passphrase,
            chain_id=self.cfg.chain_id,
        )
        self._selector = MarketSelector(self._client, self.cfg)
        self._orders = OrderManager(self._client, self.cfg)
        self._risk = RiskManager(self.cfg)

        # IMPORTANT:
        # Bot must start idle and only begin selection/execution after
        # an explicit START command from the dashboard.
        self._risk.set_paused()

        self._running = True
        self._fills: list[Fill] = []
        self._positions: list[Position] = []
        self._open_orders: list[OpenOrder] = []

        self._current_market: Optional[MarketInfo] = None

        # WS task handles
        self._ws_tasks: list[asyncio.Task] = []

        # Wallet status cache
        self._available_usdc: Optional[float] = None
        self._available_usdc_last_refresh: float = 0.0

        # Cooperative cancellation for blocking market selection work
        self._selection_cancel = threading.Event()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self):
        logger.info(
            "RewardBot starting up — bankroll=%.2f mode=%s market_selection=%s",
            self.cfg.bankroll_usdc,
            self.cfg.mode,
            self.cfg.market_selection_mode,
        )
        logger.info("RewardBot initial state: PAUSED (waiting for dashboard START)")

        self._ws_tasks = [
            asyncio.create_task(self._ws_book_listener()),
        ]
        # Temporarily disabled: Polymarket WS user auth changed
        # Fills will be detected via REST polling
        # if self.cfg.private_key:
        #    self._ws_tasks.append(asyncio.create_task(self._ws_user_listener()))

        await self._refresh_wallet_balance(force=True)
        await self._update_dashboard(self._current_market)

        while self._running:
            try:
                self._apply_dashboard_overrides()
                await self._handle_dashboard_command()
                await self._tick()
            except Exception as exc:
                logger.error("Unhandled exception in main loop: %s", exc, exc_info=True)
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Tick — called each iteration
    # ------------------------------------------------------------------

    async def _tick(self):
        await self._refresh_wallet_balance()

        state = self._risk.state

        if state == BotState.SELECTING:
            market = await self._select_market_async()
            if market is None:
                # May have been interrupted by PAUSE/STOP while selecting.
                if self._risk.state != BotState.SELECTING:
                    await self._update_dashboard(self._current_market)
                    return

                logger.warning("No suitable market found; retrying in 60s")
                await self._update_dashboard(self._current_market)
                await self._sleep_with_command_checks(60.0)
                return

            if self._risk.state != BotState.SELECTING:
                logger.info("Selection result ignored because state changed to %s", self._risk.state)
                await self._update_dashboard(self._current_market)
                return

            if self.cfg.market_selection_mode == "AUTO":
                if (
                    market.daily_reward_est < self.cfg.min_daily_reward_usd
                    and not self.cfg.allow_under_min_payout
                ):
                    logger.warning(
                        "Best market daily_reward_est=%.3f < MIN=%.2f; skipping",
                        market.daily_reward_est, self.cfg.min_daily_reward_usd,
                    )
                    await self._update_dashboard(self._current_market)
                    await self._sleep_with_command_checks(60.0)
                    return

            logger.info(
                "Entering RUNNING on market: %s (mid=%.4f reward_est=%.3f/day)",
                market.question[:60], market.midpoint, market.daily_reward_est,
            )
            self._current_market = market
            self._risk.set_running()
            self._selector._last_run = time.monotonic()
            await self._update_dashboard(self._current_market)

        elif state == BotState.RUNNING:
            market = self._current_market
            if market is None:
                self._risk.set_selecting()
                await self._update_dashboard(self._current_market)
                return

            await self._poll_rest(market)

            mid = self._get_mid()

            seconds_to_close = None
            if hasattr(self._selector, "get_seconds_to_close"):
                try:
                    seconds_to_close = self._selector.get_seconds_to_close(market)
                except Exception:
                    seconds_to_close = None

            if self._risk.check_kill_switch(seconds_to_close):
                self._orders.cancel_all(market)
                self._open_orders = []
                await self._update_dashboard(market)
                return

            if self._risk.check_jump(mid):
                self._orders.cancel_all(market)
                self._open_orders = []
                await self._update_dashboard(market)
                return

            if self._risk.check_position_cap(self._positions):
                self._orders.cancel_all(market)
                self._open_orders = []
                await self._update_dashboard(market)
                return

            if self._risk.check_reprice(mid):
                logger.info("Repricing orders at mid=%.4f", mid)
                self._orders.sync_desired_orders(market, mid)
                self._open_orders = self._orders.get_open_orders()
                self._risk.update_quote_mid(mid)
                await self._update_dashboard(market)
                return

            if self.cfg.market_selection_mode == "AUTO" and self._selector.should_reselect():
                logger.info("Reselect timer expired — SELECTING")
                self._orders.cancel_all(market)
                self._open_orders = []
                self._risk.set_selecting()
                await self._update_dashboard(market)
                return

            self._orders.sync_desired_orders(market, mid)
            self._open_orders = self._orders.get_open_orders()
            self._risk.update_quote_mid(mid)

            await self._update_dashboard(market)
            await self._sleep_with_command_checks(float(self.cfg.poll_fallback_seconds))

        elif state == BotState.COOLDOWN:
            if self._risk.check_cooldown_expired():
                await self._update_dashboard(self._current_market)
                return

            market = self._current_market
            if market:
                self._orders.cancel_all(market)
                self._open_orders = []
                self._orders.sync_desired_orders(market, self._get_mid(), skip_post=True)

            logger.info(
                "In COOLDOWN — waiting %.0fs",
                self._risk._cooldown_until - time.monotonic(),
            )
            await self._update_dashboard(market)
            await self._sleep_with_command_checks(10.0)

        elif state == BotState.UNHEDGED:
            if self._risk.check_unhedged_timeout():
                await self._update_dashboard(self._current_market)
                return

            market = self._current_market
            if not market:
                self._risk.enter_cooldown("unhedged no market")
                await self._update_dashboard(self._current_market)
                return

            await self._poll_rest(market)

            if self._is_exposure_small(market):
                logger.info("UNHEDGED: exposure already small → back to RUNNING")
                self._risk.exit_unhedged_success()
                await self._update_dashboard(market)
                return

            placed = await self._attempt_hedge(market)
            await self._update_dashboard(market)

            if not placed:
                await self._sleep_with_command_checks(2.0)
            else:
                await self._sleep_with_command_checks(1.0)

        elif state == BotState.PAUSED:
            await self._update_dashboard(self._current_market)
            await self._sleep_with_command_checks(2.0)

    # ------------------------------------------------------------------
    # Dashboard controls
    # ------------------------------------------------------------------

    def _apply_dashboard_overrides(self) -> None:
        raw = get_runtime_overrides() or {}

        overrides = dict(raw)

        mode_override = str(overrides.get("mode", "") or "").strip().upper()
        if not mode_override:
            overrides.pop("mode", None)
        else:
            overrides["mode"] = mode_override

        market_selection_mode = str(
            overrides.get("market_selection_mode", "") or ""
        ).strip().upper()
        if not market_selection_mode:
            overrides.pop("market_selection_mode", None)
        else:
            overrides["market_selection_mode"] = market_selection_mode

        try:
            self.cfg.apply_runtime_overrides(self._base_cfg, overrides)
        except Exception as exc:
            logger.error("Failed to apply runtime overrides: %s", exc, exc_info=True)

    async def _handle_dashboard_command(self) -> None:
        cmd = pop_control_command()
        if not cmd:
            return

        if cmd == "START":
            logger.info("Dashboard START command received")

            # reset cancellation + selector timer
            self._selection_cancel.clear()
            self._selector._last_run = 0

            self._risk.set_selecting()
            await self._update_dashboard(self._current_market)
            return

        if cmd == "PAUSE":
            logger.info("Dashboard PAUSE command received")
            self._selection_cancel.set()
            market = self._current_market
            if market:
                try:
                    self._orders.cancel_all(market)
                    self._open_orders = []
                except Exception as exc:
                    logger.warning("PAUSE cancel_all failed: %s", exc)
            self._risk.set_paused()
            await self._update_dashboard(self._current_market)
            return

        if cmd == "STOP":
            logger.info("Dashboard STOP command received")
            self._selection_cancel.set()
            market = self._current_market
            if market:
                try:
                    self._orders.cancel_all(market)
                except Exception as exc:
                    logger.warning("STOP cancel_all failed: %s", exc)
            self._current_market = None
            self._open_orders = []
            self._positions = []
            self._risk.set_paused()
            logger.info("Bot stopped — waiting for START command")
            await self._update_dashboard(self._current_market)
            return

    async def _sleep_with_command_checks(self, seconds: float, step: float = 0.2) -> None:
        """
        Sleep in small increments so dashboard commands remain responsive
        even while the bot is in RUNNING / COOLDOWN / UNHEDGED / PAUSED waits.
        """
        end_at = time.monotonic() + max(0.0, float(seconds))

        while self._running:
            remaining = end_at - time.monotonic()
            if remaining <= 0:
                return

            await self._handle_dashboard_command()

            if not self._running:
                return

            await asyncio.sleep(min(step, remaining))

    # ------------------------------------------------------------------
    # Market selection
    # ------------------------------------------------------------------

    async def _select_market_async(self) -> Optional[MarketInfo]:
        """
        Run market selection without blocking the event loop, so dashboard
        commands like PAUSE/STOP can still be processed while selector is busy.
        """
        self._selection_cancel.clear()

        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, self._select_market_blocking)

        while True:
            if future.done():
                try:
                    return future.result()
                except Exception as exc:
                    logger.error("Market selection failed: %s", exc, exc_info=True)
                    return None

            await self._handle_dashboard_command()

            if self._risk.state != BotState.SELECTING:
                self._selection_cancel.set()
                logger.info("Selection interrupted by state change → %s", self._risk.state)
                return None

            if self._selection_cancel.is_set():
                logger.info("Selection interrupted by cancellation request")
                return None

            await asyncio.sleep(0.2)

    def _select_market_blocking(self) -> Optional[MarketInfo]:
        if self.cfg.market_selection_mode == "MANUAL":
            return self._load_manual_market()
        return self._selector.select_best_market(
            force=self._selector.should_reselect(),
            cancel_event=self._selection_cancel,
        )

    def _load_manual_market(self) -> Optional[MarketInfo]:
        dashboard_ref = get_manual_market_ref()
        if dashboard_ref:
            raw = dashboard_ref
            logger.info("Using manual market reference from dashboard")
        else:
            path = Path(self.cfg.manual_market_file)
            if not path.is_absolute():
                path = Path(__file__).parent.parent / path

            if not path.exists():
                logger.error("Manual market file not found: %s", path)
                return None

            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error("Failed to read manual market file %s: %s", path, exc)
                return None

            if not isinstance(raw, dict):
                logger.error("Manual market file must contain a JSON object: %s", path)
                return None

        resolved_raw = self._resolve_manual_market_reference(raw)
        source_raw = resolved_raw if resolved_raw is not None else raw

        try:
            market = self._parse_manual_market(source_raw)
        except Exception as exc:
            logger.error("Invalid manual market config: %s", exc, exc_info=True)
            return None

        try:
            book = self._client.get_orderbook(market.yes_token_id)
            bids = book.get("bids", []) if isinstance(book, dict) else []
            asks = book.get("asks", []) if isinstance(book, dict) else []
            if bids and asks:
                market.midpoint = (float(bids[0][0]) + float(asks[0][0])) / 2.0
        except Exception as exc:
            logger.warning("Manual market midpoint refresh failed: %s", exc)

        logger.info(
            "Manual market loaded: market_id=%s question=%s",
            market.market_id,
            market.question[:80],
        )
        return market

    def _resolve_manual_market_reference(self, raw: dict) -> Optional[dict]:
        ref_keys = ("url", "slug", "market_slug", "condition_id", "market_id")
        has_ref = any(str(raw.get(k) or "").strip() for k in ref_keys)

        if not has_ref:
            return None

        try:
            resolved = self._client.resolve_manual_market(raw)
        except Exception as exc:
            logger.error("Manual market resolution failed: %s", exc, exc_info=True)
            return None

        if not resolved:
            logger.error("Manual market reference did not match any rewards market")
            return None

        logger.info(
            "Manual market reference resolved: market_id=%s condition_id=%s",
            resolved.get("market_id", ""),
            resolved.get("condition_id", ""),
        )
        return resolved

    def _parse_manual_market(self, raw: dict) -> MarketInfo:
        def _to_float(v, default=0.0) -> float:
            if v is None or v == "":
                return float(default)
            return float(v)

        def _build_reward_params(
            token_id: str,
            reward_raw: Optional[dict],
            fallback_budget: float,
        ) -> RewardParams:
            if reward_raw:
                return RewardParams(
                    token_id=str(reward_raw.get("token_id") or token_id),
                    min_incentive_size=_to_float(
                        reward_raw.get("min_incentive_size"),
                        raw.get("min_incentive_size", 0.0),
                    ),
                    max_incentive_spread=_to_float(
                        reward_raw.get("max_incentive_spread"),
                        raw.get("rewards_max_spread", raw.get("max_incentive_spread", 0.0)),
                    ),
                    reward_epoch_daily_budget=_to_float(
                        reward_raw.get("reward_epoch_daily_budget"),
                        reward_raw.get("daily_budget", fallback_budget),
                    ),
                    in_game_multiplier=_to_float(
                        reward_raw.get("in_game_multiplier", 1.0),
                        1.0,
                    ),
                )

            return RewardParams(
                token_id=str(token_id),
                min_incentive_size=_to_float(
                    raw.get("min_incentive_size", raw.get("rewards_min_size")),
                    0.0,
                ),
                max_incentive_spread=_to_float(
                    raw.get("max_incentive_spread", raw.get("rewards_max_spread")),
                    0.0,
                ),
                reward_epoch_daily_budget=fallback_budget,
                in_game_multiplier=_to_float(raw.get("in_game_multiplier", 1.0), 1.0),
            )

        market_id = str(raw.get("market_id") or raw.get("condition_id") or "")
        condition_id = str(raw.get("condition_id") or market_id)
        question = str(
            raw.get("question")
            or raw.get("market_slug")
            or raw.get("slug")
            or "Manual market"
        )

        yes_token_id = str(raw.get("yes_token_id") or raw.get("token_id_yes") or "")
        no_token_id = str(raw.get("no_token_id") or raw.get("token_id_no") or "")

        tokens = raw.get("tokens")
        if isinstance(tokens, list) and tokens:
            if not yes_token_id and len(tokens) >= 1 and isinstance(tokens[0], dict):
                yes_token_id = str(tokens[0].get("token_id") or "")
            if not no_token_id and len(tokens) >= 2 and isinstance(tokens[1], dict):
                no_token_id = str(tokens[1].get("token_id") or "")

        if not market_id:
            raise ValueError("manual market must include market_id or condition_id")
        if not yes_token_id:
            raise ValueError("manual market must include yes_token_id or token_id_yes")

        budget_fallback = _to_float(
            raw.get("reward_epoch_daily_budget"),
            raw.get("daily_budget", 0.0),
        )

        rewards_cfg = raw.get("rewards_config")
        if isinstance(rewards_cfg, list):
            budget_fallback = 0.0
            for item in rewards_cfg:
                if isinstance(item, dict):
                    budget_fallback += _to_float(item.get("rate_per_day"), 0.0)

        reward_params_yes_raw = raw.get("reward_params_yes")
        reward_params_no_raw = raw.get("reward_params_no")

        rp_yes = _build_reward_params(
            token_id=yes_token_id,
            reward_raw=reward_params_yes_raw if isinstance(reward_params_yes_raw, dict) else None,
            fallback_budget=budget_fallback,
        )

        rp_no = None
        if no_token_id:
            rp_no = _build_reward_params(
                token_id=no_token_id,
                reward_raw=reward_params_no_raw if isinstance(reward_params_no_raw, dict) else None,
                fallback_budget=budget_fallback,
            )

        market = MarketInfo(
            market_id=market_id,
            condition_id=condition_id,
            question=question,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            reward_params_yes=rp_yes,
            reward_params_no=rp_no,
            midpoint=_to_float(raw.get("midpoint"), 0.5),
            share_est=_to_float(raw.get("share_est"), 0.0),
            daily_reward_est=_to_float(raw.get("daily_reward_est"), 0.0),
            reward_per_capital=_to_float(raw.get("reward_per_capital"), 0.0),
            capital_required=_to_float(raw.get("capital_required"), 0.0),
        )

        seconds_to_close = None
        if hasattr(self._selector, "_extract_seconds_to_close"):
            try:
                seconds_to_close = self._selector._extract_seconds_to_close(raw)
            except Exception:
                seconds_to_close = None

        if str(market.market_id) not in self._selector._meta_by_market_id:
            self._selector._meta_by_market_id[str(market.market_id)] = {}

        if seconds_to_close is not None:
            self._selector._meta_by_market_id[str(market.market_id)]["seconds_to_close"] = float(seconds_to_close)

        return market

    # ------------------------------------------------------------------
    # Exposure helpers
    # ------------------------------------------------------------------

    def _market_exposure_usd(self, market) -> float:
        token_ids = {market.yes_token_id, market.no_token_id}
        exp = 0.0
        for p in self._positions:
            if p.token_id in token_ids:
                exp += abs(float(p.size) * float(p.avg_price or 0.5))
        return exp

    def _is_exposure_small(self, market) -> bool:
        exp = self._market_exposure_usd(market)
        threshold = min(self.cfg.max_position_usd * 0.10, self.cfg.usable_capital * 0.10)
        threshold = max(1.0, threshold)
        return exp <= threshold

    def _compute_committed_capital(self) -> float:
        open_orders_notional = 0.0
        for o in self._open_orders:
            try:
                if o.side == Side.BUY:
                    open_orders_notional += float(o.price) * float(o.size_remaining)
            except Exception:
                continue

        positions_notional = 0.0
        for p in self._positions:
            try:
                positions_notional += abs(float(p.size) * float(p.avg_price or 0.5))
            except Exception:
                continue

        return open_orders_notional + positions_notional

    async def _refresh_wallet_balance(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._available_usdc_last_refresh) < 30:
            return

        loop = asyncio.get_running_loop()
        try:
            bal = await loop.run_in_executor(None, self._client.get_available_usdc)
            self._available_usdc = bal
            self._available_usdc_last_refresh = now
        except Exception as exc:
            logger.warning("Wallet USDC refresh failed: %s", exc)
            self._available_usdc_last_refresh = now

    # ------------------------------------------------------------------
    # REST polling
    # ------------------------------------------------------------------

    async def _poll_rest(self, market) -> None:
        loop = asyncio.get_running_loop()
        try:
            raw_orders = await loop.run_in_executor(None, self._client.get_open_orders)
            self._open_orders = _parse_open_orders(raw_orders, market)
        except Exception as exc:
            logger.warning("REST poll orders failed: %s", exc)

        try:
            raw_pos = await loop.run_in_executor(None, self._client.get_positions)
            self._positions = _parse_positions(raw_pos)
        except Exception as exc:
            logger.warning("REST poll positions failed: %s", exc)

        try:
            book = await loop.run_in_executor(None, self._client.get_orderbook, market.yes_token_id)
            bids = book.get("bids", [])
            asks = book.get("asks", [])
            if bids and asks:
                market.midpoint = (float(bids[0][0]) + float(asks[0][0])) / 2.0
        except Exception as exc:
            logger.warning("REST poll book failed: %s", exc)

    # ------------------------------------------------------------------
    # Hedge attempt
    # ------------------------------------------------------------------

    async def _attempt_hedge(self, market) -> bool:
        mid_yes = self._get_mid()
        mid_no = 1.0 - mid_yes

        token_to_mid = {
            market.yes_token_id: mid_yes,
            market.no_token_id: mid_no,
        }

        min_size_by_token = {
            market.yes_token_id: market.reward_params_yes.min_incentive_size,
        }
        if market.no_token_id and market.reward_params_no:
            min_size_by_token[market.no_token_id] = market.reward_params_no.min_incentive_size

        relevant = [p for p in self._positions if p.token_id in token_to_mid]
        if not relevant:
            return False

        relevant.sort(key=lambda p: abs(float(p.size) * float(p.avg_price or 0.5)), reverse=True)

        for pos in relevant:
            token_id = pos.token_id
            pos_size = float(pos.size)
            if abs(pos_size) <= 0:
                continue

            mid = float(token_to_mid.get(token_id, 0.5))
            min_size = float(min_size_by_token.get(token_id, 0.0) or 0.0)

            if pos_size > 0:
                hedge_side = "SELL"
                hedge_price = min(0.99, round(mid + TICK_SIZE, 4))
                max_hedge_size = abs(pos_size)
            else:
                hedge_side = "BUY"
                hedge_price = max(0.01, round(mid - TICK_SIZE, 4))
                max_hedge_size = abs(pos_size)

            hedge_fraction = 0.5
            target = max_hedge_size * hedge_fraction

            if hedge_price > 0:
                target = min(target, self.cfg.max_position_usd / hedge_price)

            if hedge_side == "BUY" and hedge_price > 0:
                target = min(target, self.cfg.usable_capital / hedge_price)

            if min_size > 0 and target + 1e-9 < min_size:
                target = max_hedge_size
                if hedge_price > 0:
                    target = min(target, self.cfg.max_position_usd / hedge_price)
                    if hedge_side == "BUY":
                        target = min(target, self.cfg.usable_capital / hedge_price)

                if target + 1e-9 < min_size:
                    logger.warning(
                        "Hedge skipped: token=%s target=%.4f < min_size=%.4f (side=%s)",
                        token_id, target, min_size, hedge_side
                    )
                    continue

            hedge_size = min(target, max_hedge_size)

            try:
                self._client.place_order(token_id, hedge_side, hedge_price, hedge_size)
                logger.info(
                    "Hedge order placed: %s token=%s size=%.4f @ %.4f (pos=%.4f)",
                    hedge_side, token_id, hedge_size, hedge_price, pos_size
                )
                return True
            except Exception as exc:
                logger.error("Hedge failed token=%s: %s", token_id, exc)

        return False

    # ------------------------------------------------------------------
    # WebSocket listeners (best-effort)
    # ------------------------------------------------------------------

    async def _ws_book_listener(self):
        current_token: Optional[str] = None

        while self._running:
            market = self._current_market
            if not market:
                await asyncio.sleep(1)
                continue

            token_id = market.yes_token_id
            if current_token == token_id:
                await asyncio.sleep(1)
                continue

            current_token = token_id
            logger.info("WS book: subscribing to token=%s", token_id)

            def on_book(data):
                if isinstance(data, list):
                    data = data[0] if data else {}
                bids = data.get("bids") or data.get("bid_levels", [])
                asks = data.get("asks") or data.get("ask_levels", [])
                if bids and asks:
                    try:
                        mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
                        m = self._current_market
                        if m and m.yes_token_id == token_id:
                            m.midpoint = mid
                    except Exception:
                        pass

            try:
                await self._client.ws_subscribe_book(token_id, on_book)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("WS book listener error: %s — retrying", exc)
                await asyncio.sleep(2)

    async def _ws_user_listener(self):
        def on_user(data: dict):
            if data.get("type") == "fill":
                fill = Fill(
                    order_id=data.get("order_id", ""),
                    token_id=data.get("asset_id", ""),
                    side=Side.BUY if data.get("side") in ("BUY", "B") else Side.SELL,
                    price=float(data.get("price", 0)),
                    size=float(data.get("size", 0)),
                    timestamp=utc_iso(),
                )
                self._fills.append(fill)
                self._risk.on_fill(fill, self._positions)

                market = self._current_market
                if market:
                    try:
                        self._orders.cancel_all(market)
                        self._open_orders = []
                    except Exception as exc:
                        logger.warning("Immediate cancel_all after fill failed: %s", exc)

        try:
            await self._client.ws_subscribe_user(self.cfg.poly_l2_api_key, on_user)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("WS user listener exited: %s", exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_mid(self) -> float:
        m = self._current_market
        return m.midpoint if m else 0.5

    async def _update_dashboard(self, market: Optional[MarketInfo]) -> None:
        snap = DashboardSnapshot(
            state=self._risk.state,
            market=market,
            open_orders=self._open_orders,
            positions=self._positions,
            recent_fills=self._fills[-20:],
            last_updated=utc_iso(),
        )
        update_snapshot(snap)

        update_runtime_status(
            {
                "available_usdc": self._available_usdc,
                "usable_capital": self.cfg.usable_capital,
                "committed_capital": self._compute_committed_capital(),
                "config_effective": {
                    "market_selection_mode": self.cfg.market_selection_mode,
                    "mode": self.cfg.mode,
                    "bankroll_usdc": self.cfg.bankroll_usdc,
                    "max_capital_per_market": self.cfg.max_capital_per_market,
                    "free_usdc_buffer_pct": self.cfg.free_usdc_buffer_pct,
                    "max_position_usd": self.cfg.max_position_usd,
                    "target_min_distance": self.cfg.target_min_distance,
                    "target_max_distance": self.cfg.target_max_distance,
                },
            }
        )

    def stop(self):
        logger.info("RewardBot shutdown requested")
        self._selection_cancel.set()
        self._running = False
        for t in self._ws_tasks:
            t.cancel()


def _parse_open_orders(raw: list[dict], market) -> list[OpenOrder]:
    token_ids = {market.yes_token_id, market.no_token_id}
    orders = []
    for r in raw:
        tid = r.get("asset_id") or r.get("token_id", "")
        if tid not in token_ids:
            continue

        side = Side.BUY if str(r.get("side", "BUY")).upper() in ("BUY", "B") else Side.SELL

        original = float(r.get("original_size", r.get("size", 0)) or 0)
        matched = float(r.get("size_matched", r.get("filled_size", 0)) or 0)
        remaining = r.get("size_remaining", None)
        if remaining is None:
            size_remaining = max(0.0, original - matched)
        else:
            try:
                size_remaining = float(remaining)
            except Exception:
                size_remaining = max(0.0, original - matched)

        orders.append(OpenOrder(
            order_id=r.get("id", ""),
            token_id=tid,
            side=side,
            price=float(r.get("price", 0) or 0),
            size_remaining=size_remaining,
        ))
    return orders


def _parse_positions(raw: list[dict]) -> list[Position]:
    positions = []
    for r in raw:
        size = float(r.get("size", r.get("balance", 0)) or 0)
        if size == 0:
            continue
        positions.append(Position(
            token_id=r.get("asset_id") or r.get("token_id", ""),
            size=size,
            avg_price=float(r.get("avg_price", 0.5) or 0.5),
        ))
    return positions


def _run_dashboard(host: str = "0.0.0.0", port: int = 8090):
    config = uvicorn.Config(
        dashboard_app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.run()


async def _async_main():
    bot = RewardBot()

    def _sig_handler(sig, frame):
        logger.info("Signal %s received — shutting down", sig)
        bot.stop()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    await bot.run()


def main():
    setup_logging()
    logging.getLogger("reward_bot").setLevel(logging.INFO)

    logger.info("=" * 60)
    logger.info("Polymarket Reward-Aware Conservative Liquidity Bot v1.0")
    logger.info("=" * 60)

    dash_thread = threading.Thread(target=_run_dashboard, daemon=True)
    dash_thread.start()
    logger.info("Dashboard started at http://localhost:8090")

    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt received — shutting down")


if __name__ == "__main__":
    main()