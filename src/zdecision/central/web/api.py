"""Transport-only routes for the Central Decision Web."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from zdecision.capture.models import CandidateContent
from zdecision.central.web.application import CentralWebApplication
from zdecision.central.web.contracts import DraftItem


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _DraftItemBody(_StrictBody):
    family_id: str
    repository_id: str
    revision_id: str
    revision: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    action: Literal["accept", "edit_accept", "reject", "skip"]
    effective_content: dict[str, object] | None = None
    note: str | None = Field(default=None, max_length=1000)

    def to_contract(self) -> DraftItem:
        return DraftItem(
            family_id=self.family_id,
            repository_id=self.repository_id,
            revision_id=self.revision_id,
            revision=self.revision,
            content_digest=self.content_digest,
            action=self.action,
            effective_content=(
                CandidateContent.from_dict(self.effective_content)
                if self.effective_content is not None
                else None
            ),
            note=self.note,
        )


class _SaveDraftBody(_StrictBody):
    expected_version: int = Field(ge=0)
    items: list[_DraftItemBody] = Field(max_length=100)


router = APIRouter(prefix="/api/v1/web")


def _application(request: Request) -> CentralWebApplication:
    application = request.app.state.web_application
    if not isinstance(application, CentralWebApplication):
        raise RuntimeError("Central Web application is not configured")
    return application


@router.get("/dashboard")
async def dashboard(request: Request) -> dict[str, object]:
    identity_provider = request.app.state.identity_provider
    return _application(request).dashboard(
        identity_provider.browser_principal()
    ).to_dict()


@router.get("/products/{product_id}/candidates")
async def candidates(
    request: Request,
    product_id: str,
    search: str = Query(default="", max_length=200),
    repository_id: str | None = None,
    capture_request_id: str | None = None,
    state: Literal[
        "pending", "accepted", "rejected", "published", "all"
    ] = "pending",
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    identity_provider = request.app.state.identity_provider
    return _application(request).list_candidates(
        identity_provider.browser_principal(),
        product_id,
        search=search,
        repository_id=repository_id,
        capture_request_id=capture_request_id,
        state=state,
        limit=limit,
        offset=offset,
    ).to_dict()


@router.get("/products/{product_id}/review-draft")
async def get_review_draft(
    request: Request, product_id: str
) -> dict[str, object]:
    identity_provider = request.app.state.identity_provider
    return _application(request).get_review_draft(
        identity_provider.browser_principal(), product_id
    ).to_dict()


@router.put("/products/{product_id}/review-draft")
async def save_review_draft(
    request: Request, product_id: str, body: _SaveDraftBody
) -> dict[str, object]:
    identity_provider = request.app.state.identity_provider
    return _application(request).save_review_draft(
        identity_provider.browser_principal(),
        product_id,
        body.expected_version,
        tuple(item.to_contract() for item in body.items),
        request.app.state.current_time(),
    ).to_dict()
