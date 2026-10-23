"""Repeatable offline checks with machine-readable results and persistent transcripts."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from .sandbox import offline_only


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", action="store_true", help="Also run selected upstream Condor risk/tick checks (requires pytest)")
    args = parser.parse_args()
    out = Path("wallex_sandbox/artifacts")/datetime.now(timezone.utc).strftime("checks_%Y%m%dT%H%M%S%fZ")
    out.mkdir(parents=True)
    # All durable app paths and dotenv handling are isolated from any existing installation.
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    for name, folder in {"CONDOR_RUNTIME_ROOT":"runtime", "CONDOR_DATA_DIR":"data", "CONDOR_AGENTS_ROOT":"agents",
                         "CONDOR_STOCK_AGENTS_ROOT":"stock", "CONDOR_REPORTS_DIR":"reports"}.items():
        os.environ[name] = str((out/folder).resolve())
    for name in list(os.environ):
        if any(part in name.upper() for part in ("API_KEY", "API_SECRET", "ACCESS_TOKEN", "WALLEX", "TELEGRAM_TOKEN")):
            os.environ.pop(name, None)
    stream = io.StringIO()
    logger = logging.StreamHandler(stream)
    logging.getLogger("condor").addHandler(logger)
    suite = unittest.defaultTestLoader.loadTestsFromName("wallex_sandbox.test_sandbox")
    if args.upstream:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromName("wallex_sandbox.test_tick"))
    with offline_only():
        result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    report = {"mode":"FAKE", "created_at_utc":datetime.now(timezone.utc).isoformat(), "python":sys.version.split()[0],
              "condor_commit":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
              "checks_run":result.testsRun,"failures":len(result.failures),"errors":len(result.errors),
              "network":"IP connections blocked in check process", "credentials_used":False,
              "model":"scripted; no external model calls", "success":result.wasSuccessful()}
    if args.upstream:
        import pytest
        with offline_only(), contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            status = pytest.main(["-q", "--junitxml="+str(out/"upstream.xml"),
                "tests/test_risk_gate.py", "tests/test_risk_leverage_gate.py", "tests/test_risk_drift_gate.py",
                "tests/trading_agent/test_risk_drawdown.py", "tests/test_engine_tick_phases.py"])
        report["upstream_exit_code"] = int(status)
        report["success"] = report["success"] and status == 0
        if (out/"upstream.xml").exists():
            suites = ET.parse(out/"upstream.xml").getroot().findall(".//testsuite")
            report["upstream"] = {key: sum(int(s.get(key, 0)) for s in suites)
                                  for key in ("tests", "failures", "errors", "skipped")}
    (out/"checks.log").write_text(stream.getvalue())
    (out/"summary.json").write_text(json.dumps(report,indent=2)+"\n")
    print(json.dumps(report,indent=2))
    print(f"Full output: {out/'checks.log'}")
    if not report["success"]:
        print(stream.getvalue()[-16000:])
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
