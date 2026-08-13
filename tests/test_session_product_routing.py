from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from zdecision.agent.recall_host_state import RecallHostStore
from zdecision.agent.repository_routes import RepositoryRouteSnapshot
from zdecision.agent.session_index import FrozenSessionSource
from zdecision.app_server.models import (
    AppServerTurnReceipt,
    FeasibilityModelProfile,
    SourceBoundary,
)
from zdecision.app_server.session_product_routing import (
    SessionProductRouter,
    SessionProductRoutingInvalid,
    SessionProductRoutingRetryable,
)
from zdecision.app_server.jsonl import AppServerTimeout
from zdecision.app_server.gateway import StructuredTurnOutputInvalid
from zdecision.central.decision_spaces import RepositoryDecisionRoute


REQUEST_ID = "crq_" + "1" * 32
REPOSITORY_ID = "repo_" + "2" * 32
SOURCE_KEY = "src_" + "3" * 32
SESSION_ID = "019fb100-0000-7000-8000-000000000001"
TURN_ID = "019fb100-0000-7000-8000-000000000002"
CLOUD_ROUTE = "drr_" + "4" * 32
THIRD_PARTY_ROUTE = "drr_" + "5" * 32


class FakeGateway:
    def __init__(self, cwd: str, profile: FeasibilityModelProfile) -> None:
        self.cwd = cwd
        self.profile = profile
        self.interactive = frozenset((SESSION_ID,))
        self.boundary_cwd = cwd
        self.output: object = {"route_id": THIRD_PARTY_ROUTE}
        self.fail_turn = False
        self.invalid_turn = False
        self.prompt = ""
        self.schema: dict[str, object] | None = None
        self.archived: list[str] = []

    def list_interactive_thread_ids(self, cwd: str) -> frozenset[str]:
        return self.interactive if cwd == self.cwd else frozenset()

    def read_completed_boundary(self, thread_id: str, turn_id: str):
        return SourceBoundary(
            thread_id=thread_id,
            turn_id=turn_id,
            cwd=self.boundary_cwd,
            status="completed",
            model_id=self.profile.model_id,
            reasoning_effort=self.profile.reasoning_effort,
        )

    def fork_disposable_thread(self, thread_id: str, last_turn_id: str) -> str:
        if (thread_id, last_turn_id) != (SESSION_ID, TURN_ID):
            raise AssertionError("router forked the wrong source boundary")
        return "routing-fork"

    def run_structured_turn(
        self, thread_id, prompt, output_schema, profile, cwd
    ) -> AppServerTurnReceipt:
        if self.fail_turn:
            raise AppServerTimeout("structured route result unavailable")
        if self.invalid_turn:
            raise StructuredTurnOutputInvalid("invalid structured output")
        self.prompt = prompt
        self.schema = output_schema
        if not isinstance(self.output, dict):
            structured = {"invalid": self.output}
        else:
            structured = self.output
        return AppServerTurnReceipt.create(
            thread_id=thread_id,
            turn_id="routing-turn",
            structured_output=structured,
            model_profile_id=profile.profile_id,
        )

    def archive_thread(self, thread_id: str) -> None:
        self.archived.append(thread_id)


class SessionProductRouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.cwd = str(self.root / "repository")
        Path(self.cwd).mkdir()
        self.profile = FeasibilityModelProfile.create(
            model_id="model-default",
            reasoning_effort="medium",
            discovery_digest="a" * 64,
            discovered_at="2026-08-13T10:00:00Z",
        )
        self.gateway = FakeGateway(self.cwd, self.profile)
        self.store = RecallHostStore.open(self.root / "agent.sqlite3")
        self.addCleanup(self.store.close)
        self.router = SessionProductRouter(
            gateway=self.gateway,
            recall_host_store=self.store,
            clock=lambda: datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
        )

    def source(self) -> FrozenSessionSource:
        return FrozenSessionSource(
            request_id=REQUEST_ID,
            source_key=SOURCE_KEY,
            repository_id=REPOSITORY_ID,
            session_id=SESSION_ID,
            cwd=self.cwd,
            lineage="lin_" + "6" * 32,
            previous_handled_turn_id=None,
            upper_turn_id=TURN_ID,
            source_fingerprint="7" * 64,
        )

    def snapshot(self) -> RepositoryRouteSnapshot:
        return RepositoryRouteSnapshot.create(
            REPOSITORY_ID,
            (
                RepositoryDecisionRoute(
                    CLOUD_ROUTE,
                    REPOSITORY_ID,
                    "dsp_" + "8" * 32,
                    ("packages/products/cloud",),
                    (),
                    True,
                    1,
                ),
                RepositoryDecisionRoute(
                    THIRD_PARTY_ROUTE,
                    REPOSITORY_ID,
                    "dsp_" + "9" * 32,
                    ("packages/products/third-party-services",),
                    (),
                    True,
                    1,
                ),
            ),
        )

    def test_routes_exact_frozen_session_to_registered_product(self) -> None:
        decision = self.router.route(
            self.source(), self.snapshot(), self.profile
        )

        self.assertEqual(SOURCE_KEY, decision.source_key)
        self.assertEqual(THIRD_PARTY_ROUTE, decision.route_id)
        self.assertEqual(64, len(decision.output_digest))
        self.assertTrue(self.store.is_internal_thread("routing-fork"))
        self.assertEqual(["routing-fork"], self.gateway.archived)
        self.assertEqual(
            [CLOUD_ROUTE, THIRD_PARTY_ROUTE],
            self.gateway.schema["properties"]["route_id"]["enum"],
        )
        self.assertNotIn(SESSION_ID, self.gateway.prompt)
        self.assertNotIn(TURN_ID, self.gateway.prompt)
        self.assertIn("packages/products/third-party-services", self.gateway.prompt)

    def test_invalid_boundary_or_output_fails_closed(self) -> None:
        self.gateway.boundary_cwd = str(self.root / "other")
        with self.assertRaisesRegex(
            SessionProductRoutingInvalid, "source_boundary_unavailable"
        ):
            self.router.route(self.source(), self.snapshot(), self.profile)

        self.gateway.boundary_cwd = self.cwd
        self.gateway.output = {"route_id": "drr_" + "0" * 32}
        with self.assertRaisesRegex(
            SessionProductRoutingInvalid, "session_route_invalid"
        ):
            self.router.route(self.source(), self.snapshot(), self.profile)

    def test_noninteractive_or_transport_failure_is_bounded(self) -> None:
        self.gateway.interactive = frozenset()
        with self.assertRaisesRegex(
            SessionProductRoutingInvalid, "source_not_interactive"
        ):
            self.router.route(self.source(), self.snapshot(), self.profile)

        self.gateway.interactive = frozenset((SESSION_ID,))
        self.gateway.fail_turn = True
        with self.assertRaisesRegex(
            SessionProductRoutingRetryable, "session_route_retryable"
        ):
            self.router.route(self.source(), self.snapshot(), self.profile)

        self.gateway.fail_turn = False
        self.gateway.invalid_turn = True
        with self.assertRaisesRegex(
            SessionProductRoutingInvalid, "session_route_invalid"
        ):
            self.router.route(self.source(), self.snapshot(), self.profile)


if __name__ == "__main__":
    unittest.main()
