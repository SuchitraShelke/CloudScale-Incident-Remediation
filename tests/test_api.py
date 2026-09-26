"""HTTP API: login, RBAC, decisions bound to the session and the pending plan version."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from cloudscale.orchestrator.api import router
from cloudscale.orchestrator.auth import Auth
from tests.test_graph import build_env

USERS = "viewer:v:viewer,sre1:s:sre,lead1:l:senior_sre"


@pytest.fixture
def api(mocked_backends):
    env = build_env()
    app = FastAPI()
    app.include_router(router)
    app.state.auth = Auth("test-session-secret-at-least-32-bytes!", USERS)
    app.state.service = env["service"]
    app.state.audit = env["audit"]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api")
    return client, env


async def login(client, user, password):
    r = await client.post("/auth/login", json={"username": user, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


async def settle(env):
    await asyncio.gather(*env["service"].tasks)


async def start(client, env, headers, scenario):
    r = await client.post("/incidents", json={"scenario_id": scenario}, headers=headers)
    assert r.status_code == 202, r.text
    await settle(env)
    return r.json()["incident_id"]


async def test_wrong_password_and_missing_session(api):
    client, _ = api
    assert (await client.post("/auth/login", json={"username": "sre1", "password": "nope"})).status_code == 401
    assert (await client.get("/incidents")).status_code == 401
    assert (await client.get("/incidents", headers={"Authorization": "Bearer forged"})).status_code == 401


async def test_viewer_can_read_but_not_start_or_decide(api):
    client, env = api
    viewer, sre = await login(client, "viewer", "v"), await login(client, "sre1", "s")
    assert (await client.post("/incidents", json={"scenario_id": "s01-oom-orders"}, headers=viewer)).status_code == 403
    iid = await start(client, env, sre, "s01-oom-orders")
    ph = (await client.get(f"/incidents/{iid}", headers=viewer)).json()["pending_decision"]["plan_hash"]
    r = await client.post(f"/incidents/{iid}/decision", json={"outcome": "APPROVED", "plan_hash": ph}, headers=viewer)
    assert r.status_code == 403


async def test_sre_approves_from_queue_and_audit_names_the_session_user(api):
    client, env = api
    sre = await login(client, "sre1", "s")
    iid = await start(client, env, sre, "s01-oom-orders")
    queue = (await client.get("/hitl/queue", headers=sre)).json()
    assert [(q["incident_id"], q["gate"], q["can_decide"]) for q in queue] == [(iid, "APPROVAL", True)]

    ph = (await client.get(f"/incidents/{iid}", headers=sre)).json()["pending_decision"]["plan_hash"]
    body = {"outcome": "APPROVED", "plan_hash": ph, "approver": "lead1"}      # body approver is ignored
    assert (await client.post(f"/incidents/{iid}/decision", json=body, headers=sre)).status_code == 202
    await settle(env)

    v = (await client.get(f"/incidents/{iid}", headers=sre)).json()
    assert v["status"] == "RESOLVED" and v["decision"]["approver"] == "sre1"
    decision_rows = [r for r in env["audit"].rows if r["event_type"] == "HITL_DECISION"]
    assert [(r["actor_type"], r["actor_id"]) for r in decision_rows] == [("human", "sre1")]
    assert (await client.get("/hitl/queue", headers=sre)).json() == []


async def test_sre_cannot_approve_escalation_but_senior_can(api):
    client, env = api
    sre, lead = await login(client, "sre1", "s"), await login(client, "lead1", "l")
    iid = await start(client, env, sre, "s03-payment-pool")
    ph = (await client.get(f"/incidents/{iid}", headers=sre)).json()["pending_decision"]["plan_hash"]
    queue = (await client.get("/hitl/queue", headers=sre)).json()
    assert queue[0]["gate"] == "ESCALATION" and queue[0]["can_decide"] is False
    r = await client.post(f"/incidents/{iid}/decision", json={"outcome": "APPROVED", "plan_hash": ph}, headers=sre)
    assert r.status_code == 403
    r = await client.post(f"/incidents/{iid}/decision", json={"outcome": "APPROVED", "plan_hash": ph}, headers=lead)
    assert r.status_code == 202
    await settle(env)
    assert (await client.get(f"/incidents/{iid}", headers=lead)).json()["status"] == "RESOLVED"


async def test_stale_plan_hash_and_no_pending_decision_rejected(api):
    client, env = api
    sre = await login(client, "sre1", "s")
    iid = await start(client, env, sre, "s01-oom-orders")
    r = await client.post(f"/incidents/{iid}/decision", json={"outcome": "APPROVED", "plan_hash": "a" * 64},
                          headers=sre)
    assert r.status_code == 409
    done = await start(client, env, sre, "s02-cache-bloat")
    r = await client.post(f"/incidents/{done}/decision", json={"outcome": "APPROVED", "plan_hash": "a" * 64},
                          headers=sre)
    assert r.status_code == 409
