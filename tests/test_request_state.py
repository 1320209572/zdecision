from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from zdecision.app_server.jsonl import (
    AppServerRequestError,
    AppServerTimeout,
)
from zdecision.app_server.models import AppServerTurnReceipt


REQUEST_ID = "crq_11111111111111111111111111111111"
OPERATION_KEY = "src_22222222222222222222222222222222"
PROFILE_ID = "fmp_33333333333333333333333333333333"


class RequestStateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.path = Path(self.temporary_directory.name) / "agent.sqlite3"

    def _api(self):
        try:
            from zdecision.agent.request_state import (
                CaptureResultUnknown,
                NativeAttemptConflict,
                NativeCallCoordinator,
                RequestStateStore,
            )
        except ModuleNotFoundError as error:
            self.fail(f"Request state API is missing: {error}")
        return (
            CaptureResultUnknown,
            NativeAttemptConflict,
            NativeCallCoordinator,
            RequestStateStore,
        )

    def test_native_attempt_transitions_are_durable_and_idempotent(self) -> None:
        _, _, _, RequestStateStore = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)

        prepared = store.get_or_create_native_attempt(
            REQUEST_ID,
            OPERATION_KEY,
            "capture_fork",
            "zdecision/capture/cap_1",
        )
        pending = store.mark_native_pending(
            REQUEST_ID, OPERATION_KEY, "capture_fork"
        )
        attached = store.attach_native_result(
            REQUEST_ID,
            OPERATION_KEY,
            "capture_fork",
            "thread-fork",
        )
        completed = store.complete_native_attempt(
            REQUEST_ID,
            OPERATION_KEY,
            "capture_fork",
            "a" * 64,
        )

        self.assertEqual("prepared", prepared.state)
        self.assertEqual("pending", pending.state)
        self.assertEqual("attached", attached.state)
        self.assertEqual("completed", completed.state)
        self.assertEqual("thread-fork", completed.native_id)
        self.assertEqual(
            completed,
            store.complete_native_attempt(
                REQUEST_ID,
                OPERATION_KEY,
                "capture_fork",
                "a" * 64,
            ),
        )
        store.close()
        reopened = RequestStateStore.open(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(
            completed,
            reopened.get_native_attempt(
                REQUEST_ID, OPERATION_KEY, "capture_fork"
            ),
        )

    def test_attempt_identity_or_result_conflicts_are_rejected(self) -> None:
        _, NativeAttemptConflict, _, RequestStateStore = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)
        store.get_or_create_native_attempt(
            REQUEST_ID,
            OPERATION_KEY,
            "inventory",
            "zdecision/cap_1/inventory",
        )

        with self.assertRaises(NativeAttemptConflict):
            store.get_or_create_native_attempt(
                REQUEST_ID,
                OPERATION_KEY,
                "inventory",
                "zdecision/cap_1/changed",
            )

        store.mark_native_pending(REQUEST_ID, OPERATION_KEY, "inventory")
        store.attach_native_result(
            REQUEST_ID, OPERATION_KEY, "inventory", "turn-1"
        )
        with self.assertRaises(NativeAttemptConflict):
            store.attach_native_result(
                REQUEST_ID, OPERATION_KEY, "inventory", "turn-2"
            )

    def test_unknown_thread_result_is_adopted_without_duplicate_creation(
        self,
    ) -> None:
        (
            CaptureResultUnknown,
            _,
            NativeCallCoordinator,
            RequestStateStore,
        ) = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)
        coordinator = NativeCallCoordinator(store)
        created = 0
        visible: dict[str, str] = {}

        def create() -> str:
            nonlocal created
            created += 1
            visible["zdecision/capture/cap_1"] = "thread-fork"
            raise AppServerTimeout("transport result unknown")

        with self.assertRaises(CaptureResultUnknown):
            coordinator.resolve_thread(
                request_id=REQUEST_ID,
                operation_key=OPERATION_KEY,
                stage="capture_fork",
                stable_tag="zdecision/capture/cap_1",
                find=visible.get,
                create=create,
            )
        self.assertEqual("pending", store.get_native_attempt(
            REQUEST_ID, OPERATION_KEY, "capture_fork"
        ).state)

        adopted = coordinator.resolve_thread(
            request_id=REQUEST_ID,
            operation_key=OPERATION_KEY,
            stage="capture_fork",
            stable_tag="zdecision/capture/cap_1",
            find=visible.get,
            create=create,
        )

        self.assertEqual("thread-fork", adopted)
        self.assertEqual(1, created)
        self.assertEqual("completed", store.get_native_attempt(
            REQUEST_ID, OPERATION_KEY, "capture_fork"
        ).state)

    def test_pending_without_visible_result_never_starts_replacement(self) -> None:
        (
            CaptureResultUnknown,
            _,
            NativeCallCoordinator,
            RequestStateStore,
        ) = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)
        store.get_or_create_native_attempt(
            REQUEST_ID,
            OPERATION_KEY,
            "capture_fork",
            "zdecision/capture/cap_1",
        )
        store.mark_native_pending(REQUEST_ID, OPERATION_KEY, "capture_fork")
        create_calls = 0

        def create() -> str:
            nonlocal create_calls
            create_calls += 1
            return "replacement-forbidden"

        with self.assertRaises(CaptureResultUnknown):
            NativeCallCoordinator(store).resolve_thread(
                request_id=REQUEST_ID,
                operation_key=OPERATION_KEY,
                stage="capture_fork",
                stable_tag="zdecision/capture/cap_1",
                find=lambda _: None,
                create=create,
            )

        self.assertEqual(0, create_calls)

    def test_explicit_native_rejection_resets_the_attempt(self) -> None:
        _, _, NativeCallCoordinator, RequestStateStore = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)

        with self.assertRaises(AppServerRequestError):
            NativeCallCoordinator(store).resolve_thread(
                request_id=REQUEST_ID,
                operation_key=OPERATION_KEY,
                stage="capture_fork",
                stable_tag="zdecision/capture/cap_1",
                find=lambda _: None,
                create=lambda: (_ for _ in ()).throw(
                    AppServerRequestError("thread/fork", -32602)
                ),
            )

        self.assertEqual(
            "prepared",
            store.get_native_attempt(
                REQUEST_ID, OPERATION_KEY, "capture_fork"
            ).state,
        )

    def test_structured_turn_unknown_result_is_read_back_by_client_id(
        self,
    ) -> None:
        (
            CaptureResultUnknown,
            _,
            NativeCallCoordinator,
            RequestStateStore,
        ) = self._api()
        store = RequestStateStore.open(self.path)
        self.addCleanup(store.close)
        coordinator = NativeCallCoordinator(store)
        visible: dict[str, AppServerTurnReceipt] = {}
        create_calls = 0
        receipt = AppServerTurnReceipt.create(
            thread_id="thread-fork",
            turn_id="turn-inventory",
            structured_output={"signals": []},
            model_profile_id=PROFILE_ID,
        )

        def create() -> AppServerTurnReceipt:
            nonlocal create_calls
            create_calls += 1
            visible["zdecision/cap_1/inventory"] = receipt
            raise AppServerTimeout("transport result unknown")

        with self.assertRaises(CaptureResultUnknown):
            coordinator.resolve_structured_turn(
                request_id=REQUEST_ID,
                operation_key=OPERATION_KEY,
                stage="inventory",
                stable_tag="zdecision/cap_1/inventory",
                read=visible.get,
                create=create,
            )

        adopted = coordinator.resolve_structured_turn(
            request_id=REQUEST_ID,
            operation_key=OPERATION_KEY,
            stage="inventory",
            stable_tag="zdecision/cap_1/inventory",
            read=visible.get,
            create=create,
        )

        self.assertEqual(receipt, adopted)
        self.assertEqual(1, create_calls)
        attempt = store.get_native_attempt(
            REQUEST_ID, OPERATION_KEY, "inventory"
        )
        self.assertEqual(receipt.turn_id, attempt.native_id)
        self.assertEqual(receipt.output_sha256, attempt.output_digest)


if __name__ == "__main__":
    unittest.main()
