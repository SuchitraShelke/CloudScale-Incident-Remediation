"""Stateful infrastructure simulator. State is per incident (a re-run starts fresh).

Effects from the scenario's `simulation.effects` move metrics linearly toward `recover_to`
over `over_seconds`; `relapse_after_seconds` reverts them afterwards (e.g. a restart that only
masks an OOM). Faults make a tool fail N times (circuit-breaker demo).
"""

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cloudscale.common.scenarios import load_scenario


class SimulatedToolFailure(Exception):
    pass


@dataclass
class _Transition:
    start: float
    origin: dict[str, float]
    target: dict[str, float]
    over_s: float
    relapse_after_s: float | None


def _quantity(v: Any) -> float | None:
    """Numbers as-is; Kubernetes quantities (512Mi, 2Gi) in MiB."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str) and v[:-2].isdigit() and v[-2:] in ("Mi", "Gi"):
        return float(v[:-2]) * (1024 if v[-2:] == "Gi" else 1)
    return None


def _matches(actual: Any, expected: Any) -> bool:
    """Effect rule condition: a literal, or {"gte": x} / {"lt": x} compared as numbers or quantities.
    Thresholds let any sufficient fix work (a planner choosing 60 instead of 50 still recovers)."""
    if isinstance(expected, dict) and set(expected) <= {"gte", "lt"}:
        a = _quantity(actual)
        if a is None:
            return False
        return all(a >= _quantity(v) if op == "gte" else a < _quantity(v) for op, v in expected.items())
    return actual == expected


def _lookup(obj: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


@dataclass
class IncidentSim:
    scenario_id: str
    simulation: dict[str, Any]
    clock: Callable[[], float]
    deployments: dict[str, dict] = field(default_factory=dict)
    transitions: list[_Transition] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.deployments = copy.deepcopy(self.simulation["deployments"])

    def metrics(self) -> dict[str, float]:
        m = dict(self.simulation["initial_metrics"])
        now = self.clock()
        for t in self.transitions:
            elapsed = now - t.start
            if t.relapse_after_s is not None and elapsed >= t.over_s + t.relapse_after_s:
                m.update({k: self.simulation["initial_metrics"][k] for k in t.target})
                continue
            frac = min(1.0, elapsed / t.over_s) if t.over_s else 1.0
            for k, target in t.target.items():
                m[k] = round(t.origin.get(k, m[k]) + (target - t.origin.get(k, m[k])) * frac, 2)
        return m

    def logs(self, lines: int) -> list[str]:
        return self.simulation.get("logs", [])[-lines:]

    def deployment(self, name: str) -> dict:
        if name not in self.deployments:
            raise SimulatedToolFailure(f"deployment {name!r} not found")
        return {"deployment": name, **copy.deepcopy(self.deployments[name])}

    def apply(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        target = args.get("deployment") or args.get("service")
        dep = self.deployments.get(target)
        if dep is None:
            raise SimulatedToolFailure(f"{target!r} not found")

        if tool == "apply_hotfix":
            patch = args["patch"]
            if patch.get("resources"):
                for kind, spec in patch["resources"].items():
                    if spec:
                        dep.setdefault("resources", {}).setdefault(kind, {}).update(
                            {k: v for k, v in spec.items() if v is not None})
            for key in ("connection_pool_max_size", "tls_secret_ref", "image_tag"):
                if patch.get(key) is not None:
                    dep[key] = patch[key]
            dep["previous_revision"], dep["revision"] = dep["revision"], dep["revision"] + 1
        elif tool == "rollback_deployment":
            if args["revision"] not in (dep["previous_revision"], dep["revision"]):
                raise SimulatedToolFailure(f"revision {args['revision']} not in rollout history")
            dep["revision"] = args["revision"]
        elif tool == "restart_service":
            dep["restarts"] = dep.get("restarts", 0) + 1

        self._run_effects(tool, args)
        self.actions.append({"tool": tool, "target": target, "at": self.clock()})
        return {"ok": True, "tool": tool, "target": target, "deployment": self.deployment(target)}

    def _settled(self) -> dict[str, float]:
        """Where metrics are heading once running transitions finish (relapse ignored)."""
        m = dict(self.simulation["initial_metrics"])
        for t in self.transitions:
            m.update(t.target)
        return m

    def _run_effects(self, tool: str, args: dict[str, Any]) -> None:
        call = {"tool": tool, **args}
        for effect in self.simulation.get("effects", []):
            if all(_matches(_lookup(call, k), v) for k, v in effect["when"].items()):
                then = effect["then"]
                # Metrics are lower-is-better. Rollback may worsen them on purpose ("undo the fix");
                # any other action only moves a metric if it beats where it is already heading,
                # so a restart right after a hotfix can't overwrite the hotfix's recovery.
                settled = self._settled()
                target = then["recover_to"] if tool == "rollback_deployment" else {
                    k: v for k, v in then["recover_to"].items() if v < settled.get(k, float("inf"))}
                if target:
                    self.transitions.append(_Transition(
                        start=self.clock(), origin=self.metrics(), target=target,
                        over_s=then.get("over_seconds", 0), relapse_after_s=then.get("relapse_after_seconds")))
                return


class Simulator:
    def __init__(self, scenarios_dir: Path, clock: Callable[[], float] = time.monotonic):
        self.dir, self.clock = scenarios_dir, clock
        self.incidents: dict[str, IncidentSim] = {}
        self.faults: dict[str, int] = {}      # tool -> remaining forced failures (all incidents)

    def register(self, incident_id: str, scenario_id: str) -> None:
        sc = load_scenario(scenario_id, self.dir)
        self.incidents[incident_id] = IncidentSim(scenario_id, sc.simulation, self.clock)

    def get(self, incident_id: str) -> IncidentSim:
        if incident_id not in self.incidents:
            raise SimulatedToolFailure(f"incident {incident_id!r} not registered with the simulator")
        return self.incidents[incident_id]

    def inject_fault(self, tool: str, fail_times: int) -> None:
        self.faults[tool] = fail_times

    def check_fault(self, tool: str) -> None:
        if self.faults.get(tool, 0) > 0:
            self.faults[tool] -= 1
            raise SimulatedToolFailure(f"injected fault: {tool} failed ({self.faults[tool]} more queued)")
