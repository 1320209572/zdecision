"""Bounded authenticated HTTP client for the central coordination service."""

from __future__ import annotations

import time
import re
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

import httpx

from zdecision.sync.contracts import (
    CandidateBatchUpload,
    CaptureRequestView,
    ClaimedCaptureRequest,
    ProgressEvent,
    UploadReceipt,
)


_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_ATTEMPTS = 3
_REQUEST_ID = re.compile(r"^crq_[0-9a-f]{32}$")


class CentralClientError(Exception):
    """A sanitized central transport or response failure."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CentralClient:
    def __init__(
        self,
        base_url: str,
        device_token: str,
        *,
        timeout: httpx.Timeout | None = None,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        normalized_url = _base_url(base_url)
        token = _device_token(device_token)
        self.client = httpx.Client(
            base_url=normalized_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout
            or httpx.Timeout(
                30.0,
                connect=5.0,
                write=30.0,
                pool=5.0,
            ),
            transport=transport,
        )
        self.sleeper = sleeper or time.sleep

    def close(self) -> None:
        self.client.close()

    def claim_next(self) -> ClaimedCaptureRequest | None:
        status_code, value = self._post(
            "/api/v1/agent/capture-requests/claim",
            {},
            allowed_statuses=(200, 204),
        )
        if status_code == 204:
            return None
        try:
            return ClaimedCaptureRequest.from_dict(value)
        except (TypeError, ValueError) as error:
            raise CentralClientError("central_response_invalid") from error

    def heartbeat(self, request_id: str, lease_token: str) -> None:
        status_code, _ = self._post(
            _action_path(request_id, "heartbeat"),
            {"lease_token": lease_token},
            allowed_statuses=(204,),
        )
        if status_code != 204:
            raise CentralClientError("central_response_invalid")

    def start(self, request_id: str, lease_token: str) -> None:
        _, value = self._post(
            _action_path(request_id, "start"),
            {"lease_token": lease_token},
            allowed_statuses=(200,),
        )
        try:
            ProgressEvent.from_dict(value)
        except (TypeError, ValueError) as error:
            raise CentralClientError("central_response_invalid") from error

    def progress(
        self,
        request_id: str,
        lease_token: str,
        code: str,
    ) -> None:
        _, value = self._post(
            _action_path(request_id, "progress"),
            {"lease_token": lease_token, "code": code},
            allowed_statuses=(200,),
        )
        try:
            ProgressEvent.from_dict(value)
        except (TypeError, ValueError) as error:
            raise CentralClientError("central_response_invalid") from error

    def upload_candidates(
        self,
        lease_token: str,
        batch: CandidateBatchUpload,
    ) -> UploadReceipt:
        if not isinstance(batch, CandidateBatchUpload):
            raise TypeError("batch must be a CandidateBatchUpload")
        _, value = self._post(
            _action_path(batch.request_id, "candidates"),
            {
                "lease_token": lease_token,
                "batch": batch.to_dict(),
            },
            allowed_statuses=(200,),
        )
        try:
            return UploadReceipt.from_dict(value)
        except (TypeError, ValueError) as error:
            raise CentralClientError("central_response_invalid") from error

    def complete(
        self,
        request_id: str,
        lease_token: str,
        batch_digest: str,
    ) -> None:
        _, value = self._post(
            _action_path(request_id, "complete"),
            {
                "lease_token": lease_token,
                "batch_digest": batch_digest,
            },
            allowed_statuses=(200,),
        )
        try:
            CaptureRequestView.from_dict(value)
        except (TypeError, ValueError) as error:
            raise CentralClientError("central_response_invalid") from error

    def fail(
        self,
        request_id: str,
        lease_token: str,
        code: str,
        retryable: bool,
    ) -> None:
        _, value = self._post(
            _action_path(request_id, "fail"),
            {
                "lease_token": lease_token,
                "code": code,
                "retryable": retryable,
            },
            allowed_statuses=(200,),
        )
        try:
            CaptureRequestView.from_dict(value)
        except (TypeError, ValueError) as error:
            raise CentralClientError("central_response_invalid") from error

    def _post(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        allowed_statuses: tuple[int, ...],
    ) -> tuple[int, object | None]:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                response = self.client.post(path, json=dict(payload))
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                if attempt + 1 == _MAX_ATTEMPTS:
                    raise CentralClientError(
                        "central_connection_unavailable"
                    ) from error
                self.sleeper(_retry_delay(attempt))
                continue
            try:
                if _retryable_status(response.status_code):
                    if attempt + 1 == _MAX_ATTEMPTS:
                        raise CentralClientError(
                            "central_temporarily_unavailable"
                        )
                    self.sleeper(_retry_delay(attempt))
                    continue
                if response.status_code not in allowed_statuses:
                    raise CentralClientError("central_request_rejected")
                content = response.content
                if len(content) > _MAX_RESPONSE_BYTES:
                    raise CentralClientError("central_response_too_large")
                if response.status_code == 204:
                    return response.status_code, None
                try:
                    return response.status_code, response.json()
                except ValueError as error:
                    raise CentralClientError(
                        "central_response_invalid"
                    ) from error
            finally:
                response.close()
        raise CentralClientError("central_connection_unavailable")


def _base_url(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 2048:
        raise ValueError("base_url is invalid")
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise ValueError("base_url is invalid") from error
    if (
        parsed.scheme not in ("http", "https")
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("base_url is invalid")
    return value.rstrip("/")


def _device_token(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 512
        or any(character.isspace() for character in value)
    ):
        raise ValueError("device_token is invalid")
    return value


def _action_path(request_id: str, action: str) -> str:
    if (
        not isinstance(request_id, str)
        or _REQUEST_ID.fullmatch(request_id) is None
    ):
        raise ValueError("request_id is invalid")
    if action not in (
        "start",
        "heartbeat",
        "progress",
        "candidates",
        "complete",
        "fail",
    ):
        raise ValueError("action is invalid")
    return f"/api/v1/agent/capture-requests/{request_id}/{action}"


def _retryable_status(status_code: int) -> bool:
    return status_code in (408, 429) or 500 <= status_code <= 599


def _retry_delay(attempt: int) -> float:
    return min(0.1 * (2**attempt), 1.0)
