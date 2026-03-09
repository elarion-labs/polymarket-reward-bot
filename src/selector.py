"""
selector.py — MarketSelector: discovers and ranks markets for the bot.

Runs every MARKET_RESELECT_MINUTES.  Applies hard filters, simulates bot
orders, estimates reward_per_capital, and returns the TOP-1 market.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Any

from config import Config
from models import MarketInfo, RewardParams
from poly_client import PolyClient, PolyClientError
from rewards import RewardEstimator

logger = logging.getLogger("reward_bot")

# ---------------------------------------------------------------------------
# Hard filter constants
# ---------------------------------------------------------------------------
MIN_BOOK_DEPTH = 2
MIN_MIDPOINT = 0.05
MAX_MIDPOINT = 0.95
MIN_REWARD_BUDGET = 0.0

# Rewards-page signals (important for small bankrolls)
SMALL_BANKROLL_CAP_USD = 50.0
MAX_COMPETITIVENESS_SMALL = 0.60   # ignore very competitive markets for small bankroll
COMPETITIVENESS_PENALTY_K = 4.0    # reward_per_capital /= (1 + K*competitiveness)

# Rewards pagination (critical when small bankroll filters out first page)
REWARDS_PAGE_LIMIT = 100
REWARDS_MAX_PAGES = 20


class MarketSelector:
    """
    Selects the best market to provide liquidity on, based on estimated
    reward_per_capital.
    """

    def __init__(self, client: PolyClient, config: Config):
        self._client = client
        self._cfg = config
        self._last_run: float = 0.0
        self._selected: Optional[MarketInfo] = None

        # store extra rewards-page fields here (avoid mutating MarketInfo / models.py)
        # key: market_id (string); value: {"market_competitiveness": float, "spread": float, "seconds_to_close": float}
        self._meta_by_market_id: dict[str, dict[str, float]] = {}

    def should_reselect(self) -> bool:
        elapsed = time.monotonic() - self._last_run
        return elapsed >= self._cfg.market_reselect_seconds

    def get_seconds_to_close(self, market: MarketInfo) -> Optional[float]:
        """
        Best-effort seconds-to-close pulled from rewards metadata.
        Returns None if not available.
        """
        meta = self._meta_by_market_id.get(str(market.market_id), {})
        if "seconds_to_close" in meta:
            try:
                return float(meta["seconds_to_close"])
            except Exception:
                return None
        return None

    def _cancelled(self, cancel_event: Any) -> bool:
        return bool(cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set())

    def select_best_market(self, force: bool = False, cancel_event: Any = None) -> Optional[MarketInfo]:
        if self._cancelled(cancel_event):
            logger.info("MarketSelector: selection cancelled before start")
            return self._selected

        if not force and not self.should_reselect():
            return self._selected

        logger.info("MarketSelector: starting selection run")
        self._last_run = time.monotonic()

        candidates: list[MarketInfo] = []

        # IMPORTANT: paginate rewards results, because small bankroll filters
        # often make page-1 unusable (min_size too high).
        next_cursor = "MA=="
        seen_cursors: set[str] = set()

        for page in range(1, REWARDS_MAX_PAGES + 1):
            if self._cancelled(cancel_event):
                logger.info("MarketSelector: selection cancelled during pagination")
                return self._selected

            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)

            try:
                resp = self._client.get_reward_markets_page(
                    next_cursor=next_cursor,
                    limit=REWARDS_PAGE_LIMIT,
                )
                raw_markets = resp.get("data", []) or []
            except Exception as exc:
                logger.error("MarketSelector: failed to fetch markets: %s", exc)
                return self._selected

            logger.debug(
                "MarketSelector: rewards page %d got %d markets (cursor=%s)",
                page, len(raw_markets), next_cursor
            )

            for raw in raw_markets:
                if self._cancelled(cancel_event):
                    logger.info("MarketSelector: selection cancelled while scoring markets")
                    return self._selected

                try:
                    info = self._parse_market(raw)
                    if info is None:
                        continue
                    info = self._score_market(info, cancel_event=cancel_event)
                    candidates.append(info)
                except Exception as exc:
                    logger.warning(
                        "MarketSelector: skipping market %s — %s",
                        raw.get("condition_id", "?"),
                        exc,
                    )
                    continue

            # Advance cursor
            next_cursor = resp.get("nextCursor") or resp.get("next_cursor") or ""

            # Only stop early if we already found a VERY good candidate
            if len(candidates) >= 10 and candidates[0].reward_per_capital > 0.05:
                break

        if self._cancelled(cancel_event):
            logger.info("MarketSelector: selection cancelled before final ranking")
            return self._selected

        if not candidates:
            logger.warning("MarketSelector: no eligible markets found")
            return None

        candidates.sort(key=lambda m: m.reward_per_capital, reverse=True)
        best = candidates[0]

        logger.info(
            "MarketSelector: selected market_id=%s reward_per_capital=%.4f "
            "daily_reward_est=%.2f capital_required=%.2f",
            best.market_id,
            best.reward_per_capital,
            best.daily_reward_est,
            best.capital_required,
        )

        self._selected = best
        return best

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_seconds_to_close(self, raw: dict) -> Optional[float]:
        """
        Best-effort extraction of close/end timestamp from Rewards payload.

        Tries common keys:
          end_date / endDate / close_time / closeTime / expires_at / expiresAt
        Supports:
          - unix seconds
          - unix milliseconds
          - ISO strings (best-effort via datetime.fromisoformat after normalization)
        """
        import datetime as _dt

        candidates = [
            raw.get("end_date"),
            raw.get("endDate"),
            raw.get("close_time"),
            raw.get("closeTime"),
            raw.get("expires_at"),
            raw.get("expiresAt"),
            raw.get("resolved_at"),
            raw.get("resolvedAt"),
        ]

        # Some payloads nest it
        if isinstance(raw.get("market"), dict):
            m = raw["market"]
            candidates.extend([
                m.get("end_date"),
                m.get("endDate"),
                m.get("close_time"),
                m.get("closeTime"),
                m.get("expires_at"),
                m.get("expiresAt"),
                m.get("resolved_at"),
                m.get("resolvedAt"),
            ])

        now = _dt.datetime.now(tz=_dt.timezone.utc)

        for v in candidates:
            if v is None:
                continue

            # numeric epoch
            if isinstance(v, (int, float)):
                ts = float(v)
                # heuristic: ms vs s
                if ts > 1e12:
                    ts = ts / 1000.0
                try:
                    end = _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
                    return max(0.0, (end - now).total_seconds())
                except Exception:
                    continue

            # string
            if isinstance(v, str) and v.strip():
                s = v.strip()
                # normalize Z
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                try:
                    end = _dt.datetime.fromisoformat(s)
                    if end.tzinfo is None:
                        end = end.replace(tzinfo=_dt.timezone.utc)
                    return max(0.0, (end.astimezone(_dt.timezone.utc) - now).total_seconds())
                except Exception:
                    continue

        return None

    def _parse_market(self, raw: dict) -> Optional[MarketInfo]:
        """
        Parse raw Rewards API dict → MarketInfo.
        """
        condition_id = raw.get("condition_id", "")
        market_id = raw.get("market_id", "") or condition_id
        question = raw.get("question", "")
        tokens = raw.get("tokens", [])

        if not isinstance(tokens, list) or len(tokens) < 1:
            return None

        yes_token = tokens[0]
        no_token = tokens[1] if len(tokens) >= 2 else None

        if not isinstance(yes_token, dict) or not yes_token.get("token_id"):
            return None

        min_size = float(raw.get("rewards_min_size", 0.0))
        max_spread_price = float(raw.get("rewards_max_spread", 0.0))

        rewards_cfg = raw.get("rewards_config") or []
        daily_budget = 0.0
        if isinstance(rewards_cfg, list):
            for r in rewards_cfg:
                if isinstance(r, dict):
                    daily_budget += float(r.get("rate_per_day", 0.0))

        if min_size <= 0 or max_spread_price <= 0:
            return None

        if daily_budget < MIN_REWARD_BUDGET:
            return None

        # --- competitiveness/spread from rewards page
        competitiveness = raw.get("market_competitiveness", 0.0)
        spread = raw.get("spread", 0.0)
        try:
            competitiveness_f = float(competitiveness)
        except Exception:
            competitiveness_f = 0.0
        try:
            spread_f = float(spread)
        except Exception:
            spread_f = 0.0

        # Hard gate for small bankroll
        if self._cfg.usable_capital <= SMALL_BANKROLL_CAP_USD:
            if competitiveness_f > MAX_COMPETITIVENESS_SMALL:
                return None

        rp_yes = RewardParams(
            token_id=str(yes_token["token_id"]),
            min_incentive_size=min_size,
            max_incentive_spread=max_spread_price,
            reward_epoch_daily_budget=daily_budget,
        )

        rp_no = None
        if no_token and isinstance(no_token, dict) and no_token.get("token_id"):
            rp_no = RewardParams(
                token_id=str(no_token["token_id"]),
                min_incentive_size=min_size,
                max_incentive_spread=max_spread_price,
                reward_epoch_daily_budget=daily_budget,
            )

        info = MarketInfo(
            market_id=str(market_id),
            condition_id=str(condition_id),
            question=question,
            yes_token_id=str(yes_token["token_id"]),
            no_token_id=str(no_token["token_id"]) if (no_token and no_token.get("token_id")) else "",
            reward_params_yes=rp_yes,
            reward_params_no=rp_no,
        )

        seconds_to_close = self._extract_seconds_to_close(raw)

        # Store rewards-page meta WITHOUT touching MarketInfo object
        self._meta_by_market_id[str(info.market_id)] = {
            "market_competitiveness": competitiveness_f,
            "spread": spread_f,
            **({"seconds_to_close": float(seconds_to_close)} if seconds_to_close is not None else {}),
        }

        return info

    def _score_market(self, info: MarketInfo, cancel_event: Any = None) -> MarketInfo:
        def _levels_from_book(book_obj: object, side: str) -> list[tuple[float, float]]:
            raw_levels = []
            if isinstance(book_obj, dict):
                raw_levels = book_obj.get(side, []) or []
            else:
                if hasattr(book_obj, side):
                    raw_levels = getattr(book_obj, side) or []
                elif hasattr(book_obj, side.lower()):
                    raw_levels = getattr(book_obj, side.lower()) or []

            out: list[tuple[float, float]] = []
            for lvl in raw_levels:
                p = s = None
                if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
                    p, s = lvl[0], lvl[1]
                elif isinstance(lvl, dict):
                    p = lvl.get("price", lvl.get("p"))
                    s = lvl.get("size", lvl.get("s"))
                else:
                    p = getattr(lvl, "price", getattr(lvl, "p", None))
                    s = getattr(lvl, "size", getattr(lvl, "s", None))

                if p is None or s is None:
                    continue

                try:
                    out.append((float(p), float(s)))
                except Exception:
                    continue
            return out

        if self._cancelled(cancel_event):
            raise RuntimeError("selection cancelled")

        try:
            book_raw = self._client.get_orderbook(info.yes_token_id)
        except PolyClientError as exc:
            raise RuntimeError(
                f"Cannot fetch book for {info.yes_token_id}: {exc}"
            ) from exc

        if self._cancelled(cancel_event):
            raise RuntimeError("selection cancelled")

        bids_t = _levels_from_book(book_raw, "bids")
        asks_t = _levels_from_book(book_raw, "asks")

        book = {
            "bids": [[p, s] for p, s in bids_t],
            "asks": [[p, s] for p, s in asks_t],
        }

        if len(bids_t) < MIN_BOOK_DEPTH or len(asks_t) < MIN_BOOK_DEPTH:
            raise RuntimeError("Insufficient book depth")

        best_bid = float(bids_t[0][0])
        best_ask = float(asks_t[0][0])
        mid = (best_bid + best_ask) / 2.0

        if not (MIN_MIDPOINT <= mid <= MAX_MIDPOINT):
            raise RuntimeError(f"Midpoint {mid:.3f} outside allowed range")

        info.midpoint = mid

        rp = info.reward_params_yes
        cfg = self._cfg

        target_price = round(mid - cfg.target_min_distance, 4)
        target_price = max(0.01, min(0.99, target_price))

        alloc_cap = min(cfg.usable_capital, cfg.max_capital_per_market)
        min_required_cap = rp.min_incentive_size * target_price
        if min_required_cap > alloc_cap:
            raise RuntimeError(
                f"Min incentive unaffordable: need {min_required_cap:.2f} "
                f"(min_size={rp.min_incentive_size:.2f} @ price={target_price:.4f}), "
                f"cap={alloc_cap:.2f}"
            )

        desired_size = max(rp.min_incentive_size, 10.0)
        max_affordable_size = alloc_cap / target_price if target_price > 0 else 0.0
        actual_size = min(desired_size, max_affordable_size)
        actual_size = max(actual_size, rp.min_incentive_size)

        capital_required = actual_size * target_price
        capital_required = min(capital_required, alloc_cap)

        book_no = None
        if info.no_token_id:
            if self._cancelled(cancel_event):
                raise RuntimeError("selection cancelled")

            try:
                book_no_raw = self._client.get_orderbook(info.no_token_id)
                bids_no_t = _levels_from_book(book_no_raw, "bids")
                asks_no_t = _levels_from_book(book_no_raw, "asks")
                book_no = {
                    "bids": [[p, s] for p, s in bids_no_t],
                    "asks": [[p, s] for p, s in asks_no_t],
                }
            except Exception:
                pass

        if self._cancelled(cancel_event):
            raise RuntimeError("selection cancelled")

        estimator = RewardEstimator(rp)
        est = estimator.estimate(
            book=book,
            our_bid_price=target_price,
            our_bid_size=actual_size,
            our_ask_price=None,
            our_ask_size=0.0,
            book_no=book_no,
            capital_required=capital_required,
        )

        info.share_est = est.share_est
        info.daily_reward_est = est.daily_reward_est
        info.reward_per_capital = est.reward_per_capital
        info.capital_required = capital_required

        meta = self._meta_by_market_id.get(str(info.market_id), {})
        comp_f = float(meta.get("market_competitiveness", 0.0) or 0.0)
        if comp_f > 0:
            info.reward_per_capital = info.reward_per_capital / (1.0 + COMPETITIVENESS_PENALTY_K * comp_f)

        return info