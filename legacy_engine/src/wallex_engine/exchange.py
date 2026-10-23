"""A deliberately GET-only adapter. This module cannot place exchange orders."""
from __future__ import annotations

import json
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .core import Book, Candle, dec, now_iso, pair


class ExchangeError(RuntimeError):
    pass


class WallexPublic:
    origin = "https://api.wallex.ir"

    def __init__(self, timeout: float = 15, attempts: int = 3):
        if timeout <= 0 or attempts < 1:
            raise ValueError("Invalid HTTP timeout/retry count")
        self.timeout, self.attempts = timeout, attempts

    def get(self, path: str, params: dict | None = None) -> dict:
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise ValueError("Invalid API path")
        url = self.origin + path + ("?" + urlencode(params) if params else "")
        for attempt in range(self.attempts):
            try:
                request = Request(url, headers={"Accept": "application/json", "User-Agent": "wallex-challenge/0.1"}, method="GET")
                with urlopen(request, timeout=self.timeout) as response:
                    result = json.loads(response.read(), parse_float=str)
                if not isinstance(result, dict) or result.get("success") is False:
                    raise ExchangeError("Exchange returned an unsuccessful or malformed response")
                return result
            except HTTPError as exc:
                if exc.code != 429 and exc.code < 500:
                    raise ExchangeError(f"Read-only exchange request failed: HTTP {exc.code}") from None
                if attempt + 1 == self.attempts:
                    raise ExchangeError(f"Exchange unavailable: HTTP {exc.code}") from None
            except (URLError, TimeoutError, socket.timeout):
                if attempt + 1 == self.attempts:
                    raise ExchangeError("Read-only exchange request timed out or failed to connect") from None
            except (ValueError, KeyError, TypeError):
                raise ExchangeError("Invalid JSON response from exchange") from None
            time.sleep(min(2 ** attempt, 4))
        raise ExchangeError("Read-only exchange request failed")

    def markets(self) -> list[dict]:
        """Current documented public directory; no inference about credit eligibility."""
        data = self.get("/hector/web/v1/markets")
        rows = data["result"]["markets"]
        if not isinstance(rows, list):
            raise ExchangeError("Unexpected market directory schema")
        return rows

    def legacy_metadata(self) -> dict[str, dict]:
        """Supplemental precision/minimum metadata, explicitly marked legacy."""
        data = self.get("/v1/markets")["result"]["symbols"]
        if not isinstance(data, dict):
            raise ExchangeError("Unexpected legacy market metadata schema")
        return data

    def candles(self, symbol: str, start: int, end: int, resolution: int = 15) -> list[Candle]:
        pair(symbol)
        if not 0 <= start < end or resolution not in {1, 15, 60, 240, 480, 720}:
            raise ValueError("Invalid history request")
        data = self.get("/v1/udf/history", {"symbol": symbol, "resolution": resolution, "from": start, "to": end})
        if data.get("s") == "no_data":
            return []
        keys = ("t", "o", "h", "l", "c", "v")
        if data.get("s") != "ok" or any(not isinstance(data.get(k), list) for k in keys):
            raise ExchangeError("Unexpected candle response schema")
        if len({len(data[k]) for k in keys}) != 1:
            raise ExchangeError("Misaligned candle arrays")
        rows = [Candle(int(t), dec(o), dec(h), dec(l), dec(c), dec(v))
                for t, o, h, l, c, v in zip(*(data[k] for k in keys))]
        # API can include either endpoint; explicitly retain only requested closed bars.
        return sorted({c.timestamp: c for c in rows if start <= c.timestamp and c.timestamp + resolution * 60 <= end}.values(),
                      key=lambda c: c.timestamp)

    def book(self, symbol: str) -> Book:
        pair(symbol)
        data = self.get("/v1/depth", {"symbol": symbol})["result"]
        def levels(name):
            values = data[name]
            parsed = []
            for row in values:
                if isinstance(row, dict):
                    price, quantity = row["price"], row["quantity"]
                elif isinstance(row, list) and len(row) >= 2:
                    price, quantity = row[:2]
                else:
                    raise ExchangeError("Unexpected depth level schema")
                parsed.append((dec(price), dec(quantity)))
            return tuple(sorted(parsed, reverse=name == "bid"))
        return Book(symbol, levels("bid"), levels("ask"), time.time())


def download(client: WallexPublic, symbol: str, start: int, end: int, resolution: int, output: Path) -> dict:
    if start >= end:
        raise ValueError("Empty historical range")
    cursor, rows = start, {}
    # Small bounded pages avoid relying on undocumented server history limits.
    page_seconds = 400 * resolution * 60
    while cursor < end:
        stop = min(end, cursor + page_seconds)
        page = client.candles(symbol, cursor, stop, resolution)
        for c in page:
            if c.timestamp in rows and rows[c.timestamp] != c:
                raise ExchangeError("Conflicting historical candle")
            rows[c.timestamp] = c
        cursor = stop
    ordered = sorted(rows.values(), key=lambda c: c.timestamp)
    interval = resolution * 60
    gaps = [{"after": a.timestamp, "before": b.timestamp, "missing": (b.timestamp-a.timestamp)//interval-1}
            for a, b in zip(ordered, ordered[1:]) if b.timestamp-a.timestamp != interval]
    payload = {"schema_version": 1, "source": client.origin + "/v1/udf/history", "fetched_at": now_iso(),
               "symbol": symbol, "resolution_minutes": resolution, "requested_start": start, "requested_end": end,
               "gaps": gaps, "candles": [c.record() for c in ordered]}
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(output)
    return {"symbol": symbol, "candles": len(ordered), "gaps": len(gaps), "path": str(output),
            "first": ordered[0].timestamp if ordered else None, "last": ordered[-1].timestamp if ordered else None}


def load_history(path: str | Path, resolution: int) -> tuple[str, list[Candle], dict]:
    value = json.loads(Path(path).read_text())
    symbol = value["symbol"]
    pair(symbol)
    if value["resolution_minutes"] != resolution:
        raise ValueError("Dataset resolution does not match configuration")
    candles = [Candle.from_record(row) for row in value["candles"]]
    if any(b.timestamp <= a.timestamp for a, b in zip(candles, candles[1:])):
        raise ValueError("History contains duplicate or unordered timestamps")
    if any(c.timestamp % (resolution * 60) for c in candles):
        raise ValueError("Candle timestamps do not align to resolution")
    return symbol, candles, value
