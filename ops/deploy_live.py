"""Deploy only a pushed revision and a verified real account. Never print credentials."""
import argparse
import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
APP = "wallex-kavoshgaran"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def cli(*arguments):
    team = json.loads((ROOT / "liara.json").read_text()).get("team-id")
    team_flags = ["--team-id", team] if team else []
    command = ["npm", "exec", "--yes", "--package=@liara/cli@9.6.0", "--", "liara", *arguments, *team_flags]
    return subprocess.run(command, cwd=ROOT, check=True)


async def preflight(args):
    import aiohttp
    from wallex_live.client import Wallex
    from wallex_sandbox.wallex_fee_probe import read_key
    os.environ["NETRC"] = "/dev/null"
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    key = read_key(args.key_file)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15),
                                     cookie_jar=aiohttp.DummyCookieJar(), trust_env=True) as http:
        client = Wallex(http, key, args.account_type, args.client_id)
        await client.balances()
        await client.open_orders()
        fees = (await client.request("GET", "/v1/account/fee", private=True))["result"]
        for symbol in ("BTCUSDT", "ETHUSDT", "USDTTMN"):
            if symbol not in fees:
                raise ValueError("account_fees_missing_" + symbol)
        await client.book("USDTTMN")
    return key


def liara_api():
    config = json.loads((Path.home() / ".liara-auth.json").read_text())
    account = next((a for a in config["accounts"].values() if a.get("current")), None)
    if account is None:
        raise ValueError("Liara login required")
    token = account["api_token"]
    selected = json.loads((ROOT / "liara.json").read_text()).get("team-id")
    team = account.get("team") or {}
    query = urlencode({"teamID": selected if selected is not None else team.get("id", "")})
    opener = build_opener(NoRedirect())
    def request(method, path, body=None):
        payload = None if body is None else json.dumps(body).encode()
        req = Request("https://api.liara.ir/" + path + "?" + query, data=payload, method=method,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                               "User-Agent": "Wallex-Kavoshgaran-Deployment"})
        try:
            with opener.open(req, timeout=30) as response:
                raw = response.read()
                if not raw.strip() and method != "GET":
                    return {}
                return json.loads(raw)
        except HTTPError as exc:
            raise ValueError("Liara HTTP " + str(exc.code) + "; no secret-bearing response printed") from None
    return request


def deploy(args):
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise ValueError("Commit and push the source before deployment")
    remote = subprocess.check_output(["git", "ls-remote", "origin", "refs/heads/main"], cwd=ROOT, text=True)
    if remote.split()[0] != sha:
        raise ValueError("GitHub main does not match the deployment revision")
    if not args.disarmed and not args.account_type:
        raise ValueError("Explicit account type is required before enabling real orders")
    if not args.disarmed and args.account_type == "credit" and not args.credit_markets_file:
        raise ValueError("Credit-account allowed-market permissions must be verified before enabling trades")
    allowed = json.loads(args.credit_markets_file.read_text()) if args.credit_markets_file else []
    if not isinstance(allowed, list) or any(not isinstance(s, str) for s in allowed):
        raise ValueError("Invalid allowed-market manifest")
    if args.disarmed:
        from wallex_sandbox.wallex_fee_probe import read_key
        key = read_key(args.key_file) if args.key_file else ""
        print("Deploying the real service with live orders DISARMED; account readiness is not asserted.", flush=True)
    else:
        if not args.key_file:
            raise ValueError("An API key file is required before enabling real orders")
        key = asyncio.run(preflight(args))
        print("Real-account read-only preflight passed. No orders submitted during preflight.", flush=True)
    api = liara_api()
    projects = api("GET", "v1/projects")["projects"]
    project = next((p for p in projects if p["project_id"] == APP), None)
    if project is None:
        networks = api("GET", "v1/networks")["networks"]
        if not any(n.get("name") == "wallex-challenge-net" for n in networks):
            cli("network", "create", "--name", "wallex-challenge-net")
        cli("app", "create", "--app", APP, "--platform", "docker", "--plan", "small-g2",
            "--feature-plan", "standard", "--network", "wallex-challenge-net", "--read-only", "true")
    project = api("GET", "v1/projects/" + APP)["project"]
    # Liara lists disks by internal project ID, while creation accepts the app slug.
    disks = api("GET", "v1/projects/" + project["_id"] + "/disks")["disks"]
    if not any((d.get("name") if isinstance(d, dict) else d) == "wallex-state" for d in disks):
        cli("disk", "create", "--app", APP, "--name", "wallex-state", "--size", "1")
    private = ROOT / ".wallex-live"
    private.mkdir(mode=0o700, exist_ok=True)
    password_file = private / "operator-password.txt"
    if password_file.exists():
        password = password_file.read_text().strip()
    else:
        password = secrets.token_urlsafe(32)
        fd = os.open(password_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(password + "\n")
    if len(password) < 20:
        raise ValueError("Dashboard password is too short")
    envs = {e["key"]: e["value"] for e in project.get("envs", [])}
    envs.update(WALLEX_API_KEY=key, WALLEX_ACCOUNT_TYPE=args.account_type,
                WALLEX_SUB_ACCOUNT_CLIENT_ID=args.client_id,
                WALLEX_ENABLE_LIVE_ORDERS="0" if args.disarmed else "1",
                WALLEX_DASHBOARD_PASSWORD=password, WALLEX_DATA_DIR="/data",
                WALLEX_CREDIT_ALLOWED_MARKETS=json.dumps(allowed), SOURCE_REVISION=sha)
    api("POST", "v1/projects/update-envs",
        {"project": APP, "variables": [{"key": k, "value": v} for k, v in envs.items()]})
    cli("deploy", "--app", APP, "--platform", "docker", "--port", "8080",
        "--build-arg", "SOURCE_REVISION=" + sha, "--no-app-logs")
    url = "https://" + APP + ".liara.run"
    import base64
    auth = "Basic " + base64.b64encode(("operator:" + password).encode()).decode()
    opener = build_opener(NoRedirect())
    for attempt in range(12):
        try:
            with opener.open(url + ("/health" if args.disarmed else "/ready"), timeout=10) as response:
                readiness = json.loads(response.read())
                if (not args.disarmed and not readiness.get("ready")) or readiness.get("source_revision") != sha:
                    raise ValueError("Deployed readiness/revision mismatch")
            with opener.open(Request(url + "/api/status", headers={"Authorization": auth}), timeout=10) as response:
                state = json.loads(response.read())
                if state.get("mode") != "REAL":
                    raise ValueError("Deployed service is not the real-account application")
                if args.disarmed and state.get("orders_enabled"):
                    raise ValueError("Disarmed deployment unexpectedly enabled orders")
                if not args.disarmed and (not state.get("connected") or not state.get("orders_enabled")):
                    raise ValueError("Real exchange connection is not ready")
            print(("Verified hosted REAL service (orders DISARMED): " if args.disarmed else "Verified live service: ") + url)
            print("Dashboard username: operator; password stored locally at " + str(password_file))
            return
        except (HTTPError, OSError, ValueError):
            if attempt == 11:
                raise ValueError("Deployment exists but real-account readiness was not verified") from None
            time.sleep(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--account-type", choices=["main", "credit"], default="")
    parser.add_argument("--client-id", default="")
    parser.add_argument("--credit-markets-file", type=Path)
    parser.add_argument("--disarmed", action="store_true",
                        help="Host the real application with all live order submissions disabled")
    args = parser.parse_args()
    try:
        deploy(args)
    except Exception as exc:
        from wallex_live.client import ExchangeError
        reason = str(exc) if isinstance(exc, (ValueError, ExchangeError)) else type(exc).__name__
        print("Deployment blocked: " + reason, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
