"""Minimal authenticated HTTP boundary for on-demand Candidate refresh."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from zdecision.central.auth import (
    DemoIdentityProvider,
    InvalidCredentials,
    Principal,
)
from zdecision.central.service import (
    AccessDenied,
    CaptureRequestService,
    CentralRequestError,
    InvalidLease,
    RepositoryUnavailable,
    RequestNotFound,
)
from zdecision.central.web.application import CentralWebApplication
from zdecision.central.web.reviews import (
    ProductNotFound,
    ProductOwnershipConflict,
    ReviewStale,
)
from zdecision.central.web.store import DraftConflict, WebActionConflict
from zdecision.sync.contracts import (
    CAPTURE_REQUEST_LEASE_SECONDS,
    CandidateBatchUpload,
    CaptureRequestCreate,
)


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _CaptureRequestBody(_StrictBody):
    repository_id: str = Field(min_length=1, max_length=64)
    template_id: str = Field(min_length=1, max_length=128)
    capture_scope: str = Field(min_length=1, max_length=32)
    client_action_id: str = Field(min_length=1, max_length=128)


class _EmptyBody(_StrictBody):
    pass


class _LeaseBody(_StrictBody):
    lease_token: str = Field(min_length=8, max_length=256)


class _ProgressBody(_LeaseBody):
    code: str = Field(min_length=1, max_length=128)


class _CompleteBody(_LeaseBody):
    batch_digest: str = Field(min_length=64, max_length=64)


class _CandidateBatchBody(_LeaseBody):
    batch: dict[str, object]


class _FailBody(_ProgressBody):
    retryable: bool


def create_app(
    service: CaptureRequestService,
    identity_provider: DemoIdentityProvider,
    *,
    clock: Callable[[], datetime] | None = None,
    web_application: CentralWebApplication | None = None,
    static_root: Path | None = None,
) -> FastAPI:
    if not isinstance(service, CaptureRequestService):
        raise TypeError("service must be a CaptureRequestService")
    if not isinstance(identity_provider, DemoIdentityProvider):
        raise TypeError("identity_provider must be a DemoIdentityProvider")
    if web_application is not None and not isinstance(
        web_application, CentralWebApplication
    ):
        raise TypeError("web_application must be a CentralWebApplication")
    current_time = clock or (lambda: datetime.now(UTC))
    selected_static_root = (
        Path(static_root)
        if static_root is not None
        else Path(__file__).with_name("static")
    )
    app = FastAPI(
        title="ZDecision Central",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.web_application = web_application
    app.state.identity_provider = identity_provider
    app.state.current_time = current_time
    if web_application is not None:
        from zdecision.central.web.api import router as web_router

        app.include_router(web_router)
        assets = selected_static_root / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.exception_handler(InvalidCredentials)
    async def invalid_credentials_handler(
        request: Request, error: InvalidCredentials
    ) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"error": error.code},
            headers={"WWW-Authenticate": "Bearer"},
        )

    @app.exception_handler(CentralRequestError)
    async def central_error_handler(
        request: Request, error: CentralRequestError
    ) -> JSONResponse:
        if isinstance(error, RequestNotFound):
            status_code = 404
        elif isinstance(error, AccessDenied):
            status_code = 403
        elif isinstance(error, RepositoryUnavailable):
            status_code = 409
        elif isinstance(error, InvalidLease):
            status_code = 409
        else:
            status_code = 409
        return JSONResponse(
            status_code=status_code,
            content={"error": error.code},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request"},
        )

    @app.exception_handler(ProductNotFound)
    async def product_not_found_handler(
        request: Request, error: ProductNotFound
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"error": error.code})

    @app.exception_handler(ProductOwnershipConflict)
    async def product_ownership_handler(
        request: Request, error: ProductOwnershipConflict
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": error.code})

    @app.exception_handler(DraftConflict)
    async def draft_conflict_handler(
        request: Request, error: DraftConflict
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"error": "review_draft_conflict"}
        )

    @app.exception_handler(ReviewStale)
    async def review_stale_handler(
        request: Request, error: ReviewStale
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"error": error.code, "family_ids": list(error.family_ids)},
        )

    @app.exception_handler(WebActionConflict)
    async def web_action_conflict_handler(
        request: Request, error: WebActionConflict
    ) -> JSONResponse:
        return JSONResponse(
            status_code=409, content={"error": "web_action_conflict"}
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(
        request: Request, error: ValueError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request"},
        )

    def browser() -> Principal:
        return identity_provider.browser_principal()

    def device(authorization: str | None) -> Principal:
        return identity_provider.authenticate_device(authorization)

    def plugin(authorization: str | None) -> Principal:
        return identity_provider.authenticate_plugin_action(authorization)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        path = selected_static_root / "index.html"
        return HTMLResponse(path.read_text("utf-8"))

    @app.get("/api/v1/repositories")
    async def list_repositories() -> dict[str, object]:
        return {
            "repositories": [
                item.to_dict() for item in service.list_repositories(browser())
            ]
        }

    @app.get("/api/v1/repositories/{repository_id}/candidates")
    async def list_current_candidates(
        repository_id: str,
    ) -> dict[str, object]:
        return {
            "items": [
                item.to_dict()
                for item in service.list_current_candidates(
                    browser(), repository_id
                )
            ]
        }

    @app.post("/api/v1/capture-requests")
    async def create_capture_request(
        body: _CaptureRequestBody,
    ) -> dict[str, object]:
        command = CaptureRequestCreate.from_dict(body.model_dump())
        result = service.create_request(browser(), command, current_time())
        return {**result.to_dict(), "capture_scope": command.capture_scope}

    @app.get("/api/v1/capture-requests/{request_id}")
    async def get_capture_request(request_id: str) -> dict[str, object]:
        return service.get_request(browser(), request_id).to_dict()

    @app.post("/api/v1/plugin/capture-requests")
    async def create_plugin_capture_request(
        body: _CaptureRequestBody,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        command = CaptureRequestCreate.from_dict(body.model_dump())
        result = service.create_request(
            plugin(authorization), command, current_time()
        )
        return result.to_dict()

    @app.get("/api/v1/plugin/capture-requests/{request_id}")
    async def get_plugin_capture_request(
        request_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return service.get_request(plugin(authorization), request_id).to_dict()

    @app.get("/api/v1/capture-requests/{request_id}/events")
    async def capture_request_events(
        request_id: str,
        after_sequence: Annotated[int, Query(ge=0, le=2_147_483_647)] = 0,
    ) -> dict[str, object]:
        return {
            "events": [
                item.to_dict()
                for item in service.events_after(
                    browser(), request_id, after_sequence
                )
            ]
        }

    @app.post("/api/v1/agent/capture-requests/claim")
    async def claim_capture_request(
        body: _EmptyBody,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        claimed = service.claim_next(
            device(authorization),
            current_time(),
            lease_seconds=CAPTURE_REQUEST_LEASE_SECONDS,
        )
        if claimed is None:
            return Response(status_code=204)
        return JSONResponse(content=claimed.to_dict())

    @app.post("/api/v1/agent/capture-requests/{request_id}/start")
    async def start_capture_request(
        request_id: str,
        body: _LeaseBody,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return service.start(
            device(authorization),
            request_id,
            body.lease_token,
            current_time(),
        ).to_dict()

    @app.post("/api/v1/agent/capture-requests/{request_id}/heartbeat")
    async def heartbeat_capture_request(
        request_id: str,
        body: _LeaseBody,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Response:
        service.heartbeat(
            device(authorization),
            request_id,
            body.lease_token,
            current_time(),
            lease_seconds=CAPTURE_REQUEST_LEASE_SECONDS,
        )
        return Response(status_code=204)

    @app.post("/api/v1/agent/capture-requests/{request_id}/progress")
    async def record_capture_progress(
        request_id: str,
        body: _ProgressBody,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return service.record_progress(
            device(authorization),
            request_id,
            body.lease_token,
            body.code,
            current_time(),
        ).to_dict()

    @app.post(
        "/api/v1/agent/capture-requests/{request_id}/candidates"
    )
    async def accept_candidate_batch(
        request_id: str,
        body: _CandidateBatchBody,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        batch = CandidateBatchUpload.from_dict(body.batch)
        if batch.request_id != request_id:
            raise ValueError("Candidate batch request conflicts")
        return service.accept_candidate_batch(
            device(authorization),
            body.lease_token,
            batch,
            current_time(),
        ).to_dict()

    @app.post("/api/v1/agent/capture-requests/{request_id}/complete")
    async def complete_capture_request(
        request_id: str,
        body: _CompleteBody,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return service.complete(
            device(authorization),
            request_id,
            body.lease_token,
            body.batch_digest,
            current_time(),
        ).to_dict()

    @app.post("/api/v1/agent/capture-requests/{request_id}/fail")
    async def fail_capture_request(
        request_id: str,
        body: _FailBody,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        return service.fail(
            device(authorization),
            request_id,
            body.lease_token,
            body.code,
            body.retryable,
            current_time(),
        ).to_dict()

    if web_application is not None:

        @app.get("/{browser_path:path}", response_class=HTMLResponse)
        async def spa_fallback(browser_path: str) -> Response:
            if browser_path == "api" or browser_path.startswith("api/"):
                return JSONResponse(
                    status_code=404, content={"detail": "Not Found"}
                )
            path = selected_static_root / "index.html"
            return HTMLResponse(path.read_text("utf-8"))

    return app
