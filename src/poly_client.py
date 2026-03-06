"""
poly_client.py — Wrapper around py-clob-client SDK.

All Polymarket REST and WebSocket interactions go through this module.

NOTE:
- SDK return shapes differ by version. This wrapper normalizes responses
  to stable shapes used by the bot.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger("reward_bot")

# ---------------------------------------------------------------------------
# Guard: import SDK with graceful degradation
# ---------------------------------------------------------------------------
try:
    from py_clob_client.clob_types import ApiCreds, OrderArgs
    from py_clob_client.client import ClobClient

    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    logger.warning(
        "py-clob-client not installed or import failed. "
        "PolyClient will run in STUB mode (no real orders placed)."
    )


class PolyClientError(Exception):
    pass


class PolyClient:
    """
    Thin wrapper over py-clob-client with error handling and response normalization.
    """

    def __init__(
        self,
        private_key: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        chain_id: int = 137,
        host: str = "https://clob.polymarket.com",
    ):
        self.address: Optional[str] = None

        self._stub = not _SDK_AVAILABLE or not private_key
        if self._stub:
            logger.warning("PolyClient running in STUB mode — no real API calls.")
            self._client = None
            return

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        self._client = ClobClient(
            host=host,
            key=private_key,
            chain_id=chain_id,
            creds=creds,
        )

        self.address = (
            getattr(self._client, "address", None)
            or getattr(self._client, "maker", None)
            or getattr(self._client, "maker_address", None)
        )
        if not self.address:
            try:
                from eth_account import Account  # type: ignore

                self.address = Account.from_key(private_key).address
            except Exception:
                self.address = None

        logger.info("PolyClient initialised (live mode). address=%s", self.address)

    # ------------------------------------------------------------------
    # Generic coercion helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _coerce_list_response(resp: Any) -> list[dict[str, Any]]:
        """
        Normalize common API shapes to list[dict].
        Accepts:
          - list[dict]
          - {"data": [..]}
          - None
        """
        if resp is None:
            return []
        if isinstance(resp, list):
            return [x for x in resp if isinstance(x, dict)]
        if isinstance(resp, dict) and isinstance(resp.get("data"), list):
            return [x for x in resp["data"] if isinstance(x, dict)]
        return []

    @staticmethod
    def _extract_first_float(obj: Any, keys: tuple[str, ...]) -> Optional[float]:
        if isinstance(obj, dict):
            for key in keys:
                if key in obj:
                    try:
                        return float(obj[key])
                    except Exception:
                        continue
        return None

    # ------------------------------------------------------------------
    # Orderbook normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _level_to_pair(level: Any) -> Optional[list[float]]:
        try:
            if isinstance(level, (list, tuple)) and len(level) >= 2:
                return [float(level[0]), float(level[1])]
            if isinstance(level, dict) and "price" in level and "size" in level:
                return [float(level["price"]), float(level["size"])]
            if isinstance(level, dict) and "price" in level and ("quantity" in level or "amount" in level):
                qty = level.get("quantity") if "quantity" in level else level.get("amount")
                return [float(level["price"]), float(qty)]
            price = getattr(level, "price", None)
            size = getattr(level, "size", None)
            if size is None:
                size = getattr(level, "quantity", None)
            if size is None:
                size = getattr(level, "amount", None)
            if price is not None and size is not None:
                return [float(price), float(size)]
        except Exception:
            return None
        return None

    def _normalize_orderbook(self, book: Any) -> dict[str, Any]:
        if isinstance(book, dict):
            bids_raw = book.get("bids", []) or book.get("bid_levels", []) or []
            asks_raw = book.get("asks", []) or book.get("ask_levels", []) or []
        else:
            bids_raw = getattr(book, "bids", None)
            asks_raw = getattr(book, "asks", None)
            if bids_raw is None:
                bids_raw = getattr(book, "bid_levels", []) or []
            if asks_raw is None:
                asks_raw = getattr(book, "ask_levels", []) or []

        bids: list[list[float]] = []
        asks: list[list[float]] = []

        for lvl in bids_raw or []:
            pair = self._level_to_pair(lvl)
            if pair:
                bids.append(pair)

        for lvl in asks_raw or []:
            pair = self._level_to_pair(lvl)
            if pair:
                asks.append(pair)

        mid = 0.5
        try:
            if bids and asks:
                mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
            else:
                mid_attr = getattr(book, "mid", None) if not isinstance(book, dict) else book.get("mid")
                if mid_attr is not None:
                    mid = float(mid_attr)
        except Exception:
            pass

        return {"bids": bids, "asks": asks, "mid": mid}

    # ------------------------------------------------------------------
    # Rewards API (polymarket.com)
    # ------------------------------------------------------------------

    def get_reward_markets_page(
        self, next_cursor: str = "MA==", limit: int = 100
    ) -> dict[str, Any]:
        """
        Fetch markets from Polymarket Rewards API (same source as /rewards page).
        Returns dict like:
            {"data": [...], "nextCursor": "...", ...}
        """
        if self._stub:
            return {"data": _stub_markets(), "nextCursor": ""}

        import requests

        url = "https://polymarket.com/api/rewards/markets"

        maker = self.address or ""

        params = {
            "orderBy": "market",
            "position": "DESC",
            "query": "",
            "showFavorites": "false",
            "tagSlug": "all",
            "makerAddress": maker,
            "authenticationType": "magic",
            "nextCursor": next_cursor,
            "requestPath": "/rewards/user/markets",
            "onlyMergeable": "false",
            "noCompetition": "false",
            "onlyOpenOrders": "false",
            "onlyPositions": "false",
            "sponsored": "true",
            "limit": str(limit),
        }
        try:
            resp = requests.get(url, params=params, timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise PolyClientError(f"get_reward_markets_page failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Manual market resolution
    # ------------------------------------------------------------------

    def resolve_manual_market(self, ref: dict[str, Any]) -> Optional[dict[str, Any]]:
        """
        Resolve a manually-selected market from a lightweight reference object.

        Supported inputs:
          - {"url": "..."}
          - {"slug": "..."}
          - {"condition_id": "..."}
          - {"market_id": "..."}
          - {"question": "..."}  # fallback / best effort

        Returns the raw rewards market payload if found, else None.
        """
        if not isinstance(ref, dict):
            raise PolyClientError("manual market reference must be a dict")

        url = str(ref.get("url") or "").strip()
        slug = str(ref.get("slug") or ref.get("market_slug") or "").strip()
        condition_id = str(ref.get("condition_id") or "").strip()
        market_id = str(ref.get("market_id") or "").strip()
        question = str(ref.get("question") or "").strip()

        slug_from_url = self._extract_slug_from_url(url) if url else ""
        slug = slug or slug_from_url

        matchers = {
            "market_id": market_id.lower(),
            "condition_id": condition_id.lower(),
            "slug": slug.lower(),
            "question": question.lower(),
            "url": url.lower(),
        }

        next_cursor = "MA=="
        seen_cursors: set[str] = set()
        max_pages = 20
        page_limit = 100

        for _ in range(max_pages):
            if not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)

            resp = self.get_reward_markets_page(next_cursor=next_cursor, limit=page_limit)
            rows = resp.get("data", []) or []

            for raw in rows:
                if self._manual_market_matches(raw, matchers):
                    return raw

            next_cursor = resp.get("nextCursor") or resp.get("next_cursor") or ""

        return None

    @staticmethod
    def _extract_slug_from_url(url: str) -> str:
        try:
            parsed = urlparse(url)
            path = (parsed.path or "").strip("/")
            if not path:
                return ""
            parts = [p for p in path.split("/") if p]
            if not parts:
                return ""

            if "event" in parts:
                idx = parts.index("event")
                if idx + 1 < len(parts):
                    return parts[idx + 1]

            return parts[-1]
        except Exception:
            return ""

    def _manual_market_matches(self, raw: dict[str, Any], matchers: dict[str, str]) -> bool:
        market_id = str(raw.get("market_id", "") or "").lower()
        condition_id = str(raw.get("condition_id", "") or "").lower()
        question = str(raw.get("question", "") or "").lower()

        slug_candidates = self._extract_slug_candidates(raw)
        slug_candidates_l = {s.lower() for s in slug_candidates if s}

        url_matcher = matchers.get("url", "")
        slug_matcher = matchers.get("slug", "")
        market_id_matcher = matchers.get("market_id", "")
        condition_id_matcher = matchers.get("condition_id", "")
        question_matcher = matchers.get("question", "")

        if market_id_matcher and market_id == market_id_matcher:
            return True

        if condition_id_matcher and condition_id == condition_id_matcher:
            return True

        if slug_matcher and slug_matcher in slug_candidates_l:
            return True

        if url_matcher:
            for slug in slug_candidates_l:
                if slug and slug in url_matcher:
                    return True
            if condition_id and condition_id in url_matcher:
                return True
            if market_id and market_id in url_matcher:
                return True

        if question_matcher and question == question_matcher:
            return True

        return False

    @staticmethod
    def _extract_slug_candidates(raw: dict[str, Any]) -> set[str]:
        candidates: set[str] = set()

        direct_keys = (
            "slug",
            "market_slug",
            "event_slug",
            "marketSlug",
            "eventSlug",
        )
        for key in direct_keys:
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                candidates.add(v.strip())

        nested = raw.get("market")
        if isinstance(nested, dict):
            for key in direct_keys:
                v = nested.get(key)
                if isinstance(v, str) and v.strip():
                    candidates.add(v.strip())

        url_keys = ("url", "market_url", "event_url")
        for key in url_keys:
            v = raw.get(key)
            if isinstance(v, str) and v.strip():
                slug = PolyClient._extract_slug_from_url(v)
                if slug:
                    candidates.add(slug)

        if isinstance(nested, dict):
            for key in url_keys:
                v = nested.get(key)
                if isinstance(v, str) and v.strip():
                    slug = PolyClient._extract_slug_from_url(v)
                    if slug:
                        candidates.add(slug)

        return candidates

    # ------------------------------------------------------------------
    # Market details
    # ------------------------------------------------------------------

    def get_market_details(self, condition_id: str) -> dict[str, Any]:
        if self._stub:
            return _stub_market_details(condition_id)
        try:
            return self._client.get_market(condition_id)  # type: ignore[union-attr]
        except Exception as exc:
            raise PolyClientError(f"get_market_details({condition_id}) failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Order book
    # ------------------------------------------------------------------

    def get_orderbook(self, token_id: str) -> dict[str, Any]:
        if self._stub:
            return _stub_orderbook(token_id)
        try:
            book = self._client.get_order_book(token_id)  # type: ignore[union-attr]
            return self._normalize_orderbook(book)
        except Exception as exc:
            raise PolyClientError(f"get_orderbook({token_id}) failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        order_type: str = "GTC",
    ) -> dict[str, Any]:
        if self._stub:
            import uuid

            oid = str(uuid.uuid4())
            logger.info(
                "[STUB] place_order token=%s side=%s price=%.4f size=%.2f → %s",
                token_id, side, price, size, oid
            )
            return {"order_id": oid, "status": "live"}

        try:
            args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
            )
            resp = self._client.create_order(args)  # type: ignore[union-attr]
            logger.info(
                "place_order token=%s side=%s price=%.4f size=%.2f → %s",
                token_id, side, price, size, resp
            )
            return resp
        except Exception as exc:
            raise PolyClientError(f"place_order failed: {exc}") from exc

    def cancel_order(self, order_id: str) -> bool:
        if self._stub:
            logger.info("[STUB] cancel_order %s", order_id)
            return True
        try:
            self._client.cancel(order_id)  # type: ignore[union-attr]
            logger.info("cancel_order %s OK", order_id)
            return True
        except Exception as exc:
            logger.warning("cancel_order %s failed: %s", order_id, exc)
            return False

    def get_open_orders(self) -> list[dict[str, Any]]:
        """
        Fetch all open orders for this account.

        Normalizes SDK shapes to list[dict] so callers can always iterate safely.
        """
        if self._stub:
            return []
        try:
            resp = self._client.get_orders()  # type: ignore[union-attr]
            return self._coerce_list_response(resp)
        except Exception as exc:
            raise PolyClientError(f"get_open_orders failed: {exc}") from exc

    def cancel_all_orders(self, token_ids: Optional[list[str]] = None) -> bool:
        """
        Cancel all open orders (optionally filtered to specific token_ids).
        Fallback: cancel each order individually.
        """
        if self._stub:
            logger.info("[STUB] cancel_all_orders token_ids=%s", token_ids)
            return True
        try:
            orders = self.get_open_orders()
            to_cancel: list[str] = []
            for o in orders:
                tid = o.get("asset_id") or o.get("token_id")
                if token_ids is None or tid in token_ids:
                    oid = o.get("id") or o.get("order_id")
                    if oid:
                        to_cancel.append(str(oid))

            for oid in to_cancel:
                self.cancel_order(oid)

            logger.info("cancel_all_orders: cancelled %d orders", len(to_cancel))
            return True
        except Exception as exc:
            logger.error("cancel_all_orders failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def get_positions(self) -> list[dict[str, Any]]:
        """
        Fetch current token positions.

        Always returns list[dict] where each dict is compatible with _parse_positions():
          - token_id or asset_id
          - size (or balance)
          - avg_price (if available; else omitted)
        """
        if self._stub:
            return []
        try:
            for method_name in ("get_positions", "get_positions_v2", "get_user_positions"):
                if hasattr(self._client, method_name):
                    method = getattr(self._client, method_name)
                    resp = method()  # type: ignore[misc]
                    return self._normalize_positions(self._coerce_positions_response(resp))

            for method_name in ("get_balances", "get_balance", "get_balance_allowance"):
                if hasattr(self._client, method_name):
                    method = getattr(self._client, method_name)
                    resp = method()  # type: ignore[misc]
                    coerced = self._coerce_positions_response(resp)
                    norm = self._normalize_positions(coerced)
                    if norm:
                        return norm

            if self.address:
                resp = self._get_positions_via_data_api(self.address)
                return self._normalize_positions(self._coerce_positions_response(resp))

            raise AttributeError(
                "No compatible positions/balances method found on ClobClient and wallet address unavailable for Data API fallback"
            )
        except Exception as exc:
            raise PolyClientError(f"get_positions failed: {exc}") from exc

    def _coerce_positions_response(self, resp: Any) -> list[dict[str, Any]]:
        if resp is None:
            return []

        if isinstance(resp, list):
            return [x for x in resp if isinstance(x, dict)]

        if isinstance(resp, dict):
            if isinstance(resp.get("data"), list):
                return [x for x in resp["data"] if isinstance(x, dict)]

            out: list[dict[str, Any]] = []
            for k, v in resp.items():
                if k in ("data", "next_cursor", "nextCursor"):
                    continue
                try:
                    amt = float(v) if not isinstance(v, dict) else float(v.get("balance", 0))
                except Exception:
                    continue
                if abs(amt) > 0:
                    out.append({"token_id": str(k), "size": amt})
            return out

        return []

    @staticmethod
    def _normalize_positions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Ensure each row has token_id/asset_id and size in a consistent place.
        """
        out: list[dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            token = r.get("asset_id") or r.get("token_id") or r.get("asset") or r.get("id")
            if not token:
                continue

            size = r.get("size", None)
            if size is None:
                size = r.get("balance", None)
            if size is None:
                size = r.get("amount", None)

            try:
                size_f = float(size)
            except Exception:
                continue

            if abs(size_f) <= 0:
                continue

            norm = dict(r)
            norm["token_id"] = str(token)
            norm["size"] = size_f

            if "avg_price" in norm:
                try:
                    norm["avg_price"] = float(norm["avg_price"])
                except Exception:
                    norm.pop("avg_price", None)

            out.append(norm)
        return out

    def _get_positions_via_data_api(self, user_address: str) -> list[dict[str, Any]]:
        import requests

        url = "https://data-api.polymarket.com/positions"
        params = {
            "user": user_address,
            "limit": 200,
            "offset": 0,
        }

        out: list[dict[str, Any]] = []
        for _ in range(20):
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            chunk = r.json()

            if isinstance(chunk, dict) and isinstance(chunk.get("data"), list):
                chunk = chunk["data"]

            if not isinstance(chunk, list) or not chunk:
                break
            out.extend([x for x in chunk if isinstance(x, dict)])

            if len(chunk) < int(params["limit"]):
                break
            params["offset"] = int(params["offset"]) + int(params["limit"])

        return out

    # ------------------------------------------------------------------
    # Wallet balance / USDC availability
    # ------------------------------------------------------------------

    def get_available_usdc(self) -> Optional[float]:
        """
        Best-effort USDC free balance reader.

        Tries multiple SDK methods / response shapes and returns:
          - float balance if detected
          - None if unavailable
        """
        if self._stub:
            return 100.0

        candidates = (
            "get_balance_allowance",
            "get_balance",
            "get_balances",
            "get_collateral",
            "get_usdc_balance",
        )

        for method_name in candidates:
            if not hasattr(self._client, method_name):
                continue

            method = getattr(self._client, method_name)
            try:
                resp = method()  # type: ignore[misc]
                bal = self._extract_usdc_from_balance_response(resp)
                if bal is not None:
                    return bal
            except Exception as exc:
                logger.debug("get_available_usdc via %s failed: %s", method_name, exc)

        return None

    def _extract_usdc_from_balance_response(self, resp: Any) -> Optional[float]:
        if resp is None:
            return None

        # Direct numeric
        try:
            if isinstance(resp, (int, float, str)):
                return float(resp)
        except Exception:
            pass

        # Dict response
        if isinstance(resp, dict):
            direct = self._extract_first_float(
                resp,
                (
                    "available",
                    "available_balance",
                    "free",
                    "free_balance",
                    "balance",
                    "usdc",
                    "usdc_balance",
                    "amount",
                ),
            )
            if direct is not None:
                return direct

            # Nested common keys
            for key in ("data", "balances", "collateral", "wallet", "funds"):
                nested = resp.get(key)
                bal = self._extract_usdc_from_balance_response(nested)
                if bal is not None:
                    return bal

            # Symbol-indexed or token-address indexed dict
            for k, v in resp.items():
                k_l = str(k).lower()
                if "usdc" in k_l or "usd" == k_l:
                    bal = self._extract_usdc_from_balance_response(v)
                    if bal is not None:
                        return bal

        # List response
        if isinstance(resp, list):
            for item in resp:
                if not isinstance(item, dict):
                    continue

                symbol = str(
                    item.get("symbol")
                    or item.get("asset")
                    or item.get("token")
                    or item.get("currency")
                    or ""
                ).lower()

                if symbol and "usdc" not in symbol and symbol != "usd":
                    continue

                bal = self._extract_first_float(
                    item,
                    (
                        "available",
                        "available_balance",
                        "free",
                        "free_balance",
                        "balance",
                        "amount",
                    ),
                )
                if bal is not None:
                    return bal

            # fallback: first parseable row
            for item in resp:
                bal = self._extract_usdc_from_balance_response(item)
                if bal is not None:
                    return bal

        return None

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    def get_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        if self._stub:
            return []
        try:
            resp = self._client.get_trades()  # type: ignore[union-attr]
            return self._coerce_list_response(resp)
        except Exception as exc:
            raise PolyClientError(f"get_trades failed: {exc}") from exc

    # ------------------------------------------------------------------
    # WebSocket subscriptions
    # ------------------------------------------------------------------

    async def ws_subscribe_book(
        self, token_id: str, callback: Callable[[dict], None]
    ) -> None:
        ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
        import json as _json
        try:
            import websockets  # type: ignore

            backoff = 1.0
            while True:
                try:
                    async with websockets.connect(
                        ws_url,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=5,
                        max_size=2**20,
                    ) as ws:
                        backoff = 1.0
                        sub = _json.dumps({
                            "assets_ids": [token_id],
                            "type": "market",
                        })
                        await ws.send(sub)
                        async for raw in ws:
                            try:
                                data = _json.loads(raw)
                                callback(data)
                            except Exception as cb_exc:
                                logger.warning("ws_book callback error: %s", cb_exc)
                except asyncio.CancelledError:
                    raise
                except Exception as ws_exc:
                    logger.warning("ws_book connection error: %s — reconnecting", ws_exc)
                    await asyncio.sleep(min(backoff, 30.0))
                    backoff = min(backoff * 2.0, 30.0)

        except ImportError:
            logger.warning("websockets library not available; WS book disabled.")

    async def ws_subscribe_user(
        self, api_key: str, callback: Callable[[dict], None]
    ) -> None:
        ws_url = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
        import json as _json
        try:
            import websockets  # type: ignore

            backoff = 1.0
            while True:
                try:
                    async with websockets.connect(
                        ws_url,
                        ping_interval=20,
                        ping_timeout=20,
                        close_timeout=5,
                        max_size=2**20,
                    ) as ws:
                        backoff = 1.0
                        sub = _json.dumps({"type": "user", "api_key": api_key})
                        await ws.send(sub)
                        async for raw in ws:
                            try:
                                data = _json.loads(raw)
                                callback(data)
                            except Exception as cb_exc:
                                logger.warning("ws_user callback error: %s", cb_exc)
                except asyncio.CancelledError:
                    raise
                except Exception as ws_exc:
                    logger.warning("ws_user connection error: %s — reconnecting", ws_exc)
                    await asyncio.sleep(min(backoff, 30.0))
                    backoff = min(backoff * 2.0, 30.0)

        except ImportError:
            logger.warning("websockets library not available; WS user channel disabled.")


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

def _stub_markets() -> list[dict[str, Any]]:
    return [
        {
            "market_id": "stub-market-001",
            "condition_id": "stub-cond-001",
            "question": "[STUB] Will X happen by 2025?",
            "market_slug": "stub-market-001",
            "tokens": [
                {"token_id": "stub-yes-001", "outcome": "Yes"},
                {"token_id": "stub-no-001", "outcome": "No"},
            ],
            "rewards_min_size": 5.0,
            "rewards_max_spread": 0.03,
            "rewards_config": [
                {"rate_per_day": 100.0},
            ],
        }
    ]


def _stub_market_details(condition_id: str) -> dict[str, Any]:
    return _stub_markets()[0]


def _stub_orderbook(token_id: str) -> dict[str, Any]:
    return {
        "bids": [
            [0.48, 10.0],
            [0.47, 20.0],
            [0.46, 30.0],
        ],
        "asks": [
            [0.52, 10.0],
            [0.53, 20.0],
            [0.54, 30.0],
        ],
        "mid": 0.50,
    }