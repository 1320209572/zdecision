"""Live-only proof that Hook Session identity is exact app-server Thread identity."""

from __future__ import annotations

import os
import unittest

from zdecision.agent.cli import database_path
from zdecision.agent.db import AgentDatabase
from zdecision.app_server.gateway import AppServerGateway


@unittest.skipUnless(
    os.environ.get("ZDECISION_LIVE_ACCEPTANCE") == "1",
    "requires ZDECISION_LIVE_ACCEPTANCE=1 and a real Codex Desktop Fork",
)
class RecallHostIdentityLiveTests(unittest.TestCase):
    def test_hook_session_ids_are_exact_root_and_child_thread_ids(self) -> None:
        root_hook_session_id = os.environ.get(
            "ZDECISION_LIVE_ROOT_HOOK_SESSION_ID"
        )
        child_hook_session_id = os.environ.get(
            "ZDECISION_LIVE_CHILD_HOOK_SESSION_ID"
        )
        if not root_hook_session_id or not child_hook_session_id:
            self.fail("host_thread_identity_unavailable")

        database = AgentDatabase.open(database_path(os.environ))
        gateway: AppServerGateway | None = None
        try:
            gateway = AppServerGateway.connect(database=database)
            root = gateway.read_thread_identity(root_hook_session_id)
            child = gateway.read_thread_identity(child_hook_session_id)
        except Exception:
            self.fail("host_thread_identity_unavailable")
        finally:
            if gateway is not None:
                gateway.close()
            database.close()

        self.assertEqual(
            root_hook_session_id,
            root.thread_id,
            "host_thread_identity_unavailable",
        )
        self.assertEqual(
            child_hook_session_id,
            child.thread_id,
            "host_thread_identity_unavailable",
        )
        self.assertEqual(
            root.thread_id,
            child.forked_from_id,
            "host_thread_identity_unavailable",
        )
        self.assertEqual(
            root.session_tree_id,
            child.session_tree_id,
            "host_thread_identity_unavailable",
        )


if __name__ == "__main__":
    unittest.main()
