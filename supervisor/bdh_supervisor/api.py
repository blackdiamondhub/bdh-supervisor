"""The supervisor's HTTP API (served on a unix socket shared only with the hub).

Every request needs ``Authorization: Bearer <BDH_SUPERVISOR_TOKEN>``. The hub
names apps by id and version; image, mounts, capabilities and networking come
from the platform templates, never from the request.
"""
import asyncio
import hmac
import json
import re
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from . import attest
from .catalog import Catalog
from .driver import DriverError
from .manager import ConflictError, Manager, NotFoundError
from .models import MAX_SLOT, NETWORKS, TOKEN_RE, valid_app_id, valid_version

TZ_RE = re.compile(r"^[A-Za-z0-9_+\-/]{1,64}$")
MAX_BRAND_BYTES = 4096
MAX_LOG_LINES = 1000


class InstallRequest(BaseModel):
    model_config = {"extra": "forbid"}

    version: str
    token: str = Field(description="The app's broker token (64 hex chars).")
    slot: int = Field(ge=0, le=MAX_SLOT, description="The app's hub port minus APP_PORT_BASE.")
    network: Literal[NETWORKS] = "none"
    allow_lan: bool = False
    time_zone: str = "UTC"
    hub_brand: dict = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _version(cls, v):
        if not valid_version(v):
            raise ValueError("bad version")
        return v

    @field_validator("token")
    @classmethod
    def _token(cls, v):
        if not TOKEN_RE.match(v):
            raise ValueError("token must be 64 lowercase hex characters")
        return v

    @field_validator("time_zone")
    @classmethod
    def _tz(cls, v):
        if not TZ_RE.match(v):
            raise ValueError("bad time zone")
        return v

    @field_validator("hub_brand")
    @classmethod
    def _brand(cls, v):
        if len(json.dumps(v)) > MAX_BRAND_BYTES:
            raise ValueError("hub_brand is too large")
        return v


def create_app(cfg, driver, catalog=None, verifier=None, run_background=True):
    catalog = catalog or Catalog(cfg.catalog_url, cfg.accepted_prefixes, cfg.allow_http)
    manager = Manager(cfg, driver, catalog, verifier or attest.from_config(cfg))
    expected = f"Bearer {cfg.token}".encode()

    def auth(request: Request):
        got = request.headers.get("authorization", "").encode()
        if not cfg.token or not hmac.compare_digest(got, expected):
            raise HTTPException(401, "unauthorized")

    def app_id_param(app_id: str):
        if not valid_app_id(app_id):
            raise HTTPException(422, "bad app id")
        return app_id

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(manager.run_forever()) if run_background else None
        try:
            yield
        finally:
            if task:
                task.cancel()

    app = FastAPI(title="bdh-supervisor", lifespan=lifespan, dependencies=[Depends(auth)],
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager

    @app.exception_handler(NotFoundError)
    async def _not_found(_request, exc):
        return JSONResponse({"detail": f"unknown app {exc}"}, status_code=404)

    @app.exception_handler(ConflictError)
    async def _conflict(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(DriverError)
    async def _driver(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "prepared": manager.prepared, "error": manager.prepare_error,
                "firewall": manager.prepare_info.get("firewall"), "hub_api": cfg.hub_api,
                "attestation": cfg.attestation, "last_catalog_poll": manager.last_poll,
                "apps": len(manager.apps)}

    @app.get("/apps")
    async def list_apps():
        return {"apps": [manager.view(rec) for rec in manager.apps.values()]}

    @app.get("/apps/{app_id}")
    async def get_app(app_id: str = Depends(app_id_param)):
        return await manager.status(app_id)

    @app.put("/apps/{app_id}", status_code=202)
    async def put_app(body: InstallRequest, app_id: str = Depends(app_id_param)):
        return await manager.put(app_id, body.model_dump())

    @app.delete("/apps/{app_id}")
    async def delete_app(app_id: str = Depends(app_id_param), purge_data: bool = False):
        await manager.delete(app_id, purge_data)
        return {"deleted": app_id, "purged_data": purge_data}

    @app.post("/apps/{app_id}/start")
    async def start(app_id: str = Depends(app_id_param), wait: bool = True):
        return await manager.start(app_id, wait=wait)

    @app.post("/apps/{app_id}/stop")
    async def stop(app_id: str = Depends(app_id_param)):
        return await manager.stop(app_id)

    @app.post("/apps/{app_id}/restart")
    async def restart(app_id: str = Depends(app_id_param), wait: bool = True):
        return await manager.start(app_id, wait=wait)

    @app.post("/apps/{app_id}/activity")
    async def activity(app_id: str = Depends(app_id_param)):
        return await manager.activity(app_id)

    @app.get("/apps/{app_id}/health")
    async def health(app_id: str = Depends(app_id_param)):
        return await manager.health(app_id)

    @app.get("/apps/{app_id}/logs", response_class=PlainTextResponse)
    async def logs(app_id: str = Depends(app_id_param), lines: int = Query(200, ge=1, le=MAX_LOG_LINES)):
        manager.get(app_id)
        return await asyncio.to_thread(driver.logs, app_id, lines)

    @app.get("/apps/{app_id}/stats")
    async def stats(app_id: str = Depends(app_id_param)):
        manager.get(app_id)
        return {"app_id": app_id, **await asyncio.to_thread(driver.stats, app_id)}

    @app.post("/apps/{app_id}/rollback")
    async def rollback(app_id: str = Depends(app_id_param)):
        return await manager.rollback(app_id)

    @app.post("/catalog/refresh")
    async def refresh():
        return await manager.poll_catalog()

    return app
