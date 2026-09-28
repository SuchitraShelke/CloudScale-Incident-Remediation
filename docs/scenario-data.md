# Scenario data: what's in the mock-data JSON, and why

Each file in [`mock-data/scenarios/`](../mock-data/scenarios/) has five top-level parts. The most important design idea is **who is allowed to see which part**:

| Part | Who reads it | Why it's kept separate |
|---|---|---|
| `scenario_id`, `description` | People and the console dropdown | A label; not used for any decision |
| **`incident`** | **The AI agents** | It's the only thing a real system would receive: the alert |
| **`simulation`** | **Only the MCP server's simulator** | It plays the role of the real cluster. The AI must discover it through tools, never read it |
| **`ground_truth`** | **Only `scripts/rehearse.py`** (the grader) | The answer key. If the AI could see it, the test would be meaningless |

A test ([test_graph.py:192](../tests/test_graph.py#L192)) checks that no word from `simulation` or `ground_truth` ever appears in an LLM prompt.

---

## 1. `incident`: the alert (what the AI sees)

Validated by the strict `Incident` schema ([schemas.py](../src/cloudscale/common/schemas.py)); unknown fields are rejected.

| Field | Example (s01) | Why it's there |
|---|---|---|
| `incident_id` | `INC-S01-…` | Unique ID. The pattern `INC-…` is enforced. It ties together the audit log, traces, tokens and the cache |
| `title` | "OOMKilled pods in orders-service" | Human summary; goes to the LLM after scrubbing |
| `severity` | `P1` / `P2` | **Drives model routing:** P1 uses the deep model, P2 the cheap one |
| `alert_name` | `KubePodOOMKilled` | The alert rule that fired. Some (such as `AZNetworkPartition`) are marked **critical in code** and force ESCALATION |
| `affected_service` | `orders-service` | Which service to act on. Must be a valid Kubernetes name (regex). It also looks up **revenue per minute** in the gate policy for the $ impact |
| `namespace` | `production` | Tokens are **scoped** to it; OPA denies a call to any other namespace |
| `cloud_provider` | `aws` / `azure` | Token scope, breaker key (one breaker per tool *per cloud*), cache filter |
| `cloud_region` | `us-east-1` | Context for the diagnosis |
| `telemetry` | memory 98.7%, errors 23.4%, p99 4500 ms, 12 restarts, `source: prometheus` | The metric snapshot at alert time, like the one Prometheus or Datadog attaches to an alert |
| `log_excerpt` | `OutOfMemoryError … OOMKilled` | The log lines attached to the alert. **Guarded and scrubbed** before the LLM sees them. **s05 hides its injection here** |
| `tags` | `["tier-1", …]` | Metadata. `tier-1` also sends P2 incidents to the deep model |

## 2. `simulation`: the fake cluster (the AI never sees it)

This replaces a real Kubernetes cluster, so the demo is safe and repeatable. The AI only experiences it **through MCP tool calls**.

| Field | What it holds | Why |
|---|---|---|
| `deployments` | Per service: `replicas`, `revision`, `previous_revision`, `image_tag`, `resources.limits` (memory/cpu), `connection_pool_max_size` | Returned by the `get_deployment_status` tool. The planner needs real current values: you can't raise a memory limit sensibly without knowing it's 1Gi now. `previous_revision` is the valid rollback target, and the simulator refuses a rollback to a revision that isn't in the history |
| `initial_metrics` | memory, errors, `p99_ms`, CPU | What `get_metrics` returns **before** any fix. The evaluator polls this to verify |
| `logs` | Log lines | What `fetch_k8s_logs` returns. **This differs from `log_excerpt` on purpose:** a real alert only carries a snippet, and the full logs come later from a tool. That's how **s06 hides its injection** (only in the fetched logs) and how **s03 hides the FALL26 campaign line** the planner needs in order to size the pool correctly |
| `effects` | Rules: *"when this tool is called with these arguments, move these metrics to these values over N seconds"* | Makes the cluster **react realistically**, so verification is a real test rather than a formality (details below) |
| `healthy_thresholds` | memory < 80, errors < 1, p99 < 500 | Documents the scenario author's intended "healthy" values. **The platform doesn't use it:** the evaluator uses its own SLOs from [`runbooks.yaml`](../src/cloudscale/common/runbooks.yaml) (the same numbers). That's deliberate: **a scenario can't grade its own success** |

### The `effects` rules

```json
{"when": {"tool": "apply_hotfix", "patch.resources.limits.memory": {"gte": "1536Mi"}},
 "then": {"recover_to": {"memory_usage_pct": 55, "error_rate_pct": 0.4, "p99_ms": 320},
          "over_seconds": 20}}
```

| Key | Meaning | Why |
|---|---|---|
| `when.tool` | Which tool call triggers the rule | Different fixes have different results |
| `when.<arg path>` + `gte` / `lt` | A condition on the arguments, understanding units such as `Mi`/`Gi` | **Checks whether the fix is big enough**, not just whether a fix happened |
| `recover_to` | Target metric values | What "the fix worked" looks like |
| `over_seconds` | Metrics move gradually, not instantly | Realistic: the evaluator must **poll and wait**, which exercises its polling loop and timeout |
| `relapse_after_seconds` | Metrics go back to bad afterwards | Models a **symptom fix** that doesn't last |

Rules are checked **top to bottom, and the first match wins** ([simulator.py](../src/cloudscale/mcp_server/simulator.py)), which lets a file express "good fix / weak fix / wrong fix". In s01:

| Plan does | Result | Final status |
|---|---|---|
| Hotfix to ≥ 1536Mi | Memory 55%, errors 0.4% | **RESOLVED** |
| Hotfix to 1100–1535Mi | Memory stays 88% | **PARTIALLY_RESOLVED** |
| Restart only | Better for a while, **relapses after 60 s** | Not a real fix |
| Rollback | Undoes the fix (metrics worsen on purpose) | Compensation |

Two safeguards in the simulator's code:
- A later action can't make metrics look *better* than a fix already in progress would, so a restart after a hotfix can't take credit for it.
- Only a rollback may make metrics worse.

**Scenarios with `"effects": []`** (s04, s05, s06) have no tool that fixes anything:
- **s04:** an AZ failover must be done by a human, so the incident escalates.
- **s05 and s06:** they should never reach execution; they get quarantined.

## 3. `ground_truth`: the answer key (only for grading)

| Field | Example (s01) | Used for |
|---|---|---|
| `root_cause_category` | `memory_limit_too_low` | Did triage diagnose correctly? |
| `expected_actions` | `apply_hotfix:resources.limits.memory`, `restart_service` | Did the plan do the right *kind* of thing? |
| `expected_gate` | `APPROVAL` | Did the gate land at the right level? |
| `expected_final_status` | `RESOLVED` | Did the whole run end correctly? |

`scripts/rehearse.py` compares a live run against these four fields. That's what "12/12 passed" means. It turns the demo into a **regression test against a real model**: a real model plans differently each run, and this catches it when it gets something wrong.

## 4. What is deliberately *not* in the JSON

| Not in the scenario | Where it lives instead | Why |
|---|---|---|
| Revenue per minute, disruption minutes, $ thresholds | [`gate_policy.yaml`](../src/cloudscale/common/gate_policy.yaml) | **A scenario can't lower its own risk.** The financial gate is platform policy |
| Which tools are destructive (op classes) | [`gate_policy.yaml`](../src/cloudscale/common/gate_policy.yaml) (also fed to OPA) | The server classifies tools; neither the data nor the LLM does |
| SLO "healthy" thresholds used for the verdict | [`runbooks.yaml`](../src/cloudscale/common/runbooks.yaml) | The platform judges success, not the scenario |
| Runbook signatures and evidence scores | [`runbooks.yaml`](../src/cloudscale/common/runbooks.yaml) | Confidence comes from the platform's own knowledge |

**For a report typed into the console:** only `incident` fields are accepted, and the API rejects any extra field. The system builds a *generic* simulation from the alert itself. So a typed report can't script its own success, and with no runbook match its confidence is capped at 0.35, which means a senior SRE decides.

---

## "Isn't this all hard-coded?"

The **infrastructure** is simulated, because we can't break a real cluster in a demo. But the AI never sees the simulation or the answer key. It has to discover the situation through the same MCP tools it would use in production. And the simulator checks whether the fix is actually correct, not just whether something was done. The scenario files are test fixtures, not the logic: any new incident, including one typed into the console, goes through the same code.
