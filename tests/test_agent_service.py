from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zdecision.agent.db import AgentDatabase
from zdecision.sync.contracts import (
    ClaimedCaptureRequest,
    RepositoryView,
)

try:
    from zdecision.agent.service import (
        AgentConfig,
        AgentService,
        AgentServiceConfigError,
        RetryableCaptureRequestError,
        TerminalCaptureRequestError,
        configured_processor,
        load_agent_config,
        mirror_repository_mappings,
    )
except ModuleNotFoundError as error:
    SERVICE_IMPORT_ERROR: ModuleNotFoundError | None = error
else:
    SERVICE_IMPORT_ERROR = None


REQUEST_ID = "crq_" + "1" * 32
REPOSITORY_ID = "repo_" + "2" * 32
PRODUCT_ID = "prod_" + "3" * 32


def claimed_request() -> ClaimedCaptureRequest:
    return ClaimedCaptureRequest(
        request_id=REQUEST_ID,
        repository_id=REPOSITORY_ID,
        product_id=PRODUCT_ID,
        product_name="ZDecision",
        template_id="business",
        lease_token="lease_0123456789abcdef",
        lease_expires_at="2026-07-31T03:00:30Z",
    )


class FakeCentralClient:
    def __init__(self, claims: list[object]) -> None:
        self.claims = list(claims)
        self.failures: list[tuple[str, str, str, bool]] = []

    def claim_next(self):
        result = self.claims.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def fail(
        self,
        request_id: str,
        lease_token: str,
        code: str,
        retryable: bool,
    ) -> None:
        self.failures.append((request_id, lease_token, code, retryable))


class FakeProcessor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.processed: list[str] = []

    def process(self, request, client) -> None:
        self.processed.append(request.request_id)
        if self.error is not None:
            raise self.error


class AgentServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNone(
            SERVICE_IMPORT_ERROR,
            f"Agent service is missing: {SERVICE_IMPORT_ERROR}",
        )

    def test_service_claims_after_codex_session_has_ended(self) -> None:
        client = FakeCentralClient([claimed_request(), None])
        processor = FakeProcessor()
        service = AgentService(
            client=client,
            processor=processor,
            sleeper=lambda _: None,
        )

        self.assertTrue(service.run_once())
        self.assertEqual([REQUEST_ID], processor.processed)
        self.assertFalse(service.run_once())

    def test_declared_and_unexpected_failures_are_sanitized(self) -> None:
        cases = (
            (
                RetryableCaptureRequestError("central_temporarily_unavailable"),
                "central_temporarily_unavailable",
                True,
            ),
            (
                TerminalCaptureRequestError("source_not_interactive"),
                "source_not_interactive",
                False,
            ),
            (
                RuntimeError("raw local path /Users/demo"),
                "unexpected_processor_error",
                True,
            ),
        )
        for error, expected_code, retryable in cases:
            with self.subTest(error=type(error).__name__):
                client = FakeCentralClient([claimed_request()])
                service = AgentService(
                    client=client,
                    processor=FakeProcessor(error),
                    sleeper=lambda _: None,
                )

                self.assertTrue(service.run_once())
                self.assertEqual(
                    (
                        REQUEST_ID,
                        "lease_0123456789abcdef",
                        expected_code,
                        retryable,
                    ),
                    client.failures[0],
                )

    def test_forever_loop_survives_claim_failure(self) -> None:
        class StopLoop(Exception):
            pass

        delays: list[float] = []

        def stop_after_two(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 2:
                raise StopLoop()

        service = AgentService(
            client=FakeCentralClient([ConnectionError("offline"), None]),
            processor=FakeProcessor(),
            sleeper=stop_after_two,
        )

        with self.assertRaises(StopLoop):
            service.run_forever()
        self.assertEqual([5.0, 5.0], delays)

    def test_config_is_owner_only_and_mirrors_repository_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "agent.json"
            path.write_text(
                json.dumps(
                    {
                        "central_url": "http://127.0.0.1:8765",
                        "organization_id": "org_demo",
                        "device_id": "device_demo",
                        "device_token": "device-secret-token",
                        "repositories": [
                            {
                                "repository_id": REPOSITORY_ID,
                                "product_id": PRODUCT_ID,
                                "product_name": "ZDecision",
                                "enabled": True,
                            }
                        ],
                    }
                ),
                "utf-8",
            )
            os.chmod(path, 0o600)
            config = load_agent_config(path)
            database = AgentDatabase.open(root / "state.sqlite3")
            try:
                mirror_repository_mappings(database, config)
                mapping = database.get_repository_mapping(REPOSITORY_ID)
            finally:
                database.close()

            self.assertIsInstance(config, AgentConfig)
            self.assertEqual(PRODUCT_ID, mapping.product_id)
            self.assertTrue(mapping.enabled)

            os.chmod(path, 0o644)
            with self.assertRaisesRegex(
                AgentServiceConfigError, "agent_config_permissions_invalid"
            ):
                load_agent_config(path)

    def test_configured_service_builds_the_on_demand_processor(
        self,
    ) -> None:
        config = AgentConfig(
            central_url="http://127.0.0.1:8765",
            organization_id="org_demo",
            device_id="device_demo",
            device_token="device-secret-token",
            repositories=(
                RepositoryView(
                    repository_id=REPOSITORY_ID,
                    product_id=PRODUCT_ID,
                    product_name="ZDecision",
                    enabled=True,
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            state_path = root / "agent" / "zdecision.sqlite3"
            database = AgentDatabase.open(state_path)
            self.addCleanup(database.close)
            fake_gateway = object()
            with patch(
                "zdecision.app_server.gateway.AppServerGateway.connect",
                return_value=fake_gateway,
            ) as connect:
                processor = configured_processor(
                    database, config, state_path
                )

            from zdecision.agent.capture_processor import (
                OnDemandCaptureProcessor,
            )

            self.assertIsInstance(
                processor, OnDemandCaptureProcessor
            )
            connect.assert_called_once_with(database=database)
            processor.session_index.close()
            processor.request_state.close()
            processor.capture_runner.operation_store.close()


if __name__ == "__main__":
    unittest.main()
