import asyncio
import hashlib
import json
import math
import re
import time

import aiohttp

from .policy import decimal

ORIGIN = "https://api.wallex.ir"
SYMBOL = re.compile(r"^[A-Z0-9]{2,30}$")
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class ExchangeError(RuntimeError):
    def __init__(self, reason, status=None):
        super().__init__(reason)
        self.status = status


class Wallex:
    def __init__(self, http, key, account_type, client_id=""):
        if account_type not in {"main", "credit"}:
            raise ValueError("account_scope_required")
        if not key or any(c in key for c in "\r\n"):
            raise ValueError("api_key_required")
        if account_type == "credit" and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client_id):
            raise ValueError("credit_client_id_required")
        if account_type == "main" and client_id:
            raise ValueError("main_account_cannot_have_credit_client_id")
        self.http, self.key = http, key
        self.account_type, self.client_id = account_type, client_id
        self.identity = hashlib.sha256((key + "|" + account_type + "|" + client_id).encode()).hexdigest()

    async def request(self, method, path, *, private=False, params=None, body=None):
        public = {"/v1/depth", "/v1/markets", "/hector/web/v1/markets"}
        private_get = {"/v1/account/balances", "/v1/account/balances-detail",
                       "/v1/account/fee", "/v1/account/openOrders"}
        order_path = re.fullmatch(r"/v1/account/orders/[A-Za-z0-9_-]{1,128}", path)
        allowed = (
            (method == "GET" and not private and path in public)
            or (method == "GET" and private and (path in private_get or order_path))
            or (method == "POST" and private and path == "/v1/account/orders")
            or (method == "DELETE" and private and order_path)
        )
        if not allowed:
            raise ValueError("unreviewed_exchange_route")
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "User-Agent": "Wallex-Kavoshgaran/1.0", "Cache-Control": "no-cache"}
        if private:
            headers["X-API-Key"] = self.key
            if self.account_type == "credit":
                headers["sub-account-client-id"] = self.client_id
        try:
            async with self.http.request(method, ORIGIN + path, headers=headers,
                                         params=params, json=body, allow_redirects=False) as response:
                if response.status not in {200, 201}:
                    # Never include request headers, private response bodies or key in errors.
                    raise ExchangeError("wallex_http_" + str(response.status), response.status)
                result = json.loads(await response.text(), parse_float=str)
                if not isinstance(result, dict) or result.get("success") is False:
                    raise ExchangeError("wallex_unsuccessful_response")
                if not private:
                    age = float(response.headers.get("Age", "0"))
                    if not math.isfinite(age) or not 0 <= age <= 8:
                        raise ExchangeError("stale_cached_public_quote")
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError):
            raise ExchangeError("wallex_transport_error") from None

    async def balances(self):
        path = "/v1/account/balances" if self.account_type == "main" else "/v1/account/balances-detail"
        data = (await self.request("GET", path, private=True))["result"]
        rows = data["balances"] if self.account_type == "main" else data
        if not isinstance(rows, dict):
            raise ExchangeError("unexpected_balance_schema")
        balances = {}
        for asset, row in rows.items():
            if not SYMBOL.fullmatch(asset):
                raise ExchangeError("invalid_balance_asset")
            if self.account_type == "main":
                total, locked = decimal(row["value"]), decimal(row["locked"])
                free = total - locked
            else:
                total, locked, free = map(decimal, (row["total"], row["freeze"], row["available"]))
            if min(total, locked, free) < 0 or abs(total - locked - free) > decimal(".00000001"):
                raise ExchangeError("balance_reconciliation_failed")
            balances[asset] = {"total": str(total), "locked": str(locked), "free": str(free)}
        return balances

    async def open_orders(self):
        result, seen = [], set()
        for page in range(1, 101):
            payload = await self.request("GET", "/v1/account/openOrders", private=True,
                                         params={"page": page, "per_page": 100})
            groups = payload["result"]
            rows = groups if isinstance(groups, list) else [o for group in groups.values() for o in group]
            if not isinstance(rows, list) or any(not isinstance(x, dict) for x in rows):
                raise ExchangeError("unexpected_open_orders_schema")
            for row in rows:
                cid = row.get("clientOrderId")
                if not isinstance(cid, str) or not CLIENT_ID.fullmatch(cid):
                    raise ExchangeError("invalid_open_order_client_id")
                if cid in seen:
                    raise ExchangeError("open_order_pagination_not_progressing")
                seen.add(cid)
                result.append(row)
            if len(rows) < 100:
                return result
        raise ExchangeError("open_order_pagination_limit")

    async def order(self, client_id):
        if not CLIENT_ID.fullmatch(client_id):
            raise ValueError("invalid_client_id")
        result = (await self.request("GET", "/v1/account/orders/" + client_id, private=True))["result"]
        if result.get("clientOrderId") != client_id or type(result.get("active")) is not bool:
            raise ExchangeError("order_identity_or_status_mismatch")
        return result

    async def cancel(self, client_id):
        if not CLIENT_ID.fullmatch(client_id):
            raise ValueError("invalid_client_id")
        return await self.request("DELETE", "/v1/account/orders/" + client_id, private=True)

    async def create(self, body):
        return (await self.request("POST", "/v1/account/orders", private=True, body=body))["result"]

    async def book(self, symbol):
        if not SYMBOL.fullmatch(symbol):
            raise ValueError("invalid_market_symbol")
        started = time.time()
        value = (await self.request("GET", "/v1/depth", params={"symbol": symbol}))["result"]
        received = time.time()
        if received - started > 8:
            raise ExchangeError("slow_public_book")
        bids = sorted([(decimal(x["price"]), decimal(x["quantity"])) for x in value["bid"]], reverse=True)
        asks = sorted([(decimal(x["price"]), decimal(x["quantity"])) for x in value["ask"]])
        if not bids or not asks or bids[0][0] >= asks[0][0] or any(min(p, q) <= 0 for p, q in bids + asks):
            raise ExchangeError("invalid_public_book")
        return {"venue": "wallex", "symbol": symbol, "quote": "TMN" if symbol.endswith("TMN") else "USDT",
                "market_type": "spot", "bids": bids, "asks": asks,
                "received_epoch": received, "cache_age": 0, "exchange_epoch": None}
