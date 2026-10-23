from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from .sandbox import demonstration, offline_only


def main():
    parser = argparse.ArgumentParser(description="Run Condor's risk gate against an offline fake exchange")
    parser.add_argument("--output", type=Path, default=Path("wallex_sandbox/artifacts"))
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out = args.output/stamp
    out.mkdir(parents=True)
    with offline_only():
        report = asyncio.run(demonstration(out/"demonstration"))
    report["created_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["python"] = sys.version.split()[0]
    report["condor_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    report["credentials_used"] = False
    (out/"report.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps({k:v for k,v in report.items() if k != "results"},indent=2))
    print(f"Saved full trace: {out/'report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
