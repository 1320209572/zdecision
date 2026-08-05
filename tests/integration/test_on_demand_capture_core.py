from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from tests.test_inventory import VALID_INVENTORY
from zdecision.agent.capture_operation_store import CaptureOperationStore
from zdecision.agent.capture_processor import OnDemandCaptureProcessor
from zdecision.agent.capture_routing import CaptureRoutingStore
from zdecision.agent.central_client import CentralClient
from zdecision.agent.control_bindings import ControlBindingStore
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import TestRepositoryMapping
from zdecision.agent.git_path_evidence import GitPathEvidenceReader
from zdecision.agent.hooks import handle_hook
from zdecision.agent.request_state import RequestStateStore
from zdecision.agent.service import AgentService
from zdecision.agent.session_index import (
    SessionIndex,
    SessionIndexEventProcessor,
)
from zdecision.agent.worker import Worker
from zdecision.app_server.jsonl import AppServerTimeout
from zdecision.app_server.models import (
    AppServerTurnReceipt,
    FeasibilityModelProfile,
    SourceBoundary,
)
from zdecision.app_server.reconciliation_runner import ReconciliationRunner
from zdecision.app_server.requested_capture import RequestedCaptureRunner
from zdecision.capture.on_demand import ValidatedCaptureResult
from zdecision.capture.templates import TemplateCatalog
from zdecision.central.api import create_app
from zdecision.central.auth import DemoIdentityProvider
from zdecision.central.decision_spaces import (
    EnabledRepository,
    LeafDecisionSpace,
    RepositoryDecisionRoute,
)
from zdecision.central.service import CaptureRequestService
from zdecision.central.store import CentralStore
from zdecision.ids import (
    decision_space_id,
    product_id,
    repository_route_id,
)
from zdecision.sync.contracts import RepositoryView


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPOSITORY_ROOT / "src" / "zdecision"
DEVICE_TOKEN = "integration-device-token"
PRODUCT_NAME = "ZDecision"
SESSION_A = "session-a-raw-sentinel"
SESSION_B = "session-b-raw-sentinel"
SESSION_CHILD = "session-child-raw-sentinel"
SESSION_PRIVATE = "session-private-raw-sentinel"
TURN_A1 = "turn-a1-raw-sentinel"
TURN_A2 = "turn-a2-raw-sentinel"
TURN_B1 = "turn-b1-raw-sentinel"
TURN_CHILD = "turn-child-raw-sentinel"
TURN_PRIVATE = "turn-private-raw-sentinel"
RAW_PROMPT = "PROMPT-RAW-SENTINEL-DO-NOT-SYNC"
RAW_SOURCE = "def raw_source_sentinel_never_syncs(): pass"
LOCAL_PATH_SENTINEL = "LOCAL-PATH-RAW-SENTINEL"
TRANSCRIPT_PATH_SENTINEL = "TRANSCRIPT-PATH-MUST-NEVER-BE-OPENED"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            return self.value

    def advance(self, seconds: int) -> None:
        with self._lock:
            self.value += timedelta(seconds=seconds)


class TestClientBridge:
    """Synchronous httpx transport backed by a restartable FastAPI TestClient."""

    def __init__(self, owner: "OnDemandCaptureCoreTest") -> None:
        self.owner = owner
        self.records: list[tuple[str, bytes, bytes]] = []
        self.drop_upload_responses = 0
        self.condition = threading.Condition()
        self.request_lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        browser = self.owner.browser
        if browser is None:
            raise httpx.ConnectError("central unavailable", request=request)
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query.decode('ascii')}"
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length"}
        }
        with self.request_lock:
            response = browser.request(
                request.method,
                path,
                headers=headers,
                content=request.content,
            )
        response_body = bytes(response.content)
        with self.condition:
            self.records.append(
                (request.url.path, bytes(request.content), response_body)
            )
            self.condition.notify_all()
        if (
            request.url.path.endswith(("/candidates", "/batch"))
            and self.drop_upload_responses > 0
        ):
            self.drop_upload_responses -= 1
            raise httpx.ConnectError(
                "simulated upload response loss", request=request
            )
        return httpx.Response(
            response.status_code,
            headers=dict(response.headers),
            content=response_body,
            request=request,
        )

    def heartbeat_count(self, request_id: str) -> int:
        suffix = f"/{request_id}/heartbeat"
        with self.condition:
            return sum(
                path.endswith(suffix) for path, _, _ in self.records
            )

    def wait_for_heartbeat(
        self,
        request_id: str,
        *,
        after_count: int,
        timeout: float,
    ) -> bool:
        suffix = f"/{request_id}/heartbeat"
        with self.condition:
            return self.condition.wait_for(
                lambda: sum(
                    path.endswith(suffix)
                    for path, _, _ in self.records
                )
                > after_count,
                timeout=timeout,
            )


class FakeAppServerGateway:
    """Unique native attempts; no stable tags or native-result adoption."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.interactive_ids = {SESSION_A, SESSION_B}
        self.boundaries = {
            SESSION_A: TURN_A1,
            SESSION_B: TURN_B1,
            SESSION_CHILD: TURN_CHILD,
        }
        self.available_boundaries = {
            (SESSION_A, TURN_A1),
            (SESSION_B, TURN_B1),
            (SESSION_CHILD, TURN_CHILD),
        }
        self.profile = FeasibilityModelProfile.create(
            model_id="model-default",
            reasoning_effort="medium",
            discovery_digest="a" * 64,
            discovered_at="2026-07-31T06:00:00Z",
        )
        self.catalog_digest = "a" * 64
        self.active_profile_resolutions = 0
        self.frozen_profile_checks = 0
        self.observed_catalog_digests: list[str] = []
        self.capture_source_by_fork: dict[str, tuple[str, str]] = {}
        self.source_context_by_boundary: dict[tuple[str, str], str] = {}
        self.source_context_by_fork: dict[str, str] = {}
        self.source_context_reads: list[str] = []
        self.source_reads: list[str] = []
        self.fork_creates = 0
        self.reconciliation_thread_creates = 0
        self.turn_creates = 0
        self.structured_turn_creates: dict[str, int] = {
            "inventory": 0,
            "extraction": 0,
            "reconciliation": 0,
        }
        self.archived_threads: list[str] = []
        self.zero_candidates = False
        self.replace_mode = False
        self.drop_next_fork_result = False
        self.drop_next_stage_result: str | None = None
        self.extraction_claims: list[str] = []
        self.before_inventory_result: Callable[[], None] | None = None
        self.version_by_session = {
            SESSION_A: "A 初始",
            SESSION_B: "B 初始",
        }
        self.version_by_boundary = {
            (SESSION_A, TURN_A1): "A 初始",
            (SESSION_B, TURN_B1): "B 初始",
        }

    def list_interactive_thread_ids(self, cwd: str) -> frozenset[str]:
        return (
            frozenset(self.interactive_ids)
            if cwd == self.cwd
            else frozenset()
        )

    def read_completed_boundary(
        self, thread_id: str, turn_id: str
    ) -> SourceBoundary:
        if (
            (thread_id, turn_id) not in self.available_boundaries
            and self.boundaries.get(thread_id) != turn_id
        ):
            from zdecision.app_server.gateway import UnknownSourceTurn

            raise UnknownSourceTurn("exact source boundary is unavailable")
        self.source_reads.append(thread_id)
        return SourceBoundary(
            thread_id=thread_id,
            turn_id=turn_id,
            cwd=self.cwd,
            status="completed",
            model_id=self.profile.model_id,
            reasoning_effort=self.profile.reasoning_effort,
        )

    def discover_and_freeze_profile(
        self, boundary: SourceBoundary
    ) -> FeasibilityModelProfile:
        return self.profile

    def resolve_active_profile(self) -> FeasibilityModelProfile:
        self.active_profile_resolutions += 1
        self.observed_catalog_digests.append(self.catalog_digest)
        return self.profile

    def require_supported_profile(
        self, profile: FeasibilityModelProfile
    ) -> FeasibilityModelProfile:
        self.frozen_profile_checks += 1
        self.observed_catalog_digests.append(self.catalog_digest)
        if (
            profile.model_id != self.profile.model_id
            or profile.reasoning_effort != self.profile.reasoning_effort
        ):
            from zdecision.app_server.gateway import (
                FrozenModelProfileUnavailable,
            )

            raise FrozenModelProfileUnavailable("profile is unsupported")
        return profile

    def fork_disposable_thread(
        self,
        thread_id: str,
        last_turn_id: str,
    ) -> str:
        if (
            (thread_id, last_turn_id) not in self.available_boundaries
            and self.boundaries.get(thread_id) != last_turn_id
        ):
            raise AssertionError("Capture fork used the wrong boundary")
        self.fork_creates += 1
        fork_id = f"capture-thread-{self.fork_creates}"
        self.capture_source_by_fork[fork_id] = (thread_id, last_turn_id)
        source_context = self.source_context_by_boundary.get(
            (thread_id, last_turn_id)
        )
        if source_context is not None:
            self.source_context_by_fork[fork_id] = source_context
        if self.drop_next_fork_result:
            self.drop_next_fork_result = False
            raise AppServerTimeout("external fork result unknown")
        return fork_id

    def start_disposable_thread(
        self,
        cwd: str,
        profile: FeasibilityModelProfile,
    ) -> str:
        self._assert_context(cwd, profile)
        self.reconciliation_thread_creates += 1
        return f"reconciliation-thread-{self.reconciliation_thread_creates}"

    def run_structured_turn(
        self,
        thread_id: str,
        prompt: str,
        output_schema: dict[str, object],
        profile: FeasibilityModelProfile,
        cwd: str,
    ) -> AppServerTurnReceipt:
        self._assert_context(cwd, profile)
        properties = output_schema.get("properties", {})
        if "signals" in properties:
            stage = "inventory"
            output = copy.deepcopy(VALID_INVENTORY)
        elif "candidates" in properties:
            stage = "extraction"
            output = self._extraction_output(thread_id)
        elif "results" in properties:
            stage = "reconciliation"
            output = self._reconciliation_output(prompt)
        else:
            raise AssertionError("Unexpected structured Turn")
        self.structured_turn_creates[stage] += 1
        self.turn_creates += 1
        if (
            stage == "inventory"
            and self.before_inventory_result is not None
        ):
            callback = self.before_inventory_result
            self.before_inventory_result = None
            callback()
        receipt = AppServerTurnReceipt.create(
            thread_id=thread_id,
            turn_id=f"{stage}-turn-{self.turn_creates}",
            structured_output=output,
            model_profile_id=profile.profile_id,
        )
        if self.drop_next_stage_result == stage:
            self.drop_next_stage_result = None
            raise AppServerTimeout("external Turn result unknown")
        return receipt

    def archive_thread(self, thread_id: str) -> None:
        self.archived_threads.append(thread_id)

    def _extraction_output(self, thread_id: str) -> dict[str, object]:
        if self.zero_candidates:
            return {"candidates": []}
        source_context = self.source_context_by_fork.get(thread_id)
        if source_context is not None:
            self.source_context_reads.append(source_context)
        source, turn = self.capture_source_by_fork[thread_id]
        version = (
            self.extraction_claims.pop(0)
            if self.extraction_claims
            else self.version_by_boundary.get(
                (source, turn), self.version_by_session[source]
            )
        )
        return self.candidate_output(version)

    @staticmethod
    def candidate_output(version: str) -> dict[str, object]:
        return {
            "candidates": [
                {
                    "product": PRODUCT_NAME,
                    "claim": f"页面请求产生候选决策：{version}",
                    "future_action": "只处理页面请求冻结的本地开发边界。",
                    "scope": {
                        "summary": "按需 Candidate 采集",
                        "repositories": ["zdecision"],
                        "paths": [],
                    },
                    "invalidation_conditions": [
                        "产品重新定义 Candidate 采集授权方式"
                    ],
                }
            ]
        }

    def _reconciliation_output(self, prompt: str) -> dict[str, object]:
        payload = prompt.split(
            "BEGIN_UNTRUSTED_RECONCILIATION_DATA\n", 1
        )[1].split("\nEND_UNTRUSTED_RECONCILIATION_DATA", 1)[0]
        data = json.loads(payload)
        current = data["current_families"]
        results = []
        for observation in data["observations"]:
            if self.replace_mode and current:
                results.append(
                    {
                        "observation_id": observation["observation_id"],
                        "relation": "replace",
                        "family_id": current[0]["family_id"],
                        "effective_content": observation["content"],
                    }
                )
            else:
                results.append(
                    {
                        "observation_id": observation["observation_id"],
                        "relation": "unrelated",
                        "family_id": observation["proposed_family_id"],
                        "effective_content": None,
                    }
                )
        return {"results": results}

    def _assert_context(
        self, cwd: str, profile: FeasibilityModelProfile
    ) -> None:
        if cwd != self.cwd or profile != self.profile:
            raise AssertionError("Wrong app-server context")



class OnDemandCaptureCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name).resolve()
        self.registered_repository = self.root / LOCAL_PATH_SENTINEL
        self.private_repository = self.root / "private-repository"
        self._make_repository(
            self.registered_repository,
            "https://github.com/example/zdecision-integration.git",
        )
        self._make_repository(
            self.private_repository,
            "https://github.com/example/private-integration.git",
        )

        from zdecision.agent.repository import RepositoryResolver

        resolver = RepositoryResolver(timeout_seconds=1.0)
        snapshot = resolver.resolve(self.registered_repository)
        self.assertIsNotNone(snapshot)
        self.repository_id = snapshot.repository_id
        self.product_id = product_id(PRODUCT_NAME)
        self.decision_space_id = decision_space_id(
            "product", self.product_id
        )
        self.route_id = repository_route_id(
            self.repository_id, self.decision_space_id
        )
        (self.registered_repository / "source.py").write_text(
            f"{RAW_SOURCE}\n# locally changed for trusted route evidence\n",
            "utf-8",
        )
        self.clock = MutableClock()
        self.central_path = self.root / "central.sqlite3"
        self.agent_path = self.root / "agent.sqlite3"
        self.browser: TestClient | None = None
        self.central_store: CentralStore | None = None
        self.bridge = TestClientBridge(self)
        self.gateway = FakeAppServerGateway(str(self.registered_repository))
        self.agent_database: AgentDatabase | None = None
        self.session_index: SessionIndex | None = None
        self.operation_store: CaptureOperationStore | None = None
        self.request_state: RequestStateStore | None = None
        self.routing_store: CaptureRoutingStore | None = None
        self.control_store: ControlBindingStore | None = None
        self.capture_runner: RequestedCaptureRunner | None = None
        self.reconciliation_runner: ReconciliationRunner | None = None
        self.central_client: CentralClient | None = None
        self.agent_service: AgentService | None = None
        self._start_central()
        self._start_local()

    def tearDown(self) -> None:
        self._stop_local()
        self._stop_central()

    def test_one_click_captures_changed_sessions_and_survives_restart(
        self,
    ) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._observe(SESSION_B, TURN_B1, self.registered_repository)
        self._observe(SESSION_CHILD, TURN_CHILD, self.registered_repository)
        self._observe(SESSION_PRIVATE, TURN_PRIVATE, self.private_repository)
        self._drain_hooks()

        request_id = self._click("web_action_restart")
        self._restart_central()
        self.assertTrue(self._run_agent_once())

        request = self._request(request_id)
        candidates = self._candidates()
        events = self.browser.get(
            f"/api/v1/capture-requests/{request_id}/events",
            params={"after_sequence": 1},
        )

        self.assertEqual("succeeded", request["state"])
        self.assertEqual({SESSION_A, SESSION_B}, set(self.gateway.source_reads))
        self.assertNotIn(SESSION_CHILD, self.gateway.source_reads)
        self.assertNotIn(SESSION_PRIVATE, self.gateway.source_reads)
        self.assertEqual(2, len(candidates))
        self.assertEqual(200, events.status_code, events.text)
        self.assertGreater(len(events.json()["events"]), 1)
        self.assertEqual(
            "succeeded", events.json()["events"][-1]["state"]
        )
        self._assert_central_has_no_raw_source()

    def test_one_action_routes_three_changed_leaves_to_three_slices(self) -> None:
        prefixes = (
            "packages/products/cloud/apps/core-shell",
            "packages/products/shared/zcf-license",
            "packages/shared/theme",
        )
        routes: list[RepositoryDecisionRoute] = []
        for index, prefix in enumerate(prefixes, start=1):
            compatibility_id = product_id(f"ZDecision leaf {index}")
            leaf_id = decision_space_id("product", compatibility_id)
            self.central_store.put_decision_space(
                "org_demo",
                LeafDecisionSpace(
                    decision_space_id=leaf_id,
                    kind="product",
                    display_name=f"Leaf {index}",
                    compatibility_product_id=compatibility_id,
                    compatibility_product_name=PRODUCT_NAME,
                    catalog_group_id=None,
                    catalog_breadcrumb=(),
                    source_root=prefix,
                    package_name=None,
                    asset_type=None,
                    enabled=True,
                ),
            )
            routes.append(
                RepositoryDecisionRoute(
                    route_id=repository_route_id(
                        self.repository_id, leaf_id
                    ),
                    repository_id=self.repository_id,
                    decision_space_id=leaf_id,
                    path_prefixes=(prefix,),
                    excluded_prefixes=(),
                    enabled=True,
                    configuration_version=1,
                )
            )
            changed = self.registered_repository / prefix / "changed.ts"
            changed.parent.mkdir(parents=True, exist_ok=True)
            changed.write_text("private local source\n", "utf-8")
        self.central_store.replace_trusted_route_heads(
            "org_demo", self.repository_id, tuple(routes)
        )
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()

        request_id = self._click("web_action_three_slices")
        self.assertTrue(self._run_agent_once())

        slices = self.central_store.connection.execute(
            "SELECT state, decision_space_id FROM capture_slices "
            "WHERE request_id = ? ORDER BY slice_order",
            (request_id,),
        ).fetchall()
        self.assertEqual(3, len(slices))
        self.assertEqual({"accepted"}, {item["state"] for item in slices})
        self.assertEqual(
            {route.decision_space_id for route in routes},
            {item["decision_space_id"] for item in slices},
        )
        self.assertEqual(3, len(self._candidates()))
        self.assertEqual(
            {"inventory": 3, "extraction": 3, "reconciliation": 3},
            self.gateway.structured_turn_creates,
        )

    def test_no_routable_path_finishes_without_model_and_acknowledges(self) -> None:
        self.central_store.replace_trusted_route_heads(
            "org_demo",
            self.repository_id,
            (
                RepositoryDecisionRoute(
                    route_id=self.route_id,
                    repository_id=self.repository_id,
                    decision_space_id=self.decision_space_id,
                    path_prefixes=("packages/unmodified",),
                    excluded_prefixes=(),
                    enabled=True,
                    configuration_version=2,
                ),
            ),
        )
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_no_route")
        source = self.session_index.freeze_sources(
            request_id,
            self.repository_id,
            self.clock(),
            capture_scope="all_valid_sessions",
        )[0]

        self.assertTrue(self._run_agent_once())

        self.assertEqual(
            "succeeded_no_candidates", self._request(request_id)["state"]
        )
        self.assertEqual(
            {"inventory": 0, "extraction": 0, "reconciliation": 0},
            self.gateway.structured_turn_creates,
        )
        self.assertEqual(
            TURN_A1, self.session_index.handled_turn(source.source_key)
        )

    def test_blocking_inventory_keeps_one_live_lease_past_thirty_seconds(
        self,
    ) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_long_inventory")

        def cross_lease_window() -> None:
            for _ in range(4):
                self.clock.advance(9)
                before = self.bridge.heartbeat_count(request_id)
                self.assertTrue(
                    self.bridge.wait_for_heartbeat(
                        request_id,
                        after_count=before,
                        timeout=1.0,
                    )
                )

        self.gateway.before_inventory_result = cross_lease_window

        self.assertTrue(self._run_agent_once())

        request = self._request(request_id)
        event_codes = [
            item["code"]
            for item in self.browser.get(
                f"/api/v1/capture-requests/{request_id}/events"
            ).json()["events"]
        ]
        record = self.central_store.get_request_record(request_id)
        self.assertEqual(
            "succeeded",
            request["state"],
            (request, event_codes),
        )
        self.assertEqual(1, record.attempt_count)
        self.assertNotIn("lease_expired_requeued", event_codes)
        self.assertNotIn("retry_exhausted", event_codes)

    def test_catalog_change_keeps_one_request_profile_across_restart(
        self,
    ) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._observe(SESSION_B, TURN_B1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_profile_restart")
        self.gateway.drop_next_stage_result = "inventory"

        self.assertTrue(self._run_agent_once())
        self.assertEqual(
            "failed_retryable", self._request(request_id)["state"]
        )
        sources = self.session_index.freeze_sources(
            request_id,
            self.repository_id,
            self.clock(),
            capture_scope="all_valid_sessions",
        )
        frozen_profile = self.session_index.request_model_profile(request_id)
        self.assertIsNotNone(frozen_profile)

        self.gateway.catalog_digest = "b" * 64
        self._restart_local()
        self._retry_request()

        self.assertEqual("succeeded", self._request(request_id)["state"])
        replayed_profile = self.session_index.request_model_profile(request_id)
        self.assertEqual(frozen_profile, replayed_profile)
        with closing(sqlite3.connect(self.agent_path)) as connection:
            rows = connection.execute(
                """
                SELECT frozen_json, status
                FROM capture_operations
                WHERE request_id = ?
                ORDER BY source_key
                """,
                (request_id,),
            ).fetchall()
        self.assertEqual(2, len(rows))
        self.assertEqual({"committed"}, {row[1] for row in rows})
        self.assertEqual(
            {frozen_profile.profile_id},
            {
                json.loads(row[0])["model_profile_id"]
                for row in rows
            },
        )
        self.assertEqual(1, self.gateway.active_profile_resolutions)
        self.assertEqual(1, self.gateway.frozen_profile_checks)
        self.assertEqual(
            ["a" * 64, "b" * 64],
            self.gateway.observed_catalog_digests,
        )
        self.assertTrue(
            all(
                self.session_index.handled_turn(source.source_key)
                == source.upper_turn_id
                for source in sources
            )
        )
        self.assertEqual(1, self._central_count("candidate_batches", request_id))

    def test_pre_amendment_null_profile_is_filled_before_first_operation(
        self,
    ) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_pre_amendment_profile")
        self.session_index.freeze_sources(
            request_id,
            self.repository_id,
            self.clock(),
            capture_scope="all_valid_sessions",
        )
        self.assertIsNone(
            self.session_index.request_model_profile(request_id)
        )
        self._restart_local()

        self.assertTrue(self._run_agent_once())

        profile = self.session_index.request_model_profile(request_id)
        self.assertIsNotNone(profile)
        operation = self._capture_operation(request_id)
        self.assertEqual(profile.profile_id, operation.frozen.model_profile_id)

    def test_no_click_runs_no_model_and_zero_candidates_is_success(
        self,
    ) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()

        self.assertFalse(self._run_agent_once())
        self.assertEqual(0, sum(self.gateway.structured_turn_creates.values()))

        self.gateway.zero_candidates = True
        request_id = self._click("web_action_zero")
        self.assertTrue(self._run_agent_once())

        self.assertEqual(
            "succeeded_no_candidates", self._request(request_id)["state"]
        )
        self.assertEqual([], self._candidates())
        self._assert_central_has_no_raw_source()

    def test_later_click_can_replace_the_current_candidate_revision(
        self,
    ) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        first_request = self._click("web_action_first")
        self.assertTrue(self._run_agent_once())
        first = self._candidates()
        self.assertEqual("succeeded", self._request(first_request)["state"])
        self.assertEqual(1, first[0]["revision"])

        self.clock.advance(60)
        self.gateway.boundaries[SESSION_A] = TURN_A2
        self.gateway.available_boundaries.add((SESSION_A, TURN_A2))
        self.gateway.version_by_session[SESSION_A] = "A 后续推翻"
        self.gateway.version_by_boundary[(SESSION_A, TURN_A2)] = "A 后续推翻"
        self.gateway.replace_mode = True
        self._observe(SESSION_A, TURN_A2, self.registered_repository)
        self._drain_hooks()
        second_request = self._click("web_action_second")
        self.assertTrue(self._run_agent_once())

        second = self._candidates()
        self.assertEqual("succeeded", self._request(second_request)["state"])
        self.assertEqual(1, len(second))
        self.assertEqual(first[0]["family_id"], second[0]["family_id"])
        self.assertEqual(2, second[0]["revision"])
        self.assertIn("后续推翻", second[0]["content"]["claim"])
        history_count = self.central_store.connection.execute(
            "SELECT COUNT(*) AS count FROM candidate_revisions"
        ).fetchone()["count"]
        self.assertEqual(2, history_count)

    def test_unknown_fork_retries_a_fresh_attempt_with_one_effect(self) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_unknown_fork")
        self.gateway.drop_next_fork_result = True

        self.assertTrue(self._run_agent_once())
        self.assertEqual("failed_retryable", self._request(request_id)["state"])
        self._retry_request()

        self.assertEqual("succeeded", self._request(request_id)["state"])
        self.assertEqual(2, self.gateway.fork_creates)
        self.assertEqual(1, self.gateway.structured_turn_creates["inventory"])
        self.assertEqual(1, self.gateway.structured_turn_creates["extraction"])
        attempts = self._capture_attempts(request_id)
        self.assertEqual([1, 2], [row["generation"] for row in attempts])
        self.assertEqual("fork_result_unknown", attempts[0]["failure_code"])
        self._assert_one_request_effect(request_id)

    def test_unknown_inventory_reruns_inventory_in_a_fresh_fork(self) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_unknown_inventory")
        self.gateway.drop_next_stage_result = "inventory"

        self.assertTrue(self._run_agent_once())
        self.assertEqual("failed_retryable", self._request(request_id)["state"])
        self._retry_request()

        self.assertEqual("succeeded", self._request(request_id)["state"])
        self.assertEqual(2, self.gateway.fork_creates)
        self.assertEqual(2, self.gateway.structured_turn_creates["inventory"])
        self.assertEqual(1, self.gateway.structured_turn_creates["extraction"])
        self._assert_one_request_effect(request_id)

    def test_unknown_extraction_reruns_both_stages_with_one_effect(self) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_unknown_extraction")
        self.gateway.drop_next_stage_result = "extraction"

        self.assertTrue(self._run_agent_once())
        self.assertEqual("failed_retryable", self._request(request_id)["state"])
        self._retry_request()

        self.assertEqual("succeeded", self._request(request_id)["state"])
        self.assertEqual(2, self.gateway.fork_creates)
        self.assertEqual(2, self.gateway.structured_turn_creates["inventory"])
        self.assertEqual(2, self.gateway.structured_turn_creates["extraction"])
        self._assert_one_request_effect(request_id)

    def test_late_abandoned_generation_cannot_change_candidate_family(self) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_late_generation")
        self.gateway.extraction_claims = ["abandoned output", "winning output"]
        self.gateway.drop_next_stage_result = "extraction"

        self.assertTrue(self._run_agent_once())
        self._retry_request()
        winner = self._candidates()[0]
        self.assertIn("winning output", winner["content"]["claim"])

        operation = self._capture_operation(request_id)
        first_attempt = self._capture_attempts(request_id)[0]
        late = ValidatedCaptureResult.create(
            operation.frozen,
            VALID_INVENTORY,
            self.gateway.candidate_output("late different output"),
        )
        self.operation_store.store_validated_attempt(
            first_attempt["attempt_id"], late, "2026-07-31T06:10:00Z"
        )
        late_commit = self.operation_store.commit_attempt(
            first_attempt["attempt_id"]
        )

        self.assertEqual("superseded", late_commit.attempt.state)
        self.assertIn(
            "winning output", late_commit.result.observations[0].content.claim
        )
        self.assertEqual(winner, self._candidates()[0])
        self._assert_one_request_effect(request_id)

    def test_restart_after_validated_capture_before_cas_uses_no_new_model(self) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_capture_pre_cas")
        with patch.object(
            self.operation_store,
            "commit_attempt",
            side_effect=RuntimeError("crash before Capture CAS"),
        ):
            self.assertTrue(self._run_agent_once())
        self.assertEqual("failed_retryable", self._request(request_id)["state"])
        self.assertEqual(
            "validated", self._capture_attempts(request_id)[0]["state"]
        )
        before = dict(self.gateway.structured_turn_creates)

        self._restart_local()
        self._retry_request()

        self.assertEqual("succeeded", self._request(request_id)["state"])
        self.assertEqual(before["inventory"], 1)
        self.assertEqual(before["extraction"], 1)
        self.assertEqual(1, self.gateway.structured_turn_creates["inventory"])
        self.assertEqual(1, self.gateway.structured_turn_creates["extraction"])
        self._assert_one_request_effect(request_id)

    def test_restart_after_capture_cas_replays_without_capture_model_work(self) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_after_capture_cas")
        with patch.object(
            self.reconciliation_runner,
            "run",
            side_effect=RuntimeError("crash after Capture CAS"),
        ):
            self.assertTrue(self._run_agent_once())
        self.assertEqual("failed_retryable", self._request(request_id)["state"])
        before = dict(self.gateway.structured_turn_creates)
        self.assertEqual("committed", self._capture_operation(request_id).status)

        self._restart_local()
        self._retry_request()

        self.assertEqual(
            before["inventory"],
            self.gateway.structured_turn_creates["inventory"],
        )
        self.assertEqual(
            before["extraction"],
            self.gateway.structured_turn_creates["extraction"],
        )
        self.assertEqual("succeeded", self._request(request_id)["state"])
        self._assert_one_request_effect(request_id)

    def test_restart_immediately_before_candidate_transaction_is_exact(self) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_before_candidate_tx")
        with patch.object(
            self.request_state,
            "commit_slice_result",
            side_effect=RuntimeError("crash before Candidate transaction"),
        ):
            self.assertTrue(self._run_agent_once())
        self.assertEqual("failed_retryable", self._request(request_id)["state"])
        before = dict(self.gateway.structured_turn_creates)

        self._restart_local()
        self._retry_request()

        self.assertEqual(before, self.gateway.structured_turn_creates)
        self.assertEqual("succeeded", self._request(request_id)["state"])
        self._assert_one_request_effect(request_id)

    def test_restart_immediately_after_candidate_transaction_replays_outbox(self) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_after_candidate_tx")
        original = self.request_state.commit_slice_result

        def commit_then_crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("crash after Candidate transaction")

        with patch.object(
            self.request_state,
            "commit_slice_result",
            side_effect=commit_then_crash,
        ):
            self.assertTrue(self._run_agent_once())
        self.assertEqual("failed_retryable", self._request(request_id)["state"])
        self.assertEqual(
            1,
            self._local_count("slice_candidate_outbox", request_id),
        )
        before = dict(self.gateway.structured_turn_creates)

        self._restart_local()
        self._retry_request()

        self.assertEqual(before, self.gateway.structured_turn_creates)
        self.assertEqual("succeeded", self._request(request_id)["state"])
        self._assert_one_request_effect(request_id)

    def test_later_turn_stays_outside_frozen_request_and_next_click_sees_it(self) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_frozen_boundary")
        frozen = self.session_index.freeze_sources(
            request_id,
            self.repository_id,
            self.clock(),
            capture_scope="all_valid_sessions",
        )[0]
        self.clock.advance(1)
        self.gateway.boundaries[SESSION_A] = TURN_A2
        self.gateway.available_boundaries.add((SESSION_A, TURN_A2))
        self.gateway.version_by_session[SESSION_A] = "later turn"
        self.gateway.version_by_boundary[(SESSION_A, TURN_A2)] = "later turn"
        self._observe(SESSION_A, TURN_A2, self.registered_repository)
        self._drain_hooks()

        self.assertTrue(self._run_agent_once())
        events = self.browser.get(
            f"/api/v1/capture-requests/{request_id}/events",
            params={"after_sequence": 0},
        ).json()["events"]
        self.assertEqual(
            "succeeded", self._request(request_id)["state"], events
        )
        self.assertEqual([SESSION_A], self.gateway.source_reads)
        self.assertEqual(
            TURN_A1, self.session_index.handled_turn(frozen.source_key)
        )
        self.assertNotIn(
            "later turn", self._candidates()[0]["content"]["claim"]
        )

        self.clock.advance(1)
        second = self._click("web_action_later_boundary")
        self.assertTrue(self._run_agent_once())
        self.assertEqual("succeeded", self._request(second)["state"])
        self.assertTrue(
            any(
                "later turn" in item["content"]["claim"]
                for item in self._candidates()
            )
        )

    def test_missing_exact_source_boundary_fails_closed(self) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_missing_boundary")
        frozen = self.session_index.freeze_sources(
            request_id,
            self.repository_id,
            self.clock(),
            capture_scope="all_valid_sessions",
        )[0]
        del self.gateway.boundaries[SESSION_A]
        self.gateway.available_boundaries.discard((SESSION_A, TURN_A1))

        self.assertTrue(self._run_agent_once())

        request = self._request(request_id)
        self.assertEqual("failed_terminal", request["state"])
        self.assertEqual(
            "source_boundary_unavailable",
            self.central_store.get_request_record(request_id).terminal_code,
        )
        self.assertIsNone(self.session_index.handled_turn(frozen.source_key))
        self.assertEqual(0, self._central_count("candidate_batches", request_id))

    def test_transcript_path_is_never_opened_or_persisted(self) -> None:
        transcript = self.root / TRANSCRIPT_PATH_SENTINEL
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if Path(path) == transcript:
                raise AssertionError("transcript_path was opened")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", new=guarded_open):
            self._observe(
                SESSION_A,
                TURN_A1,
                self.registered_repository,
                transcript_path=str(transcript),
            )
            self._drain_hooks()
            request_id = self._click("web_action_transcript_sentinel")
            self.assertTrue(self._run_agent_once())

        local_bytes = b"".join(
            path.read_bytes()
            for path in self.root.glob("agent.sqlite3*")
            if path.is_file()
        )
        self.assertNotIn(TRANSCRIPT_PATH_SENTINEL.encode(), local_bytes)
        self._assert_central_has_no_raw_source()

    def test_lost_upload_response_replays_exact_batch_after_agent_restart(
        self,
    ) -> None:
        self._observe(SESSION_A, TURN_A1, self.registered_repository)
        self._drain_hooks()
        request_id = self._click("web_action_upload_replay")
        self.bridge.drop_upload_responses = 3

        self.assertTrue(self._run_agent_once())
        self.assertEqual(
            "failed_retryable", self._request(request_id)["state"]
        )
        frozen = self.session_index.freeze_sources(
            request_id,
            self.repository_id,
            self.clock(),
            capture_scope="all_valid_sessions",
        )[0]
        self.assertIsNone(
            self.session_index.handled_turn(frozen.source_key)
        )

        first_turn_counts = dict(self.gateway.structured_turn_creates)
        self._restart_local()
        self._retry_request()

        self.assertEqual("succeeded", self._request(request_id)["state"])
        self.assertEqual(
            TURN_A1, self.session_index.handled_turn(frozen.source_key)
        )
        self.assertEqual(first_turn_counts, self.gateway.structured_turn_creates)
        upload_batches = [
            json.loads(body)["batch"]
            for path, body, _ in self.bridge.records
            if path.endswith(("/candidates", "/batch"))
        ]
        self.assertEqual(4, len(upload_batches))
        self.assertTrue(
            all(batch == upload_batches[0] for batch in upload_batches)
        )
        batch_count = self.central_store.connection.execute(
            "SELECT COUNT(*) AS count FROM capture_slices "
            "WHERE receipt_json IS NOT NULL"
        ).fetchone()["count"]
        self.assertEqual(1, batch_count)

    def _start_central(self) -> None:
        self.central_store = CentralStore.open(self.central_path)
        self.central_store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                repository_id=self.repository_id,
                product_id=self.product_id,
                product_name=PRODUCT_NAME,
                enabled=True,
            ),
        )
        self.central_store.put_repository(
            "org_demo", EnabledRepository(self.repository_id, True)
        )
        self.central_store.put_decision_space(
            "org_demo",
            LeafDecisionSpace(
                decision_space_id=self.decision_space_id,
                kind="product",
                display_name=PRODUCT_NAME,
                compatibility_product_id=self.product_id,
                compatibility_product_name=PRODUCT_NAME,
                catalog_group_id=None,
                catalog_breadcrumb=(),
                source_root=".",
                package_name=None,
                asset_type=None,
                enabled=True,
            ),
        )
        self.central_store.replace_trusted_route_heads(
            "org_demo",
            self.repository_id,
            (
                RepositoryDecisionRoute(
                    route_id=self.route_id,
                    repository_id=self.repository_id,
                    decision_space_id=self.decision_space_id,
                    path_prefixes=(".",),
                    excluded_prefixes=(),
                    enabled=True,
                    configuration_version=1,
                ),
            ),
        )
        identity = DemoIdentityProvider(
            organization_id="org_demo",
            user_id="user_demo",
            device_id="device_demo",
            device_token_sha256=hashlib.sha256(
                DEVICE_TOKEN.encode("utf-8")
            ).hexdigest(),
        )
        self.browser = TestClient(
            create_app(
                CaptureRequestService(self.central_store),
                identity,
                clock=self.clock,
            )
        )

    def _stop_central(self) -> None:
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        if self.central_store is not None:
            self.central_store.close()
            self.central_store = None

    def _restart_central(self) -> None:
        self._stop_central()
        self._start_central()

    def _start_local(self) -> None:
        self.agent_database = AgentDatabase.open(self.agent_path)
        self.agent_database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=self.repository_id,
                product_id=self.product_id,
                product_name=PRODUCT_NAME,
                enabled=True,
            )
        )
        self.agent_database.put_enabled_repository(
            EnabledRepository(self.repository_id, True)
        )
        self.session_index = SessionIndex.open(self.agent_path)
        self.operation_store = CaptureOperationStore.open(self.agent_path)
        self.request_state = RequestStateStore.open(self.agent_path)
        self.routing_store = CaptureRoutingStore.open(self.agent_path)
        self.control_store = ControlBindingStore.open(self.agent_path)
        self.agent_database.retire_legacy_automatic_capture()
        catalog = TemplateCatalog(
            REPOSITORY_ROOT / "decision-templates",
            PACKAGE_ROOT / "capture" / "prompt_contracts",
        )
        self.capture_runner = RequestedCaptureRunner(
            gateway=self.gateway,
            operation_store=self.operation_store,
            template_catalog=catalog,
        )
        self.reconciliation_runner = ReconciliationRunner(
            gateway=self.gateway,
            request_state=self.request_state,
        )
        processor = OnDemandCaptureProcessor(
            database=self.agent_database,
            session_index=self.session_index,
            git_paths=GitPathEvidenceReader(timeout_seconds=1.0),
            routing_store=self.routing_store,
            capture_runner=self.capture_runner,
            reconciliation_runner=self.reconciliation_runner,
            request_state=self.request_state,
            control_store=self.control_store,
            clock=self.clock,
        )
        self.central_client = CentralClient(
            "http://central.test",
            DEVICE_TOKEN,
            transport=httpx.MockTransport(self.bridge),
            sleeper=lambda _: None,
        )
        self.agent_service = AgentService(
            client=self.central_client,
            processor=processor,
            lease_client_factory=lambda: CentralClient(
                "http://central.test",
                DEVICE_TOKEN,
                transport=httpx.MockTransport(self.bridge),
                sleeper=lambda _: None,
            ),
            lease_interval_seconds=0.001,
        )

    def _stop_local(self) -> None:
        if self.central_client is not None:
            self.central_client.close()
            self.central_client = None
        if self.request_state is not None:
            self.request_state.close()
            self.request_state = None
        if self.routing_store is not None:
            self.routing_store.close()
            self.routing_store = None
        if self.control_store is not None:
            self.control_store.close()
            self.control_store = None
        if self.operation_store is not None:
            self.operation_store.close()
            self.operation_store = None
        if self.session_index is not None:
            self.session_index.close()
            self.session_index = None
        if self.agent_database is not None:
            self.agent_database.close()
            self.agent_database = None
        self.capture_runner = None
        self.reconciliation_runner = None
        self.agent_service = None

    def _restart_local(self) -> None:
        self._stop_local()
        self._start_local()

    def _observe(
        self,
        session_id: str,
        turn_id: str,
        cwd: Path,
        **extra: object,
    ) -> None:
        common = {
            "session_id": session_id,
            "cwd": str(cwd),
            **extra,
        }
        for value in (
            {
                **common,
                "hook_event_name": "SessionStart",
                "source": "startup",
            },
            {
                **common,
                "hook_event_name": "UserPromptSubmit",
                "turn_id": turn_id,
                "prompt": RAW_PROMPT,
            },
            {
                **common,
                "hook_event_name": "Stop",
                "turn_id": turn_id,
                "prompt": RAW_PROMPT,
            },
        ):
            handle_hook(
                value,
                database=self.agent_database,
                clock=self.clock,
                worker_waker=lambda _: None,
            )

    def _drain_hooks(self) -> None:
        cycle = Worker(
            database=self.agent_database,
            processor=SessionIndexEventProcessor(self.session_index),
            sync_poller=None,
            lock_path=self.root / "worker.lock",
        ).run_once(self.clock())
        self.assertEqual(cycle.claimed, cycle.consumed)

    def _click(self, action_id: str) -> str:
        response = self.browser.post(
            "/api/v1/capture-requests",
            json={
                "repository_id": self.repository_id,
                "template_id": "business",
                "capture_scope": "all_valid_sessions",
                "client_action_id": action_id,
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["request_id"]

    def _run_agent_once(self) -> bool:
        return self.agent_service.run_once()

    def _retry_request(self) -> None:
        self.clock.advance(31)
        self.assertTrue(self._run_agent_once())

    def _capture_attempts(self, request_id: str) -> list[sqlite3.Row]:
        connection = sqlite3.connect(self.agent_path)
        connection.row_factory = sqlite3.Row
        try:
            return connection.execute(
                """
                SELECT attempt.*
                FROM capture_execution_attempts AS attempt
                JOIN capture_operations AS operation
                  ON operation.operation_id = attempt.operation_id
                WHERE operation.request_id = ?
                ORDER BY attempt.generation
                """,
                (request_id,),
            ).fetchall()
        finally:
            connection.close()

    def _capture_operation(self, request_id: str):
        connection = sqlite3.connect(self.agent_path)
        try:
            rows = connection.execute(
                """
                SELECT source_key
                FROM capture_operations
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(1, len(rows))
        operation = self.operation_store.operation_for_source(
            request_id, rows[0][0]
        )
        self.assertIsNotNone(operation)
        return operation

    def _central_count(self, table: str, request_id: str) -> int:
        if table not in {"capture_requests", "candidate_batches"}:
            raise AssertionError("unexpected central table")
        if table == "candidate_batches":
            return self.central_store.connection.execute(
                "SELECT COUNT(*) AS count FROM capture_slices "
                "WHERE request_id = ? AND receipt_json IS NOT NULL",
                (request_id,),
            ).fetchone()["count"]
        if table == "capture_requests":
            table = "capture_groups"
        return self.central_store.connection.execute(
            f"SELECT COUNT(*) AS count FROM {table} WHERE request_id = ?",
            (request_id,),
        ).fetchone()["count"]

    def _assert_one_request_effect(self, request_id: str) -> None:
        connection = sqlite3.connect(self.agent_path)
        connection.row_factory = sqlite3.Row
        try:
            operation = connection.execute(
                """
                SELECT status, active_generation, winner_generation
                FROM capture_operations
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchall()
            reconciliations = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM slice_reconciliation_results
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()["count"]
            outbox = connection.execute(
                """
                SELECT batch_json, batch_digest
                FROM slice_candidate_outbox
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(1, len(operation))
        self.assertEqual("committed", operation[0]["status"])
        self.assertEqual(
            operation[0]["active_generation"],
            operation[0]["winner_generation"],
        )
        self.assertEqual(1, reconciliations)
        self.assertEqual(1, len(outbox))
        self.assertEqual(1, self._central_count("capture_requests", request_id))
        self.assertEqual(1, self._central_count("candidate_batches", request_id))
        central_batch = self.central_store.connection.execute(
            "SELECT batch_json FROM capture_slices WHERE request_id = ?",
            (request_id,),
        ).fetchone()["batch_json"]
        central_digest = json.loads(central_batch)["batch_digest"]
        local_batch = json.loads(outbox[0]["batch_json"])
        self.assertEqual(local_batch["batch_digest"], central_digest)

    def _local_count(self, table: str, request_id: str) -> int:
        if table != "slice_candidate_outbox":
            raise AssertionError("unexpected local table")
        with closing(sqlite3.connect(self.agent_path)) as connection:
            return connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE request_id = ?",
                (request_id,),
            ).fetchone()[0]

    def _request(self, request_id: str) -> dict[str, object]:
        response = self.browser.get(
            f"/api/v1/capture-requests/{request_id}"
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def _candidates(self) -> list[dict[str, object]]:
        response = self.browser.get(
            f"/api/v1/repositories/{self.repository_id}/candidates"
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["items"]

    def _assert_central_has_no_raw_source(self) -> None:
        central_bytes = b"".join(
            path.read_bytes()
            for path in self.root.glob("central.sqlite3*")
            if path.is_file()
        )
        http_bytes = b"".join(
            request + response
            for _, request, response in self.bridge.records
        )
        combined = central_bytes + http_bytes
        for forbidden in (
            RAW_PROMPT,
            RAW_SOURCE,
            LOCAL_PATH_SENTINEL,
            SESSION_A,
            SESSION_B,
            SESSION_CHILD,
            SESSION_PRIVATE,
            TURN_A1,
            TURN_B1,
            TURN_CHILD,
            TURN_PRIVATE,
            TRANSCRIPT_PATH_SENTINEL,
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden.encode("utf-8"), combined)

    @staticmethod
    def _make_repository(path: Path, remote: str) -> None:
        path.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "integration@example.com"],
            cwd=path,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "ZDecision Integration"],
            cwd=path,
            check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "origin", remote],
            cwd=path,
            check=True,
        )
        (path / "source.py").write_text(f"{RAW_SOURCE}\n", "utf-8")
        subprocess.run(["git", "add", "source.py"], cwd=path, check=True)
        subprocess.run(
            ["git", "commit", "-m", "fixture"],
            cwd=path,
            check=True,
            capture_output=True,
        )


if __name__ == "__main__":
    unittest.main()
