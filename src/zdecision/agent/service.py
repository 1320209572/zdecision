"""Persistent local delivery loop independent of Codex Session lifetime."""

from __future__ import annotations

import json
import re
import stat
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from zdecision.agent.central_client import CentralClient
from zdecision.agent.db import AgentDatabase
from zdecision.agent.events import TestRepositoryMapping
from zdecision.sync.contracts import ClaimedCaptureRequest, RepositoryView


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class AgentServiceConfigError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _CaptureRequestError(Exception):
    def __init__(self, code: str) -> None:
        if not isinstance(code, str) or _ERROR_CODE.fullmatch(code) is None:
            raise ValueError("Capture Request error code is invalid")
        self.code = code
        super().__init__(code)


class RetryableCaptureRequestError(_CaptureRequestError):
    pass


class TerminalCaptureRequestError(_CaptureRequestError):
    pass


class CaptureRequestProcessor(Protocol):
    def process(
        self,
        request: ClaimedCaptureRequest,
        client: CentralClient,
    ) -> None: ...


@dataclass(frozen=True)
class AgentConfig:
    central_url: str
    organization_id: str
    device_id: str
    device_token: str
    repositories: tuple[RepositoryView, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.central_url, str)
            or not self.central_url.startswith(("http://", "https://"))
            or len(self.central_url) > 2048
        ):
            raise ValueError("central_url is invalid")
        for name in ("organization_id", "device_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ValueError(f"{name} is invalid")
        if (
            not isinstance(self.device_token, str)
            or not 8 <= len(self.device_token) <= 512
            or any(character.isspace() for character in self.device_token)
        ):
            raise ValueError("device_token is invalid")
        if (
            not isinstance(self.repositories, tuple)
            or not 1 <= len(self.repositories) <= 100
            or any(
                not isinstance(item, RepositoryView)
                for item in self.repositories
            )
        ):
            raise ValueError("repositories are invalid")
        repository_ids = [item.repository_id for item in self.repositories]
        if len(set(repository_ids)) != len(repository_ids):
            raise ValueError("repositories contain duplicates")


class AgentService:
    def __init__(
        self,
        *,
        client: CentralClient,
        processor: CaptureRequestProcessor | None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.client = client
        self.processor = processor
        self.sleeper = sleeper or time.sleep

    def run_once(self) -> bool:
        if self.processor is None:
            return False
        request = self.client.claim_next()
        if request is None:
            return False
        try:
            self.processor.process(request, self.client)
        except RetryableCaptureRequestError as error:
            self.client.fail(
                request.request_id,
                request.lease_token,
                error.code,
                retryable=True,
            )
        except TerminalCaptureRequestError as error:
            self.client.fail(
                request.request_id,
                request.lease_token,
                error.code,
                retryable=False,
            )
        except Exception:
            self.client.fail(
                request.request_id,
                request.lease_token,
                "unexpected_processor_error",
                retryable=True,
            )
        return True

    def run_forever(self) -> None:
        while True:
            try:
                did_work = self.run_once()
            except Exception:
                did_work = False
            self.sleeper(0.1 if did_work else 5.0)


def load_agent_config(path: Path) -> AgentConfig:
    config_path = Path(path)
    if not config_path.is_absolute():
        raise AgentServiceConfigError("agent_config_path_not_absolute")
    try:
        mode = stat.S_IMODE(config_path.stat().st_mode)
        if mode & 0o077:
            raise AgentServiceConfigError(
                "agent_config_permissions_invalid"
            )
        value = json.loads(config_path.read_text("utf-8"))
    except AgentServiceConfigError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise AgentServiceConfigError("agent_config_invalid") from error
    expected = frozenset(
        (
            "central_url",
            "organization_id",
            "device_id",
            "device_token",
            "repositories",
        )
    )
    if not isinstance(value, Mapping) or frozenset(value) != expected:
        raise AgentServiceConfigError("agent_config_invalid")
    try:
        raw_repositories = value["repositories"]
        if not isinstance(raw_repositories, list):
            raise ValueError("repositories are invalid")
        return AgentConfig(
            central_url=value["central_url"],
            organization_id=value["organization_id"],
            device_id=value["device_id"],
            device_token=value["device_token"],
            repositories=tuple(
                RepositoryView.from_dict(item) for item in raw_repositories
            ),
        )
    except (TypeError, ValueError) as error:
        raise AgentServiceConfigError("agent_config_invalid") from error


def mirror_repository_mappings(
    database: AgentDatabase,
    config: AgentConfig,
) -> None:
    if not isinstance(database, AgentDatabase):
        raise TypeError("database must be an AgentDatabase")
    if not isinstance(config, AgentConfig):
        raise TypeError("config must be an AgentConfig")
    for repository in config.repositories:
        database.put_test_repository_mapping(
            TestRepositoryMapping(
                repository_id=repository.repository_id,
                product_id=repository.product_id,
                product_name=repository.product_name,
                enabled=repository.enabled,
            )
        )


def configured_processor(
    database: AgentDatabase,
    config: AgentConfig,
    state_path: Path,
) -> CaptureRequestProcessor | None:
    if not isinstance(database, AgentDatabase):
        raise TypeError("database must be an AgentDatabase")
    if not isinstance(config, AgentConfig):
        raise TypeError("config must be an AgentConfig")
    if not Path(state_path).is_absolute():
        raise ValueError("state_path must be absolute")
    from zdecision.agent.capture_processor import (
        OnDemandCaptureProcessor,
    )
    from zdecision.agent.capture_operation_store import (
        CaptureOperationStore,
    )
    from zdecision.agent.request_state import RequestStateStore
    from zdecision.agent.session_index import SessionIndex
    from zdecision.app_server.gateway import AppServerGateway
    from zdecision.app_server.reconciliation_runner import (
        ReconciliationRunner,
    )
    from zdecision.app_server.requested_capture import (
        RequestedCaptureRunner,
    )
    from zdecision.capture.templates import TemplateCatalog

    local_state_path = Path(state_path)
    package_root = Path(__file__).resolve().parents[1]
    repository_root = package_root.parents[1]
    session_index = SessionIndex.open(local_state_path)
    operation_store = CaptureOperationStore.open(local_state_path)
    request_state = RequestStateStore.open(local_state_path)
    database.retire_legacy_automatic_capture()
    gateway = None
    try:
        gateway = AppServerGateway.connect(database=database)
        template_catalog = TemplateCatalog(
            repository_root / "decision-templates",
            package_root / "capture" / "prompt_contracts",
        )
        return OnDemandCaptureProcessor(
            database=database,
            session_index=session_index,
            capture_runner=RequestedCaptureRunner(
                gateway=gateway,
                operation_store=operation_store,
                template_catalog=template_catalog,
            ),
            reconciliation_runner=ReconciliationRunner(
                gateway=gateway,
                request_state=request_state,
            ),
            request_state=request_state,
            clock=lambda: datetime.now(UTC),
        )
    except Exception:
        session_index.close()
        request_state.close()
        operation_store.close()
        close_gateway = getattr(gateway, "close", None)
        if callable(close_gateway):
            close_gateway()
        raise
