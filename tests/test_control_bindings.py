from __future__ import annotations

import tempfile
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

try:
    from zdecision.agent.control_bindings import (
        ControlBindingExpired,
        ControlBindingNotFound,
        ControlBindingStore,
        ControlRepositoryMismatch,
        ControlRequestConflict,
        ControlScopeConflict,
    )
except ModuleNotFoundError as error:
    CONTROL_BINDING_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    CONTROL_BINDING_IMPORT_ERROR = None


NOW = datetime(2026, 7, 31, 3, 0, tzinfo=UTC)
CONTROL_ID = "ctl_" + "1" * 32
REPOSITORY_ID = "repo_" + "2" * 32
PRODUCT_ID = "prod_" + "3" * 32
ACTION_ID = "codex_action_first"
REQUEST_ID = "crq_" + "4" * 32


class ControlBindingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            CONTROL_BINDING_IMPORT_ERROR,
            f"Control Binding store is missing: {CONTROL_BINDING_IMPORT_ERROR}",
        )
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.path = self.root / "agent" / "zdecision.sqlite3"
        self.store = ControlBindingStore.open(self.path)
        self.addCleanup(self.store.close)

    def create_binding(self, *, control_id: str = CONTROL_ID):
        return self.store.create_binding(
            session_id="session-local-private",
            render_turn_id="turn-local-private",
            cwd="/Users/private/worktree",
            repository_id=REPOSITORY_ID,
            product_id=PRODUCT_ID,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=15),
            control_id=control_id,
        )

    def test_scope_choice_is_durable_idempotent_and_conflict_checked(self) -> None:
        control = self.create_binding()

        self.assertEqual("ready", control.submission_state)

        first = self.store.choose_scope(
            control.control_id,
            expected_repository_id=REPOSITORY_ID,
            scope="current_session",
            proposed_client_action_id=ACTION_ID,
            now=NOW,
        )
        replay = self.store.choose_scope(
            control.control_id,
            expected_repository_id=REPOSITORY_ID,
            scope="current_session",
            proposed_client_action_id="codex_action_ignored",
            now=NOW + timedelta(minutes=30),
        )

        self.assertEqual(ACTION_ID, first.client_action_id)
        self.assertEqual("pending", first.submission_state)
        self.assertEqual(ACTION_ID, replay.client_action_id)
        with self.assertRaises(ControlScopeConflict):
            self.store.choose_scope(
                control.control_id,
                expected_repository_id=REPOSITORY_ID,
                scope="all_valid_sessions",
                proposed_client_action_id="codex_action_second",
                now=NOW,
            )

    def test_choice_and_request_attachment_survive_restart(self) -> None:
        self.create_binding()
        self.store.choose_scope(
            CONTROL_ID,
            expected_repository_id=REPOSITORY_ID,
            scope="all_valid_sessions",
            proposed_client_action_id=ACTION_ID,
            now=NOW,
        )
        attached = self.store.attach_request(
            CONTROL_ID,
            client_action_id=ACTION_ID,
            central_request_id=REQUEST_ID,
        )
        self.store.close()
        self.store = ControlBindingStore.open(self.path)

        self.assertEqual(attached, self.store.get_by_client_action_id(ACTION_ID))
        self.assertEqual("attached", attached.submission_state)
        self.assertEqual(REQUEST_ID, self.store.get(CONTROL_ID).central_request_id)
        self.assertEqual(attached, self.store.get(CONTROL_ID))

    def test_terminal_submission_dispositions_are_durable_and_not_reopened(
        self,
    ) -> None:
        cases = (
            ("busy", "ctl_" + "5" * 32, "codex_action_busy"),
            ("rejected", "ctl_" + "6" * 32, "codex_action_rejected"),
        )

        for disposition, control_id, action_id in cases:
            with self.subTest(disposition=disposition):
                self.create_binding(control_id=control_id)
                chosen = self.store.choose_scope(
                    control_id,
                    expected_repository_id=REPOSITORY_ID,
                    scope="current_session",
                    proposed_client_action_id=action_id,
                    now=NOW,
                )
                terminal = self.store.finish_submission(
                    control_id,
                    client_action_id=chosen.client_action_id,
                    disposition=disposition,
                )
                replay = self.store.choose_scope(
                    control_id,
                    expected_repository_id=REPOSITORY_ID,
                    scope="current_session",
                    proposed_client_action_id="codex_action_ignored",
                    now=NOW + timedelta(days=1),
                )

                self.assertEqual(disposition, terminal.submission_state)
                self.assertEqual(disposition, replay.submission_state)
                with self.assertRaises(ControlRequestConflict):
                    self.store.attach_request(
                        control_id,
                        client_action_id=action_id,
                        central_request_id="crq_" + "7" * 32,
                    )

        self.store.close()
        self.store = ControlBindingStore.open(self.path)
        self.assertEqual(
            "busy", self.store.get("ctl_" + "5" * 32).submission_state
        )
        self.assertEqual(
            "rejected", self.store.get("ctl_" + "6" * 32).submission_state
        )

    def test_legacy_selected_rows_fail_closed_while_attached_rows_migrate(
        self,
    ) -> None:
        legacy_path = self.root / "legacy" / "zdecision.sqlite3"
        legacy_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(legacy_path)
        connection.executescript(
            """
            CREATE TABLE control_bindings (
                control_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                render_turn_id TEXT NOT NULL,
                cwd TEXT NOT NULL,
                repository_id TEXT NOT NULL,
                product_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                chosen_scope TEXT,
                client_action_id TEXT UNIQUE,
                central_request_id TEXT UNIQUE
            );
            """
        )
        rows = (
            (
                "ctl_" + "7" * 32,
                "current_session",
                "codex_action_legacy_unknown",
                None,
            ),
            (
                "ctl_" + "8" * 32,
                "all_valid_sessions",
                "codex_action_legacy_attached",
                "crq_" + "9" * 32,
            ),
        )
        connection.executemany(
            """
            INSERT INTO control_bindings(
                control_id, session_id, render_turn_id, cwd,
                repository_id, product_id, created_at, expires_at,
                chosen_scope, client_action_id, central_request_id
            ) VALUES (?, 'session', 'turn', '/private/repository', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    control_id,
                    REPOSITORY_ID,
                    PRODUCT_ID,
                    "2026-07-31T03:00:00.000000Z",
                    "2026-07-31T03:15:00.000000Z",
                    scope,
                    action_id,
                    request_id,
                )
                for control_id, scope, action_id, request_id in rows
            ),
        )
        connection.commit()
        connection.close()

        migrated = ControlBindingStore.open(legacy_path)
        self.addCleanup(migrated.close)

        self.assertEqual(
            "legacy_unknown",
            migrated.get("ctl_" + "7" * 32).submission_state,
        )
        self.assertEqual(
            "attached", migrated.get("ctl_" + "8" * 32).submission_state
        )

    def test_concurrent_double_clicks_share_the_first_persisted_action(self) -> None:
        self.create_binding()

        def choose(action_id):
            store = ControlBindingStore.open(self.path)
            try:
                return store.choose_scope(
                    CONTROL_ID,
                    expected_repository_id=REPOSITORY_ID,
                    scope="current_session",
                    proposed_client_action_id=action_id,
                    now=NOW,
                )
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = (
                executor.submit(choose, "codex_action_click_one"),
                executor.submit(choose, "codex_action_click_two"),
            )
            results = [future.result(timeout=5) for future in futures]

        self.assertEqual(results[0].client_action_id, results[1].client_action_id)
        self.assertIn(
            results[0].client_action_id,
            {"codex_action_click_one", "codex_action_click_two"},
        )

    def test_only_unused_controls_expire(self) -> None:
        self.create_binding()
        with self.assertRaises(ControlBindingExpired):
            self.store.choose_scope(
                CONTROL_ID,
                expected_repository_id=REPOSITORY_ID,
                scope="current_session",
                proposed_client_action_id=ACTION_ID,
                now=NOW + timedelta(minutes=15),
            )

        selected_id = "ctl_" + "5" * 32
        self.create_binding(control_id=selected_id)
        self.store.choose_scope(
            selected_id,
            expected_repository_id=REPOSITORY_ID,
            scope="current_session",
            proposed_client_action_id=ACTION_ID,
            now=NOW,
        )
        replay = self.store.choose_scope(
            selected_id,
            expected_repository_id=REPOSITORY_ID,
            scope="current_session",
            proposed_client_action_id="codex_action_late_replay",
            now=NOW + timedelta(days=1),
        )
        self.assertEqual(ACTION_ID, replay.client_action_id)

    def test_rejects_fabricated_control_and_repository_mismatch(self) -> None:
        self.create_binding()
        with self.assertRaises(ControlBindingNotFound):
            self.store.choose_scope(
                "ctl_" + "9" * 32,
                expected_repository_id=REPOSITORY_ID,
                scope="current_session",
                proposed_client_action_id=ACTION_ID,
                now=NOW,
            )
        with self.assertRaises(ControlRepositoryMismatch):
            self.store.choose_scope(
                CONTROL_ID,
                expected_repository_id="repo_" + "8" * 32,
                scope="current_session",
                proposed_client_action_id=ACTION_ID,
                now=NOW,
            )

    def test_request_attachment_is_idempotent_but_not_replaceable(self) -> None:
        self.create_binding()
        self.store.choose_scope(
            CONTROL_ID,
            expected_repository_id=REPOSITORY_ID,
            scope="current_session",
            proposed_client_action_id=ACTION_ID,
            now=NOW,
        )
        first = self.store.attach_request(
            CONTROL_ID,
            client_action_id=ACTION_ID,
            central_request_id=REQUEST_ID,
        )
        replay = self.store.attach_request(
            CONTROL_ID,
            client_action_id=ACTION_ID,
            central_request_id=REQUEST_ID,
        )
        self.assertEqual(first, replay)

        for action_id, request_id in (
            ("codex_action_wrong", REQUEST_ID),
            (ACTION_ID, "crq_" + "6" * 32),
        ):
            with self.subTest(action_id=action_id, request_id=request_id):
                with self.assertRaises(ControlRequestConflict):
                    self.store.attach_request(
                        CONTROL_ID,
                        client_action_id=action_id,
                        central_request_id=request_id,
                    )

    def test_private_database_never_contains_discarded_raw_content(self) -> None:
        discarded = (
            "PROMPT_SENTINEL_do_not_store",
            "DIFF_SENTINEL_do_not_store",
            "TOOL_INPUT_SENTINEL_do_not_store",
            "/private/transcript/TRANSCRIPT_SENTINEL.jsonl",
            "CANDIDATE_SENTINEL_do_not_store",
        )
        self.create_binding()
        self.store.choose_scope(
            CONTROL_ID,
            expected_repository_id=REPOSITORY_ID,
            scope="current_session",
            proposed_client_action_id=ACTION_ID,
            now=NOW,
        )
        self.store.close()

        database_bytes = b"".join(
            path.read_bytes() for path in self.path.parent.iterdir() if path.is_file()
        )
        for sentinel in discarded:
            with self.subTest(sentinel=sentinel):
                self.assertNotIn(sentinel.encode("utf-8"), database_bytes)

    def test_identifiers_and_fifteen_minute_lifetime_are_validated(self) -> None:
        invalid_control_ids = (
            "ctl_ABCDEF0123456789abcdef0123456789",
            "ctl_short",
            "codex_action_" + "1" * 32,
        )
        for invalid in invalid_control_ids:
            with self.subTest(control_id=invalid):
                with self.assertRaises(ValueError):
                    self.create_binding(control_id=invalid)

        with self.assertRaises(ValueError):
            self.store.create_binding(
                session_id="session",
                render_turn_id="turn",
                cwd="/absolute/cwd",
                repository_id=REPOSITORY_ID,
                product_id=PRODUCT_ID,
                created_at=NOW,
                expires_at=NOW + timedelta(minutes=16),
                control_id=CONTROL_ID,
            )


if __name__ == "__main__":
    unittest.main()
