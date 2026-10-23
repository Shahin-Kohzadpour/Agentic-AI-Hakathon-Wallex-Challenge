from __future__ import annotations

import fcntl
import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .core import Portfolio, canonical, dec, digest, now_iso, pair

ACTIVE = {"PREPARED", "SUBMITTED", "OPEN", "PARTIALLY_FILLED", "CANCEL_REQUESTED", "UNKNOWN"}


@dataclass(frozen=True)
class Intent:
    client_id: str
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    fee_rate: Decimal

    def record(self) -> dict:
        return {k: str(v) for k, v in vars(self).items()}


class Store:
    """Single-writer, append-only event chain with atomic paper fills and balances."""

    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path)
        self.lock = None
        if read_only:
            self.db = sqlite3.connect(self.path.resolve().as_uri()+"?mode=ro", uri=True)
            self.db.row_factory = sqlite3.Row
            self.db.execute("BEGIN")
            self.verify()
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(str(self.path) + ".lockfile", "a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError("Another execution writer already holds this ledger") from None
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS events(
                seq INTEGER PRIMARY KEY, time TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(
                client_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
                filled TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fills(fill_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        """)
        self.verify()

    def close(self):
        self.db.close()
        if self.lock is not None:
            self.lock.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def verify(self) -> int:
        previous = "0" * 64
        count = 0
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            count += 1
            content = {"seq": row["seq"], "time": row["time"], "kind": row["kind"],
                       "payload": json.loads(row["payload"]), "previous_hash": previous}
            if row["seq"] != count or row["previous_hash"] != previous or digest(content) != row["hash"]:
                raise ValueError("Audit event chain integrity check failed")
            previous = row["hash"]
        return count

    def _event(self, kind: str, payload: dict):
        last = self.db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, previous = (last["seq"] + 1, last["hash"]) if last else (1, "0" * 64)
        stamp = now_iso()
        body = {"seq": seq, "time": stamp, "kind": kind, "payload": payload, "previous_hash": previous}
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (seq, stamp, kind, canonical(payload), previous, digest(body)))

    def event(self, kind: str, payload: dict, state_updates: dict | None = None):
        with self.db:
            self._event(kind, payload)
            for key, value in (state_updates or {}).items():
                self._put(key, value)

    def get(self, key: str, default=None):
        row = self.db.execute("SELECT payload FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _put(self, key: str, value):
        self.db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, canonical(value)))

    def put(self, key: str, value):
        with self.db:
            self._put(key, value)

    def initialize(self, initial_cash: Decimal, fingerprint: str):
        with self.db:
            old = self.get("fingerprint")
            if old is not None and old != fingerprint:
                raise ValueError("Ledger belongs to a different configuration/model; use a new run directory")
            if old is None:
                self._put("fingerprint", fingerprint)
                self._put("initial_cash", str(initial_cash))
                self._put("portfolio", Portfolio(initial_cash).record())
                self._event("initialized", {"initial_cash_tmn": str(initial_cash), "fingerprint": fingerprint, "mode": "shadow"})

    @property
    def portfolio(self) -> Portfolio:
        value = self.get("portfolio")
        if value is None:
            raise ValueError("Ledger has not been initialized")
        return Portfolio.from_record(value)

    def open_orders(self) -> list[dict]:
        return [{**json.loads(row["payload"]), "status": row["status"], "filled": row["filled"]}
                for row in self.db.execute("SELECT * FROM orders") if row["status"] in ACTIVE]

    def prepare(self, intent: Intent) -> bool:
        if intent.side not in {"BUY", "SELL"} or not intent.client_id:
            raise ValueError("Invalid intent")
        pair(intent.symbol)
        if any(not x.is_finite() for x in (intent.quantity, intent.limit_price, intent.fee_rate)):
            raise ValueError("Non-finite intent")
        if intent.quantity <= 0 or intent.limit_price <= 0 or not 0 <= intent.fee_rate < 1:
            raise ValueError("Invalid intent amounts")
        payload = canonical(intent.record())
        with self.db:
            old = self.db.execute("SELECT payload FROM orders WHERE client_id=?", (intent.client_id,)).fetchone()
            if old:
                if old[0] != payload:
                    raise ValueError("Client order ID reused for different intent")
                return False
            # No crossing own active orders, and no double-spending reserved funds.
            opens = self.open_orders()
            for order in opens:
                if order["status"] == "UNKNOWN":
                    raise ValueError("Unresolved order state")
                if order["symbol"] == intent.symbol and order["side"] != intent.side:
                    crosses = (intent.limit_price >= dec(order["limit_price"]) if intent.side == "BUY"
                               else intent.limit_price <= dec(order["limit_price"]))
                    if crosses:
                        raise ValueError("Would cross an existing own order")
            portfolio = self.portfolio
            if intent.side == "BUY":
                reserved = sum(((dec(o["quantity"]) - dec(o["filled"])) * dec(o["limit_price"]) * (1 + dec(o["fee_rate"]))
                                for o in opens if o["side"] == "BUY"), Decimal(0))
                if reserved + intent.quantity * intent.limit_price * (1 + intent.fee_rate) > portfolio.cash:
                    raise ValueError("Insufficient unreserved cash")
            else:
                reserved = sum((dec(o["quantity"]) - dec(o["filled"]) for o in opens
                                if o["symbol"] == intent.symbol and o["side"] == "SELL"), Decimal(0))
                held = portfolio.positions.get(intent.symbol)
                if held is None or reserved + intent.quantity > held.quantity:
                    raise ValueError("Insufficient unreserved inventory")
            self.db.execute("INSERT INTO orders VALUES(?,?,?,?)", (intent.client_id, payload, "PREPARED", "0"))
            self._event("order_intent", intent.record())
        return True

    def transition(self, client_id: str, status: str):
        allowed = {
            "PREPARED": {"SUBMITTED", "OPEN", "CANCELED", "REJECTED", "UNKNOWN"},
            "SUBMITTED": {"OPEN", "CANCEL_REQUESTED", "REJECTED", "UNKNOWN"},
            "OPEN": {"CANCEL_REQUESTED", "CANCELED", "UNKNOWN"},
            "PARTIALLY_FILLED": {"CANCEL_REQUESTED", "CANCELED", "UNKNOWN"},
            "CANCEL_REQUESTED": {"CANCELED", "UNKNOWN"},
            "UNKNOWN": {"OPEN", "CANCELED", "CANCEL_REQUESTED"},
        }
        with self.db:
            row = self.db.execute("SELECT status FROM orders WHERE client_id=?", (client_id,)).fetchone()
            if not row:
                raise ValueError("Unknown client order ID")
            if status == row[0]:
                return
            if status not in allowed.get(row[0], set()):
                raise ValueError(f"Illegal order transition {row[0]} -> {status}")
            self.db.execute("UPDATE orders SET status=? WHERE client_id=?", (status, client_id))
            self._event("order_status", {"client_id": client_id, "status": status})

    def fill(self, fill_id: str, client_id: str, quantity: Decimal, price: Decimal,
             fee: Decimal, fee_asset: str, *, state_updates: dict | None = None) -> bool:
        body = {"fill_id": fill_id, "client_id": client_id, "quantity": str(quantity), "price": str(price),
                "fee": str(fee), "fee_asset": fee_asset}
        with self.db:
            old = self.db.execute("SELECT payload FROM fills WHERE fill_id=?", (fill_id,)).fetchone()
            if old:
                if old[0] != canonical(body):
                    raise ValueError("Conflicting duplicate fill")
                return False
            row = self.db.execute("SELECT * FROM orders WHERE client_id=?", (client_id,)).fetchone()
            if row is None or row["status"] not in ACTIVE:
                raise ValueError("Fill requires reconciliation with an active known intent")
            order = json.loads(row["payload"])
            filled = dec(row["filled"]) + quantity
            if quantity <= 0 or filled > dec(order["quantity"]):
                raise ValueError("Fill exceeds remaining order quantity")
            if ((order["side"] == "BUY" and price > dec(order["limit_price"])) or
                    (order["side"] == "SELL" and price < dec(order["limit_price"]))):
                raise ValueError("Fill violates order limit")
            portfolio = self.portfolio
            portfolio.apply_fill(order["symbol"], order["side"], quantity, price, fee, fee_asset)
            status = "FILLED" if filled == dec(order["quantity"]) else (
                "CANCEL_REQUESTED" if row["status"] == "CANCEL_REQUESTED" else "PARTIALLY_FILLED")
            self.db.execute("INSERT INTO fills VALUES(?,?)", (fill_id, canonical(body)))
            self.db.execute("UPDATE orders SET status=?,filled=? WHERE client_id=?", (status, str(filled), client_id))
            self._put("portfolio", portfolio.record())
            for key, value in (state_updates or {}).items():
                self._put(key, value)
            self._event("fill", {**body, "symbol": order["symbol"], "side": order["side"], "status": status,
                                  "portfolio": portfolio.record(), "simulated": True})
        return True

    def events(self) -> list[dict]:
        return [{"seq": r["seq"], "time": r["time"], "kind": r["kind"], "payload": json.loads(r["payload"]), "hash": r["hash"]}
                for r in self.db.execute("SELECT * FROM events ORDER BY seq")]
