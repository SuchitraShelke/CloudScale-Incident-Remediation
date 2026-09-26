"""Full rehearsal against the running stack: every scenario end to end, checked against its ground truth.

    uv run python scripts/rehearse.py              # all scenarios + cache, fault, audit, metrics checks
    uv run python scripts/rehearse.py --crash      # also kill the orchestrator mid-incident and check it resumes

Human decisions are made through the API with the right role (sre1 for APPROVAL, lead1 for ESCALATION),
exactly as a person would in the console. Exit code 1 if anything differs from the expected outcome.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

API = "http://localhost:8000"
SCENARIOS = Path(__file__).resolve().parents[1] / "mock-data" / "scenarios"
ROLE_FOR_GATE = {"APPROVAL": ("sre1", "sre1"), "ESCALATION": ("lead1", "lead1")}
results: list[tuple[str, bool, str]] = []


def session(user: str, password: str) -> httpx.Client:
    token = httpx.post(f"{API}/auth/login", json={"username": user, "password": password}, timeout=30).json()["token"]
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=60)


def wait(c: httpx.Client, iid: str, until, timeout_s: float = 180) -> dict:
    t0 = time.time()
    while True:
        try:
            v = c.get(f"/incidents/{iid}").json()
        except httpx.HTTPError:
            v = {}                                       # orchestrator restarting (crash test)
        if v and until(v):
            return v
        if time.time() - t0 > timeout_s:
            return v
        time.sleep(1)


def check(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name:34} {detail}")


def compose(*args: str) -> str:
    return subprocess.run(["docker", "compose", *args], capture_output=True, text=True, timeout=180,
                          check=False).stdout


def run_scenario(sre: httpx.Client, sid: str) -> dict:
    gt = json.loads((SCENARIOS / f"{sid}.json").read_text(encoding="utf-8"))["ground_truth"]
    t0 = time.time()
    iid = sre.post("/incidents", json={"scenario_id": sid}).json()["incident_id"]
    v = wait(sre, iid, lambda v: v.get("terminal") or str(v.get("status", "")).startswith("AWAITING"))
    gate = (v.get("pending_decision") or v.get("gate") or {}).get("gate")
    decisions = []
    while str(v.get("status", "")).startswith("AWAITING"):
        p = v["pending_decision"]
        user, pw = ROLE_FOR_GATE[p["gate"]]
        r = session(user, pw).post(f"/incidents/{iid}/decision",
                                   json={"outcome": "APPROVED", "plan_hash": p["plan_hash"],
                                         "comment": "rehearsal"})
        decisions.append(f"{user}:{r.status_code}")
        decided = p["plan_hash"]
        v = wait(sre, iid, lambda v, decided=decided: v.get("terminal")
                 or (v.get("pending_decision") or {}).get("plan_hash") not in (None, decided))
    status = v.get("status")
    expected_gate = gt["expected_gate"] if status != "QUARANTINED" else None
    ok = status == gt["expected_final_status"] and (expected_gate is None or gate == expected_gate)
    detail = (f"{status} via {gate or '-'} gate in {time.time() - t0:.0f}s"
              + (f" (decided by {', '.join(decisions)})" if decisions else "")
              + ("" if ok else f"  EXPECTED {gt['expected_final_status']} via {gt['expected_gate']}"))
    check(sid, ok, detail)
    return v


def main() -> int:
    sre = session("sre1", "sre1")
    print("Scenarios vs ground truth")
    for sid in sorted(p.stem for p in SCENARIOS.glob("s*.json")):
        run_scenario(sre, sid)

    print("\nSemantic cache")
    v = run_scenario(sre, "s02-cache-bloat")
    hit = (v.get("cache") or {}).get("hit")
    check("re-run served from cache", bool(hit), f"similarity {(v.get('cache') or {}).get('similarity')}"
          if hit else "no cache hit (was the first s02 run RESOLVED?)")

    print("\nFault -> circuit breaker -> rollback")
    compose("exec", "-T", "orchestrator", "python", "scripts/fault.py", "reset")
    compose("exec", "-T", "orchestrator", "python", "scripts/fault.py", "inject", "restart_service", "1")
    iid = sre.post("/incidents", json={"scenario_id": "s01-oom-orders"}).json()["incident_id"]
    v = wait(sre, iid, lambda v: str(v.get("status", "")).startswith("AWAITING"))
    sre.post(f"/incidents/{iid}/decision", json={"outcome": "APPROVED", "plan_hash": v["pending_decision"]["plan_hash"]})
    v = wait(sre, iid, lambda v: v.get("terminal"))
    steps = [(r["step_id"], r["status"]) for r in v.get("results", [])]
    check("fault rolls back the hotfix", v.get("status") == "ESCALATED" and
          ("raise-memory-limit-rollback", "ROLLED_BACK") in steps, f"{v.get('status')}: {steps}")
    compose("exec", "-T", "orchestrator", "python", "scripts/fault.py", "reset")

    if "--crash" in sys.argv:
        print("\nCrash during an incident -> resume from checkpoint")
        iid = sre.post("/incidents", json={"scenario_id": "s02-cache-bloat"}).json()["incident_id"]
        wait(sre, iid, lambda v: v.get("status") in ("VERIFYING", "RESOLVED"), 120)
        compose("kill", "-s", "SIGKILL", "orchestrator")          # no graceful shutdown
        compose("up", "-d", "orchestrator")
        sre = session_when_up()
        v = wait(sre, iid, lambda v: v.get("terminal"), 180)
        resumed = any("INCIDENT_RESUMED" == r["event_type"] for r in sre.get("/audit", params={"incident_id": iid}).json())
        check("killed mid-verify, resumed", v.get("status") == "RESOLVED" and resumed,
              f"{v.get('status')}, INCIDENT_RESUMED audited: {resumed}")

    print("\nAudit + metrics")
    ver = sre.get("/audit/verify").json()
    check("audit hash chain verifies", ver.get("ok") is True, f"{ver.get('rows_checked')} rows")
    m = sre.get("/metrics/live", params={"window": "1h"}).json()
    have = [k for k in ("lpt_p95_ms_by_agent", "chr_cache_hit_ratio", "tfr_tool_failure_rate", "tcr_tokens_per_min")
            if m.get(k) not in (None, [])]
    check("4 jury metrics reporting", len(have) == 4, ", ".join(have))

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def session_when_up() -> httpx.Client:
    for _ in range(90):
        try:
            if httpx.get(f"{API}/livez", timeout=3).status_code == 200:
                return session("sre1", "sre1")
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise SystemExit("orchestrator did not come back")


if __name__ == "__main__":
    sys.exit(main())
