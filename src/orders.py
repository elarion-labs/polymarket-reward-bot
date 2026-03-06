"""
orders.py — OrderManager: maintains desired vs actual order state.

Responsibilities:
  - Compute desired orders from market state and config
  - Reconcile desired vs open orders (cancel/replace)
  - Validate tick size and capital limits
  - Support SINGLE_SIDED_SAFE and TWO_SIDED_BALANCED modes
"""
from __future__ import annotations

import logging
from typing import Optional

from config import Config
from models import DesiredOrder, MarketInfo, OpenOrder, Side
from poly_client import PolyClient
from utils import round_price, round_size

logger = logging.getLogger("reward_bot")

TICK_SIZE = 0.01       # Polymarket standard tick
MIN_ORDER_SIZE = 5.0   # fallback minimum if not specified by market


class OrderManager:
    """
    Manages the bot's order lifecycle.

    State:
        _desired: list of DesiredOrder (what we want in the book)
        _open:    list of OpenOrder   (what is currently live)
    """

    def __init__(self, client: PolyClient, config: Config):
        self._client = client
        self._cfg = config
        self._desired: list[DesiredOrder] = []
        self._open: list[OpenOrder] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def sync_desired_orders(
        self,
        market: MarketInfo,
        midpoint: float,
        skip_post: bool = False,
    ) -> None:
        """
        Compute desired orders from market + config, then reconcile with
        live orders (cancel stale, place new).

        Args:
            market:     current selected MarketInfo
            midpoint:   latest midpoint price
            skip_post:  if True, cancel all but don't post new (e.g. cooldown)
        """
        self._desired = self._compute_desired(market, midpoint)
        self._open = self._fetch_open(market)
        self._reconcile(market, skip_post=skip_post)

    def cancel_all(self, market: MarketInfo) -> None:
        """Cancel all open orders for this market."""
        token_ids = [market.yes_token_id]
        if market.no_token_id:
            token_ids.append(market.no_token_id)
        self._client.cancel_all_orders(token_ids=token_ids)
        self._open = []
        logger.info("cancel_all: cancelled all orders for market %s", market.market_id)

    def get_open_orders(self) -> list[OpenOrder]:
        return list(self._open)

    # ------------------------------------------------------------------
    # Desired order computation
    # ------------------------------------------------------------------

    def _compute_desired(
        self,
        market: MarketInfo,
        midpoint: float,
    ) -> list[DesiredOrder]:
        cfg = self._cfg
        rp = market.reward_params_yes
        min_size = rp.min_incentive_size if rp else MIN_ORDER_SIZE
        v = rp.max_incentive_spread if rp else 0.02

        desired: list[DesiredOrder] = []

        # Target price: mid minus TARGET_MIN_DISTANCE (conservative)
        distance = (cfg.target_min_distance + cfg.target_max_distance) / 2.0
        distance = min(distance, v - TICK_SIZE)   # must be within eligible spread

        bid_price = round_price(midpoint - distance, TICK_SIZE)
        bid_price = max(0.01, min(0.99, bid_price))

        # Capital-constrained size
        capital_available = min(
            cfg.usable_capital,
            cfg.max_capital_per_market,
        )

        # USDC cost of a YES bid = price * size
        max_size_by_capital = capital_available / bid_price if bid_price > 0 else 0.0

        # HARD SAFETY GUARD:
        # If we can't afford the market's min_incentive_size, do NOT post anything.
        if max_size_by_capital + 1e-9 < min_size:
            logger.warning(
                "Not enough capital for min_incentive_size: need %.2f (min_size=%.2f @ price=%.4f), have %.2f. Skipping posting.",
                min_size * bid_price,
                min_size,
                bid_price,
                capital_available,
            )
            return []

        bid_size = round_size(max_size_by_capital, min_size)
        bid_size = max(bid_size, min_size)

        # Ensure we don't exceed MAX_POSITION_USD in notional
        bid_size = min(bid_size, cfg.max_position_usd / bid_price if bid_price > 0 else bid_size)
        bid_size = max(bid_size, min_size)

        desired.append(DesiredOrder(
            token_id=market.yes_token_id,
            side=Side.BUY,
            price=bid_price,
            size=bid_size,
            tag="YES_BID",
        ))

        if cfg.mode == "TWO_SIDED_BALANCED" and market.no_token_id and market.reward_params_no:
            rp_no = market.reward_params_no
            min_size_no = rp_no.min_incentive_size

            # Complement: NO bid at (1 - ask_price) equivalent distance
            mid_no = 1.0 - midpoint
            no_bid_price = round_price(mid_no - distance, TICK_SIZE)
            no_bid_price = max(0.01, min(0.99, no_bid_price))

            # Split capital evenly between YES and NO
            cap_no = capital_available / 2.0
            max_no_size_by_capital = cap_no / no_bid_price if no_bid_price > 0 else 0.0

            # Same safety guard for NO leg
            if max_no_size_by_capital + 1e-9 < min_size_no:
                logger.warning(
                    "Not enough capital for NO min_incentive_size: need %.2f (min_size=%.2f @ price=%.4f), have %.2f. Skipping NO leg.",
                    min_size_no * no_bid_price,
                    min_size_no,
                    no_bid_price,
                    cap_no,
                )
            else:
                no_size = round_size(max_no_size_by_capital, min_size_no)
                no_size = max(no_size, min_size_no)
                no_size = min(no_size, cfg.max_position_usd / no_bid_price if no_bid_price > 0 else no_size)
                no_size = max(no_size, min_size_no)

                desired.append(DesiredOrder(
                    token_id=market.no_token_id,
                    side=Side.BUY,
                    price=no_bid_price,
                    size=no_size,
                    tag="NO_BID",
                ))

        logger.debug("desired orders: %s", [d.model_dump() for d in desired])
        return desired

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def _fetch_open(self, market: MarketInfo) -> list[OpenOrder]:
        """Fetch and parse open orders for this market's tokens."""
        token_ids = {market.yes_token_id, market.no_token_id}
        try:
            raw = self._client.get_open_orders()
        except Exception as exc:
            logger.warning("_fetch_open failed: %s", exc)
            return []

        orders: list[OpenOrder] = []
        for r in raw:
            token_id = r.get("asset_id") or r.get("token_id", "")
            if token_id not in token_ids:
                continue

            side_raw = str(r.get("side", "BUY")).upper()
            side = Side.BUY if side_raw in ("BUY", "B") else Side.SELL

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
                token_id=token_id,
                side=side,
                price=float(r.get("price", 0) or 0),
                size_remaining=size_remaining,
                created_at=r.get("created_at"),
            ))
        return orders

    def _reconcile(self, market: MarketInfo, skip_post: bool = False) -> None:
        """
        Cancel orders that differ from desired; place missing desired orders.

        Idempotent: only cancels/places what's needed.
        """
        open_by_tag = self._index_open_by_token_side()

        for desired in self._desired:
            key = (desired.token_id, desired.side)
            existing = open_by_tag.get(key)

            if existing:
                # Check if price matches (within tolerance)
                price_ok = abs(existing.price - desired.price) < TICK_SIZE / 2
                if price_ok:
                    logger.debug("reconcile: order %s OK (no change needed)", key)
                    continue

                # Price drifted → cancel and repost
                logger.info(
                    "reconcile: repricing %s from %.4f → %.4f",
                    key, existing.price, desired.price,
                )
                self._client.cancel_order(existing.order_id)

            if not skip_post:
                self._post(desired)

        # Cancel any orphan orders not in desired
        desired_keys = {(d.token_id, d.side) for d in self._desired}
        for key, order in open_by_tag.items():
            if key not in desired_keys:
                logger.info("reconcile: cancelling orphan order %s", order.order_id)
                self._client.cancel_order(order.order_id)

    def _index_open_by_token_side(self) -> dict[tuple, OpenOrder]:
        idx: dict[tuple, OpenOrder] = {}
        for o in self._open:
            idx[(o.token_id, o.side)] = o
        return idx

    def _post(self, order: DesiredOrder) -> None:
        try:
            resp = self._client.place_order(
                token_id=order.token_id,
                side=order.side.value,
                price=order.price,
                size=order.size,
            )
            logger.info(
                "placed order tag=%s token=%s side=%s price=%.4f size=%.2f resp=%s",
                order.tag, order.token_id, order.side, order.price, order.size, resp,
            )
        except Exception as exc:
            logger.error("failed to place order %s: %s", order.tag, exc)