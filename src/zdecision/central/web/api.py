"""Transport-only routes for the Central Decision Web."""

from __future__ import annotations

from fastapi import APIRouter, Request

from zdecision.central.web.application import CentralWebApplication


router = APIRouter(prefix="/api/v1/web")


@router.get("/dashboard")
async def dashboard(request: Request) -> dict[str, object]:
    application = request.app.state.web_application
    identity_provider = request.app.state.identity_provider
    if not isinstance(application, CentralWebApplication):
        raise RuntimeError("Central Web application is not configured")
    return application.dashboard(identity_provider.browser_principal()).to_dict()
