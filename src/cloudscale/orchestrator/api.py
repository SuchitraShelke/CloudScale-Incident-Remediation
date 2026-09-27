"""HTTP API. Every route except health needs a session; decisions need sre / senior_sre."""

import uuid
from pathlib import Path
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from cloudscale.common.config import get_settings
from cloudscale.common.scenarios import list_scenarios, load_scenario
from cloudscale.orchestrator.auth import User, current_user, require

router = APIRouter()
DECIDERS = {"APPROVAL": {"sre", "senior_sre"}, "ESCALATION": {"senior_sre"}}
CAN_ACT = require("sre", "senior_sre")        # start incidents, make decisions


class Login(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
async def login(body: Login, request: Request) -> dict:
    result = request.app.state.auth.login(body.username, body.password)
    if result is None:
        raise HTTPException(401, "Wrong username or password.")
    token, user = result
    return {"token": token, "user": user.model_dump()}


@router.get("/me")
async def me(user: User = Depends(current_user)) -> dict:
    return user.model_dump()


@router.get("/scenarios")
async def scenarios(_: User = Depends(current_user)) -> list[dict]:
    d = Path(get_settings().scenarios_dir)
    return [{"scenario_id": sid, "title": (sc := load_scenario(sid, d)).incident.title,
             "description": sc.description} for sid in list_scenarios(d)]


class StartIncident(BaseModel):
    scenario_id: str


@router.post("/incidents", status_code=202)
async def start_incident(body: StartIncident, request: Request,
                         user: User = Depends(CAN_ACT)) -> dict:
    if body.scenario_id not in list_scenarios(Path(get_settings().scenarios_dir)):
        raise HTTPException(404, f"Unknown scenario {body.scenario_id!r}. See GET /scenarios.")
    incident_id = await request.app.state.service.start(body.scenario_id, started_by=user.username)
    return {"incident_id": incident_id}


class ReportedIncident(BaseModel):
    """What a person types into the report form. Validated by the same strict Incident model as scenario
    alerts; no simulation or deployment fields exist here, so a report can't script its own success."""
    model_config = {"extra": "forbid"}
    title: str
    severity: Literal["P1", "P2"]
    alert_name: str
    affected_service: str
    namespace: str
    cloud_provider: Literal["aws", "azure"]
    cloud_region: str = "us-east-1"
    telemetry: dict
    log_excerpt: str
    tags: list[str] = []


@router.post("/incidents/custom", status_code=202)
async def report_incident(body: ReportedIncident, request: Request, user: User = Depends(CAN_ACT)) -> dict:
    from pydantic import ValidationError

    from cloudscale.common.schemas import Incident
    try:
        incident = Incident.model_validate({**body.model_dump(), "incident_id": "INC-C-new"}).model_dump()
    except ValidationError as e:
        def explain(err: dict) -> str:
            if err["type"] == "string_pattern_mismatch" and err["loc"][0] in ("affected_service", "namespace"):
                return ("use lowercase letters, digits and dashes, starting and ending with a letter or digit "
                        "(a Kubernetes name, e.g. notification-worker)")
            return err["msg"]
        raise HTTPException(422, [{"field": ".".join(map(str, err["loc"])), "problem": explain(err)}
                                  for err in e.errors()]) from e
    incident_id = await request.app.state.service.start_custom(incident, started_by=user.username)
    return {"incident_id": incident_id}


@router.get("/incidents")
async def list_incidents(request: Request, _: User = Depends(current_user)) -> list[dict]:
    return await request.app.state.service.list()


@router.get("/incidents/{incident_id}")
async def get_incident(incident_id: str, request: Request, _: User = Depends(current_user)) -> dict:
    view = await request.app.state.service.get(incident_id)
    if view is None:
        raise HTTPException(404, f"Incident {incident_id} not found.")
    return view


@router.get("/hitl/queue")
async def hitl_queue(request: Request, user: User = Depends(current_user)) -> list[dict]:
    rows = [r for r in await request.app.state.service.list() if r["status"] in ("AWAITING_APPROVAL",
                                                                                   "AWAITING_ESCALATION")]
    return [{**r, "can_decide": user.role in DECIDERS[r["gate"]]} for r in rows]


class Decision(BaseModel):
    outcome: Literal["APPROVED", "REJECTED", "ESCALATE"]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    comment: str = Field(default="", max_length=500)


@router.post("/incidents/{incident_id}/decision", status_code=202)
async def decide(incident_id: str, body: Decision, request: Request,
                 user: User = Depends(CAN_ACT)) -> dict:
    service = request.app.state.service
    view = await service.get(incident_id)
    pending = (view or {}).get("pending_decision")
    if not pending:
        raise HTTPException(409, "This incident isn't waiting for a decision.")
    if body.plan_hash != pending["plan_hash"]:
        raise HTTPException(409, "The plan changed since you loaded it. Reload and review it again.")
    gate = pending["gate"]
    if body.outcome == "ESCALATE" and gate == "ESCALATION":
        raise HTTPException(409, "Already escalated to a senior SRE.")
    if body.outcome != "ESCALATE" and user.role not in DECIDERS[gate]:
        raise HTTPException(403, f"A {gate} gate needs role {sorted(DECIDERS[gate])}; you are '{user.role}'.")

    decision = {"decision_id": uuid.uuid4().hex, "outcome": body.outcome, "plan_hash": body.plan_hash,
                "gate": gate, "approver": user.username, "role": user.role, "comment": body.comment}
    await request.app.state.audit.append("HITL_DECISION", "human", user.username,
                                         {k: v for k, v in decision.items() if k != "approver"}, incident_id)
    await service.resume(incident_id, decision)
    return {"decision_id": decision["decision_id"], "outcome": body.outcome}


# The 4 jury metrics (+3 supporting) as fixed PromQL over a window. Names follow Prometheus' OTLP
# translation: dots -> underscores, unit suffix (ms -> _milliseconds), counters get _total.
PROMQL = {
    "lpt_p95_ms_by_agent": 'histogram_quantile(0.95, sum by (le, agent) (rate(incident_task_latency_milliseconds_bucket[{w}])))',
    "tcr_tokens_per_min": 'sum(rate(llm_tokens_total[{w}])) * 60',
    "tokens_in_window": 'sum(increase(llm_tokens_total[{w}]))',
    "chr_cache_hit_ratio": 'sum(increase(cache_lookups_total{{result="hit"}}[{w}])) / sum(increase(cache_lookups_total[{w}]))',
    "tfr_tool_failure_rate": 'sum(increase(mcp_tool_calls_total{{outcome="error"}}[{w}])) / sum(increase(mcp_tool_calls_total[{w}]))',
    "tool_calls_by_outcome": 'sum by (outcome) (increase(mcp_tool_calls_total[{w}]))',
    "llm_cost_usd": 'sum(increase(llm_cost_usd_total[{w}]))',
    "hitl_wait_p50_s": 'histogram_quantile(0.5, sum by (le) (rate(hitl_wait_seconds_bucket[{w}])))',
    "circuit_state": 'max by (tool, cloud) (circuit_state)',
}


@router.get("/metrics/live")
async def metrics_live(window: str = "1h", _: User = Depends(current_user)) -> dict:
    if window not in ("15m", "1h", "6h", "24h"):
        raise HTTPException(400, "window must be one of 15m, 1h, 6h, 24h")
    out: dict = {"window": window}
    async with httpx.AsyncClient(base_url=get_settings().prometheus_url, timeout=10) as c:
        for name, q in PROMQL.items():
            try:
                r = await c.get("/api/v1/query", params={"query": q.format(w=window)})
                series = r.json()["data"]["result"]
            except (httpx.HTTPError, KeyError, ValueError) as e:
                out[name] = {"error": type(e).__name__}
                continue
            rows = [{"labels": s["metric"], "value": None if s["value"][1] in ("NaN", "+Inf") else float(s["value"][1])}
                    for s in series]
            out[name] = rows[0]["value"] if len(rows) == 1 and not rows[0]["labels"] else rows
    return out


@router.get("/ledger/summary")
async def ledger_summary(request: Request, days: int = 30, _: User = Depends(current_user)) -> dict:
    return await request.app.state.ledger.summary(max(1, min(days, 365)))


@router.get("/incidents/{incident_id}/ledger")
async def incident_ledger(incident_id: str, request: Request, _: User = Depends(current_user)) -> list[dict]:
    return await request.app.state.ledger.for_incident(incident_id)


@router.get("/incidents/{incident_id}/handoff")
async def handoff(incident_id: str, request: Request, _: User = Depends(current_user)) -> dict:
    view = await request.app.state.service.get(incident_id)
    if not view or not view.get("handoff"):
        raise HTTPException(404, "No hand-off: the incident isn't escalated.")
    h = view["handoff"]
    md = "\n".join([
        f"### {h['incident_id']}: {h['title']}",
        f"**{h['severity']}** · `{h['service']}` in `{h['namespace']}` on `{h['cloud']}`",
        f"**Why escalated:** {h['why_escalated']}",
        f"**What changed:** {h['what_changed']}",
        f"**Diagnosis:** {h['diagnosis']} (`{h['category']}`)",
        f"**Confidence:** {h['confidence']} (LLM {h['llm_confidence']}, evidence {h['evidence_score']})",
        f"**Gate:** {h['gate']} · {', '.join(h['gate_reasons']) or '-'}",
        f"**Proposed plan:** {h['proposed_plan']}",
        f"**Proposed steps:** {', '.join(s for s in h['proposed_steps'] if s) or '-'}",
        f"**Drafted artifacts (not applied):** {', '.join(h['artifacts']) or '-'}",
        f"**Metrics at close:** {h['final_metrics']}",
        f"Full trail: GET /audit?incident_id={h['incident_id']}",
    ])
    return {**h, "markdown": md}


class Resolution(BaseModel):
    how_fixed: str = Field(min_length=5, max_length=1000)
    root_cause_category: str = Field(pattern=r"^[a-z0-9_]{3,60}$")


@router.post("/incidents/{incident_id}/resolution")
async def record_resolution(incident_id: str, body: Resolution, request: Request,
                            user: User = Depends(CAN_ACT)) -> dict:
    try:
        return await request.app.state.service.record_resolution(incident_id, user.username, body.how_fixed,
                                                                 body.root_cause_category)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/runbooks/proposals")
async def runbook_proposals(request: Request, _: User = Depends(current_user)) -> list[dict]:
    return await request.app.state.service.store.proposals()


@router.get("/audit")
async def audit_list(request: Request, incident_id: str | None = None, limit: int = 300,
                     _: User = Depends(current_user)) -> list[dict]:
    return await request.app.state.audit.list(incident_id, min(limit, 1000))


@router.get("/audit/verify")
async def audit_verify(request: Request, _: User = Depends(current_user)) -> dict:
    return await request.app.state.audit.verify()
