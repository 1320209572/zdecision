from __future__ import annotations

import unittest
from types import SimpleNamespace


REPOSITORY_ID = "repo_11111111111111111111111111111111"
PRODUCT_ID = "prod_4d7b16e1616dd4cd1aeb2411836fd687"
REQUEST_ID = "crq_224319c1c20d7d5d2a7891662473a9d6"
FAMILY_ID = "cfm_fa3371772687d82189cc81956c95334d"
REVISION_ID = "crv_2f528d577d3a955b1004588bf5752538"
CONTENT_DIGEST = "81b931b380cf5f476d9c149240024884144f1909d70e84b21a366d1ab2199fed"
BATCH_DIGEST = "411f17dc8ba2e0dfd9a11259681bb6a0754f82ee7f27b9ab8bf59dd4a7feda30"
EMPTY_BATCH_DIGEST = (
    "e813d564bccbeefe1db875d1c9abb55d63c52b639acc61134a5f1d19cc489b67"
)
EVIDENCE_DIGEST = "e" * 64


def _content_dict() -> dict[str, object]:
    return {
        "product": "ZDecision",
        "claim": "正式决策按产品隔离保存。",
        "future_action": "新增决策时写入对应产品目录。",
        "scope_summary": "ZDecision Registry 的正式存储布局",
        "repositories": [REPOSITORY_ID],
        "paths": ["decision-registry/"],
        "invalidation_conditions": ["产品隔离策略被新的正式决策替代"],
    }


def _revision_dict() -> dict[str, object]:
    return {
        "family_id": FAMILY_ID,
        "revision_id": REVISION_ID,
        "revision": 1,
        "content": _content_dict(),
        "content_digest": CONTENT_DIGEST,
        "evidence_digest": EVIDENCE_DIGEST,
    }


def _batch_dict() -> dict[str, object]:
    return {
        "request_id": REQUEST_ID,
        "repository_id": REPOSITORY_ID,
        "items": [_revision_dict()],
        "batch_digest": BATCH_DIGEST,
    }


class SyncContractsTest(unittest.TestCase):
    def sync_api(self) -> SimpleNamespace:
        try:
            from zdecision.ids import (
                candidate_family_id,
                candidate_revision_id,
                capture_request_id,
            )
            from zdecision.sync.contracts import (
                CandidateBatchUpload,
                CandidateRevisionUpload,
                CaptureRequestCreate,
                CaptureRequestView,
                ClaimedCaptureRequest,
                ProgressEvent,
                RepositoryView,
                UploadReceipt,
                EnabledRepository,
                RepositoryCatalogView,
            )
        except (ImportError, ModuleNotFoundError) as error:
            self.fail(f"On-demand sync contract API is missing: {error}")

        return SimpleNamespace(
            CandidateBatchUpload=CandidateBatchUpload,
            CandidateRevisionUpload=CandidateRevisionUpload,
            CaptureRequestCreate=CaptureRequestCreate,
            CaptureRequestView=CaptureRequestView,
            ClaimedCaptureRequest=ClaimedCaptureRequest,
            ProgressEvent=ProgressEvent,
            RepositoryView=RepositoryView,
            UploadReceipt=UploadReceipt,
            EnabledRepository=EnabledRepository,
            RepositoryCatalogView=RepositoryCatalogView,
            candidate_family_id=candidate_family_id,
            candidate_revision_id=candidate_revision_id,
            capture_request_id=capture_request_id,
        )

    def test_neutral_repository_contract_round_trips_without_product_ownership(self) -> None:
        api = self.sync_api()
        repository = api.EnabledRepository.from_dict(
            {"repository_id": REPOSITORY_ID, "enabled": True}
        )

        self.assertEqual(
            {"repository_id": REPOSITORY_ID, "enabled": True},
            repository.to_dict(),
        )
        with self.assertRaisesRegex(ValueError, "EnabledRepository fields are invalid"):
            api.EnabledRepository.from_dict(
                {"repository_id": REPOSITORY_ID, "enabled": True, "product_id": PRODUCT_ID}
            )

    def test_stable_ids_match_hand_checked_canonical_fixtures(self) -> None:
        """Catch random IDs or hashes that omit an identity input."""
        api = self.sync_api()

        request_id = api.capture_request_id(
            "org_demo", REPOSITORY_ID, "business", "web_action_001"
        )
        family_id = api.candidate_family_id(
            REPOSITORY_ID, "cand_" + "2" * 32 + "_01"
        )

        self.assertEqual(REQUEST_ID, request_id)
        self.assertEqual(FAMILY_ID, family_id)
        self.assertEqual(
            "crv_0b108e986aebe4ec83d1d2be7924ed60",
            api.candidate_revision_id(FAMILY_ID, 1, "3" * 64),
        )
        self.assertNotEqual(
            request_id,
            api.capture_request_id(
                "org_demo", REPOSITORY_ID, "business", "web_action_002"
            ),
        )

    def test_stable_ids_reject_malformed_native_values(self) -> None:
        """Catch acceptance of IDs that cannot belong to this protocol."""
        api = self.sync_api()

        invalid_calls = (
            lambda: api.capture_request_id(
                "", REPOSITORY_ID, "business", "web_action_001"
            ),
            lambda: api.capture_request_id(
                "org_demo", "repo_bad", "business", "web_action_001"
            ),
            lambda: api.candidate_family_id(REPOSITORY_ID, "candidate_bad"),
            lambda: api.candidate_revision_id("cfm_bad", 1, "3" * 64),
            lambda: api.candidate_revision_id(FAMILY_ID, 0, "3" * 64),
            lambda: api.candidate_revision_id(FAMILY_ID, 1, "digest_bad"),
        )
        for call in invalid_calls:
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_browser_request_rejects_identity_and_source_fields(self) -> None:
        """Catch browser-controlled identity or native source selection."""
        api = self.sync_api()
        payload = {
            "repository_id": REPOSITORY_ID,
            "template_id": "business",
            "capture_scope": "current_session",
            "client_action_id": "web_action_001",
            "organization_id": "org_forbidden",
            "session_id": "session_forbidden",
        }

        with self.assertRaisesRegex(ValueError, "CaptureRequestCreate fields"):
            api.CaptureRequestCreate.from_dict(payload)

    def test_request_and_progress_values_round_trip_exact_fields(self) -> None:
        """Catch lost lifecycle fields or permissive transport serialization."""
        api = self.sync_api()
        values = (
            api.RepositoryView.from_dict(
                {
                    "repository_id": REPOSITORY_ID,
                    "product_id": PRODUCT_ID,
                    "product_name": "ZDecision",
                    "enabled": True,
                }
            ),
            api.CaptureRequestCreate.from_dict(
                {
                    "repository_id": REPOSITORY_ID,
                    "template_id": "business",
                    "capture_scope": "current_session",
                    "client_action_id": "web_action_001",
                }
            ),
            api.CaptureRequestView.from_dict(
                {
                    "request_id": REQUEST_ID,
                    "repository_id": REPOSITORY_ID,
                    "product_id": PRODUCT_ID,
                    "product_name": "ZDecision",
                    "template_id": "business",
                    "state": "queued",
                    "progress_code": "request_queued",
                    "candidate_revision_count": None,
                    "last_sequence": 1,
                    "created_at": "2026-07-31T00:00:00Z",
                    "updated_at": "2026-07-31T00:00:00Z",
                }
            ),
            api.ClaimedCaptureRequest.from_dict(
                {
                    "request_id": REQUEST_ID,
                    "repository_id": REPOSITORY_ID,
                    "product_id": PRODUCT_ID,
                    "product_name": "ZDecision",
                    "template_id": "business",
                    "capture_scope": "current_session",
                    "client_action_id": "web_action_001",
                    "lease_token": "lease_0123456789abcdef",
                    "lease_expires_at": "2026-07-31T00:00:30Z",
                }
            ),
            api.ProgressEvent.from_dict(
                {
                    "request_id": REQUEST_ID,
                    "sequence": 2,
                    "state": "claimed",
                    "code": "device_claimed",
                    "occurred_at": "2026-07-31T00:00:01Z",
                }
            ),
            api.UploadReceipt.from_dict(
                {
                    "request_id": REQUEST_ID,
                    "batch_digest": BATCH_DIGEST,
                    "acknowledged_at": "2026-07-31T00:01:00Z",
                }
            ),
        )

        for value in values:
            with self.subTest(value=type(value).__name__):
                self.assertEqual(
                    value,
                    type(value).from_dict(value.to_dict()),
                )

    def test_capture_scope_is_closed_and_required(self) -> None:
        """Catch broad, missing, or silently defaulted source selection."""
        api = self.sync_api()
        command = api.CaptureRequestCreate.from_dict(
            {
                "repository_id": REPOSITORY_ID,
                "template_id": "business",
                "capture_scope": "current_session",
                "client_action_id": "codex_action_001",
            }
        )

        self.assertEqual("current_session", command.capture_scope)
        with self.assertRaisesRegex(ValueError, "capture_scope is invalid"):
            api.CaptureRequestCreate.from_dict(
                {
                    "repository_id": REPOSITORY_ID,
                    "template_id": "business",
                    "capture_scope": "recent_session",
                    "client_action_id": "codex_action_001",
                }
            )

    def test_candidate_batch_round_trips_only_validated_content(self) -> None:
        """Catch digest drift or serialization that changes a Candidate revision."""
        api = self.sync_api()

        batch = api.CandidateBatchUpload.from_dict(_batch_dict())

        self.assertEqual(_batch_dict(), batch.to_dict())
        self.assertEqual(
            batch,
            api.CandidateBatchUpload.from_dict(batch.to_dict()),
        )

    def test_candidate_upload_rejects_native_or_unknown_fields(self) -> None:
        """Catch Session, Turn, or arbitrary payload data crossing the boundary."""
        api = self.sync_api()
        cases = []
        root_extra = _batch_dict()
        root_extra["session_id"] = "session_forbidden"
        cases.append(root_extra)
        item_extra = _batch_dict()
        item_extra["items"][0]["turn_id"] = "turn_forbidden"
        cases.append(item_extra)
        content_extra = _batch_dict()
        content_extra["items"][0]["content"]["prompt"] = "raw prompt"
        cases.append(content_extra)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    api.CandidateBatchUpload.from_dict(payload)

    def test_candidate_upload_rejects_tampered_digests_and_oversize_items(
        self,
    ) -> None:
        """Catch batches whose claimed identity differs from their exact bytes."""
        api = self.sync_api()
        bad_content = _batch_dict()
        bad_content["items"][0]["content_digest"] = "0" * 64
        bad_revision = _batch_dict()
        bad_revision["items"][0]["revision_id"] = "crv_" + "0" * 32
        bad_batch = _batch_dict()
        bad_batch["batch_digest"] = "0" * 64
        oversize = _batch_dict()
        oversize["items"][0]["content"]["claim"] = "x" * (16 * 1024)

        for payload in (bad_content, bad_revision, bad_batch, oversize):
            with self.subTest(kind=payload):
                with self.assertRaises(ValueError):
                    api.CandidateBatchUpload.from_dict(payload)

    def test_empty_candidate_batch_has_one_exact_digest(self) -> None:
        """Catch fabricated zero-result acknowledgements with arbitrary digests."""
        api = self.sync_api()
        payload = {
            "request_id": REQUEST_ID,
            "repository_id": REPOSITORY_ID,
            "items": [],
            "batch_digest": EMPTY_BATCH_DIGEST,
        }

        self.assertEqual(
            payload,
            api.CandidateBatchUpload.from_dict(payload).to_dict(),
        )
        payload["batch_digest"] = "0" * 64
        with self.assertRaises(ValueError):
            api.CandidateBatchUpload.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
