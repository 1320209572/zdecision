from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from zdecision.central.registry_projection import (
    ProjectionState,
    RegistryProjectionState,
)
from zdecision.central.web.application import CentralWebApplication
from zdecision.recall.demo.publication import RecallDemoPublicationError
from tests import test_central_web_api


PRODUCT_ID = test_central_web_api.PRODUCT_ID


class _RecordingSynchronizer:
    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self.calls = calls

    def synchronize(
        self,
        organization_id: str,
        commit_sha: str,
        verified_at: str,
    ) -> RegistryProjectionState:
        self.calls.append(("registry", commit_sha))
        return _projection_state("available", commit_sha)


class _UnavailableSynchronizer(_RecordingSynchronizer):
    def synchronize(
        self,
        organization_id: str,
        commit_sha: str,
        verified_at: str,
    ) -> RegistryProjectionState:
        self.calls.append(("registry", commit_sha))
        return _projection_state("unavailable", None)


class _RecordingPublisher:
    def __init__(self, calls: list[tuple[str, str]]) -> None:
        self.calls = calls

    def refresh(self, publication_commit: str) -> None:
        self.calls.append(("recall-demo", publication_commit))


class _FailingPublisher:
    def __init__(self, pointer_path: Path) -> None:
        self.pointer_path = pointer_path

    def refresh(self, publication_commit: str) -> None:
        raise RecallDemoPublicationError("generation_conflict")


def _projection_state(
    state: ProjectionState, active_commit: str | None,
) -> RegistryProjectionState:
    return RegistryProjectionState(
        organization_id="org_demo",
        state=state,
        active_commit=active_commit,
        active_tree_oid="a" * 40 if active_commit is not None else None,
        desired_commit=active_commit,
        desired_tree_oid="a" * 40 if active_commit is not None else None,
        verified_at="2026-08-06T10:00:00Z",
        updated_at="2026-08-06T10:00:00Z",
        product_count=1 if active_commit is not None else None,
        decision_count=1 if active_commit is not None else None,
        projection_digest="b" * 64 if active_commit is not None else None,
        error_code=None if active_commit is not None else "registry_invalid",
    )


class RecallDemoPublicationBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.central = test_central_web_api.CentralWebApiTest(
            methodName="runTest"
        )
        self.central.setUp()
        self.addCleanup(self.central.doCleanups)

    def _configure_bridge(
        self,
        publisher: _RecordingPublisher | _FailingPublisher | None,
        synchronizer: _RecordingSynchronizer | None = None,
    ) -> None:
        current = self.central.web
        assert current.previews is not None
        self.central.web = CentralWebApplication(
            store=current.store,
            queries=current.queries,
            catalog=current.previews.catalog,
            git=current.previews.git,
            registry_synchronizer=synchronizer or self.central.synchronizer,
            recall_demo_publisher=publisher,
        )
        self.central.client.app.state.web_application = self.central.web

    def _publish(self, action: str) -> dict[str, object]:
        draft = self.central.client.put(
            f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
            json=self.central.draft_body(action="accept"),
        ).json()
        review = self.central.client.post(
            f"/api/v1/web/products/{PRODUCT_ID}/reviews",
            json={
                "client_action_id": f"web_action_{action}-review",
                "expected_draft_version": draft["version"],
                "items": draft["items"],
            },
        ).json()
        preview = self.central.client.post(
            f"/api/v1/web/reviews/{review['review_batch_id']}/previews",
            json={"client_action_id": f"web_action_{action}-preview"},
        ).json()
        response = self.central.client.post(
            f"/api/v1/web/publication-previews/{preview['preview_id']}/publish",
            json={"client_action_id": f"web_action_{action}-publish"},
        )
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def test_completed_publication_refreshes_after_registry_projection(self) -> None:
        """Moving refresh ahead of projection would expose an unprojected head."""
        calls: list[tuple[str, str]] = []
        publisher = _RecordingPublisher(calls)
        self._configure_bridge(publisher, _RecordingSynchronizer(calls))

        publication = self._publish("bridge-order")

        self.assertEqual("completed", publication["state"])
        self.assertEqual(calls, [
            ("registry", publication["commit_sha"]),
            ("recall-demo", publication["commit_sha"]),
        ])

    def test_pending_or_failed_publication_never_refreshes(self) -> None:
        """Treating a non-authoritative publication as completed would publish early."""
        calls: list[tuple[str, str]] = []
        self._configure_bridge(
            _RecordingPublisher(calls), _RecordingSynchronizer(calls)
        )

        for state in ("pending", "failed"):
            with self.subTest(state=state):
                self.central.web._synchronize_completed_publication(
                    SimpleNamespace(state=state, commit_sha="a" * 40)
                )

        self.assertEqual([], calls)

    def test_unavailable_projection_result_never_refreshes(self) -> None:
        """A returned unavailable projection must not select an unprojected Demo."""
        calls: list[tuple[str, str]] = []
        self._configure_bridge(
            _RecordingPublisher(calls), _UnavailableSynchronizer(calls)
        )

        publication = self._publish("bridge-unavailable-projection")

        self.assertEqual("completed", publication["state"])
        self.assertEqual([("registry", publication["commit_sha"])], calls)

    def test_unconfigured_central_behavior_is_unchanged(self) -> None:
        """An absent Demo config must retain the established publication result."""
        publication = self._publish("bridge-unconfigured")
        detail = self.central.client.get(
            f"/api/v1/web/publications/{publication['publication_id']}"
        )

        self.assertEqual(200, detail.status_code, detail.text)
        self.assertEqual(publication, detail.json())
        self.assertEqual("completed", publication["state"])

    def test_refresh_failure_returns_bounded_error_and_preserves_pointer(self) -> None:
        """Leaking publisher details or changing its pointer on failure is unsafe."""
        with tempfile.TemporaryDirectory() as directory:
            pointer_path = Path(directory) / "current.json"
            pointer_bytes = b'{"existing":"pointer"}\n'
            pointer_path.write_bytes(pointer_bytes)
            self._configure_bridge(_FailingPublisher(pointer_path))

            draft = self.central.client.put(
                f"/api/v1/web/products/{PRODUCT_ID}/review-draft",
                json=self.central.draft_body(action="accept"),
            ).json()
            review = self.central.client.post(
                f"/api/v1/web/products/{PRODUCT_ID}/reviews",
                json={
                    "client_action_id": "web_action_bridge-failure-review",
                    "expected_draft_version": draft["version"],
                    "items": draft["items"],
                },
            ).json()
            preview = self.central.client.post(
                f"/api/v1/web/reviews/{review['review_batch_id']}/previews",
                json={"client_action_id": "web_action_bridge-failure-preview"},
            ).json()
            response = self.central.client.post(
                f"/api/v1/web/publication-previews/{preview['preview_id']}/publish",
                json={"client_action_id": "web_action_bridge-failure-publish"},
            )

            self.assertEqual(503, response.status_code, response.text)
            self.assertEqual({"error": "recall_demo_refresh_failed"}, response.json())
            self.assertEqual(pointer_bytes, pointer_path.read_bytes())
            self.assertNotIn("generation_conflict", response.text)

    def test_retry_completed_publication_refreshes_without_second_publish_commit(
        self,
    ) -> None:
        """A completed retry must refresh the same Demo commit without republishing."""
        calls: list[tuple[str, str]] = []
        publisher = _RecordingPublisher(calls)
        self._configure_bridge(publisher, _RecordingSynchronizer(calls))
        assert self.central.web.previews is not None
        git = self.central.web.previews.git
        git.commit_exact = mock.Mock(wraps=git.commit_exact)

        publication = self._publish("bridge-retry")
        resumed = self.central.client.post(
            f"/api/v1/web/publications/{publication['publication_id']}/resume",
            json={"client_action_id": "web_action_bridge-retry-resume"},
        )

        self.assertEqual(200, resumed.status_code, resumed.text)
        self.assertEqual(publication, resumed.json())
        self.assertEqual(1, git.commit_exact.call_count)
        self.assertEqual(
            [("recall-demo", publication["commit_sha"])] * 2,
            [call for call in calls if call[0] == "recall-demo"],
        )


if __name__ == "__main__":
    unittest.main()
