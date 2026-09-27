"""HITL timeout ladder + escalation hand-off. The timer escalates and expires; it never approves."""

import asyncio
import time

from tests.test_api import REPORT
from tests.test_graph import approve, build_env

HOUR = 3600


async def settle(svc):
    await asyncio.gather(*svc.tasks)


async def test_unanswered_approval_is_promoted_to_the_senior_queue(mocked_backends):
    env = build_env()
    svc = env["service"]
    iid = await svc.start("s01-oom-orders", wait=True)
    assert await svc.sweep_deadlines(now=time.time() + 60) == []                 # not yet
    assert await svc.sweep_deadlines(now=time.time() + 601) == [(iid, "ESCALATE")]
    await settle(svc)
    v = await svc.get(iid)
    assert v["status"] == "AWAITING_ESCALATION" and env["sim"].get(iid).actions == []
    promoted = [r for r in env["audit"].rows if r["event_type"] == "HITL_PROMOTED"]
    assert [(r["actor_type"], r["actor_id"]) for r in promoted] == [("system", "system:hitl-timer")]


async def test_unanswered_escalation_expires_without_changing_anything(mocked_backends):
    env = build_env()
    svc = env["service"]
    iid = await svc.start("s01-oom-orders", wait=True)
    await svc.sweep_deadlines(now=time.time() + 601)
    await settle(svc)
    assert await svc.sweep_deadlines(now=time.time() + HOUR) == [(iid, "EXPIRE")]
    await settle(svc)
    v = await svc.get(iid)
    assert v["status"] == "ESCALATED" and v["timed_out"] is True
    assert env["sim"].get(iid).actions == []                                     # the hotfix never ran
    assert v["handoff"]["what_changed"] == "Nothing was changed." and v["handoff"]["timed_out"] is True
    assert v["handoff"]["diagnosis"] and "RB-" not in (v["handoff"]["proposed_steps"] or [""])[0]
    assert not any(e.get("type") == "CACHE_WRITTEN" for e in v["events"])


async def test_expired_incident_that_recovered_by_itself_closes_as_resolved(mocked_backends):
    env = build_env()
    svc = env["service"]
    healthy = {**REPORT["telemetry"], "memory_usage_pct": 40, "error_rate_pct": 0.2, "request_latency_p99_ms": 200}
    from cloudscale.common.schemas import Incident
    inc = Incident.model_validate({**REPORT, "telemetry": healthy, "cloud_region": "us-east-1",
                                   "incident_id": "INC-C-x"}).model_dump()
    iid = await svc.start_custom(inc, started_by="sre1", wait=True)
    assert (await svc.get(iid))["status"] == "AWAITING_ESCALATION"               # unknown pattern -> senior
    await svc.sweep_deadlines(now=time.time() + HOUR)
    await settle(svc)
    v = await svc.get(iid)
    assert v["status"] == "RESOLVED" and v["summary"].startswith("Recovered without action")
    assert env["sim"].get(iid).actions == []


async def test_a_human_decision_before_the_deadline_wins(mocked_backends):
    env = build_env()
    svc = env["service"]
    iid = await svc.start("s01-oom-orders", wait=True)
    await svc.resume(iid, approve(await svc.get(iid)), wait=True)
    assert (await svc.get(iid))["status"] == "RESOLVED"
    assert await svc.sweep_deadlines(now=time.time() + HOUR) == []               # nothing left to time out


async def test_queue_rows_show_the_timer(mocked_backends):
    env = build_env()
    iid = await env["service"].start("s01-oom-orders", wait=True)
    row = next(r for r in await env["service"].list() if r["incident_id"] == iid)
    assert row["timer_action"] == "escalates" and 590 <= row["timer_in_s"] <= 600 and row["overdue"] is False


async def test_handoff_after_a_failed_step_reports_the_rollback(mocked_backends):
    env = build_env(breaker_threshold=1)
    svc = env["service"]
    iid = await svc.start("s01-oom-orders", wait=True)
    env["sim"].inject_fault("restart_service", 1)
    await svc.resume(iid, approve(await svc.get(iid)), wait=True)
    h = (await svc.get(iid))["handoff"]
    assert "rollback" in h["what_changed"] and h["gate"] == "APPROVAL"


async def test_expiry_after_the_simulator_lost_the_incident_escalates_and_spares_the_breaker(mocked_backends):
    # Found live: an old incident whose simulator state was lost on restart expired, its metric re-check
    # failed, and that opened the get_metrics breaker shared by every other incident.
    env = build_env(breaker_threshold=1)
    svc = env["service"]
    iid = await svc.start("s01-oom-orders", wait=True)
    await svc.sweep_deadlines(now=time.time() + 601)
    await settle(svc)
    del env["sim"].incidents[iid]
    await svc.sweep_deadlines(now=time.time() + HOUR)
    await settle(svc)
    v = await svc.get(iid)
    assert v["status"] == "ESCALATED" and "couldn't be re-checked" in v["summary"]
    assert v["handoff"]["what_changed"] == "Nothing was changed."
    other = await svc.start("s02-cache-bloat", wait=True)                       # breaker still closed
    assert (await svc.get(other))["status"] == "RESOLVED"


async def test_verification_without_metrics_hands_off_what_ran_instead_of_failing(mocked_backends):
    env = build_env(breaker_threshold=1)
    svc = env["service"]
    iid = await svc.start("s01-oom-orders", wait=True)
    env["sim"].inject_fault("get_metrics", 10)
    await svc.resume(iid, approve(await svc.get(iid)), wait=True)
    v = await svc.get(iid)
    assert v["status"] == "ESCALATED" and v["verification"]["healthy"] is False
    assert "Verification couldn't read metrics" in v["summary"]
    assert "ran" in v["handoff"]["what_changed"] and env["sim"].get(iid).actions


async def test_only_one_decision_can_resume_a_gate(mocked_backends):
    # A resumed run can queue behind the concurrency limit while the checkpoint still shows the gate as
    # pending. A second resume in that window (timer, or another human) must be refused, not run twice.
    env = build_env()
    svc = env["service"]
    iid = await svc.start("s01-oom-orders", wait=True)
    first = approve(await svc.get(iid))
    await svc.resume(iid, first)                                                # launched, not awaited
    import pytest

    from cloudscale.orchestrator.service import DecisionInFlight
    with pytest.raises(DecisionInFlight):
        await svc.resume(iid, {**first, "decision_id": "second", "outcome": "REJECTED"})
    assert await svc.sweep_deadlines(now=time.time() + HOUR) == []             # timer backs off too
    await settle(svc)
    v = await svc.get(iid)
    assert v["status"] == "RESOLVED" and v["decision"]["decision_id"] == first["decision_id"]
