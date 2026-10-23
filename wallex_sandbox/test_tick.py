"""Exercise Condor's actual gather -> streamed model -> persisted experiment path."""
from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from .sandbox import OPTIONS, buy, offline_only


class CondorTickChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        values = {"PYTHON_DOTENV_DISABLED": "1"}
        for key in ("CONDOR_RUNTIME_ROOT", "CONDOR_DATA_DIR", "CONDOR_AGENTS_ROOT", "CONDOR_STOCK_AGENTS_ROOT", "CONDOR_REPORTS_DIR"):
            values[key] = str(self.root/key.lower())
        self.env = patch.dict(os.environ, values)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.network = offline_only()
        self.network.__enter__()
        self.addCleanup(self.network.__exit__, None, None, None)

    def engine(self):
        from condor.agents.agent import Agent
        from condor.agents.engine import TickEngine
        from condor.agents.strategy import Strategy
        strategy = Strategy(agent_slug="wallex_paper", name="Offline Tick")
        strategy.home.mkdir(parents=True, exist_ok=True)
        engine = TickEngine(agent=Agent(slug="wallex_paper", name="Wallex Paper"), strategy=strategy,
                            config={"execution_mode": "dry_run", "tick_timeout_sec": 2}, chat_id=1, user_id=1)
        engine._get_client = AsyncMock(return_value=object())
        engine._adopt_running_bots = AsyncMock()
        engine._adopt_running_executors = AsyncMock()
        engine.provider_registry.run_core_providers = AsyncMock(return_value={})
        engine._notify = AsyncMock()
        return engine

    def test_complete_scripted_tick_records_denial_and_releases_model(self):
        from condor.acp.client import TextChunk, ToolCallEvent, ToolCallUpdate, PromptDone
        from condor.agents.risk import auto_approve_with_risk_check
        engine = self.engine()
        class ScriptedModel:
            active_model_id = "FAKE-scripted-no-provider"
            stopped = 0
            async def start(self):
                pass
            async def stop(self):
                self.stopped += 1
            async def prompt_stream(self, prompt):
                callback = auto_approve_with_risk_check(engine.risk, engine.risk.get_state(type("Tracker", (), {"get_drawdown_pct": lambda self: 0.0})()),
                                                      execution_mode="dry_run", refusals=engine._refusals)
                call = buy(controller_id=engine.agent_id)
                verdict = await callback(call, OPTIONS)
                yield ToolCallEvent("fake_buy", "create_order_executor", "in_progress", input=call["input"])
                yield ToolCallUpdate("fake_buy", status="failed", output=verdict["reason"])
                yield TextChunk("FAKE: proposed buy refused by Condor dry-run; no order executed.")
                yield PromptDone("end_turn")
        fake = ScriptedModel()
        engine._create_client = AsyncMock(return_value=fake)
        asyncio.run(engine._tick())
        snapshots = list(engine.strategy.home.rglob("experiment_*.md"))
        self.assertTrue(snapshots)
        text = snapshots[0].read_text()
        self.assertIn("FAKE: proposed buy", text)
        self.assertIn("create_order_executor", text)
        self.assertEqual(fake.stopped, 1)
        self.assertIsNone(engine._active_client)

    def test_model_failure_is_persisted_then_raised(self):
        from condor.acp.client import TextChunk, PromptDone
        engine = self.engine()
        class BrokenModel:
            stopped = False
            async def start(self):
                pass
            async def stop(self):
                self.stopped = True
            async def prompt_stream(self, prompt):
                yield TextChunk("FAKE model provider unavailable")
                yield PromptDone("error")
        fake = BrokenModel()
        engine._create_client = AsyncMock(return_value=fake)
        with self.assertRaisesRegex(RuntimeError, "FAKE model provider unavailable"):
            asyncio.run(engine._tick())
        snapshots = list(engine.strategy.home.rglob("experiment_*.md"))
        self.assertTrue(snapshots)
        self.assertIn("error", snapshots[0].read_text().lower())
        self.assertTrue(fake.stopped)
