"""HTTP API: POST /solve, GET /health (readiness), GET /livez (liveness).

/solve always answers 200 with the contract body before the deadline; a bad
archive or any internal failure yields "diff": null with the reason in the
record, never a 5xx. Duplicate request_ids (client retries) attach to the
in-flight work instead of starting it again.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import ConfigError, Settings
from .llm import LLMClient
from .procs import run_process
from .sandbox import make_sandbox
from .solver import SolveRequest, SolveResult, Solver
from .workspace import UnsafeArchiveError, decode_archive

log = logging.getLogger("codefix.api")
HEALTH_TTL_S = 10.0
RESULT_CACHE_SIZE = 256
CacheKey = tuple[str, str]


class SolveBody(BaseModel):
    model_config = ConfigDict(extra="ignore")          # tolerant reader: new client fields don't break us
    request_id: str = Field(min_length=1, max_length=256)
    repo_archive_b64: str = Field(max_length=16 * 1024 * 1024)
    task: str = Field(max_length=200_000)
    deadline_seconds: float = Field(gt=0, le=3600)


class Service:
    def __init__(self, settings: Settings, solver: Solver | None, config_error: str | None) -> None:
        self.settings = settings
        self.solver = solver
        self.config_error = config_error
        self.inflight: dict[CacheKey, asyncio.Task] = {}
        self.results: OrderedDict[CacheKey, dict[str, Any]] = OrderedDict()
        self._health: tuple[float, bool, str] = (0.0, False, "not checked")

    async def readiness(self) -> tuple[bool, str]:
        checked_at, ok, reason = self._health
        if time.monotonic() - checked_at < HEALTH_TTL_S:
            return ok, reason
        ok, reason = await self._check_ready()
        self._health = (time.monotonic(), ok, reason)
        return ok, reason

    async def _check_ready(self) -> tuple[bool, str]:
        if self.config_error:
            return False, self.config_error
        if self.settings.sandbox_backend == "docker":
            r = await run_process(["docker", "image", "inspect", self.settings.sandbox_image,
                                   "--format", "{{.Id}}"], timeout=10)
            if not r.ok:
                return False, f"sandbox image {self.settings.sandbox_image} not available"
        elif shutil.which("git") is None:
            return False, "git not found"
        return True, (f"ready (sandbox={self.settings.sandbox_backend}, "
                      f"provider={self.settings.llm_provider}, model={self.settings.model_primary})")

    async def solve(self, body: SolveBody) -> dict[str, Any]:
        key = _cache_key(body)
        if key in self.results:
            return self.results[key]
        task = self.inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._solve(body, time.monotonic()))
            self.inflight[key] = task
            task.add_done_callback(lambda _t, cache_key=key: self.inflight.pop(cache_key, None))
        response = await asyncio.shield(task)
        return response

    async def _solve(self, body: SolveBody, received_at: float) -> dict[str, Any]:
        if self.solver is None:
            return _null(body.request_id, f"service not configured: {self.config_error}")
        try:
            archive = decode_archive(body.repo_archive_b64)
        except UnsafeArchiveError as exc:
            return _null(body.request_id, str(exc))
        result: SolveResult = await self.solver.solve(
            SolveRequest(request_id=body.request_id, archive=archive, task=body.task,
                         deadline_seconds=body.deadline_seconds, received_at=received_at))
        log.info("solved request_id=%s tier=%s elapsed=%.1fs cost=$%.4f", body.request_id,
                 result.meta.get("tier"), result.meta.get("elapsed_s", 0),
                 result.usage.get("estimated_cost_usd", 0))
        response = result.response()
        self.results[_cache_key(body)] = response
        while len(self.results) > RESULT_CACHE_SIZE:
            self.results.popitem(last=False)
        return response


def _null(request_id: str, reason: str) -> dict[str, Any]:
    return {"request_id": request_id, "diff": None,
            "record": [{"role": "assistant", "type": "text", "text": reason}], "usage": {}}


def _cache_key(body: SolveBody) -> CacheKey:
    """Idempotency is scoped to a complete request, not an arbitrary caller ID."""
    digest = hashlib.sha256()
    for value in (body.repo_archive_b64, body.task, repr(body.deadline_seconds)):
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    return body.request_id, digest.hexdigest()


def build_service(settings: Settings) -> Service:
    try:
        llm = LLMClient(settings)
    except ConfigError as exc:
        log.error("LLM not configured: %s", exc)
        return Service(settings, None, str(exc))
    return Service(settings, Solver(settings, llm, make_sandbox(settings)), None)


def create_app(settings: Settings | None = None, service: Service | None = None) -> FastAPI:
    holder: dict[str, Service] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        holder["svc"] = service or build_service(settings or Settings.from_env())
        yield

    app = FastAPI(title="codefix-service", lifespan=lifespan)

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "alive"}

    @app.get("/health")
    async def health():
        ok, reason = await holder["svc"].readiness()
        return JSONResponse({"status": "ok" if ok else "unavailable", "detail": reason},
                            status_code=200 if ok else 503)

    @app.post("/solve")
    async def solve(body: SolveBody) -> dict[str, Any]:
        return await holder["svc"].solve(body)

    return app
