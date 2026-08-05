from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from zdecision.capture.models import CandidateContent
from zdecision.central.auth import DemoIdentityProvider
from zdecision.central.decision_spaces import (
    EnabledRepository,
    LeafDecisionSpace,
    RepositoryDecisionRoute,
)
from zdecision.central.service import CaptureRequestService
from zdecision.central.store import CentralStore
from zdecision.ids import (
    candidate_family_id,
    candidate_revision_id,
    capture_request_id,
    decision_space_id,
    repository_route_id,
)
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateBatchUpload,
    CaptureRequestView,
    RouteSelection,
    CandidateRevisionUpload,
    RepositoryView,
)

try:
    from fastapi.testclient import TestClient
    from zdecision.central.api import create_app
except (ImportError, ModuleNotFoundError) as error:
    API_IMPORT_ERROR: ImportError | ModuleNotFoundError | None = error
else:
    API_IMPORT_ERROR = None


REPOSITORY_ID = "repo_" + "1" * 32
PRODUCT_ID = "prod_4d7b16e1616dd4cd1aeb2411836fd687"
DEVICE_TOKEN = "demo-device-token"
NOW = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
EMPTY_BATCH_DIGEST = (
    "e813d564bccbeefe1db875d1c9abb55d63c52b639acc61134a5f1d19cc489b67"
)


def candidate_batch(
    request_id: str,
    *,
    repository_id: str = REPOSITORY_ID,
    claim: str = "页面操作是 Candidate 采集授权边界。",
    product: str = "ZDecision",
    family_id: str | None = None,
    revision: int = 1,
) -> CandidateBatchUpload:
    content = CandidateContent(
        product=product,
        claim=claim,
        future_action="只有页面请求才运行本地 Capture。",
        scope_summary="按需 Candidate 采集",
        repositories=("zdecision",),
        paths=(),
        invalidation_conditions=("产品重新定义采集边界",),
    )
    content_digest = hashlib.sha256(
        canonical_json_bytes(content.to_dict())
    ).hexdigest()
    selected_family_id = family_id or candidate_family_id(
        repository_id, "cand_" + "4" * 32 + "_01"
    )
    item = CandidateRevisionUpload(
        family_id=selected_family_id,
        revision_id=candidate_revision_id(
            selected_family_id, revision, content_digest
        ),
        revision=revision,
        content=content,
        content_digest=content_digest,
        evidence_digest="5" * 64,
    )
    return CandidateBatchUpload(
        request_id=request_id,
        repository_id=repository_id,
        items=(item,),
        batch_digest=hashlib.sha256(
            canonical_json_bytes({"items": [item.to_dict()]})
        ).hexdigest(),
    )


def empty_batch(request_id: str) -> CandidateBatchUpload:
    return CandidateBatchUpload(
        request_id=request_id,
        repository_id=REPOSITORY_ID,
        items=(),
        batch_digest=EMPTY_BATCH_DIGEST,
    )


class CentralApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            API_IMPORT_ERROR,
            f"Central API dependencies are missing: {API_IMPORT_ERROR}",
        )
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "central.sqlite3"
        self.store = CentralStore.open(self.database_path)
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                repository_id=REPOSITORY_ID,
                product_id=PRODUCT_ID,
                product_name="ZDecision",
                enabled=True,
            ),
        )
        self.store.put_repository(
            "org_demo", EnabledRepository(REPOSITORY_ID, True)
        )
        cloud = LeafDecisionSpace(
            decision_space_id=decision_space_id("product", PRODUCT_ID),
            kind="product",
            display_name="ZDecision",
            compatibility_product_id=PRODUCT_ID,
            compatibility_product_name="ZDecision",
            catalog_group_id=None,
            catalog_breadcrumb=(),
            source_root="src",
            package_name=None,
            asset_type=None,
            enabled=True,
        )
        self.store.put_decision_space("org_demo", cloud)
        self.store.replace_trusted_route_heads(
            "org_demo",
            REPOSITORY_ID,
            (
                RepositoryDecisionRoute(
                    route_id=repository_route_id(REPOSITORY_ID, cloud.decision_space_id),
                    repository_id=REPOSITORY_ID,
                    decision_space_id=cloud.decision_space_id,
                    path_prefixes=(".",),
                    excluded_prefixes=(),
                    enabled=True,
                    configuration_version=1,
                ),
            ),
        )
        service = CaptureRequestService(self.store)
        identity = DemoIdentityProvider(
            organization_id="org_demo",
            user_id="user_demo",
            device_id="device_demo",
            device_token_sha256=hashlib.sha256(
                DEVICE_TOKEN.encode("utf-8")
            ).hexdigest(),
        )
        self.client = TestClient(
            create_app(service, identity, clock=lambda: NOW)
        )

    def tearDown(self) -> None:
        if hasattr(self, "client"):
            self.client.close()
        if hasattr(self, "store"):
            self.store.close()
        if hasattr(self, "temporary_directory"):
            self.temporary_directory.cleanup()

    @property
    def authorization(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {DEVICE_TOKEN}"}

    def create_request(
        self, action_id: str = "web_action_001"
    ) -> str:
        response = self.client.post(
            "/api/v1/capture-requests",
            json={
                "repository_id": REPOSITORY_ID,
                "template_id": "business",
                "capture_scope": "all_valid_sessions",
                "client_action_id": action_id,
            },
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()["request_id"]

    def start_request(
        self, action_id: str = "web_action_001"
    ) -> tuple[str, str]:
        request_id = self.create_request(action_id)
        claimed = self.client.post(
            "/api/v1/agent/capture-requests/claim",
            headers=self.authorization,
            json={},
        )
        self.assertEqual(200, claimed.status_code, claimed.text)
        lease_token = claimed.json()["lease_token"]
        started = self.client.post(
            f"/api/v1/agent/capture-requests/{request_id}/start",
            headers=self.authorization,
            json={"lease_token": lease_token},
        )
        self.assertEqual(200, started.status_code, started.text)
        return request_id, lease_token

    def upload(
        self,
        request_id: str,
        lease_token: str,
        batch: CandidateBatchUpload,
    ):
        return self.client.post(
            (
                "/api/v1/agent/capture-requests/"
                f"{request_id}/candidates"
            ),
            headers=self.authorization,
            json={
                "lease_token": lease_token,
                "batch": batch.to_dict(),
            },
        )

    def test_browser_routes_derive_identity_and_return_registered_repositories(
        self,
    ) -> None:
        repositories = self.client.get("/api/v1/repositories")

        self.assertEqual(200, repositories.status_code)
        self.assertEqual(
            [REPOSITORY_ID],
            [
                item["repository_id"]
                for item in repositories.json()["repositories"]
            ],
        )
        request_id = self.create_request()
        request = self.client.get(f"/api/v1/capture-requests/{request_id}")
        self.assertEqual("queued", request.json()["state"])

    def test_repository_spaces_returns_the_enabled_route_catalog(self) -> None:
        response = self.client.get(f"/api/v1/repositories/{REPOSITORY_ID}/spaces")

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(REPOSITORY_ID, response.json()["repository_id"])
        self.assertIn("spaces", response.json())

    def test_create_rejects_unknown_identity_or_source_fields(self) -> None:
        response = self.client.post(
            "/api/v1/capture-requests",
            json={
                "repository_id": REPOSITORY_ID,
                "template_id": "business",
                "capture_scope": "all_valid_sessions",
                "client_action_id": "web_action_001",
                "organization_id": "forbidden",
                "session_id": "forbidden",
            },
        )

        self.assertEqual(422, response.status_code)
        self.assertNotIn("org_demo", response.text)

    def test_refresh_reconnects_from_event_cursor(self) -> None:
        request_id = self.create_request()
        claimed = self.client.post(
            "/api/v1/agent/capture-requests/claim",
            headers=self.authorization,
            json={},
        )
        self.assertEqual(200, claimed.status_code, claimed.text)

        response = self.client.get(
            f"/api/v1/capture-requests/{request_id}/events",
            params={"after_sequence": 1},
        )

        self.assertEqual(200, response.status_code)
        self.assertEqual(
            [2],
            [item["sequence"] for item in response.json()["events"]],
        )

    def test_public_request_reads_include_legacy_capture_evidence(self) -> None:
        action_id = "web_action_legacy_read"
        request_id = capture_request_id(
            "org_demo", REPOSITORY_ID, "business", action_id
        )
        timestamp = "2026-07-31T02:00:00Z"
        with self.store.connection:
            self.store.connection.execute(
                """INSERT INTO capture_requests(
                request_id, organization_id, actor_id, repository_id,
                product_id, product_name, template_id, capture_scope,
                client_action_id, state, attempt_count, claimed_device_id,
                lease_token_digest, lease_expires_at, retry_at,
                result_batch_digest, result_candidate_count, terminal_code,
                last_sequence, created_at, updated_at
                ) VALUES (?, 'org_demo', 'user_demo', ?, ?, 'ZDecision',
                'business', 'all_valid_sessions', ?, 'succeeded_no_candidates',
                1, NULL, NULL, NULL, NULL, ?, 0,
                'capture_succeeded_no_candidates', 1, ?, ?)""",
                (
                    request_id,
                    REPOSITORY_ID,
                    PRODUCT_ID,
                    action_id,
                    EMPTY_BATCH_DIGEST,
                    timestamp,
                    timestamp,
                ),
            )
            self.store.connection.execute(
                """INSERT INTO capture_request_events(
                request_id, sequence, state, code, occurred_at
                ) VALUES (?, 1, 'succeeded_no_candidates',
                'capture_succeeded_no_candidates', ?)""",
                (request_id, timestamp),
            )

        browser_read = self.client.get(
            f"/api/v1/capture-requests/{request_id}"
        )
        plugin_read = self.client.get(
            f"/api/v1/plugin/capture-requests/{request_id}",
            headers=self.authorization,
        )
        events = self.client.get(
            f"/api/v1/capture-requests/{request_id}/events"
        )

        self.assertEqual(200, browser_read.status_code, browser_read.text)
        self.assertEqual(200, plugin_read.status_code, plugin_read.text)
        self.assertEqual(200, events.status_code, events.text)
        self.assertEqual("succeeded_no_candidates", browser_read.json()["state"])
        self.assertEqual(
            ["capture_succeeded_no_candidates"],
            [item["code"] for item in events.json()["events"]],
        )

    def test_public_update_uses_group_slice_endpoints_only(self) -> None:
        request_id = self.create_request("web_action_group_api")
        claimed = self.client.post(
            "/api/v1/agent/capture-requests/claim",
            headers=self.authorization,
            json={},
        ).json()
        route = claimed["route_snapshot"][0]
        selection = RouteSelection(
            route_id=route["route_id"],
            configuration_version=route["configuration_version"],
            matched_path_digest="1" * 64,
            source_boundary_digest="2" * 64,
        )

        planned = self.client.post(
            f"/api/v1/agent/capture-requests/{request_id}/slices",
            headers=self.authorization,
            json={
                "lease_token": claimed["lease_token"],
                "selections": [selection.to_dict()],
            },
        )

        self.assertEqual(200, planned.status_code, planned.text)
        self.assertEqual(1, len(planned.json()["slices"]))
        self.assertEqual(
            0,
            self.store.connection.execute(
                "SELECT COUNT(*) FROM capture_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()[0],
        )

    def test_device_endpoint_requires_configured_bearer_token(self) -> None:
        missing = self.client.post(
            "/api/v1/agent/capture-requests/claim", json={}
        )
        wrong = self.client.post(
            "/api/v1/agent/capture-requests/claim",
            headers={"Authorization": "Bearer wrong-token"},
            json={},
        )

        self.assertEqual(401, missing.status_code)
        self.assertEqual({"error": "device_authentication_failed"}, missing.json())
        self.assertEqual(401, wrong.status_code)
        self.assertNotIn(DEVICE_TOKEN, wrong.text)

    def test_plugin_create_uses_device_authentication_and_demo_user(self) -> None:
        response = self.client.post(
            "/api/v1/plugin/capture-requests",
            headers=self.authorization,
            json={
                "repository_id": REPOSITORY_ID,
                "template_id": "business",
                "capture_scope": "current_session",
                "client_action_id": "codex_action_001",
            },
        )

        self.assertEqual(200, response.status_code, response.text)
        request_id = response.json()["request_id"]
        self.assertEqual(
            "user_demo",
            self.store.connection.execute(
                "SELECT actor_id FROM capture_groups WHERE request_id = ?",
                (request_id,),
            ).fetchone()["actor_id"],
        )

        for headers in ({}, {"Authorization": "Bearer wrong-token"}):
            with self.subTest(headers=headers):
                rejected = self.client.post(
                    "/api/v1/plugin/capture-requests",
                    headers=headers,
                    json={
                        "repository_id": REPOSITORY_ID,
                        "template_id": "business",
                        "capture_scope": "current_session",
                        "client_action_id": "codex_action_002",
                    },
                )
                self.assertEqual(401, rejected.status_code)

        current = self.client.get(
            f"/api/v1/plugin/capture-requests/{request_id}",
            headers=self.authorization,
        )
        self.assertEqual(200, current.status_code, current.text)
        self.assertEqual(request_id, current.json()["request_id"])
        for headers in ({}, {"Authorization": "Bearer wrong-token"}):
            with self.subTest(headers=headers):
                rejected = self.client.get(
                    f"/api/v1/plugin/capture-requests/{request_id}",
                    headers=headers,
                )
                self.assertEqual(401, rejected.status_code)

    def test_plugin_create_response_passes_the_strict_client_contract(
        self,
    ) -> None:
        response = self.client.post(
            "/api/v1/plugin/capture-requests",
            headers=self.authorization,
            json={
                "repository_id": REPOSITORY_ID,
                "template_id": "business",
                "capture_scope": "current_session",
                "client_action_id": "codex_action_strict-client",
            },
        )

        self.assertEqual(200, response.status_code, response.text)
        parsed = CaptureRequestView.from_dict(response.json())
        self.assertEqual(response.json()["request_id"], parsed.request_id)

    def test_plugin_create_rejects_identity_and_local_source_fields(self) -> None:
        command = {
            "repository_id": REPOSITORY_ID,
            "template_id": "business",
            "capture_scope": "current_session",
            "client_action_id": "codex_action_001",
        }
        for field in (
            "organization_id",
            "actor_id",
            "product_id",
            "device_id",
            "session_id",
            "turn_id",
            "cwd",
            "control_id",
        ):
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/v1/plugin/capture-requests",
                    headers=self.authorization,
                    json={**command, field: "forbidden"},
                )
                self.assertEqual(422, response.status_code)
                self.assertEqual({"error": "invalid_request"}, response.json())

    def test_plugin_create_exposes_the_central_busy_code(self) -> None:
        command = {
            "repository_id": REPOSITORY_ID,
            "template_id": "business",
            "capture_scope": "current_session",
            "client_action_id": "codex_action_001",
        }
        created = self.client.post(
            "/api/v1/plugin/capture-requests",
            headers=self.authorization,
            json=command,
        )
        self.assertEqual(200, created.status_code, created.text)

        response = self.client.post(
            "/api/v1/plugin/capture-requests",
            headers=self.authorization,
            json={**command, "client_action_id": "codex_action_002"},
        )

        self.assertEqual(409, response.status_code)
        self.assertEqual({"error": "repository_capture_busy"}, response.json())

    def test_device_routes_expose_only_bounded_lifecycle_values(self) -> None:
        request_id, lease_token = self.start_request()
        start = self.client.get(
            f"/api/v1/capture-requests/{request_id}"
        )
        progress = self.client.post(
            f"/api/v1/agent/capture-requests/{request_id}/progress",
            headers=self.authorization,
            json={
                "lease_token": lease_token,
                "code": "extracting_candidates",
            },
        )
        upload = self.upload(
            request_id, lease_token, empty_batch(request_id)
        )
        complete = self.client.post(
            f"/api/v1/agent/capture-requests/{request_id}/complete",
            headers=self.authorization,
            json={
                "lease_token": lease_token,
                "batch_digest": EMPTY_BATCH_DIGEST,
            },
        )

        self.assertEqual("running", start.json()["state"])
        self.assertEqual("extracting_candidates", progress.json()["code"])
        self.assertEqual(200, upload.status_code, upload.text)
        self.assertEqual("succeeded_no_candidates", complete.json()["state"])
        for response in (start, progress, upload, complete):
            self.assertNotIn("session_id", response.text)
            self.assertNotIn("turn_id", response.text)
            self.assertNotIn(lease_token, response.text)

    def test_duplicate_batch_replays_receipt_and_conflict_is_409(
        self,
    ) -> None:
        request_id, lease_token = self.start_request()
        batch = candidate_batch(request_id)

        first = self.upload(request_id, lease_token, batch)
        replay = self.upload(request_id, lease_token, batch)
        conflict = self.upload(
            request_id,
            lease_token,
            candidate_batch(
                request_id,
                claim="不同的批次不能覆盖原始请求结果。",
            ),
        )

        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(409, conflict.status_code)
        self.assertEqual(
            {"error": "batch_conflict"}, conflict.json()
        )

    def test_page_lists_only_current_repository_candidates(
        self,
    ) -> None:
        request_id, lease_token = self.start_request()
        batch = candidate_batch(request_id)
        uploaded = self.upload(request_id, lease_token, batch)
        self.assertEqual(200, uploaded.status_code, uploaded.text)

        response = self.client.get(
            f"/api/v1/repositories/{REPOSITORY_ID}/candidates"
        )

        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(
            [batch.items[0].revision_id],
            [
                item["revision_id"]
                for item in response.json()["items"]
            ],
        )
        self.assertEqual(
            batch.items[0].content.claim,
            response.json()["items"][0]["content"]["claim"],
        )
        for forbidden in (
            "session_id",
            "turn_id",
            "source-thread",
            "/Users/",
            "prompt",
            "diff",
        ):
            self.assertNotIn(forbidden, response.text)

    def test_completion_requires_the_exact_stored_batch(self) -> None:
        request_id, lease_token = self.start_request()
        premature = self.client.post(
            f"/api/v1/agent/capture-requests/{request_id}/complete",
            headers=self.authorization,
            json={
                "lease_token": lease_token,
                "batch_digest": EMPTY_BATCH_DIGEST,
            },
        )
        self.assertEqual(409, premature.status_code)

        upload = self.upload(
            request_id, lease_token, empty_batch(request_id)
        )
        completed = self.client.post(
            f"/api/v1/agent/capture-requests/{request_id}/complete",
            headers=self.authorization,
            json={
                "lease_token": lease_token,
                "batch_digest": EMPTY_BATCH_DIGEST,
            },
        )

        self.assertEqual(200, upload.status_code, upload.text)
        self.assertEqual(
            "succeeded_no_candidates", completed.json()["state"]
        )

    def test_candidate_product_must_match_server_mapping(self) -> None:
        request_id, lease_token = self.start_request()

        response = self.upload(
            request_id,
            lease_token,
            candidate_batch(request_id, product="Wrong Product"),
        )

        self.assertEqual(409, response.status_code)
        self.assertEqual(
            {"error": "candidate_product_mismatch"},
            response.json(),
        )

    def test_later_revision_replaces_only_the_current_head(self) -> None:
        first_request, first_lease = self.start_request(
            "web_action_first"
        )
        first_batch = candidate_batch(first_request)
        first_upload = self.upload(
            first_request, first_lease, first_batch
        )
        self.assertEqual(
            200, first_upload.status_code, first_upload.text
        )
        completed = self.client.post(
            (
                "/api/v1/agent/capture-requests/"
                f"{first_request}/complete"
            ),
            headers=self.authorization,
            json={
                "lease_token": first_lease,
                "batch_digest": first_batch.batch_digest,
            },
        )
        self.assertEqual(200, completed.status_code, completed.text)

        second_request, second_lease = self.start_request(
            "web_action_second"
        )
        second_batch = candidate_batch(
            second_request,
            claim="后续产品决策推翻并替换了原始约束。",
            family_id=first_batch.items[0].family_id,
            revision=2,
        )
        second_upload = self.upload(
            second_request, second_lease, second_batch
        )
        self.assertEqual(
            200, second_upload.status_code, second_upload.text
        )

        response = self.client.get(
            f"/api/v1/repositories/{REPOSITORY_ID}/candidates"
        )
        self.assertEqual(
            [second_batch.items[0].revision_id],
            [
                item["revision_id"]
                for item in response.json()["items"]
            ],
        )
        history_count = self.store.connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM candidate_revisions
            WHERE organization_id = 'org_demo'
              AND repository_id = ?
            """,
            (REPOSITORY_ID,),
        ).fetchone()["count"]
        self.assertEqual(2, history_count)


if __name__ == "__main__":
    unittest.main()
