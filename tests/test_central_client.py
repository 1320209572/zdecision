from __future__ import annotations

import hashlib
import json
import unittest

import httpx

from zdecision.capture.models import CandidateContent
from zdecision.ids import candidate_family_id, candidate_revision_id
from zdecision.jsonio import canonical_json_bytes
from zdecision.sync.contracts import (
    CandidateBatchUpload,
    CandidateRevisionUpload,
    CaptureRequestCreate,
)

try:
    from zdecision.agent.central_client import CentralClient, CentralClientError
except ModuleNotFoundError as error:
    CLIENT_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    CLIENT_IMPORT_ERROR = None


BASE_URL = "http://127.0.0.1:8765"
DEVICE_TOKEN = "device-secret-token"
REQUEST_ID = "crq_" + "1" * 32
REPOSITORY_ID = "repo_" + "2" * 32
PRODUCT_ID = "prod_" + "3" * 32
EMPTY_BATCH_DIGEST = hashlib.sha256(
    canonical_json_bytes({"items": []})
).hexdigest()


def claimed_payload() -> dict[str, object]:
    return {
        "request_id": REQUEST_ID,
        "repository_id": REPOSITORY_ID,
        "product_id": PRODUCT_ID,
        "product_name": "ZDecision",
        "template_id": "business",
        "capture_scope": "all_valid_sessions",
        "client_action_id": "web_action_001",
        "lease_token": "lease_0123456789abcdef",
        "lease_expires_at": "2026-07-31T03:00:30Z",
    }


def capture_request_payload() -> dict[str, object]:
    return {
        "request_id": REQUEST_ID,
        "repository_id": REPOSITORY_ID,
        "product_id": PRODUCT_ID,
        "product_name": "ZDecision",
        "template_id": "business",
        "state": "queued",
        "progress_code": "queued",
        "candidate_revision_count": None,
        "last_sequence": 1,
        "created_at": "2026-07-31T03:00:00Z",
        "updated_at": "2026-07-31T03:00:00Z",
    }


def valid_upload_batch() -> CandidateBatchUpload:
    content = CandidateContent(
        product="ZDecision",
        claim="正式决策按产品隔离保存。",
        future_action="新增决策时写入对应产品目录。",
        scope_summary="正式决策的产品归属",
        repositories=(REPOSITORY_ID,),
        paths=("decision-registry/",),
        invalidation_conditions=("产品隔离策略被替代",),
    )
    content_digest = hashlib.sha256(
        canonical_json_bytes(content.to_dict())
    ).hexdigest()
    family_id = candidate_family_id(
        REPOSITORY_ID,
        "cand_" + "4" * 32 + "_01",
    )
    item = CandidateRevisionUpload(
        family_id=family_id,
        revision_id=candidate_revision_id(family_id, 1, content_digest),
        revision=1,
        content=content,
        content_digest=content_digest,
        evidence_digest="5" * 64,
    )
    batch_digest = hashlib.sha256(
        canonical_json_bytes({"items": [item.to_dict()]})
    ).hexdigest()
    return CandidateBatchUpload(
        request_id=REQUEST_ID,
        repository_id=REPOSITORY_ID,
        items=(item,),
        batch_digest=batch_digest,
    )


class RecordingTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(request)
        return response


class CentralClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            CLIENT_IMPORT_ERROR,
            f"Central client is missing: {CLIENT_IMPORT_ERROR}",
        )

    def test_claim_sends_only_authorization_and_empty_body(self) -> None:
        transport = RecordingTransport(
            [httpx.Response(200, json=claimed_payload())]
        )
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(transport),
        )
        try:
            claimed = client.claim_next()
        finally:
            client.close()

        self.assertEqual(REQUEST_ID, claimed.request_id)
        request = transport.requests[0]
        self.assertEqual({}, json.loads(request.content))
        self.assertEqual(
            f"Bearer {DEVICE_TOKEN}",
            request.headers["Authorization"],
        )
        self.assertEqual(
            "/api/v1/agent/capture-requests/claim",
            request.url.path,
        )

    def test_plugin_capture_client_sends_only_command_and_reads_request(self) -> None:
        transport = RecordingTransport(
            [
                httpx.Response(200, json=capture_request_payload()),
                httpx.Response(200, json=capture_request_payload()),
            ]
        )
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(transport),
        )
        command = CaptureRequestCreate(
            repository_id=REPOSITORY_ID,
            template_id="business",
            capture_scope="current_session",
            client_action_id="codex_action_001",
        )
        try:
            created = client.create_capture_request(command)
            read = client.get_capture_request(created.request_id)
        finally:
            client.close()

        self.assertEqual(REQUEST_ID, read.request_id)
        self.assertEqual(
            {
                "repository_id": REPOSITORY_ID,
                "template_id": "business",
                "capture_scope": "current_session",
                "client_action_id": "codex_action_001",
            },
            json.loads(transport.requests[0].content),
        )
        self.assertEqual(
            "/api/v1/plugin/capture-requests", transport.requests[0].url.path
        )
        self.assertEqual(
            f"/api/v1/plugin/capture-requests/{REQUEST_ID}",
            transport.requests[1].url.path,
        )

    def test_plugin_create_preserves_only_repository_busy_error(self) -> None:
        transport = RecordingTransport(
            [httpx.Response(409, json={"error": "repository_capture_busy"})]
        )
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(transport),
        )
        command = CaptureRequestCreate(
            repository_id=REPOSITORY_ID,
            template_id="business",
            capture_scope="current_session",
            client_action_id="codex_action_001",
        )
        try:
            with self.assertRaisesRegex(
                CentralClientError, "repository_capture_busy"
            ):
                client.create_capture_request(command)
        finally:
            client.close()

        unexpected = RecordingTransport(
            [httpx.Response(409, json={"error": "sensitive_internal_code"})]
        )
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(unexpected),
        )
        try:
            with self.assertRaisesRegex(
                CentralClientError, "central_request_rejected"
            ):
                client.create_capture_request(command)
        finally:
            client.close()

        read = RecordingTransport(
            [httpx.Response(409, json={"error": "repository_capture_busy"})]
        )
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(read),
        )
        try:
            with self.assertRaisesRegex(
                CentralClientError, "central_request_rejected"
            ):
                client.get_capture_request(REQUEST_ID)
        finally:
            client.close()

    def test_plugin_create_sanitizes_busy_code_without_conflict_status(
        self,
    ) -> None:
        transport = RecordingTransport(
            [httpx.Response(422, json={"error": "repository_capture_busy"})]
        )
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(transport),
        )
        command = CaptureRequestCreate(
            repository_id=REPOSITORY_ID,
            template_id="business",
            capture_scope="current_session",
            client_action_id="codex_action_001",
        )
        try:
            with self.assertRaisesRegex(
                CentralClientError, "central_request_rejected"
            ):
                client.create_capture_request(command)
        finally:
            client.close()

    def test_client_never_serializes_local_source_values(self) -> None:
        batch = valid_upload_batch()
        transport = RecordingTransport(
            [
                httpx.Response(
                    200,
                    json={
                        "request_id": REQUEST_ID,
                        "batch_digest": batch.batch_digest,
                        "acknowledged_at": "2026-07-31T03:01:00Z",
                    },
                )
            ]
        )
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(transport),
        )
        try:
            receipt = client.upload_candidates(
                "lease_0123456789abcdef", batch
            )
        finally:
            client.close()

        self.assertEqual(batch.batch_digest, receipt.batch_digest)
        body = transport.requests[-1].content.decode("utf-8")
        for forbidden in ("session_id", "turn_id", "/Users/", "prompt", "diff"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, body)

    def test_only_transient_statuses_are_retried_with_bounded_delays(self) -> None:
        transient = RecordingTransport(
            [
                httpx.Response(503, json={"error": "unavailable"}),
                httpx.Response(429, json={"error": "busy"}),
                httpx.Response(204),
            ]
        )
        delays: list[float] = []
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(transient),
            sleeper=delays.append,
        )
        try:
            self.assertIsNone(client.claim_next())
        finally:
            client.close()

        self.assertEqual(3, len(transient.requests))
        self.assertEqual([0.1, 0.2], delays)

        permanent = RecordingTransport(
            [httpx.Response(400, json={"error": "invalid_request"})]
        )
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(permanent),
            sleeper=lambda _: self.fail("400 response must not retry"),
        )
        try:
            with self.assertRaisesRegex(
                CentralClientError, "central_request_rejected"
            ):
                client.claim_next()
        finally:
            client.close()
        self.assertEqual(1, len(permanent.requests))

    def test_connection_failure_retries_without_exposing_exception_text(self) -> None:
        transport = RecordingTransport(
            [
                httpx.ConnectError(
                    "contains-secret",
                    request=httpx.Request("POST", BASE_URL),
                ),
                httpx.Response(204),
            ]
        )
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(transport),
            sleeper=lambda _: None,
        )
        try:
            self.assertIsNone(client.claim_next())
        finally:
            client.close()
        self.assertEqual(2, len(transport.requests))

    def test_ambiguous_transport_failure_is_sanitized_without_retry(self) -> None:
        transport = RecordingTransport(
            [
                httpx.ReadTimeout(
                    "contains-secret",
                    request=httpx.Request("POST", BASE_URL),
                )
            ]
        )
        client = CentralClient(
            BASE_URL,
            DEVICE_TOKEN,
            transport=httpx.MockTransport(transport),
            sleeper=lambda _: self.fail(
                "ambiguous transport failure must not retry"
            ),
        )
        try:
            with self.assertRaisesRegex(
                CentralClientError, "central_connection_unavailable"
            ):
                client.claim_next()
        finally:
            client.close()

        self.assertEqual(1, len(transport.requests))


if __name__ == "__main__":
    unittest.main()
