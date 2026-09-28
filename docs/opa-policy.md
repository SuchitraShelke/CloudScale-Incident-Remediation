# OPA: the policy check on every tool call

## What OPA is

**OPA (Open Policy Agent)** is a small, separate program whose only job is to answer one question: **"Is this action allowed?"**

Think of it as a **security guard with a rulebook** at the door of a server room:
- The engineer (our MCP server) arrives with a request: *"I want to run `apply_hotfix` on orders-service, here's my ticket."*
- The guard doesn't do any work. It only **checks the request against the rulebook** and says **allow** or **deny, and here's why**.

The rulebook is written in **Rego**, OPA's policy language: [`opa/policies/mcp.rego`](../opa/policies/mcp.rego).

## Why a separate policy engine instead of `if` statements in Python

| Reason | What it means for us |
|---|---|
| **Separation of duties** | The code that *does* things (the MCP server) isn't the code that *decides whether it's allowed*. A bug in the tool code can't quietly skip the rules |
| **Readable, auditable rules** | All permission rules sit in one short file that a security reviewer can read without reading the app |
| **Testable on its own** | The rules have their own unit tests (`opa test`, 17 cases), separate from the app |
| **Industry standard** | OPA is a CNCF project, widely used for Kubernetes admission control and API authorization. It's the same pattern real platforms use |

In standard terms:
- **PEP (Policy Enforcement Point):** the **MCP server**. It stops the call if the answer is "deny".
- **PDP (Policy Decision Point):** **OPA**. It only decides.

## Where OPA sits in the flow

```
Executor ──(tool call + signed token)──► MCP server
                                           1. verify the token signature and expiry
                                           2. normalize the arguments, compute their hash
                                           3. ask OPA ──────► OPA: allow / deny + reasons
                                           4. denied? stop, audit the reasons
                                           5. check the single-use token and idempotency (Redis)
                                           6. run the tool on the simulator
```

**Every** tool call is checked by OPA, including read-only calls ([enforcement.py](../src/cloudscale/mcp_server/enforcement.py)). OPA runs as its own container (`opa`, port 8181).

## What the MCP server sends to OPA (the "input")

For each call, the MCP server builds a small JSON document. For s01's approved hotfix (shortened):

```json
{
  "tool": "apply_hotfix",
  "op_class": "DESTRUCTIVE",
  "args": {"scope": {"namespace": "production", "cloud_provider": "aws"},
           "deployment": "orders-service",
           "patch": {"resources": {"limits": {"memory": "1536Mi"}}}},
  "args_hash": "d61c…",
  "token": {"typ": "exec", "namespace": "production", "cloud_provider": "aws",
            "step": {"tool": "apply_hotfix", "op_class": "DESTRUCTIVE", "args_hash": "d61c…"},
            "approval": {"gate": "APPROVAL", "approver": "sre1"}},
  "counters": {"calls_last_minute": 3, "breaker_state": "CLOSED"}
}
```

- `op_class` is decided by the server from the tool name, never by the LLM.
- `args_hash` is a hash of the exact normalized arguments.
- `counters` come from Redis: calls in the last minute for this incident, and the tool's breaker state.

OPA also has **data**: which tool is which operation class, how the classes rank, and the rate limit ([`opa/data/data.json`](../opa/data/data.json)). That file is **generated from [`gate_policy.yaml`](../src/cloudscale/common/gate_policy.yaml)** by `scripts/gen_opa_data.py`, so the HITL gate and OPA always use **the same definitions**. `gen_opa_data.py --check` proves they match.

## The rules, in plain language

The policy is **default deny** (`default allow := false`). A call is allowed only if **no deny rule fires**. Each rule is a reason to say no:

| Rule | Plain meaning | The attack or mistake it stops |
|---|---|---|
| `malformed_input` | A required field is missing | A broken or crafted request slipping through because a rule had nothing to check (see below) |
| `unknown_tool` | The tool isn't on the list | Calling a tool that doesn't officially exist |
| `bad_token_type` | The token is neither `read` nor `exec` | Forged or odd tokens |
| `namespace_scope` | The call targets a different namespace than the token | A token for `production/orders` used against another team's namespace |
| `cloud_scope` | Same for the cloud | An AWS token used on Azure |
| `wildcard_arg` | Any argument contains `*` | "Restart `*`", i.e. everything at once |
| `read_token_mutation` | A **read** token used for a **change** | Triage (read-only) trying to restart something |
| `exec_tool_mismatch` | The token was issued for tool A, but the call is for tool B | Using an approval for `clear_pod_cache` to run `apply_hotfix` |
| `exec_args_mismatch` | The arguments differ from the approved ones (hash mismatch) | Approved "memory 1536Mi", then sending "memory 64Gi" |
| `op_class_escalated` | The call is riskier than the token allows | A token approved for a safe operation used for a destructive one |
| **`missing_approval`** | **A DESTRUCTIVE call without a named human approver** | **The key rule: no destructive change on an AUTO decision, ever** |
| `rate_limited` | More than 30 calls per minute for one incident | An agent stuck in a loop hammering the cluster |
| `circuit_open` | That tool's breaker is OPEN | Calling a tool that is currently failing |

When a call is denied, OPA returns **all** the reasons, for example `["exec_args_mismatch", "missing_approval"]`. They're written to the **audit log** (`POLICY_DECISION` rows), so a post-mortem shows exactly why it was blocked.

## Two subtle safety details

1. **The "missing field" trap.** In Rego, a rule that reads a missing field is *undefined*, not true, so it simply doesn't fire. Without protection, a request **missing** the token field would trigger **no** deny rule and be **allowed**. The `malformed_input` rule closes this: every required field must be present, or the call is denied. The human-approval check is also written *positively* (`approved_by_human`) and then negated, for the same reason.
2. **Fail closed.** If OPA is down, times out or returns nothing, the MCP server treats that as a **deny** (`opa_unavailable` / `opa_no_result`). A broken guard means a locked door, not an open one.

## How OPA differs from the HITL gate

| | HITL gate (in the orchestrator) | OPA (at the MCP server) |
|---|---|---|
| **Question** | "Does a human need to look at this plan?" | "Is this exact call allowed, right now, with this token?" |
| **When** | Once per plan, before execution | On **every single tool call** |
| **Decides** | AUTO / APPROVAL / ESCALATION | allow / deny |

They're **two independent layers** (defense in depth). Suppose the orchestrator had a bug, or was tricked into skipping the human gate. OPA at the tool door would still refuse a destructive call without a named human approver. The MCP server also repeats a few critical checks in Python after OPA says yes (for example, "a read token can never mutate"), in case the policy file were ever misconfigured.

## How it's tested and shown

- **`opa test`:** 17 unit tests on the rules alone (allowed calls, each deny reason, malformed input), in [`opa/tests/`](../opa/tests/).
- **`scripts/mcp_smoke.py`:** 11 live attacks against the running stack, including a replayed token, tampered arguments, a read token used to mutate, a wrong namespace and a missing approval. Run it with `docker compose exec orchestrator python scripts/mcp_smoke.py`.
- **Audit log:** every call has a `POLICY_DECISION` row with OPA's verdict.

**In one line:** OPA is the policy decision point at the tool door. Every MCP call, read or write, is checked against a default-deny Rego policy: the token must match the namespace, cloud, tool, exact arguments and risk class, and destructive operations need a named human approver. If OPA can't answer, the call is denied.
