"""Bounded JSONL transport and request correlation for Codex app-server."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from typing import IO, Protocol


CONTROLLED_APP_SERVER_COMMAND = (
    "codex",
    "app-server",
    "--listen",
    "stdio://",
)
_MAX_JSONL_BYTES = 4 * 1024 * 1024


class AppServerError(Exception):
    """Base class for bounded app-server failures."""


class AppServerProtocolError(AppServerError):
    """The peer violated the JSONL or JSON-RPC contract."""


class AppServerTimeout(AppServerError):
    """A bounded app-server wait reached its deadline."""


class AppServerEOF(AppServerError):
    """The app-server transport closed before the operation completed."""


class AppServerRequestError(AppServerError):
    """An app-server request returned a JSON-RPC error."""

    def __init__(self, method: str, code: object) -> None:
        safe_code = code if isinstance(code, int) and not isinstance(code, bool) else "unknown"
        super().__init__(f"app-server request {method!r} failed (code={safe_code})")
        self.method = method
        self.code = safe_code


class UnexpectedServerRequest(AppServerError):
    """The non-interactive client received a request it cannot authorize."""

    def __init__(self, method: str) -> None:
        super().__init__(f"Unexpected app-server request: {method}")
        self.method = method


class AppServerTransport(Protocol):
    def send(self, message: Mapping[str, object]) -> None: ...

    def receive(self, timeout_seconds: float) -> Mapping[str, object]: ...

    def close(self) -> None: ...


class ProcessJsonlTransport:
    """One controlled app-server process with bounded stdout/stderr readers."""

    def __init__(self, process, *, max_stderr_lines: int = 40) -> None:
        if not isinstance(max_stderr_lines, int) or max_stderr_lines <= 0:
            raise ValueError("max_stderr_lines must be positive")
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise ValueError("app-server process must expose all standard streams")
        self._process = process
        self._stdin: IO[str] = process.stdin
        self._stdout: IO[str] = process.stdout
        self._stderr: IO[str] = process.stderr
        self._messages: queue.Queue[object] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=max_stderr_lines)
        self._send_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            name="zdecision-app-server-jsonl",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="zdecision-app-server-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

    @classmethod
    def launch(cls) -> "ProcessJsonlTransport":
        process = subprocess.Popen(
            list(CONTROLLED_APP_SERVER_COMMAND),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            bufsize=1,
        )
        return cls(process)

    @property
    def stderr_tail(self) -> tuple[str, ...]:
        return tuple(self._stderr_lines)

    def send(self, message: Mapping[str, object]) -> None:
        if not isinstance(message, Mapping):
            raise TypeError("app-server message must be an object")
        try:
            encoded = json.dumps(
                dict(message),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            raise AppServerProtocolError("app-server message is not JSON-safe") from None
        if len(encoded.encode("utf-8")) > _MAX_JSONL_BYTES:
            raise AppServerProtocolError("app-server message exceeds the size limit")
        with self._send_lock:
            if self._closed:
                raise AppServerEOF("app-server transport is closed")
            try:
                self._stdin.write(encoded + "\n")
                self._stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                raise AppServerEOF("app-server transport closed during send") from None

    def receive(self, timeout_seconds: float) -> Mapping[str, object]:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        try:
            value = self._messages.get(timeout=timeout_seconds)
        except queue.Empty:
            raise AppServerTimeout("Timed out waiting for app-server") from None
        if isinstance(value, BaseException):
            raise value
        if value is _EOF:
            raise AppServerEOF("app-server transport closed")
        if not isinstance(value, Mapping):
            raise AppServerProtocolError("app-server emitted a non-object message")
        return value

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._stdin.close()
            except (OSError, ValueError):
                pass
            if self._process.poll() is None:
                try:
                    self._process.terminate()
                    self._process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                    try:
                        self._process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
                except OSError:
                    pass
        self._stdout_thread.join(timeout=1.0)
        self._stderr_thread.join(timeout=1.0)

    def _read_stdout(self) -> None:
        try:
            for raw_line in self._stdout:
                if len(raw_line.encode("utf-8")) > _MAX_JSONL_BYTES:
                    self._messages.put(
                        AppServerProtocolError(
                            "app-server JSONL message exceeds the size limit"
                        )
                    )
                    return
                try:
                    value = json.loads(raw_line)
                except (json.JSONDecodeError, UnicodeError):
                    self._messages.put(
                        AppServerProtocolError("Malformed app-server JSONL message")
                    )
                    return
                if not isinstance(value, dict):
                    self._messages.put(
                        AppServerProtocolError(
                            "app-server JSONL message must be an object"
                        )
                    )
                    return
                self._messages.put(value)
        except (OSError, UnicodeError):
            self._messages.put(AppServerEOF("app-server stdout closed"))
        finally:
            self._messages.put(_EOF)

    def _read_stderr(self) -> None:
        try:
            for line in self._stderr:
                bounded = line.rstrip("\r\n")[:512]
                self._stderr_lines.append(bounded)
        except (OSError, UnicodeError):
            return


_EOF = object()


class JsonlAppServerClient:
    """Thread-safe request routing over an initialized app-server transport."""

    def __init__(
        self,
        transport: AppServerTransport,
        *,
        default_timeout_seconds: float = 30.0,
    ) -> None:
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        self._transport = transport
        self._default_timeout_seconds = default_timeout_seconds
        self._next_request_id = 1
        self._pending: dict[int, queue.Queue[object]] = {}
        self._pending_lock = threading.Lock()
        self._initialize_lock = threading.Lock()
        self._notification_condition = threading.Condition()
        self._notifications: list[tuple[str, Mapping[str, object]]] = []
        self._fatal: AppServerError | None = None
        self._initialized = False
        self._closed = False
        self._dispatcher = threading.Thread(
            target=self._dispatch,
            name="zdecision-app-server-dispatch",
            daemon=True,
        )
        self._dispatcher.start()

    def initialize(self) -> Mapping[str, object]:
        with self._initialize_lock:
            if self._initialized:
                return {}
            result = self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "zdecision",
                        "title": "ZDecision",
                        "version": "0.1.0",
                    }
                },
                allow_uninitialized=True,
            )
            if not isinstance(result, Mapping):
                raise AppServerProtocolError(
                    "app-server initialize result must be an object"
                )
            self._transport.send({"method": "initialized", "params": {}})
            self._initialized = True
            return result

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        timeout_seconds: float | None = None,
    ) -> object:
        if not self._initialized:
            raise AppServerProtocolError("app-server client is not initialized")
        return self._request(method, params, timeout_seconds=timeout_seconds)

    def wait_for_notification(
        self,
        method: str,
        predicate: Callable[[Mapping[str, object]], bool],
        timeout_seconds: float | None = None,
    ) -> Mapping[str, object]:
        if not isinstance(method, str) or not method:
            raise ValueError("notification method is invalid")
        timeout = self._timeout(timeout_seconds)
        deadline = time.monotonic() + timeout
        with self._notification_condition:
            while True:
                if self._fatal is not None:
                    raise self._fatal
                for index, (candidate_method, params) in enumerate(
                    self._notifications
                ):
                    if candidate_method == method and predicate(params):
                        self._notifications.pop(index)
                        return params
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AppServerTimeout(
                        f"Timed out waiting for {method!r} notification"
                    )
                self._notification_condition.wait(timeout=remaining)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._transport.close()
        with self._notification_condition:
            self._notification_condition.notify_all()
        self._dispatcher.join(timeout=1.0)

    def _request(
        self,
        method: str,
        params: Mapping[str, object],
        timeout_seconds: float | None = None,
        *,
        allow_uninitialized: bool = False,
    ) -> object:
        if not isinstance(method, str) or not method:
            raise ValueError("app-server method is invalid")
        if not isinstance(params, Mapping):
            raise TypeError("app-server params must be an object")
        if self._closed:
            raise AppServerEOF("app-server client is closed")
        if not allow_uninitialized and not self._initialized:
            raise AppServerProtocolError("app-server client is not initialized")
        response_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        with self._pending_lock:
            if self._fatal is not None:
                raise self._fatal
            request_id = self._next_request_id
            self._next_request_id += 1
            self._pending[request_id] = response_queue
        try:
            self._transport.send(
                {"method": method, "id": request_id, "params": dict(params)}
            )
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise AppServerEOF("app-server send failed") from None
        try:
            response = response_queue.get(timeout=self._timeout(timeout_seconds))
        except queue.Empty:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise AppServerTimeout(
                f"Timed out waiting for app-server request {method!r}"
            ) from None
        if isinstance(response, BaseException):
            raise response
        if not isinstance(response, Mapping):
            raise AppServerProtocolError("app-server response must be an object")
        if "error" in response:
            error = response["error"]
            code = error.get("code") if isinstance(error, Mapping) else None
            raise AppServerRequestError(method, code)
        if "result" not in response:
            raise AppServerProtocolError("app-server response has no result")
        return response["result"]

    def _timeout(self, value: float | None) -> float:
        timeout = self._default_timeout_seconds if value is None else value
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")
        return timeout

    def _dispatch(self) -> None:
        while not self._closed:
            try:
                message = self._transport.receive(0.1)
            except AppServerTimeout:
                continue
            except AppServerEOF:
                if not self._closed:
                    self._set_fatal(AppServerEOF("app-server transport closed"))
                return
            except Exception:
                self._set_fatal(
                    AppServerProtocolError("app-server transport failed")
                )
                return
            try:
                self._route_message(message)
            except AppServerError as error:
                self._set_fatal(error)
                return
            except Exception:
                self._set_fatal(
                    AppServerProtocolError("Malformed app-server protocol message")
                )
                return

    def _route_message(self, message: Mapping[str, object]) -> None:
        if not isinstance(message, Mapping):
            raise AppServerProtocolError("app-server message must be an object")
        has_id = "id" in message
        method = message.get("method")
        if has_id and isinstance(method, str):
            self._answer_server_request(message["id"], method)
            raise UnexpectedServerRequest(method)
        if has_id:
            request_id = message["id"]
            if not isinstance(request_id, int) or isinstance(request_id, bool):
                raise AppServerProtocolError("app-server response id is invalid")
            with self._pending_lock:
                response_queue = self._pending.pop(request_id, None)
            if response_queue is None:
                raise AppServerProtocolError("app-server response id is unknown")
            response_queue.put(dict(message))
            return
        if not isinstance(method, str) or not method:
            raise AppServerProtocolError("app-server notification is invalid")
        params = message.get("params", {})
        if not isinstance(params, Mapping):
            raise AppServerProtocolError("app-server notification params are invalid")
        with self._notification_condition:
            self._notifications.append((method, dict(params)))
            self._notification_condition.notify_all()

    def _answer_server_request(self, request_id: object, method: str) -> None:
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            response: dict[str, object] = {
                "id": request_id,
                "result": {"decision": "cancel"},
            }
        else:
            response = {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": "Unsupported non-interactive server request",
                },
            }
        try:
            self._transport.send(response)
        except Exception:
            return

    def _set_fatal(self, error: AppServerError) -> None:
        with self._pending_lock:
            if self._fatal is not None:
                return
            self._fatal = error
            pending = tuple(self._pending.values())
            self._pending.clear()
        for response_queue in pending:
            response_queue.put(error)
        with self._notification_condition:
            self._notification_condition.notify_all()
