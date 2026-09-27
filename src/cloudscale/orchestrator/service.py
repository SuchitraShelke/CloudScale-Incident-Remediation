"""Runs incidents through the graph: one LangGraph thread per incident (thread_id = incident_id).

Audit: node updates are streamed and each event is appended to the hash-chained log as the node
finishes. A node that calls interrupt() emits no update, so a re-run on resume never double-audits.
"""

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langgraph.types import Command

from cloudscale.common.runbooks import slo_thresholds
from cloudscale.common.scenarios import load_scenario
from cloudscale.common.telemetry import instruments, tracer
from cloudscale.orchestrator.graph import TERMINAL, TIMER, event
from cloudscale.orchestrator.store import IncidentStore

log = logging.getLogger("cloudscale.orchestrator")

EVENT_TYPES = {"intake": "GUARD_CHECK", "triage": "AGENT_DECISION", "planner": "PLAN_PROPOSED",
               "validate_plan": "PLAN_VALIDATED", "hitl_gate": "HITL_DECISION_APPLIED",
               "execute": "TOOL_EXECUTION", "evaluate": "VERIFICATION", "close": "INCIDENT_CLOSED"}
ACTORS = {"triage": ("agent", "triage"), "planner": ("agent", "planner"), "execute": ("agent", "executor"),
          "evaluate": ("agent", "evaluator")}          # everything else: the deterministic supervisor


def draft_runbook(view: dict[str, Any], category: str, how_fixed: str) -> dict[str, Any]:
    """Proposed catalogue entry from a verified human fix: the most specific error lines as signatures and
    the worst-breached SLO metric (at alert time) as corroboration. A senior reviews it before it's used."""
    lines = [ln for ln in view["incident"]["log_excerpt"].splitlines() if ln.strip()]
    ranked = sorted(lines, key=lambda ln: (0 if re.search(r"\b(FATAL|ERROR)\b", ln) else 1, -len(ln)))
    signatures = []
    for ln in ranked[:2]:
        text = re.sub(r"^\S*\d{4}-\d\d-\d\dT\S+\s+", "", ln)          # drop the timestamp
        text = re.sub(r"^(FATAL|ERROR|WARN|INFO|DEBUG)\s+", "", text)
        signatures.append(re.escape(text[:80]))
    baseline, thresholds = view["observations"]["metrics"], slo_thresholds()
    breached = {k: baseline[k] / t for k, t in thresholds.items() if baseline.get(k, 0) > t}
    metric = max(breached, key=breached.get) if breached else "error_rate_pct"
    return {"id": f"RB-PROPOSED-{view['incident']['incident_id']}", "root_cause_category": category,
            "signatures": signatures, "corroborate": {"metric": metric, "above": thresholds[metric]},
            "how_fixed": how_fixed, "source_incident": view["incident"]["incident_id"],
            "service": view["incident"]["affected_service"]}


def generic_simulation(incident: dict[str, Any]) -> dict[str, Any]:
    """Built server-side from the report only: the client can't supply effects or deployment state."""
    t = incident["telemetry"]
    svc = incident["affected_service"]
    return {
        "deployments": {svc: {"replicas": 3, "revision": 2, "previous_revision": 1, "image_tag": "v1.0.0",
                              "resources": {"limits": {"memory": "1Gi", "cpu": "1000m"}},
                              "connection_pool_max_size": 20}},
        "initial_metrics": {"cpu_usage_pct": t["cpu_usage_pct"], "memory_usage_pct": t["memory_usage_pct"],
                            "error_rate_pct": t["error_rate_pct"], "p99_ms": t["request_latency_p99_ms"]},
        "logs": incident["log_excerpt"].splitlines()[-100:],
        "effects": [],          # unknown incident: nothing is known to fix it
    }


class NullAudit:
    async def append(self, *a, **kw) -> dict:
        return {}


class IncidentService:
    def __init__(self, graph, register_sim: Callable[[str, str], Awaitable[None]], scenarios_dir: Path,
                 store: IncidentStore, audit=None, ledger=None, max_concurrent: int = 5,
                 promote_after_s: float = 600, expire_after_s: float = 1800, read_metrics=None):
        self.graph, self.register_sim, self.dir, self.store = graph, register_sim, scenarios_dir, store
        self.audit, self.ledger = audit or NullAudit(), ledger
        self.promote_after_s, self.expire_after_s = promote_after_s, expire_after_s
        self.read_metrics = read_metrics        # async (incident) -> metrics; re-verifies a human's fix
        self.sem = asyncio.Semaphore(max_concurrent)   # released while a graph waits on a human
        self.errors: dict[str, str] = {}
        self.pending_since: dict[str, tuple[float, str]] = {}   # HITL wait metric
        self._closed_rows: dict[str, dict[str, Any]] = {}       # list() cache for terminal incidents
        self.tasks: set[asyncio.Task] = set()

    @staticmethod
    def _config(incident_id: str) -> dict:
        return {"configurable": {"thread_id": incident_id}}

    async def start(self, scenario_id: str, started_by: str = "system", wait: bool = False) -> str:
        sc = load_scenario(scenario_id, self.dir)
        incident_id = f"INC-{scenario_id.split('-')[0].upper()}-{uuid.uuid4().hex[:6]}"
        incident = sc.incident.model_copy(update={"incident_id": incident_id}).model_dump()
        await self.register_sim(incident_id, scenario_id=scenario_id)
        return await self._begin(incident, scenario_id, started_by, f"scenario {scenario_id}", wait)

    async def start_custom(self, incident: dict[str, Any], started_by: str, wait: bool = False) -> str:
        """An incident typed in by a person. Its simulated infrastructure is generic: the reported metrics and
        logs, a default deployment and no rules for what fixes it, so no fix can be verified as working."""
        incident_id = f"INC-C-{uuid.uuid4().hex[:6]}"
        incident = {**incident, "incident_id": incident_id}
        await self.register_sim(incident_id, simulation=generic_simulation(incident))
        return await self._begin(incident, "custom", started_by, "the report form", wait)

    async def _begin(self, incident: dict[str, Any], source: str, started_by: str, via: str, wait: bool) -> str:
        incident_id = incident["incident_id"]
        await self.store.add(incident_id, source)
        await self.audit.append("INCIDENT_RECEIVED", "human" if started_by != "system" else "system", started_by,
                                {"source": source, "severity": incident["severity"],
                                 "alert_name": incident["alert_name"], "service": incident["affected_service"]},
                                incident_id)
        initial = {"incident": incident, "status": "RECEIVED",
                   "events": [event("api", f"Incident received from {via} (by {started_by})")]}
        await self._launch(incident_id, initial, wait)
        return incident_id

    async def resume(self, incident_id: str, decision: dict, wait: bool = False) -> None:
        if incident_id in self.pending_since:
            since, gate = self.pending_since.pop(incident_id)
            instruments().hitl_wait.record(time.time() - since, {"gate": gate})
        await self._launch(incident_id, Command(resume=decision), wait)

    async def resume_in_flight(self) -> list[str]:
        """Startup sweeper: continue incidents that were mid-run when the process died."""
        resumed = []
        for row in await self.store.all():
            iid = row["incident_id"]
            snap = await self.graph.aget_state(self._config(iid))
            waiting_on_human = any(t.interrupts for t in snap.tasks)
            if snap.values and snap.next and not waiting_on_human and snap.values.get("status") not in TERMINAL:
                await self.audit.append("INCIDENT_RESUMED", "system", "orchestrator",
                                        {"next": list(snap.next)}, iid)
                await self._launch(iid, None, wait=False)          # None = continue from the last checkpoint
                resumed.append(iid)
        return resumed

    async def _launch(self, incident_id: str, payload: Any, wait: bool) -> None:
        coro = self._run(incident_id, payload)
        if wait:
            await coro
            return
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(self, incident_id: str, payload: Any) -> None:
        phase = "start" if isinstance(payload, dict) else ("resume" if payload is not None else "recover")
        async with self.sem:
            try:
                # One trace per run: a HITL pause ends the trace instead of holding it open for hours.
                # incident.id on every span ties the runs together in Tempo.
                with tracer.start_as_current_span("incident.run") as s:
                    s.set_attribute("incident.id", incident_id)
                    s.set_attribute("run.phase", phase)
                    async for chunk in self.graph.astream(payload, self._config(incident_id), stream_mode="updates"):
                        await self._audit_chunk(incident_id, chunk)
            except Exception as e:  # recorded and shown as FAILED
                log.exception("incident %s failed", incident_id)
                self.errors[incident_id] = f"{type(e).__name__}: {e}"
                await self.audit.append("INCIDENT_FAILED", "system", "orchestrator", {"error": self.errors[incident_id]},
                                        incident_id)

    async def _audit_chunk(self, incident_id: str, chunk: dict[str, Any]) -> None:
        for node, update in chunk.items():
            if node == "__interrupt__":
                for intr in update:
                    v = intr.value
                    self.pending_since[incident_id] = (time.time(), v["gate"])
                    await self.audit.append("HITL_REQUEST", "system", "supervisor",
                                            {"gate": v["gate"], "required_role": v["required_role"],
                                             "plan_hash": v["plan_hash"], "risk_level": v["risk_level"],
                                             "steps": v["steps"]}, incident_id)
                continue
            actor_type, actor_id = ACTORS.get(node, ("system", "supervisor"))
            m = instruments()
            if node == "triage" and (update or {}).get("cache") is not None:
                m.cache_lookups.add(1, {"result": "hit" if update["cache"].get("hit") else "miss"})
            for u in (update or {}).get("usage", []):
                labels = {"model": u["model"], "agent": u["agent"]}
                m.llm_tokens.add(u.get("input_tokens", 0), {**labels, "direction": "in"})
                m.llm_tokens.add(u.get("output_tokens", 0), {**labels, "direction": "out"})
                m.llm_cost.add(u.get("cost_usd", 0), labels)
                if self.ledger:
                    await self.ledger.record(incident_id, u)
                await self.audit.append("TOKEN_USAGE", actor_type, actor_id, u, incident_id)
            for e in (update or {}).get("events", []):
                payload = {k: v for k, v in e.items() if k not in ("type", "node")}
                await self.audit.append(e.get("type") or EVENT_TYPES.get(node, node.upper()), actor_type, actor_id,
                                        payload, incident_id)

    async def get(self, incident_id: str) -> dict[str, Any] | None:
        snap = await self.graph.aget_state(self._config(incident_id))
        error = self.errors.get(incident_id)
        if not snap.values:
            known = any(r["incident_id"] == incident_id for r in await self.store.all())
            if not known:
                return None
            # Accepted, but the graph hasn't written its first checkpoint yet.
            return {"status": "FAILED" if error else "RECEIVED", "events": [], "terminal": False,
                    "pending_decision": None, "error": error}
        values = dict(snap.values)
        pending = [i.value for t in snap.tasks for i in t.interrupts]
        status = values.get("status", "RECEIVED")
        if error:
            status = "FAILED"
        elif pending:
            status = f"AWAITING_{pending[0]['gate']}"
        return {**values, "status": status, "pending_decision": pending[0] if pending else None,
                "error": error, "terminal": status in TERMINAL}

    async def sweep_deadlines(self, now: float | None = None) -> list[tuple[str, str]]:
        """Timeout ladder for unanswered HITL gates. It never approves:
        APPROVAL unanswered >= promote_after -> ESCALATE (senior queue);
        ESCALATION unanswered >= expire_after -> EXPIRE (re-check metrics, close; nothing is executed)."""
        now = now or time.time()
        actions = []
        for row in await self.store.all():
            v = await self.get(row["incident_id"])
            p = (v or {}).get("pending_decision")
            if not p or "requested_at" not in p:
                continue
            waited = now - p["requested_at"]
            if p["gate"] == "APPROVAL" and waited >= self.promote_after_s:
                outcome, event_type = "ESCALATE", "HITL_PROMOTED"
            elif p["gate"] == "ESCALATION" and waited >= self.expire_after_s:
                outcome, event_type = "EXPIRE", "HITL_EXPIRED"
            else:
                continue
            iid = row["incident_id"]
            await self.audit.append(event_type, "system", TIMER,
                                    {"gate": p["gate"], "waited_s": round(waited), "plan_hash": p["plan_hash"]}, iid)
            await self.resume(iid, {"decision_id": f"timer-{uuid.uuid4().hex[:8]}", "outcome": outcome,
                                    "plan_hash": p["plan_hash"], "gate": p["gate"], "approver": TIMER, "role": None})
            actions.append((iid, outcome))
        return actions

    async def record_resolution(self, incident_id: str, recorded_by: str, how_fixed: str,
                                category: str) -> dict[str, Any]:
        """A human fixed an escalated incident outside the platform. Re-verify against live metrics; if the fix
        holds, draft a runbook entry for review. Nothing goes to runbooks.yaml or the semantic cache by itself."""
        v = await self.get(incident_id)
        if not v or v.get("status") != "ESCALATED":
            raise ValueError("Only escalated incidents can have a manual fix recorded.")
        metrics = await self.read_metrics(v["incident"]) if self.read_metrics else {}
        thresholds = slo_thresholds()
        verified = bool(metrics) and all(metrics[k] < t for k, t in thresholds.items() if k in metrics)
        row = {"incident_id": incident_id, "recorded_by": recorded_by, "how_fixed": how_fixed,
               "category": category, "verified": verified, "metrics": metrics}
        await self.store.add_resolution(row)
        await self.audit.append("MANUAL_RESOLUTION_RECORDED", "human", recorded_by,
                                {k: row[k] for k in ("how_fixed", "category", "verified", "metrics")}, incident_id)
        proposal = draft_runbook(v, category, how_fixed) if verified else None
        if proposal:
            await self.store.add_proposal(proposal, incident_id)
            await self.audit.append("RUNBOOK_PROPOSED", "system", "orchestrator", proposal, incident_id)
        return {"verified": verified, "metrics": metrics, "proposal": proposal}

    def deadline(self, pending: dict | None) -> dict[str, Any]:
        """For the console: when the timer acts next, and whether the request is overdue."""
        if not pending or "requested_at" not in pending:
            return {}
        limit = self.promote_after_s if pending["gate"] == "APPROVAL" else self.expire_after_s
        remaining = pending["requested_at"] + limit - time.time()
        return {"timer_action": "escalates" if pending["gate"] == "APPROVAL" else "expires",
                "timer_in_s": max(0, round(remaining)), "overdue": remaining <= limit / 2}

    async def list(self) -> list[dict[str, Any]]:
        out = []
        for row in await self.store.all():
            if cached := self._closed_rows.get(row["incident_id"]):
                out.append(cached)
                continue
            v = await self.get(row["incident_id"]) or {}
            inc = v.get("incident", {})
            out.append({"incident_id": row["incident_id"], "scenario_id": row["scenario_id"],
                        "title": inc.get("title"), "severity": inc.get("severity"),
                        "service": inc.get("affected_service"), "status": v.get("status"),
                        "gate": (v.get("gate") or {}).get("gate") or (v.get("pending_decision") or {}).get("gate"),
                        "required_role": (v.get("pending_decision") or {}).get("required_role"),
                        **self.deadline(v.get("pending_decision")),
                        "created_at": row["created_at"]})
            if v.get("terminal"):
                self._closed_rows[row["incident_id"]] = out[-1]
        return out
