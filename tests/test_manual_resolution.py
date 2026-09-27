"""Record how a human fixed an escalated incident: re-verified, and only a verified fix drafts a runbook."""

import time

from tests.test_api import api, login, settle, start  # noqa: F401  (api is a fixture)

HEALTHY = {"memory_usage_pct": 40.0, "error_rate_pct": 0.2, "request_latency_p99_ms": 180.0, "cpu_usage_pct": 30.0}
FIX = {"how_fixed": "Raised the memory limit to 2Gi and restarted the pods", "root_cause_category": "memory_leak"}


async def escalated(client, env, headers) -> str:
    iid = await start(client, env, headers, "s01-oom-orders")
    svc = env["service"]
    await svc.sweep_deadlines(now=time.time() + 601)
    await settle(env)
    await svc.sweep_deadlines(now=time.time() + 3600)
    await settle(env)
    assert (await svc.get(iid))["status"] == "ESCALATED"
    return iid


async def test_unverified_fix_is_recorded_but_proposes_nothing(api):  # noqa: F811
    client, env = api
    h = await login(client, "sre1", "s")
    iid = await escalated(client, env, h)
    async def still_broken(inc):
        return {**HEALTHY, "memory_usage_pct": 97.0}
    env["service"].read_metrics = still_broken
    r = await client.post(f"/incidents/{iid}/resolution", json=FIX, headers=h)
    assert r.status_code == 200 and r.json()["verified"] is False and r.json()["proposal"] is None
    assert (await client.get("/runbooks/proposals", headers=h)).json() == []
    types = [x["event_type"] for x in env["audit"].rows]
    assert "MANUAL_RESOLUTION_RECORDED" in types and "RUNBOOK_PROPOSED" not in types


async def test_verified_fix_drafts_a_runbook_proposal(api):  # noqa: F811
    client, env = api
    h = await login(client, "sre1", "s")
    iid = await escalated(client, env, h)
    ho = (await client.get(f"/incidents/{iid}/handoff", headers=h)).json()
    assert "Nothing was changed." in ho["markdown"] and ho["timed_out"] is True
    async def healthy(inc):
        return HEALTHY
    env["service"].read_metrics = healthy
    body = (await client.post(f"/incidents/{iid}/resolution", json=FIX, headers=h)).json()
    p = body["proposal"]
    assert body["verified"] and p["id"] == f"RB-PROPOSED-{iid}" and p["root_cause_category"] == "memory_leak"
    assert 1 <= len(p["signatures"]) <= 2 and p["corroborate"]["metric"] in HEALTHY
    assert (await client.get("/runbooks/proposals", headers=h)).json()[0]["id"] == p["id"]
    assert "RUNBOOK_PROPOSED" in [x["event_type"] for x in env["audit"].rows]


async def test_viewer_cannot_record_and_only_escalated_incidents_accept_a_fix(api):  # noqa: F811
    client, env = api
    sre, viewer = await login(client, "sre1", "s"), await login(client, "viewer", "v")
    iid = await start(client, env, sre, "s01-oom-orders")                        # still AWAITING_APPROVAL
    assert (await client.post(f"/incidents/{iid}/resolution", json=FIX, headers=viewer)).status_code == 403
    assert (await client.post(f"/incidents/{iid}/resolution", json=FIX, headers=sre)).status_code == 409
    assert (await client.get(f"/incidents/{iid}/handoff", headers=sre)).status_code == 404
