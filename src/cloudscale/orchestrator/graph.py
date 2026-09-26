"""Incident pipeline as a LangGraph StateGraph (hierarchical topology).

Supervisor = the routing functions below: deterministic code, no LLM.
Agents: Triage (LLM + READ tools), Planner (LLM), Executor (tool calls), Evaluator (SLO check + LLM summary).
State holds plain JSON-able dicts so checkpoints serialize cleanly in Postgres.

intake -> triage -> planner -> validate_plan -> hitl_gate -> execute -> evaluate -> close
   |         |                     |  ^ re-plan once   |  interrupt() unless AUTO
   +-QUARANTINED                   +- ESCALATED        +- REJECTED -> close
"""

import asyncio
import operator
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from cloudscale.common.canonical import args_hash, idempotency_key, plan_hash
from cloudscale.common.gate import classify, evaluate_plan, load_policy
from cloudscale.common.runbooks import score_evidence, slo_thresholds
from cloudscale.common.safety.llama_guard import Guard
from cloudscale.common.safety.normalizer import clean_log
from cloudscale.common.safety.scrubber import scrub
from cloudscale.common.safety.validators import validate_artifact, validate_patch_bounds
from cloudscale.common.schemas import GateContext, RemediationPlan
from cloudscale.common.telemetry import instruments, tracer
from cloudscale.common.tokens import ApprovalClaim, StepClaim, TokenIssuer
from cloudscale.common.tool_args import normalize
from cloudscale.orchestrator.breaker import CircuitOpenError
from cloudscale.orchestrator.cache import SemanticCache, bind_template, cache_key_text, triage_for_cache
from cloudscale.orchestrator.llm.base import LLM
from cloudscale.orchestrator.llm.claude import BudgetExceeded
from cloudscale.orchestrator.tools import ToolCallError, ToolClient

TERMINAL = {"QUARANTINED", "ESCALATED", "REJECTED", "RESOLVED", "PARTIALLY_RESOLVED", "FAILED"}
MAX_PLAN_ATTEMPTS = 2


class IncidentState(TypedDict, total=False):
    incident: dict
    status: str
    guard: dict
    pseudonyms: dict[str, str]
    observations: dict
    evidence: dict
    triage: dict
    plan: dict | None
    plan_hash: str | None
    plan_attempts: int
    cache: dict | None
    validation_errors: list[str]
    tag_mismatch: bool
    gate_floor: str | None
    gate: dict | None
    decision: dict | None
    results: list[dict]
    verification: dict | None
    summary: str
    usage: Annotated[list[dict], operator.add]
    events: Annotated[list[dict], operator.add]


@dataclass
class Deps:
    llm: LLM
    tools: ToolClient
    issuer: TokenIssuer
    guard: Guard
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    poll_interval_s: float = 3.0
    verify_timeout_s: float = 45.0
    cache: SemanticCache | None = None


def event(node: str, msg: str, **data: Any) -> dict:
    return {"ts": time.time(), "node": node, "msg": msg, **data}


def _scope(inc: dict) -> dict:
    return {"namespace": inc["namespace"], "cloud_provider": inc["cloud_provider"]}


def _spent(state: IncidentState) -> float:
    return round(sum(u.get("cost_usd", 0) for u in state.get("usage", [])), 6)


def _usage_events(node: str, usage) -> list[dict]:
    if usage.failover:
        return [event(node, f"LLM failover: {usage.failover}", type="PROVIDER_FAILOVER")]
    return []


PHASE = {"intake": "triage_plan", "triage": "triage_plan", "planner": "triage_plan", "validate_plan": "triage_plan",
         "hitl_gate": "gate", "execute": "execute_verify", "evaluate": "execute_verify", "close": "execute_verify"}


def traced(name: str, fn):
    """Span `agent.<name>` + LPT histogram per node. interrupt() is a pause for a human, not an error."""
    async def wrapper(state):
        t0 = time.perf_counter()
        iid = (state.get("incident") or {}).get("incident_id")
        with tracer.start_as_current_span(f"agent.{name}", record_exception=False,
                                          set_status_on_exception=False) as s:
            s.set_attribute("incident.id", iid or "")
            s.set_attribute("agent", name)
            try:
                out = fn(state)
                out = await out if hasattr(out, "__await__") else out
            except GraphInterrupt:
                s.set_attribute("hitl.interrupted", True)
                raise
            except Exception as e:
                s.record_exception(e)
                s.set_status(trace_status_error(str(e)))
                raise
            update = out.update if isinstance(out, Command) else out
            if isinstance(update, dict) and update.get("status"):
                s.set_attribute("incident.status", update["status"])
        instruments().task_latency.record((time.perf_counter() - t0) * 1000, {"agent": name, "phase": PHASE[name]})
        return out
    return wrapper


def trace_status_error(msg: str):
    from opentelemetry.trace import Status, StatusCode
    return Status(StatusCode.ERROR, msg)


def _read_token(deps: Deps, inc: dict) -> str:
    return deps.issuer.mint_read(inc["incident_id"], inc["namespace"], inc["cloud_provider"])


def build_graph(deps: Deps, checkpointer=None):
    # ------------------------------------------------------------------ intake (no LLM)
    async def intake(state: IncidentState) -> dict:
        inc = state["incident"]
        title, pseudonyms, n1 = scrub(inc["title"])
        log, pseudonyms, n2 = scrub(clean_log(inc["log_excerpt"]), pseudonyms)
        g = await deps.guard.check(f"{title}\n{log}")
        if g.verdict == "unsafe":
            return {"status": "QUARANTINED", "guard": g.model_dump(),
                    "events": [event("intake", "Quarantined: injection detected in the alert payload",
                                     type="QUARANTINED", heuristics=g.heuristics, source=g.source)]}
        scrubbed = f"; scrubbed {n1 + n2} secret(s), pseudonymized {len(pseudonyms)} IP(s)" if n1 + n2 or pseudonyms else ""
        return {"status": "TRIAGING", "guard": g.model_dump(), "pseudonyms": pseudonyms,
                "incident": {**inc, "title": title, "log_excerpt": log},
                "events": [event("intake", f"Accepted; guard {g.verdict} via {g.source}{scrubbed}",
                                 degraded=g.degraded)]}

    # ------------------------------------------------------------------ triage agent
    async def triage(state: IncidentState) -> dict:
        inc = state["incident"]
        read, scope, svc = _read_token(deps, inc), _scope(inc), inc["affected_service"]
        try:
            metrics = await deps.tools.call("get_metrics", {"scope": scope, "service": svc}, read)
            logs = await deps.tools.call("fetch_k8s_logs", {"scope": scope, "service": svc, "lines": 100}, read)
            dep = await deps.tools.call("get_deployment_status", {"scope": scope, "deployment": svc}, read)
        except (ToolCallError, CircuitOpenError) as e:
            return {"status": "ESCALATED", "events": [event("triage", f"Evidence collection failed: {e}")]}

        fetched, pseudonyms, _ = scrub(clean_log("\n".join(logs["lines"])), state.get("pseudonyms"))
        g = await deps.guard.check(fetched)          # tool output is untrusted too (indirect injection)
        degraded = state["guard"]["degraded"] or g.degraded
        if g.verdict == "unsafe":
            return {"status": "QUARANTINED", "guard": {**g.model_dump(), "degraded": degraded},
                    "events": [event("triage", "Quarantined: injection detected in fetched pod logs",
                                     type="QUARANTINED", heuristics=g.heuristics, source=g.source)]}

        evidence = score_evidence(f"{inc['log_excerpt']}\n{fetched}", metrics["metrics"])
        observations = {"metrics": metrics["metrics"], "logs": fetched.splitlines(), "deployment": dep}
        key_text = cache_key_text(inc, evidence.model_dump(), metrics["metrics"])
        base = {"guard": {**state["guard"], "degraded": degraded}, "observations": observations,
                "evidence": evidence.model_dump(), "pseudonyms": pseudonyms}

        hit = await deps.cache.lookup(inc, key_text) if deps.cache else None
        if hit:
            t = hit["triage"]
            confidence = min(t["llm_confidence"], evidence.score)
            return {**base, "status": "PLANNING",
                    "cache": {"hit": True, "key_text": key_text, **{k: v for k, v in hit.items() if k != "triage"}},
                    "triage": {**t, "evidence_score": evidence.score, "confidence": confidence},
                    "usage": [{"agent": "triage", "model": "semantic-cache", "cost_usd": 0.0, "cache_tier": "semantic",
                               "avoided_cost_usd": hit["original_cost_usd"]}],
                    "events": [event("triage", f"Semantic cache hit (similarity {hit['similarity']}) from "
                                               f"{hit['source_incident']}: {t['root_cause_category']}; "
                                               "no LLM call, confidence capped at 0.79 in the gate",
                                     type="CACHE_HIT", entry_id=hit["entry_id"])]}

        ctx = {"incident": inc, "metrics": metrics["metrics"], "logs": fetched, "deployment": dep,
               "evidence": evidence.model_dump(), "spent_usd": _spent(state)}
        try:
            out, usage = await deps.llm.triage(ctx)
        except BudgetExceeded as e:
            return {**base, "status": "ESCALATED", "events": [event("triage", str(e), type="BUDGET_EXCEEDED")]}
        confidence = min(out.llm_confidence, evidence.score)
        return {
            **base,
            "status": "PLANNING",
            "cache": {"hit": False, "key_text": key_text} if deps.cache else None,
            "triage": {**out.model_dump(), "evidence_score": evidence.score, "confidence": confidence},
            "usage": [{"agent": "triage", **usage.model_dump()}],
            "events": _usage_events("triage", usage) + [
                event("triage", f"{out.root_cause_category}: confidence {confidence:.2f} "
                                f"(LLM {out.llm_confidence:.2f}, evidence {evidence.score:.2f}) via {usage.model}",
                      runbook=evidence.runbook_id, model=usage.model, cost_usd=usage.cost_usd)],
        }

    # ------------------------------------------------------------------ planner agent
    async def planner(state: IncidentState) -> dict:
        attempt = state.get("plan_attempts", 0) + 1
        cache = state.get("cache") or {}
        if cache.get("hit") and attempt == 1:
            plan = RemediationPlan.model_validate(
                bind_template(cache["plan_template"], state["incident"], state["observations"]["deployment"]))
            return {"plan": plan.model_dump(), "plan_attempts": attempt, "status": "PLANNING",
                    "usage": [{"agent": "planner", "model": "semantic-cache", "cost_usd": 0.0,
                               "cache_tier": "semantic"}],
                    "events": [event("planner", f"Plan v{attempt} re-bound from cache: {len(plan.steps)} step(s). "
                                                f"{plan.summary}")]}
        ctx = {"incident": state["incident"], "deployment": state["observations"]["deployment"],
               "metrics": state["observations"]["metrics"], "triage": state["triage"],
               "validation_errors": state.get("validation_errors", []), "spent_usd": _spent(state)}
        try:
            plan, usage = await deps.llm.plan(ctx)
        except BudgetExceeded as e:
            return {"status": "ESCALATED", "plan_attempts": attempt,
                    "events": [event("planner", str(e), type="BUDGET_EXCEEDED")]}
        return {"plan": plan.model_dump(), "plan_attempts": attempt, "status": "PLANNING",
                "usage": [{"agent": "planner", **usage.model_dump()}],
                "events": _usage_events("planner", usage) + [
                    event("planner", f"Plan v{attempt}: {len(plan.steps)} step(s) via {usage.model}. {plan.summary}",
                          model=usage.model, cost_usd=usage.cost_usd)]}

    # ------------------------------------------------------------------ validate_plan (no LLM)
    def validate_plan(state: IncidentState) -> dict:
        inc = state["incident"]
        plan = RemediationPlan.model_validate(state["plan"])
        errors: list[str] = []
        mismatch = False
        for step in plan.steps:
            if step.kind == "manual_runbook":
                if not step.runbook_ref:
                    errors.append(f"{step.step_id}: manual step without runbook_ref")
                continue
            if step.call is None:
                errors.append(f"{step.step_id}: tool step without a call")
                continue
            for label, call in (("call", step.call), ("rollback", step.rollback)):
                if call is None:
                    continue
                try:
                    norm = normalize(call.tool_name, call.tool_args)
                except (ValidationError, KeyError) as e:
                    errors.append(f"{step.step_id}.{label}: invalid args ({type(e).__name__})")
                    continue
                if norm["scope"]["namespace"] != inc["namespace"] or \
                        norm["scope"]["cloud_provider"] != inc["cloud_provider"]:
                    errors.append(f"{step.step_id}.{label}: scope outside the incident")
                call.tool_args = norm
                if call.tool_name == "apply_hotfix":
                    errors += [f"{step.step_id}.{label}: {v}" for v in validate_patch_bounds(norm["patch"])]
            if step.llm_claims_destructive != (classify(step) == "DESTRUCTIVE"):
                mismatch = True

        for a in plan.artifacts:
            errors += [f"artifact {a.filename}: {v}" for v in validate_artifact(a.content)]
        if errors:
            if state["plan_attempts"] < MAX_PLAN_ATTEMPTS:
                return {"validation_errors": errors, "status": "PLANNING",
                        "events": [event("validate_plan", f"Rejected, re-planning: {errors}", type="PLAN_REJECTED")]}
            return {"validation_errors": errors, "status": "ESCALATED",
                    "events": [event("validate_plan", f"Invalid twice, escalating: {errors}", type="POLICY_VIOLATION")]}
        return {"plan": plan.model_dump(), "plan_hash": plan_hash(plan), "tag_mismatch": mismatch,
                "validation_errors": [], "status": "GATING",
                "events": [event("validate_plan", "Plan valid" + ("; op-class tag mismatch" if mismatch else ""))]}

    # ------------------------------------------------------------------ HITL gate
    async def hitl_gate(state: IncidentState) -> Command:
        inc, tri = state["incident"], state["triage"]
        ctx = GateContext(incident_id=inc["incident_id"], severity=inc["severity"], service=inc["affected_service"],
                          alert_name=inc["alert_name"], confidence=tri["confidence"],
                          guard_degraded=state["guard"]["degraded"], tag_mismatch=state.get("tag_mismatch", False),
                          from_cache=bool((state.get("cache") or {}).get("hit")))
        ev = evaluate_plan(RemediationPlan.model_validate(state["plan"]), ctx)
        if state.get("gate_floor") == "ESCALATION" and ev.gate != "ESCALATION":
            ev = ev.model_copy(update={"gate": "ESCALATION", "required_role": "senior_sre"})
        gate = ev.model_dump()

        if ev.gate == "AUTO":
            decision = {"decision_id": f"auto-{ev.plan_hash[:12]}", "outcome": "APPROVED", "gate": "AUTO",
                        "approver": "system", "role": None}
            return Command(goto="execute", update={
                "gate": gate, "decision": decision, "status": "EXECUTING",
                "events": [event("hitl_gate", "AUTO: executing without human review", type="GATE_EVALUATED",
                                   gate="AUTO", reasons=_reasons(ev))]})

        # No side effects above this line: LangGraph re-runs the node from the top on resume.
        decision = interrupt({"incident_id": inc["incident_id"], "gate": ev.gate, "plan_hash": ev.plan_hash,
                              "required_role": ev.required_role, "risk_level": ev.risk_level,
                              "steps": gate["steps"]})

        allowed = {"APPROVAL": {"sre", "senior_sre"}, "ESCALATION": {"senior_sre"}}[ev.gate]
        outcome = decision.get("outcome")
        if decision.get("plan_hash") != ev.plan_hash:
            return Command(goto="close", update={"status": "ESCALATED", "gate": gate, "events": [
                event("hitl_gate", "Decision was for a different plan version; escalating")]})
        if outcome == "APPROVED" and decision.get("role") in allowed:
            return Command(goto="execute", update={
                "gate": gate, "decision": decision, "status": "EXECUTING",
                "events": [event("hitl_gate", f"{ev.gate} approved by {decision['approver']} ({decision['role']})")]})
        if outcome == "REJECTED" and decision.get("role") in allowed:
            return Command(goto="close", update={
                "gate": gate, "decision": decision, "status": "REJECTED",
                "events": [event("hitl_gate", f"Rejected by {decision['approver']}")]})
        if outcome == "ESCALATE":
            return Command(goto="hitl_gate", update={
                "gate_floor": "ESCALATION",
                "events": [event("hitl_gate", f"Escalated to senior SRE by {decision.get('approver')}")]})
        return Command(goto="hitl_gate", update={"events": [
            event("hitl_gate", f"Ignored decision {outcome!r} from role {decision.get('role')!r}; still waiting")]})

    # ------------------------------------------------------------------ executor
    async def execute(state: IncidentState) -> dict:
        inc, decision, ph = state["incident"], state["decision"], state["plan_hash"]
        plan = RemediationPlan.model_validate(state["plan"])
        approval = ApprovalClaim(decision_id=decision["decision_id"], gate=decision["gate"],
                                 approver=decision["approver"], role=decision.get("role"))
        op_classes = load_policy()["op_classes"]

        async def run(step_id: str, tool: str, args: dict) -> dict:
            ah = args_hash(tool, args)
            token = deps.issuer.mint_exec(       # minted per call, just before it; bound to this step + args
                incident_id=inc["incident_id"], namespace=inc["namespace"], cloud_provider=inc["cloud_provider"],
                plan_hash=ph, step=StepClaim(step_id=step_id, tool=tool, op_class=op_classes[tool], args_hash=ah),
                approval=approval)
            key = idempotency_key(inc["incident_id"], ph, step_id, ah)
            return await deps.tools.call(tool, {**args, "scope": {**args["scope"], "idempotency_key": key}}, token)

        # Pre-flight: don't start a plan that can't finish. An OPEN breaker on any step's tool blocks the
        # whole plan before anything changes (otherwise earlier steps would run and then need rollback).
        for step in plan.steps:
            b = deps.tools.breaker(step.call.tool_name, inc["cloud_provider"]) if step.call else None
            if b and await b.blocking():
                return {"results": [{"step_id": step.step_id, "tool": step.call.tool_name, "status": "BLOCKED",
                                     "error": f"circuit open for {b.key}"}],
                        "status": "ESCALATED",
                        "events": [event("execute", f"Plan not started: circuit breaker OPEN for {step.call.tool_name}",
                                         type="CIRCUIT_OPEN")]}

        results, events, completed, failed = [], [], [], False
        for step in plan.steps:
            if step.kind == "manual_runbook":
                results.append({"step_id": step.step_id, "status": "HANDED_OFF", "runbook_ref": step.runbook_ref})
                events.append(event("execute", f"{step.step_id}: handed off to on-call ({step.runbook_ref})"))
                continue
            tool = step.call.tool_name
            try:
                out = await run(step.step_id, tool, step.call.tool_args)
                results.append({"step_id": step.step_id, "tool": tool, "status": "OK", "output": out})
                events.append(event("execute", f"{step.step_id}: {tool} OK"))
                completed.append(step)
            except CircuitOpenError as e:
                results.append({"step_id": step.step_id, "tool": tool, "status": "BLOCKED", "error": str(e)})
                events.append(event("execute", f"{step.step_id}: {tool} not attempted: {e}", type="CIRCUIT_OPEN"))
                failed = True
                break
            except ToolCallError as e:
                results.append({"step_id": step.step_id, "tool": tool, "status": "FAILED", "error": e.message})
                events.append(event("execute", f"{step.step_id}: {tool} FAILED: {e.message}", type="TOOL_CALL_FAILED"))
                if e.breaker_change:
                    events.append(event("execute", f"Circuit breaker for {tool} is now {e.breaker_change}",
                                        type="CIRCUIT_STATE_CHANGED", tool=tool, state=e.breaker_change))
                failed = True
                break

        if failed:
            # Compensate completed steps in reverse order with their pre-approved rollbacks.
            for step in reversed(completed):
                if step.rollback is None:
                    events.append(event("execute", f"{step.step_id}: no rollback defined, left in place"))
                    continue
                rb = step.rollback
                try:
                    await run(f"{step.step_id}-rollback", rb.tool_name, rb.tool_args)
                    results.append({"step_id": f"{step.step_id}-rollback", "tool": rb.tool_name, "status": "ROLLED_BACK"})
                    events.append(event("execute", f"{step.step_id}: rolled back via {rb.tool_name}",
                                        type="ROLLBACK_EXECUTED"))
                except (ToolCallError, CircuitOpenError) as e:
                    results.append({"step_id": f"{step.step_id}-rollback", "tool": rb.tool_name,
                                    "status": "ROLLBACK_FAILED", "error": str(e)})
                    events.append(event("execute", f"{step.step_id}: ROLLBACK FAILED: {e}", type="ROLLBACK_FAILED"))
        return {"results": results, "status": "ESCALATED" if failed else "VERIFYING", "events": events}

    # ------------------------------------------------------------------ evaluator
    async def evaluate(state: IncidentState) -> dict:
        inc, results = state["incident"], state["results"]
        thresholds = slo_thresholds()
        baseline = state["observations"]["metrics"]
        handed_off = any(r["status"] == "HANDED_OFF" for r in results)
        read, waited, polls = _read_token(deps, inc), 0.0, 0
        while True:
            m = (await deps.tools.call("get_metrics", {"scope": _scope(inc), "service": inc["affected_service"]},
                                       read))["metrics"]
            polls += 1
            healthy = all(m[k] < t for k, t in thresholds.items() if k in m)
            if healthy or handed_off or waited >= deps.verify_timeout_s:
                break
            await deps.sleep(deps.poll_interval_s)
            waited += deps.poll_interval_s

        if handed_off:
            outcome = "ESCALATED"
        elif healthy:
            outcome = "RESOLVED"
        else:
            gains = [(baseline[k] - m[k]) / (baseline[k] - t) for k, t in thresholds.items()
                     if k in m and baseline.get(k, 0) > t]
            outcome = "PARTIALLY_RESOLVED" if gains and sum(gains) / len(gains) >= 0.5 else "ESCALATED"
        verification = {"outcome": outcome, "healthy": healthy, "final_metrics": m, "baseline": baseline,
                        "thresholds": thresholds, "polls": polls, "waited_s": waited}
        try:
            summary, usage = await deps.llm.summarize({"incident": inc, "results": results,
                                                       "verification": verification, "spent_usd": _spent(state)})
            usage_rows = [{"agent": "evaluator", **usage.model_dump()}]
        except BudgetExceeded:
            summary, usage_rows = f"{outcome} (summary skipped: LLM budget reached)", []
        return {"verification": verification, "summary": summary, "status": outcome,
                "usage": usage_rows,
                "events": [event("evaluate", f"{outcome} after {polls} poll(s), {waited:.0f}s", metrics=m)]}

    async def close(state: IncidentState) -> dict:
        events = []
        cache, status = state.get("cache") or {}, state["status"]
        if deps.cache and cache and state.get("verification"):
            resolved = status == "RESOLVED" and state["verification"]["healthy"]
            if cache.get("hit"):
                result = await deps.cache.feedback(cache["entry_id"], resolved)
                events.append(event("close", f"Cache entry {cache['entry_id']} {result}",
                                    type="CACHE_PROMOTED" if resolved else "CACHE_INVALIDATED"))
            elif resolved:                                   # only verified fixes are ever cached
                cost = sum(u.get("cost_usd", 0) for u in state.get("usage", []) if u["agent"] in ("triage", "planner"))
                entry = await deps.cache.store(state["incident"], cache["key_text"], triage_for_cache(state["triage"]),
                                               state["plan"], state["observations"]["deployment"], cost)
                events.append(event("close", f"Verified fix written to semantic cache (entry {entry})",
                                    type="CACHE_WRITTEN", entry_id=entry))
        return {"events": events + [event("close", f"Closed as {status}", spent_usd=_spent(state))]}

    # ------------------------------------------------------------------ supervisor (routing)
    def after(ok_next: str) -> Callable[[IncidentState], str]:
        return lambda s: "close" if s["status"] in TERMINAL else ok_next

    def after_validate(s: IncidentState) -> str:
        if s["status"] == "PLANNING":
            return "planner"
        return "close" if s["status"] in TERMINAL else "hitl_gate"

    b = StateGraph(IncidentState)
    b.add_node("intake", traced("intake", intake))
    b.add_node("triage", traced("triage", triage))
    b.add_node("planner", traced("planner", planner))
    b.add_node("validate_plan", traced("validate_plan", validate_plan))
    b.add_node("hitl_gate", traced("hitl_gate", hitl_gate), destinations=("execute", "close", "hitl_gate"))
    b.add_node("execute", traced("execute", execute))
    b.add_node("evaluate", traced("evaluate", evaluate))
    b.add_node("close", traced("close", close))
    b.add_edge(START, "intake")
    b.add_conditional_edges("intake", after("triage"), ["triage", "close"])
    b.add_conditional_edges("triage", after("planner"), ["planner", "close"])
    b.add_conditional_edges("planner", after("validate_plan"), ["validate_plan", "close"])
    b.add_conditional_edges("validate_plan", after_validate, ["planner", "hitl_gate", "close"])
    b.add_conditional_edges("execute", after("evaluate"), ["evaluate", "close"])
    b.add_edge("evaluate", "close")
    b.add_edge("close", END)
    return b.compile(checkpointer=checkpointer)


def _reasons(ev) -> dict[str, list[str]]:
    return {s.step_id: s.reasons for s in ev.steps}
