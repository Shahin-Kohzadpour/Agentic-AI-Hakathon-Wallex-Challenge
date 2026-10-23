import asyncio
import base64
import copy
import hmac
import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import aiohttp

from .client import ExchangeError, Wallex
from .engine import Engine
from .journal import Journal
from .policy import CUTOFF, CAPITAL_TMN

STATE = {"mode": "REAL", "connected": False, "ready": False, "orders_enabled": False, "state": "starting",
         "cutoff_tehran": CUTOFF.isoformat(), "capital_cap_tmn": str(CAPITAL_TMN),
         "capital_cap_irr": str(CAPITAL_TMN * 10)}
LOCK = threading.Lock()
STOP = threading.Event()
WEB = Path(__file__).parent / "web"


def publish(values):
    with LOCK:
        STATE.update(copy.deepcopy(values))
        STATE["heartbeat"] = time.time()


def snapshot():
    with LOCK:
        return copy.deepcopy(STATE)


def key_from_environment():
    key = os.environ.get("WALLEX_API_KEY", "").strip()
    if not key and os.environ.get("WALLEX_API_KEY_FILE"):
        # Only the explicit local path is read. Nothing discovers unrelated secrets.
        from wallex_sandbox.wallex_fee_probe import read_key
        key = read_key(Path(os.environ["WALLEX_API_KEY_FILE"]))
    return key


async def worker():
    journal = None
    engine = None
    try:
        key = key_from_environment()
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12), connector=aiohttp.TCPConnector(limit=8),
            cookie_jar=aiohttp.DummyCookieJar(), trust_env=True,
        ) as http:
            api = Wallex(http, key, os.environ.get("WALLEX_ACCOUNT_TYPE", ""),
                         os.environ.get("WALLEX_SUB_ACCOUNT_CLIENT_ID", ""))
            journal = Journal(os.environ.get("WALLEX_DATA_DIR", "/data"), api.identity)
            markets = json.loads(os.environ.get("WALLEX_CREDIT_ALLOWED_MARKETS", "[]"))
            if not isinstance(markets, list) or any(not isinstance(x, str) for x in markets):
                raise ValueError("credit_allowed_markets_must_be_a_json_list")
            engine = Engine(api, journal, enabled=os.environ.get("WALLEX_ENABLE_LIVE_ORDERS") == "1",
                            credit_markets=markets)
            journal.event("live_service_started", {
                "revision": os.environ.get("SOURCE_REVISION", "unrecorded"),
                "account_type": api.account_type, "orders_enabled": engine.enabled})
            while not STOP.is_set():
                try:
                    await engine.cycle(http)
                    engine.status["ready"] = engine.status["state"] not in {
                        "credit_market_permissions_required", "settlement_disarmed"}
                except (ExchangeError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
                    # A 401 is an auth failure, never a zero/pretend wallet.
                    status = exc.status if isinstance(exc, ExchangeError) else None
                    engine.status.update(ready=False, state="blocked",
                                         error=str(exc) if isinstance(exc, (ExchangeError, ValueError)) else type(exc).__name__)
                    if status in {401, 403}:
                        engine.status.update(connected=False, state="authentication_failed")
                    journal.event("live_cycle_blocked", {"error_type": type(exc).__name__, "http_status": status})
                publish(engine.status)
                remaining = CUTOFF.timestamp() - time.time()
                delay = min(10, remaining) if remaining > 0 else 10
                for _ in range(max(1, int(delay * 10))):
                    if STOP.is_set():
                        break
                    await asyncio.sleep(.1)
    except Exception as exc:
        # No raw unknown exception string: request/credential internals stay private.
        publish({"ready": False, "connected": False, "state": "configuration_required",
                 "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    finally:
        if journal:
            journal.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def respond(self, status, data, content_type="application/json; charset=utf-8", extra=None):
        body = data if isinstance(data, bytes) else json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; form-action 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        password = os.environ.get("WALLEX_DASHBOARD_PASSWORD", "")
        if len(password) < 20:
            self.respond(503, {"error": "dashboard_password_not_configured"})
            return False
        supplied = self.headers.get("Authorization", "")
        expected = "Basic " + base64.b64encode(("operator:" + password).encode()).decode()
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            self.respond(401, {"error": "authentication_required"},
                         extra={"WWW-Authenticate": 'Basic realm="Wallex Kavoshgaran", charset="UTF-8"'})
            return False
        return True

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in {"/health", "/ready"}:
            data = snapshot()
            ready = bool(data.get("ready") and data.get("connected") and time.time() - data.get("heartbeat", 0) < 40)
            self.respond(503 if path == "/ready" and not ready else 200,
                         {"service": "wallex-kavoshgaran", "mode": "REAL", "ready": ready,
                          "state": data.get("state"), "source_revision": os.environ.get("SOURCE_REVISION", "unrecorded")})
            return
        if not self.authorized():
            return
        if path == "/api/status":
            self.respond(200, snapshot())
            return
        files = {"/": ("index.html", "text/html; charset=utf-8"),
                 "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                 "/style.css": ("style.css", "text/css; charset=utf-8")}
        if path in files:
            filename, mime = files[path]
            self.respond(200, (WEB / filename).read_bytes(), mime)
        else:
            self.respond(404, {"error": "not_found"})


def main():
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    os.environ["NETRC"] = "/dev/null"
    if os.environ.get("DROP_PRIVILEGES") == "1" and os.getuid() == 0:
        directory = Path(os.environ.get("WALLEX_DATA_DIR", "/data"))
        directory.mkdir(exist_ok=True)
        os.chown(directory, 65532, 65532)
        os.setgid(65532)
        os.setuid(65532)
    thread = threading.Thread(target=lambda: asyncio.run(worker()), daemon=True)
    thread.start()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    def stop(*_):
        STOP.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        STOP.set()
        thread.join(timeout=35)
        server.server_close()
