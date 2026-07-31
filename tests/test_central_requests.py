from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zdecision.sync.contracts import CaptureRequestCreate, RepositoryView

try:
    from zdecision.central.auth import (
        DemoIdentityProvider,
        InvalidCredentials,
        Principal,
    )
    from zdecision.central.service import (
        AccessDenied,
        CaptureRequestService,
        InvalidLease,
        InvalidTransition,
        RepositoryUnavailable,
    )
    from zdecision.central.store import CentralStore
except ModuleNotFoundError as error:
    CENTRAL_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    CENTRAL_IMPORT_ERROR = None


REPOSITORY_ID = "repo_" + "1" * 32
PRODUCT_ID = "prod_4d7b16e1616dd4cd1aeb2411836fd687"
NOW = datetime(2026, 7, 31, 1, 0, tzinfo=UTC)
EMPTY_BATCH_DIGEST = (
    "e813d564bccbeefe1db875d1c9abb55d63c52b639acc61134a5f1d19cc489b67"
)
USER = None if CENTRAL_IMPORT_ERROR else Principal("user", "org_demo", "user_demo", None)
OTHER_USER = (
    None if CENTRAL_IMPORT_ERROR else Principal("user", "org_other", "user_other", None)
)
DEVICE = (
    None
    if CENTRAL_IMPORT_ERROR
    else Principal("device", "org_demo", "device_demo", "device_demo")
)
OTHER_DEVICE = (
    None
    if CENTRAL_IMPORT_ERROR
    else Principal("device", "org_demo", "device_other", "device_other")
)


class CentralRequestServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            CENTRAL_IMPORT_ERROR,
            f"zdecision.central is missing: {CENTRAL_IMPORT_ERROR}",
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
        self.service = CaptureRequestService(self.store)

    def tearDown(self) -> None:
        if hasattr(self, "store"):
            self.store.close()
        if hasattr(self, "temporary_directory"):
            self.temporary_directory.cleanup()

    def create(
        self,
        action_id: str = "web_action_001",
        *,
        now: datetime = NOW,
    ):
        return self.service.create_request(
            USER,
            CaptureRequestCreate(
                repository_id=REPOSITORY_ID,
                template_id="business",
                client_action_id=action_id,
            ),
            now,
        )

    def claim(self, now: datetime = NOW):
        claimed = self.service.claim_next(DEVICE, now, lease_seconds=30)
        self.assertIsNotNone(claimed)
        return claimed

    def test_demo_identity_provider_never_accepts_body_identity(self) -> None:
        raw_token = "device-secret-token"
        provider = DemoIdentityProvider(
            organization_id="org_demo",
            user_id="user_demo",
            device_id="device_demo",
            device_token_sha256=hashlib.sha256(raw_token.encode()).hexdigest(),
        )

        self.assertEqual(USER, provider.browser_principal())
        self.assertEqual(
            DEVICE,
            provider.authenticate_device(f"Bearer {raw_token}"),
        )
        for authorization in (None, raw_token, "Bearer wrong-token"):
            with self.subTest(authorization=authorization):
                with self.assertRaises(InvalidCredentials):
                    provider.authenticate_device(authorization)

    def test_server_derives_identity_and_product_from_mapping(self) -> None:
        created = self.create()
        stored = self.store.get_request_record(created.request_id)

        self.assertEqual(
            ("org_demo", "user_demo", PRODUCT_ID),
            (stored.organization_id, stored.actor_id, stored.product_id),
        )
        self.assertEqual("queued", created.state)
        self.assertEqual(1, created.last_sequence)
        self.assertEqual(
            [REPOSITORY_ID],
            [
                item.repository_id
                for item in self.service.list_repositories(USER)
            ],
        )

    def test_disabled_or_cross_organization_repository_is_rejected(self) -> None:
        self.store.put_repository_mapping(
            "org_demo",
            RepositoryView(
                repository_id=REPOSITORY_ID,
                product_id=PRODUCT_ID,
                product_name="ZDecision",
                enabled=False,
            ),
        )
        with self.assertRaises(RepositoryUnavailable):
            self.create()
        with self.assertRaises(RepositoryUnavailable):
            self.service.create_request(
                OTHER_USER,
                CaptureRequestCreate(
                    repository_id=REPOSITORY_ID,
                    template_id="business",
                    client_action_id="web_action_other_org",
                ),
                NOW,
            )

    def test_retry_and_second_active_click_return_one_request(self) -> None:
        first = self.create("web_action_001")
        same_action = self.create("web_action_001")
        second_action = self.create("web_action_002")

        self.assertEqual(first.request_id, same_action.request_id)
        self.assertEqual(first.request_id, second_action.request_id)

        claimed = self.claim()
        self.service.start(DEVICE, first.request_id, claimed.lease_token, NOW)
        self.service.complete(
            DEVICE,
            first.request_id,
            claimed.lease_token,
            EMPTY_BATCH_DIGEST,
            NOW,
        )
        self.assertEqual(
            first.request_id,
            self.create("web_action_002", now=NOW + timedelta(seconds=1)).request_id,
        )

    def test_expired_claim_requeues_and_survives_restart(self) -> None:
        created = self.create()
        claimed = self.claim()
        self.store.close()
        self.store = CentralStore.open(self.database_path)
        self.service = CaptureRequestService(self.store)

        reclaimed = self.service.claim_next(
            DEVICE,
            NOW + timedelta(seconds=31),
            lease_seconds=30,
        )

        self.assertIsNotNone(reclaimed)
        self.assertEqual(created.request_id, reclaimed.request_id)
        self.assertNotEqual(claimed.lease_token, reclaimed.lease_token)
        with self.assertRaises(InvalidLease):
            self.service.start(
                DEVICE,
                created.request_id,
                claimed.lease_token,
                NOW + timedelta(seconds=31),
            )

    def test_event_cursor_is_monotonic_and_reconnectable(self) -> None:
        created = self.create()
        claimed = self.claim()
        self.service.start(DEVICE, created.request_id, claimed.lease_token, NOW)

        events = self.service.events_after(
            USER, created.request_id, after_sequence=2
        )

        self.assertEqual([3], [event.sequence for event in events])
        self.assertEqual(["capture_started"], [event.code for event in events])
        self.assertEqual(
            [1, 2, 3],
            [
                event.sequence
                for event in self.service.events_after(
                    USER, created.request_id, after_sequence=0
                )
            ],
        )

    def test_retry_backoff_and_fifth_failure_are_bounded(self) -> None:
        created = self.create("web_action_retry")
        retry_times = (
            NOW,
            NOW + timedelta(seconds=5),
            NOW + timedelta(seconds=35),
            NOW + timedelta(seconds=155),
            NOW + timedelta(seconds=455),
        )

        for attempt, attempt_time in enumerate(retry_times):
            if attempt:
                self.assertIsNone(
                    self.service.claim_next(
                        DEVICE,
                        attempt_time - timedelta(milliseconds=1),
                        lease_seconds=30,
                    )
                )
            claimed = self.service.claim_next(
                DEVICE, attempt_time, lease_seconds=30
            )
            self.assertIsNotNone(claimed)
            self.service.fail(
                DEVICE,
                created.request_id,
                claimed.lease_token,
                "temporary_transport_failure",
                True,
                attempt_time,
            )

        view = self.service.get_request(USER, created.request_id)
        record = self.store.get_request_record(created.request_id)
        self.assertEqual("failed_terminal", view.state)
        self.assertEqual("retry_exhausted", record.terminal_code)
        self.assertIsNone(
            self.service.claim_next(
                DEVICE, retry_times[-1] + timedelta(hours=1), lease_seconds=30
            )
        )

    def test_device_mutations_require_current_device_and_unexpired_lease(self) -> None:
        created = self.create()
        claimed = self.claim()

        with self.assertRaises(AccessDenied):
            self.service.start(
                OTHER_DEVICE,
                created.request_id,
                claimed.lease_token,
                NOW,
            )
        with self.assertRaises(InvalidLease):
            self.service.start(
                DEVICE,
                created.request_id,
                claimed.lease_token,
                NOW + timedelta(seconds=30),
            )
        with self.assertRaises(AccessDenied):
            self.service.get_request(OTHER_USER, created.request_id)

    def test_progress_heartbeat_and_completion_are_strict_and_idempotent(self) -> None:
        created = self.create()
        claimed = self.claim()
        self.service.start(DEVICE, created.request_id, claimed.lease_token, NOW)
        heartbeat = self.service.heartbeat(
            DEVICE,
            created.request_id,
            claimed.lease_token,
            NOW + timedelta(seconds=10),
            lease_seconds=60,
        )
        event = self.service.record_progress(
            DEVICE,
            created.request_id,
            claimed.lease_token,
            "extracting_candidates",
            NOW + timedelta(seconds=11),
        )
        completed = self.service.complete(
            DEVICE,
            created.request_id,
            claimed.lease_token,
            EMPTY_BATCH_DIGEST,
            NOW + timedelta(seconds=12),
        )
        replay = self.service.complete(
            DEVICE,
            created.request_id,
            claimed.lease_token,
            EMPTY_BATCH_DIGEST,
            NOW + timedelta(seconds=13),
        )

        self.assertEqual("2026-07-31T01:01:10Z", heartbeat.lease_expires_at)
        self.assertEqual((4, "running"), (event.sequence, event.state))
        self.assertEqual("succeeded_no_candidates", completed.state)
        self.assertEqual(completed, replay)
        with self.assertRaises(InvalidTransition):
            self.service.record_progress(
                DEVICE,
                created.request_id,
                claimed.lease_token,
                "too_late",
                NOW + timedelta(seconds=14),
            )

    def test_raw_lease_token_is_never_persisted(self) -> None:
        created = self.create()
        claimed = self.claim()
        stored = self.store.get_request_record(created.request_id)

        self.assertNotEqual(claimed.lease_token, stored.lease_token_digest)
        self.assertEqual(
            hashlib.sha256(claimed.lease_token.encode()).hexdigest(),
            stored.lease_token_digest,
        )


if __name__ == "__main__":
    unittest.main()
