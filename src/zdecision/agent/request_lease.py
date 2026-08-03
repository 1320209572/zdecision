"""Independent renewal for one claimed central Capture Request lease."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from zdecision.agent.central_client import CentralClientError
from zdecision.sync.contracts import (
    CAPTURE_REQUEST_LEASE_SECONDS,
    CAPTURE_REQUEST_RENEW_INTERVAL_SECONDS,
)


class LeaseHeartbeatClient(Protocol):
    def heartbeat(self, request_id: str, lease_token: str) -> None: ...

    def close(self) -> None: ...


class RequestLeaseSession:
    """Own the renewal worker and first uncertain-ownership failure."""

    def __init__(
        self,
        request_id: str,
        lease_token: str,
        *,
        client_factory: Callable[[], LeaseHeartbeatClient],
        interval_seconds: float = CAPTURE_REQUEST_RENEW_INTERVAL_SECONDS,
    ) -> None:
        if not callable(client_factory):
            raise TypeError("client_factory must be callable")
        if (
            not isinstance(interval_seconds, (int, float))
            or isinstance(interval_seconds, bool)
            or not 0
            < interval_seconds
            <= CAPTURE_REQUEST_LEASE_SECONDS / 3
        ):
            raise ValueError("lease renewal interval is invalid")
        self.request_id = request_id
        self.lease_token = lease_token
        self.client_factory = client_factory
        self.interval_seconds = float(interval_seconds)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._failure_code: str | None = None
        self._thread: threading.Thread | None = None

    @property
    def uncertain(self) -> bool:
        with self._lock:
            return self._failure_code is not None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("lease session already started")
        self._thread = threading.Thread(
            target=self._run,
            name=f"zdecision-lease-{self.request_id[-8:]}",
            daemon=True,
        )
        self._thread.start()
        self._ready.wait()
        self.checkpoint()

    def checkpoint(self) -> None:
        with self._lock:
            code = self._failure_code
        if code is not None:
            raise CentralClientError(code)

    def mark_uncertain(self, error: Exception) -> None:
        code = (
            error.code
            if isinstance(error, CentralClientError)
            else "central_connection_unavailable"
        )
        with self._lock:
            if self._failure_code is None:
                self._failure_code = code
        self._stop.set()

    def quiesce(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self.checkpoint()

    def _run(self) -> None:
        client: LeaseHeartbeatClient | None = None
        try:
            try:
                client = self.client_factory()
            except Exception as error:
                self.mark_uncertain(error)
            finally:
                self._ready.set()
            if client is None:
                return
            while not self._stop.wait(self.interval_seconds):
                try:
                    client.heartbeat(self.request_id, self.lease_token)
                except Exception as error:
                    self.mark_uncertain(error)
                    return
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            self._ready.set()


class LeaseAwareCentralClient:
    """Guard foreground mutations with the independent lease state."""

    def __init__(
        self,
        foreground: object,
        lease_session: RequestLeaseSession,
    ) -> None:
        if not isinstance(lease_session, RequestLeaseSession):
            raise TypeError("lease_session must be a RequestLeaseSession")
        self._foreground = foreground
        self._lease_session = lease_session

    def start(self, request_id: str, lease_token: str) -> None:
        self._lease_session.checkpoint()
        self._foreground.start(request_id, lease_token)  # type: ignore[attr-defined]

    def heartbeat(self, request_id: str, lease_token: str) -> None:
        self._lease_session.checkpoint()
        try:
            self._foreground.heartbeat(  # type: ignore[attr-defined]
                request_id, lease_token
            )
        except Exception as error:
            self._lease_session.mark_uncertain(error)
            raise

    def progress(
        self,
        request_id: str,
        lease_token: str,
        code: str,
    ) -> None:
        self._lease_session.checkpoint()
        self._foreground.progress(  # type: ignore[attr-defined]
            request_id, lease_token, code
        )

    def upload_candidates(self, lease_token: str, batch: object):
        self._lease_session.checkpoint()
        return self._foreground.upload_candidates(  # type: ignore[attr-defined,no-any-return]
            lease_token, batch
        )

    def complete(
        self,
        request_id: str,
        lease_token: str,
        batch_digest: str,
    ) -> None:
        self._lease_session.checkpoint()
        self._lease_session.quiesce()
        try:
            self._foreground.heartbeat(  # type: ignore[attr-defined]
                request_id, lease_token
            )
        except Exception as error:
            self._lease_session.mark_uncertain(error)
            raise
        self._foreground.complete(  # type: ignore[attr-defined]
            request_id, lease_token, batch_digest
        )
