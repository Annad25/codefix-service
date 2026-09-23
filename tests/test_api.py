"""HTTP layer: health/readiness, contract shape, bad input, idempotent retries."""
from __future__ import annotations

import asyncio
import base64

import httpx

from app.api import Service, SolveBody, create_app
from app.config import Settings
from app.solver import SolveResult

from conftest import run


class CountingSolver:
    def __init__(self, delay=0.2):
        self.calls = 0
        self.delay = delay

    async def solve(self, req):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return SolveResult(req.request_id, "diff --git a/x b/x\n", [{"role": "assistant", "type": "text", "text": "ok"}],
                           {"estimated_cost_usd": 0.0}, {"tier": "A"})


def client_for(service):
    app = create_app(service=service)

    async def go(fn):
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                return await fn(c)
    return go


def body(rid="r1", archive=b"x"):
    return {"request_id": rid, "repo_archive_b64": base64.b64encode(archive).decode(), "task": "t",
            "deadline_seconds": 60, "some_future_field": True}


def test_health_ready_and_unconfigured(tmp_path):
    ready = Service(Settings(sandbox_backend="local"), CountingSolver(), None)
    r = run(client_for(ready)(lambda c: c.get("/health")))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    broken = Service(Settings(), None, "OPENROUTER_API_KEY is not set")
    r = run(client_for(broken)(lambda c: c.get("/health")))
    assert r.status_code == 503 and "OPENROUTER_API_KEY" in r.json()["detail"]
    assert run(client_for(broken)(lambda c: c.get("/livez"))).status_code == 200


def test_solve_contract_and_unknown_fields_tolerated():
    svc = Service(Settings(), CountingSolver(), None)
    r = run(client_for(svc)(lambda c: c.post("/solve", json=body())))
    assert r.status_code == 200
    assert set(r.json()) == {"request_id", "diff", "record", "usage"} and r.json()["request_id"] == "r1"


def test_bad_base64_and_unconfigured_service_answer_null_not_5xx():
    svc = Service(Settings(), CountingSolver(), None)
    bad = {**body(), "repo_archive_b64": "!!!not base64"}
    r = run(client_for(svc)(lambda c: c.post("/solve", json=bad)))
    assert r.status_code == 200 and r.json()["diff"] is None and svc.solver.calls == 0
    unconfigured = Service(Settings(), None, "no key")
    r = run(client_for(unconfigured)(lambda c: c.post("/solve", json=body())))
    assert r.status_code == 200 and r.json()["diff"] is None


def test_duplicate_request_ids_share_one_solve():
    svc = Service(Settings(), CountingSolver(delay=0.5), None)

    async def twice(c):
        return await asyncio.gather(c.post("/solve", json=body("same")), c.post("/solve", json=body("same")))
    a, b = run(client_for(svc)(twice))
    assert a.json() == b.json() and svc.solver.calls == 1
    run(client_for(svc)(lambda c: c.post("/solve", json=body("same"))))   # cached result, still one solve
    assert svc.solver.calls == 1


def test_reused_request_id_with_different_request_is_not_served_from_cache():
    svc = Service(Settings(), CountingSolver(), None)

    async def post_two(c):
        first = await c.post("/solve", json=body("same"))
        second = await c.post("/solve", json={**body("same"), "task": "a different task"})
        return first, second

    first, second = run(client_for(svc)(post_two))
    assert first.status_code == second.status_code == 200 and svc.solver.calls == 2


def test_health_does_not_block_while_solving():
    svc = Service(Settings(sandbox_backend="local"), CountingSolver(delay=2.0), None)

    async def concurrent(c):
        solve = asyncio.create_task(c.post("/solve", json=body("slow")))
        await asyncio.sleep(0.1)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        h = await c.get("/health")
        took = loop.time() - t0
        await solve
        return h, took
    h, took = run(client_for(svc)(concurrent))
    assert h.status_code == 200 and took < 1.0


def test_request_body_validation():
    assert SolveBody(**{**body(), "deadline_seconds": 5}).deadline_seconds == 5
