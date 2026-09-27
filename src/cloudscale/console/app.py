"""CloudScale ops console (Streamlit). A thin client: every rule is enforced by the orchestrator API;
the console only shows what the API returns and sends decisions with the user's session token."""

from datetime import UTC, datetime

import httpx
import pandas as pd
import streamlit as st

from cloudscale.common.config import get_settings

API = get_settings().orchestrator_url
STATUS_COLOR = {"RESOLVED": "green", "PARTIALLY_RESOLVED": "orange", "AWAITING_APPROVAL": "orange",
                "AWAITING_ESCALATION": "red", "ESCALATED": "red", "QUARANTINED": "violet", "REJECTED": "gray",
                "FAILED": "red"}
GATE_COLOR = {"AUTO": "green", "APPROVAL": "orange", "ESCALATION": "red"}

st.set_page_config(page_title="CloudScale Ops Console", layout="wide")


# ---------------------------------------------------------------- API helpers
def api(method: str, path: str, **kw):
    headers = {"Authorization": f"Bearer {st.session_state.token}"} if "token" in st.session_state else {}
    try:
        r = httpx.request(method, f"{API}{path}", headers=headers, timeout=20, **kw)
    except httpx.HTTPError as e:
        st.error(f"Can't reach the orchestrator at {API}: {e}")
        st.stop()
    if r.status_code == 401 and "token" in st.session_state:
        st.session_state.clear()
        st.rerun()
    return r


def ok(r) -> dict | list | None:
    if r.is_success:
        return r.json()
    st.error(r.json().get("detail", r.text) if r.headers.get("content-type", "").startswith("application/json")
             else r.text)
    return None


def badge(text: str | None, colors: dict) -> str:
    return f":{colors.get(text or '', 'blue')}-badge[{text or '—'}]"


def ago(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).astimezone().strftime("%H:%M:%S")   # viewer's local time


# ---------------------------------------------------------------- login
if "token" not in st.session_state:
    st.title("CloudScale Ops Console")
    with st.form("login"):
        username = st.text_input("Username", key="login_user")
        password = st.text_input("Password", type="password", key="login_pw")
        if st.form_submit_button("Log in", type="primary"):
            r = api("POST", "/auth/login", json={"username": username, "password": password})
            if (body := ok(r)) is not None:
                st.session_state.token, st.session_state.user = body["token"], body["user"]
                st.rerun()
    st.caption("Demo users: `sre1` / `sre1` (SRE), `lead1` / `lead1` (senior SRE), `viewer` / `viewer`.")
    st.stop()

user = st.session_state.user
can_act = user["role"] in ("sre", "senior_sre")

# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown(f"**{user['username']}** · `{user['role']}`")
    if st.button("Log out", key="logout"):
        st.session_state.clear()
        st.rerun()
    views = ["Incidents", "Approvals", "Metrics", "Audit log"] + (["Report incident"] if can_act else [])
    page = st.radio("View", views, key="page")
    st.divider()
    if can_act:
        scenarios = ok(api("GET", "/scenarios")) or []
        options = {s["scenario_id"]: s for s in scenarios}
        pick = st.selectbox("Start a scenario", list(options), key="scenario",
                            format_func=lambda sid: f"{sid} — {options[sid]['title']}")
        if pick:
            st.caption(options[pick]["description"])
        started = st.button("Start incident", type="primary", key="start") and pick
        if started and (body := ok(api("POST", "/incidents", json={"scenario_id": pick}))) is not None:
            st.session_state.selected = body["incident_id"]
            st.toast(f"Started {body['incident_id']}")
    else:
        st.caption("Viewers can watch incidents but can't start them or decide.")


# ---------------------------------------------------------------- incident detail
def show_incident(iid: str) -> None:
    v = ok(api("GET", f"/incidents/{iid}"))
    if not v:
        return
    inc = v.get("incident", {})
    st.subheader(f"{iid} · {inc.get('title', '')}")
    st.markdown(f"{badge(v['status'], STATUS_COLOR)} &nbsp; severity `{inc.get('severity')}` · "
                f"service `{inc.get('affected_service')}` · `{inc.get('namespace')}` on `{inc.get('cloud_provider')}`")
    if v.get("error"):
        st.error(v["error"])

    if tri := v.get("triage"):
        c1, c2, c3 = st.columns(3)
        c1.metric("Confidence (min of the two)", f"{tri['confidence']:.2f}")
        c2.metric("LLM confidence", f"{tri['llm_confidence']:.2f}")
        c3.metric("Evidence score", f"{tri['evidence_score']:.2f}", help=(v.get("evidence") or {}).get("runbook_id"))
        st.markdown(f"**Root cause:** {tri['root_cause']} (`{tri['root_cause_category']}`)")

    gate = v.get("gate") or v.get("pending_decision")
    if v.get("plan"):
        st.markdown(f"**Plan:** {v['plan']['summary']}")
        steps = {s["step_id"]: s for s in (gate or {}).get("steps", [])}
        rows = []
        for s in v["plan"]["steps"]:
            g = steps.get(s["step_id"], {})
            rows.append({"step": s["step_id"],
                         "action": s["call"]["tool_name"] if s.get("call") else f"manual: {s.get('runbook_ref')}",
                         "op class": g.get("op_class"), "gate": g.get("gate"),
                         "impact $": g.get("impact_usd"), "why": ", ".join(g.get("reasons", [])),
                         "rollback": (s.get("rollback") or {}).get("tool_name", "")})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        for a in v["plan"].get("artifacts", []):
            with st.expander(f"Proposed artifact (never executed): {a['filename']}"):
                st.code(a["content"], language="yaml")

    if v.get("pending_decision"):
        decision_panel(iid, v["pending_decision"])

    if ver := v.get("verification"):
        st.markdown(f"**Verification:** {badge(ver['outcome'], STATUS_COLOR)} after {ver['polls']} poll(s)")
        df = pd.DataFrame({"before": ver["baseline"], "after": ver["final_metrics"],
                           "SLO": pd.Series(ver["thresholds"])})
        st.dataframe(df, width="content")
        if v.get("summary"):
            st.caption(v["summary"])

    if usage := v.get("usage"):
        spent = sum(u.get("cost_usd", 0) for u in usage)
        avoided = sum(u.get("avoided_cost_usd", 0) for u in usage)
        st.markdown(f"**Token ledger:** ${spent:.4f} spent" + (f" · ${avoided:.4f} avoided by the semantic cache"
                                                               if avoided else ""))
        st.dataframe(pd.DataFrame([{"agent": u["agent"], "model": u["model"], "in": u.get("input_tokens", 0),
                                    "out": u.get("output_tokens", 0), "cached in": u.get("cache_read_tokens", 0),
                                    "cost $": u.get("cost_usd", 0), "avoided $": u.get("avoided_cost_usd", 0),
                                    "failover": u.get("failover") or ""} for u in usage]),
                     hide_index=True, width="stretch")

    if v["status"] == "ESCALATED":
        escalated_panel(iid)

    with st.expander("Agent timeline", expanded=True):
        t0 = v["events"][0]["ts"] if v.get("events") else 0
        st.dataframe(pd.DataFrame([{"+s": round(e["ts"] - t0, 1), "agent": e["node"], "what happened": e["msg"]}
                                   for e in v.get("events", [])]), hide_index=True, width="stretch")


def escalated_panel(iid: str) -> None:
    """Escalated = handed to the incident process. Show the hand-off, and let an SRE record how it was fixed."""
    if h := ok(api("GET", f"/incidents/{iid}/handoff")):
        with st.expander("Hand-off for the on-call / incident channel", expanded=True):
            st.markdown(h["markdown"])
            st.code(h["markdown"], language="markdown")      # built-in copy button
    if not can_act:
        return
    with st.form(f"fix-{iid}"):
        st.markdown("**Record how it was fixed** (re-checked against live metrics; a verified fix drafts a "
                    "runbook proposal for review, never an active runbook)")
        how = st.text_area("What did you do?", key=f"how-{iid}",
                           placeholder="Rolled back notification-worker to v2.3.1; lag drained in 4 min")
        cat = st.text_input("Root-cause category", key=f"cat-{iid}", placeholder="bad_deploy")
        if st.form_submit_button("Record fix") and (r := ok(api("POST", f"/incidents/{iid}/resolution",
                                                                json={"how_fixed": how,
                                                                      "root_cause_category": cat}))):
            if r["verified"]:
                st.success(f"Verified: metrics are within SLO. Proposed {r['proposal']['id']} for review.")
            else:
                st.warning("Recorded, but metrics are still outside SLO, so no runbook was proposed.")


def decision_panel(iid: str, p: dict) -> None:
    allowed = {"APPROVAL": ("sre", "senior_sre"), "ESCALATION": ("senior_sre",)}[p["gate"]]
    can_decide = user["role"] in allowed
    with st.container(border=True):
        st.markdown(f"### {badge(p['gate'], GATE_COLOR)} Waiting for a `{p['required_role']}` decision")
        st.caption(f"Risk {p['risk_level']} · plan version `{p['plan_hash'][:12]}`. "
                   "Your decision is recorded in the audit log under your login.")
        comment = st.text_input("Comment (optional)", key=f"comment-{iid}")
        b1, b2, b3 = st.columns(3)
        send = None
        if b1.button("Approve", type="primary", disabled=not can_decide, key=f"approve-{iid}"):
            send = "APPROVED"
        if b2.button("Reject", disabled=not can_decide, key=f"reject-{iid}"):
            send = "REJECTED"
        if b3.button("Escalate to senior SRE", disabled=not can_act or p["gate"] == "ESCALATION",
                     key=f"escalate-{iid}"):
            send = "ESCALATE"
        if not can_decide:
            st.info(f"This gate needs role {' or '.join(allowed)}; you're logged in as `{user['role']}`.")
        if send and ok(api("POST", f"/incidents/{iid}/decision",
                           json={"outcome": send, "plan_hash": p["plan_hash"], "comment": comment})):
            st.toast(f"{send} sent for {iid}")


# ---------------------------------------------------------------- pages
@st.fragment(run_every=5)
def incidents_page() -> None:
    rows = ok(api("GET", "/incidents")) or []
    if not rows:
        st.info("No incidents yet. Start a scenario from the sidebar.")
        return
    table = pd.DataFrame([{"incident": r["incident_id"], "status": r["status"], "gate": r["gate"],
                           "sev": r["severity"], "service": r["service"], "title": r["title"],
                           "started": ago(r["created_at"])} for r in rows])
    st.dataframe(table, hide_index=True, width="stretch")
    ids = [r["incident_id"] for r in rows]
    current = st.session_state.get("selected")
    idx = ids.index(current) if current in ids else 0
    st.session_state.selected = st.selectbox("Incident", ids, index=idx, key="incident_pick")
    st.divider()
    show_incident(st.session_state.selected)


@st.fragment(run_every=5)
def approvals_page() -> None:
    queue = ok(api("GET", "/hitl/queue")) or []
    if not queue:
        st.success("Nothing is waiting for a human decision.")
        st.divider()
        proposals_section()
        return
    for q in queue:
        who = "you can decide" if q["can_decide"] else f"needs {q['required_role']}"
        timer = ""
        if "timer_in_s" in q:
            m, sec = divmod(q["timer_in_s"], 60)
            timer = f" · {'OVERDUE · ' if q['overdue'] else ''}{q['timer_action']} in {m}m{sec:02d}s"
        with st.expander(f"{q['incident_id']} · {q['title']} · {q['gate']} ({who}){timer}",
                         expanded=q["can_decide"]):
            show_incident(q["incident_id"])
    st.caption("Unanswered approvals move to the senior queue; unanswered escalations expire. "
               "The timer never approves anything.")
    st.divider()
    proposals_section()


def proposals_section() -> None:
    props = ok(api("GET", "/runbooks/proposals")) or []
    st.markdown("**Runbook proposals from verified human fixes** (review before adding to runbooks.yaml)")
    if not props:
        st.caption("None yet. Record a fix on an escalated incident to create one.")
    for pr in props:
        with st.expander(f"{pr['id']} · {pr['root_cause_category']} · {pr['status']}"):
            st.json(pr)


def _num(v, fmt: str) -> str:
    return fmt.format(v) if isinstance(v, (int, float)) else "no data yet"


@st.fragment(run_every=15)
def metrics_page() -> None:
    window = st.segmented_control("Window", ["15m", "1h", "6h", "24h"], default="1h", key="window") or "1h"
    m = ok(api("GET", "/metrics/live", params={"window": window})) or {}
    c1, c2, c3, c4 = st.columns(4)
    lpt = {r["labels"].get("agent"): r["value"] for r in m.get("lpt_p95_ms_by_agent") or [] if r["value"] is not None}
    c1.metric("LPT · p95 per task", _num(max(lpt.values()) if lpt else None, "{:,.0f} ms"),
              help="Latency per agent task, p95 of the slowest agent")
    c2.metric("TCR · tokens/min", _num(m.get("tcr_tokens_per_min"), "{:,.1f}"),
              help="Token consumption rate (0 in scripted mode: no LLM tokens are spent)")
    c3.metric("CHR · cache hit ratio", _num(m.get("chr_cache_hit_ratio"), "{:.0%}"))
    c4.metric("TFR · tool failure rate", _num(m.get("tfr_tool_failure_rate"), "{:.1%}"))
    d1, d2, d3 = st.columns(3)
    d1.metric("LLM cost in window", _num(m.get("llm_cost_usd"), "${:.4f}"))
    d2.metric("HITL wait · p50", _num(m.get("hitl_wait_p50_s"), "{:,.0f} s"))
    open_breakers = [r["labels"].get("tool") for r in m.get("circuit_state") or [] if r["value"] == 2]
    d3.metric("Open circuit breakers", len(open_breakers), help=", ".join(open_breakers) or None)
    if lpt:
        st.markdown("**p95 latency by agent (ms)**")
        st.bar_chart(pd.Series(lpt).sort_values(ascending=False))
    st.caption("Full dashboards: Grafana http://localhost:3000 · traces: Jaeger http://localhost:16686 "
               "(search tag `incident.id=<id>`)")


EXAMPLE_LOG = (
    "2026-09-27T09:41:02Z WARN notification-worker: consumer lag 1,203,448 messages on topic notifications\n"
    "2026-09-27T09:41:05Z INFO notification-worker: consumer group rebalancing (3rd time in 5 min)\n"
    "2026-09-27T09:41:31Z ERROR notification-worker: send failed for batch 88213: upstream SMTP relay timeout")


def report_page() -> None:
    st.subheader("Report an incident")
    st.caption("For an incident that isn't one of the prepared scenarios. It runs through the same pipeline: guard, "
               "triage, planning and the gate. Its infrastructure is simulated generically from what you enter, "
               "with no known fix, so the system can diagnose and propose, but can't prove a fix worked.")
    with st.form("report", clear_on_submit=False):
        c1, c2 = st.columns(2)
        title = c1.text_input("Title", "Notification queue backlog growing", key="r_title")
        alert = c2.text_input("Alert name", "KafkaConsumerLagHigh", key="r_alert")
        c1, c2, c3, c4 = st.columns(4)
        service = c1.text_input("Service (lowercase, dashes)", "notification-worker", key="r_service")
        namespace = c2.text_input("Namespace", "messaging", key="r_ns")
        cloud = c3.selectbox("Cloud", ["aws", "azure"], key="r_cloud")
        severity = c4.selectbox("Severity", ["P2", "P1"], key="r_sev")
        c1, c2, c3, c4 = st.columns(4)
        cpu = c1.number_input("CPU %", 0.0, 100.0, 35.0, key="r_cpu")
        mem = c2.number_input("Memory %", 0.0, 100.0, 60.0, key="r_mem")
        err = c3.number_input("Error rate %", 0.0, 100.0, 4.2, key="r_err")
        p99 = c4.number_input("p99 latency (ms)", 0.0, 120000.0, 2600.0, key="r_p99")
        logs = st.text_area("Log lines (untrusted: they go through the guard)", EXAMPLE_LOG, height=140, key="r_logs")
        submitted = st.form_submit_button("Submit incident", type="primary")
    if submitted:
        body = {"title": title, "severity": severity, "alert_name": alert, "affected_service": service.strip(),
                "namespace": namespace.strip(), "cloud_provider": cloud,
                "cloud_region": "us-east-1" if cloud == "aws" else "westeurope",
                "telemetry": {"source": "prometheus", "cpu_usage_pct": cpu, "memory_usage_pct": mem,
                              "error_rate_pct": err, "request_latency_p99_ms": p99, "pod_restarts_last_1h": 0},
                "log_excerpt": logs}
        r = api("POST", "/incidents/custom", json=body)
        if r.status_code == 422 and isinstance(r.json().get("detail"), list):
            for problem in r.json()["detail"]:
                st.error(f"{problem.get('field', 'input')}: {problem.get('problem', problem)}")
        elif (res := ok(r)) is not None:
            st.session_state.selected = res["incident_id"]
            st.success(f"Started {res['incident_id']}. Open **Incidents** to follow it; it will appear in "
                       "**Approvals** if it needs a decision.")


def audit_page() -> None:
    c1, c2 = st.columns([1, 3])
    if c1.button("Verify chain", type="primary", key="verify"):
        v = ok(api("GET", "/audit/verify"))
        if v and v["ok"]:
            c2.success(f"Chain intact: {v['rows_checked']} rows, head `{v['head_hash'][:16]}…`")
        elif v:
            c2.error(f"Chain broken at seq {v['first_broken_seq']}: {v['problem']}")
    iid = st.text_input("Filter by incident ID", key="audit_filter")
    rows = ok(api("GET", "/audit", params={"incident_id": iid} if iid else {})) or []
    st.dataframe(pd.DataFrame([{"seq": r["seq"], "time": r["ts"][11:23], "incident": r["incident_id"],
                                "event": r["event_type"], "actor": f"{r['actor_type']}:{r['actor_id']}",
                                "hash": r["hash"][:12], "prev": r["prev_hash"][:12],
                                "payload": str(r["payload"])[:160]} for r in rows]),
                 hide_index=True, width="stretch")


{"Incidents": incidents_page, "Approvals": approvals_page, "Metrics": metrics_page, "Audit log": audit_page,
 "Report incident": report_page}[page]()
