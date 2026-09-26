"""Start a scenario through the orchestrator API and print the agent timeline.

    uv run python scripts/run_scenario.py s02-cache-bloat
    uv run python scripts/run_scenario.py --list
Stops when the incident closes or waits for a human decision.
"""

import sys
import time

import httpx

API = "http://localhost:8000"
WAITING = ("AWAITING_APPROVAL", "AWAITING_ESCALATION")
USER, PASSWORD = "sre1", "sre1"      # demo SRE (DEMO_USERS)


def session() -> httpx.Client:
    token = httpx.post(f"{API}/auth/login", json={"username": USER, "password": PASSWORD}, timeout=30).json()["token"]
    return httpx.Client(base_url=API, headers={"Authorization": f"Bearer {token}"}, timeout=30)


def main() -> int:
    http = session()
    if len(sys.argv) < 2 or sys.argv[1] == "--list":
        for sc in http.get("/scenarios").json():
            print(f"{sc['scenario_id']:24} {sc['title']}")
        return 0

    iid = http.post("/incidents", json={"scenario_id": sys.argv[1]}).json()["incident_id"]
    print(f"incident {iid}\n")
    shown, t0 = 0, time.time()
    while True:
        v = http.get(f"/incidents/{iid}").json()
        events = v.get("events", [])
        for e in events[shown:]:
            print(f"  +{e['ts'] - t0:6.1f}s  {e['node']:<14} {e['msg']}")
        shown = len(events)
        if v["terminal"] or v["status"] in WAITING or v["status"] == "FAILED":
            break
        time.sleep(1)

    print(f"\nstatus: {v['status']}")
    if v.get("pending_decision"):
        p = v["pending_decision"]
        print(f"waiting for role '{p['required_role']}' (risk {p['risk_level']})")
        for s in p["steps"]:
            print(f"  {s['step_id']:<20} {s['op_class']:<14} {s['gate']:<11} ${s['impact_usd']:>8,.0f}  "
                  f"{', '.join(s['reasons'])}")
    if v.get("summary"):
        print(f"summary: {v['summary']}")
    if v.get("error"):
        print(f"error: {v['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
