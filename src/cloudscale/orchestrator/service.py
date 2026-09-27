"""Runs incidents through the graph: one LangGraph thread per incident (thread_id = incident_id).

Audit: node updates are streamed and each event is appended to the hash-chained log as the node
finishes. A node that calls interrupt() emits no update, so a re-run on resume never double-audits.
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langgraph.types import Command

from cloudscale.common.scenarios import load_scenario
from cloudscale.common.telemetry import instruments, tracer
from cloudscale.orchestrator.graph import TERMINAL, event
from cloudscale.orchestrator.store import IncidentStore

log = logging.getLogger("cloudscale.orchestrator")

EVENT_TYPES = {"intake": "GUARD_CHECK", "triage": "AGENT_DECISION", "planner": "PLAN_PROPOSED",
               "validate_plan": "PLAN_VALIDATED", "hitl_gate": "HITL_DECISION_APPLIED",
               "execute": "TOOL_EXECUTION", "evaluate": "VERIFICATION", "close": "INCIDENT_CLOSED"}
ACTORS = {"triage": ("agent", "triage"), "planner": ("agent", "planner"), "execute": ("agent", "executor"),
          "evaluate": ("agent", "evaluator")}          # everything else: the deterministic supervisor


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
                 store: IncidentStore, audit=None, ledger=None, max_concurrent: int = 5):
        self.graph, self.register_sim, self.dir, self.store = graph, register_sim, scenarios_dir, store
        self.audit, self.ledger = audit or NullAudit(), ledger
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
                        "created_at": row["created_at"]})
            if v.get("terminal"):
                self._closed_rows[row["incident_id"]] = out[-1]
        return out
