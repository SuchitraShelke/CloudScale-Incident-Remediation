"""Capture the demo storyboard as screenshots: `uv run --with playwright python scripts/capture_screenshots.py`.

Drives the ops console (sre1 + lead1) through s02 twice, s01 with approval, s03 with the senior approval,
s05 and s06, then metrics, audit log, Grafana and a Jaeger trace. Needs the stack up and Microsoft Edge
installed (Playwright uses it, so no browser download). Run `scripts/demo_prep.py` first so the second s02
is a semantic-cache hit. Writes PNGs to docs/screenshots/ (or the directory given as the first argument).
"""
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

UI, API = "http://localhost:8501", "http://localhost:8000"
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/screenshots")
OUT.mkdir(parents=True, exist_ok=True)
FINAL = {"RESOLVED", "PARTIALLY_RESOLVED", "ESCALATED", "QUARANTINED", "REJECTED", "FAILED",
         "AWAITING_APPROVAL", "AWAITING_ESCALATION"}

tok = httpx.post(f"{API}/auth/login", json={"username": "sre1", "password": "sre1"}).json()["token"]
H = {"Authorization": f"Bearer {tok}"}


def ids() -> set[str]:
    return {r["incident_id"] for r in httpx.get(f"{API}/incidents", headers=H).json()}


def wait_status(iid: str, want: set[str], timeout=240) -> str:
    end = time.time() + timeout
    while time.time() < end:
        s = httpx.get(f"{API}/incidents/{iid}", headers=H).json()["status"]
        if s in want:
            return s
        time.sleep(2)
    raise TimeoutError(f"{iid} never reached {want}")


def settle(pg, ms=3000):
    pg.wait_for_timeout(ms)
    pg.wait_for_function("!document.querySelector('[data-testid=\"stStatusWidget\"]')", timeout=30000)


def shot(pg, name, height=1000):
    pg.set_viewport_size({"width": 1600, "height": height})
    settle(pg, 1500)
    pg.screenshot(path=str(OUT / f"{name}.png"))
    print("saved", name)


def login(pg, user):
    pg.goto(UI)
    pg.get_by_label("Username").fill(user)
    pg.get_by_role("textbox", name="Password").fill(user)
    pg.get_by_role("button", name="Log in").click()
    pg.locator("[data-testid=stSidebar]").get_by_text("Start a scenario").wait_for()
    settle(pg)


def view(pg, name):
    pg.locator("[data-testid=stSidebar]").get_by_text(name, exact=True).click()
    settle(pg)


def pick(pg, iid):
    view(pg, "Incidents")
    pg.locator("[data-testid=stMain] [data-testid=stSelectbox]").click()
    pg.get_by_role("option").filter(has_text=iid).first.click()
    settle(pg)
    pg.get_by_role("heading").filter(has_text=iid).first.wait_for()


def start(pg, sid) -> str:
    before = ids()
    sb = pg.locator("[data-testid=stSidebar] [data-testid=stSelectbox]")
    sb.click()
    pg.get_by_role("option").filter(has_text=sid).first.click()
    settle(pg, 1000)
    pg.get_by_role("button", name="Start incident").click()
    for _ in range(30):
        new = ids() - before
        if new:
            return new.pop()
        time.sleep(1)
    raise RuntimeError(f"{sid} didn't start")


with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge")
    pg = b.new_context(viewport={"width": 1600, "height": 1000}).new_page()

    pg.goto(UI)
    pg.get_by_label("Username").wait_for()
    shot(pg, "01-login", 700)
    login(pg, "sre1")

    iid = start(pg, "s02-cache-bloat")
    print("s02:", iid, wait_status(iid, FINAL))
    pick(pg, iid)
    shot(pg, "02-s02-auto-resolved", 2300)

    iid = start(pg, "s02-cache-bloat")
    print("s02 again:", iid, wait_status(iid, FINAL))
    pick(pg, iid)
    shot(pg, "03-s02-semantic-cache-hit", 2300)

    s01 = start(pg, "s01-oom-orders")
    print("s01:", s01, wait_status(s01, FINAL))
    view(pg, "Approvals")
    shot(pg, "04-s01-approval-pending", 1900)
    pg.get_by_role("button", name="Approve").first.click()
    print("s01 after approve:", wait_status(s01, FINAL - {"AWAITING_APPROVAL"}))
    pick(pg, s01)
    shot(pg, "05-s01-approved-resolved", 2400)

    s03 = start(pg, "s03-payment-pool")
    print("s03:", s03, wait_status(s03, FINAL))
    view(pg, "Approvals")
    pg.locator("[data-testid=stExpander] summary").filter(has_text=s03).first.click()
    pg.get_by_text("Waiting for a").first.wait_for()
    shot(pg, "06-s03-escalation-sre-blocked", 1900)

    lead = b.new_context(viewport={"width": 1600, "height": 1000}).new_page()
    login(lead, "lead1")
    view(lead, "Approvals")
    lead.get_by_role("button", name="Approve").first.wait_for()
    shot(lead, "07-s03-senior-can-approve", 1900)
    lead.get_by_role("button", name="Approve").first.click()
    print("s03 after lead approve:", wait_status(s03, FINAL - {"AWAITING_ESCALATION"}))

    s05 = start(pg, "s05-direct-injection")
    print("s05:", s05, wait_status(s05, FINAL))
    pick(pg, s05)
    shot(pg, "08-s05-direct-injection-quarantined", 1500)

    s06 = start(pg, "s06-indirect-injection")
    print("s06:", s06, wait_status(s06, FINAL))
    pick(pg, s06)
    shot(pg, "09-s06-indirect-injection-quarantined", 1500)

    view(pg, "Metrics")
    shot(pg, "10-metrics", 1100)

    view(pg, "Audit log")
    pg.get_by_role("button", name="Verify chain").click()
    pg.get_by_text("Chain intact").wait_for(timeout=30000)
    shot(pg, "11-audit-chain-verified", 1100)

    view(pg, "Report incident")
    shot(pg, "12-report-incident", 1100)

    g = b.new_context(viewport={"width": 1600, "height": 1100}).new_page()
    g.goto("http://localhost:3000/dashboards")
    g.wait_for_timeout(3000)
    link = g.locator("a[href*='/d/']").first
    link.click()
    g.wait_for_timeout(8000)
    g.screenshot(path=str(OUT / "13-grafana-dashboard.png"))
    print("saved grafana")

    now = datetime.now(UTC)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    res = httpx.get("http://localhost:16686/api/v3/traces", params={
        "query.service_name": "orchestrator", "query.attributes": json.dumps({"incident.id": s01}),
        "query.start_time_min": iso(now - timedelta(hours=1)), "query.start_time_max": iso(now)}).json()["result"]
    counts = Counter(sp["traceId"] for rs in res["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"])
    trace_id, n_spans = counts.most_common(1)[0]   # the resume trace: execute + evaluate + MCP spans
    j = b.new_context(viewport={"width": 1600, "height": 1300}).new_page()
    j.goto(f"http://localhost:16686/trace/{trace_id}")
    j.wait_for_timeout(6000)
    j.screenshot(path=str(OUT / "14-jaeger-trace-s01.png"))
    print("saved jaeger", n_spans, "spans")
    b.close()
