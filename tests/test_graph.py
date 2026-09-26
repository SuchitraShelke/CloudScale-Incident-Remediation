"""Every scenario through the real graph + real in-process MCP server + simulator.
OPA is mocked allow-all here (its rules are covered by `opa test` and scripts/mcp_smoke.py);
Ollama is mocked as unreachable, which is this dev host's reality."""

import asyncio
from pathlib import Path

import fakeredis.aioredis
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from langgraph.checkpoint.memory import InMemorySaver

from cloudscale.common.gate import load_policy
from cloudscale.common.safety.llama_guard import Guard
from cloudscale.common.tokens import TokenIssuer, TokenVerifier
from cloudscale.mcp_server.enforcement import Enforcer
from cloudscale.mcp_server.server import build_server
from cloudscale.mcp_server.simulator import Simulator
from cloudscale.orchestrator.graph import Deps, build_graph
from cloudscale.orchestrator.llm.scripted import ScriptedLLM
from cloudscale.orchestrator.service import IncidentService
from cloudscale.orchestrator.store import MemoryIncidentStore
from cloudscale.orchestrator.tools import ToolClient
from tests.conftest import OLLAMA, OPA


class MemoryAudit:
    def __init__(self):
        self.rows: list[dict] = []

    async def append(self, event_type, actor_type, actor_id, payload, incident_id=None):
        row = {"event_type": event_type, "actor_type": actor_type, "actor_id": actor_id, "payload": payload,
               "incident_id": incident_id}
        self.rows.append(row)
        return row


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def build_env(llm=None, cache=None, breaker_threshold=3) -> dict:
    key = Ed25519PrivateKey.generate()
    clock = Clock()
    sim = Simulator(Path("mock-data/scenarios"), clock=clock)
    redis = fakeredis.aioredis.FakeRedis()
    enforcer = Enforcer(TokenVerifier(key.public_key()), redis, OPA, load_policy()["op_classes"])

    async def fake_sleep(seconds: float) -> None:
        clock.t += seconds

    async def register(incident_id, scenario_id):
        sim.register(incident_id, scenario_id)

    deps = Deps(llm=llm or ScriptedLLM(), tools=ToolClient(build_server(enforcer, sim, "k"), redis, breaker_threshold, retry_wait_s=0.01),
                issuer=TokenIssuer(key), guard=Guard(OLLAMA, "llama-guard3:1b", 1.0), sleep=fake_sleep,
                cache=cache)
    audit = MemoryAudit()
    service = IncidentService(build_graph(deps, InMemorySaver()), register, Path("mock-data/scenarios"),
                              MemoryIncidentStore(), audit)
    return {"service": service, "sim": sim, "enforcer": enforcer, "audit": audit, "redis": redis}


@pytest.fixture
def env(mocked_backends):
    return build_env()


def approve(view, role="sre", approver="sre1", outcome="APPROVED"):
    return {"decision_id": "d-1", "outcome": outcome, "plan_hash": view["pending_decision"]["plan_hash"],
            "gate": view["pending_decision"]["gate"], "approver": approver, "role": role}


async def run(env, scenario):
    svc = env["service"]
    iid = await svc.start(scenario, wait=True)
    return iid, await svc.get(iid)


async def test_s02_cache_bloat_runs_auto_to_resolved(env):
    iid, v = await run(env, "s02-cache-bloat")
    assert v["status"] == "RESOLVED", v.get("error")
    assert v["gate"]["gate"] == "AUTO" and v["decision"]["approver"] == "system"
    assert v["triage"]["confidence"] == pytest.approx(0.88)          # min(LLM 0.88, evidence 0.90)
    assert [r["tool"] for r in v["results"]] == ["clear_pod_cache"]
    assert v["verification"]["healthy"] and v["verification"]["waited_s"] > 0
    nodes = [e["node"] for e in v["events"]]
    assert nodes == ["api", "intake", "triage", "planner", "validate_plan", "hitl_gate", "execute", "evaluate",
                     "close"]
    assert [a["tool"] for a in env["sim"].get(iid).actions] == ["clear_pod_cache"]


async def test_s01_oom_waits_for_approval_then_resolves(env):
    iid, v = await run(env, "s01-oom-orders")
    assert v["status"] == "AWAITING_APPROVAL"
    assert v["pending_decision"]["required_role"] == "sre"
    assert env["sim"].get(iid).actions == []                         # nothing ran before approval

    await env["service"].resume(iid, approve(v), wait=True)
    v = await env["service"].get(iid)
    assert v["status"] == "RESOLVED", v.get("error")
    assert [r["tool"] for r in v["results"]] == ["apply_hotfix", "restart_service"]
    dep = env["sim"].get(iid).deployments["orders-service"]
    assert dep["resources"]["limits"]["memory"] == "2Gi"


async def test_viewer_role_cannot_approve(env):
    iid, v = await run(env, "s01-oom-orders")
    await env["service"].resume(iid, approve(v, role="viewer"), wait=True)
    v = await env["service"].get(iid)
    assert v["status"] == "AWAITING_APPROVAL" and env["sim"].get(iid).actions == []


async def test_reject_closes_without_changes(env):
    iid, v = await run(env, "s01-oom-orders")
    await env["service"].resume(iid, approve(v, outcome="REJECTED"), wait=True)
    v = await env["service"].get(iid)
    assert v["status"] == "REJECTED" and env["sim"].get(iid).actions == []


async def test_stale_plan_hash_escalates(env):
    iid, v = await run(env, "s01-oom-orders")
    await env["service"].resume(iid, {**approve(v), "plan_hash": "0" * 64}, wait=True)
    assert (await env["service"].get(iid))["status"] == "ESCALATED"


async def test_sre_escalates_then_senior_approves(env):
    iid, v = await run(env, "s01-oom-orders")
    await env["service"].resume(iid, approve(v, outcome="ESCALATE"), wait=True)
    v = await env["service"].get(iid)
    assert v["status"] == "AWAITING_ESCALATION"
    await env["service"].resume(iid, approve(v), wait=True)             # plain sre can't approve ESCALATION
    assert (await env["service"].get(iid))["status"] == "AWAITING_ESCALATION"
    await env["service"].resume(iid, approve(v, role="senior_sre", approver="lead1"), wait=True)
    assert (await env["service"].get(iid))["status"] == "RESOLVED"


async def test_s03_payment_hotfix_escalates_on_financial_impact(env):
    _, v = await run(env, "s03-payment-pool")
    assert v["status"] == "AWAITING_ESCALATION"
    step = v["pending_decision"]["steps"][0]
    assert step["impact_usd"] == 60000 and "impact_usd>50000" in step["reasons"]
    assert v["pending_decision"]["required_role"] == "senior_sre"


async def test_s04_az_partition_escalates_and_hands_off(env):
    iid, v = await run(env, "s04-az-partition")
    assert v["status"] == "AWAITING_ESCALATION"
    await env["service"].resume(iid, approve(v, role="senior_sre", approver="lead1"), wait=True)
    v = await env["service"].get(iid)
    assert v["status"] == "ESCALATED"
    assert v["results"] == [{"step_id": "az-failover", "status": "HANDED_OFF", "runbook_ref": "RB-AZ-FAILOVER"}]


async def test_s05_direct_injection_quarantined_before_any_tool_call(env):
    _, v = await run(env, "s05-direct-injection")
    assert v["status"] == "QUARANTINED" and v["guard"]["source"] == "heuristic"
    assert "triage" not in v and env["enforcer"].decisions == []


async def test_s06_indirect_injection_in_fetched_logs_quarantined(env):
    iid, v = await run(env, "s06-indirect-injection")
    assert v["status"] == "QUARANTINED"
    assert "exfil_verb" in v["guard"]["heuristics"] and v["guard"]["degraded"]   # model down -> fail closed
    assert "plan" not in v and env["sim"].get(iid).actions == []


async def test_scenario_answers_never_reach_the_llm(mocked_backends):
    seen: list[str] = []

    class Spy(ScriptedLLM):
        async def triage(self, ctx):
            seen.append(repr(ctx))
            return await super().triage(ctx)

        async def plan(self, ctx):
            seen.append(repr(ctx))
            return await super().plan(ctx)

    env = build_env(Spy())
    for scenario in ("s01-oom-orders", "s02-cache-bloat", "s03-payment-pool", "s04-az-partition"):
        await env["service"].start(scenario, wait=True)
    blob = "\n".join(seen)
    assert len(seen) == 8
    for leaked in ("recover_to", "healthy_thresholds", "ground_truth", "expected_gate", "effects"):
        assert leaked not in blob


async def test_audit_trail_covers_decisions_tool_calls_and_state_transitions(env):
    iid, v = await run(env, "s01-oom-orders")
    await env["service"].resume(iid, approve(v), wait=True)
    types = [r["event_type"] for r in env["audit"].rows if r["incident_id"] == iid]
    assert types[0] == "INCIDENT_RECEIVED" and types[-1] == "INCIDENT_CLOSED"
    for expected in ("GUARD_CHECK", "AGENT_DECISION", "PLAN_PROPOSED", "PLAN_VALIDATED", "HITL_REQUEST",
                     "HITL_DECISION_APPLIED", "TOOL_EXECUTION", "VERIFICATION"):
        assert expected in types
    assert types.count("HITL_REQUEST") == 1            # gate re-run on resume is not audited twice
    actors = {(r["event_type"], r["actor_id"]) for r in env["audit"].rows}
    assert ("PLAN_PROPOSED", "planner") in actors and ("TOOL_EXECUTION", "executor") in actors


async def test_restart_sweeper_resumes_an_interrupted_run(env):
    """Simulate a crash after triage: the checkpoint says `planner` is next; the sweeper continues it."""
    svc = env["service"]
    iid = "INC-S02-crash1"
    env["sim"].register(iid, "s02-cache-bloat")
    await svc.store.add(iid, "s02-cache-bloat")
    from cloudscale.common.scenarios import load_scenario
    inc = load_scenario("s02-cache-bloat").incident.model_copy(update={"incident_id": iid}).model_dump()
    async for _ in svc.graph.astream({"incident": inc, "status": "RECEIVED", "events": []},
                                     svc._config(iid), stream_mode="updates", interrupt_after=["triage"]):
        pass
    assert (await svc.get(iid))["status"] == "PLANNING"
    assert await svc.resume_in_flight() == [iid]
    await asyncio.gather(*svc.tasks)
    assert (await svc.get(iid))["status"] == "RESOLVED"
