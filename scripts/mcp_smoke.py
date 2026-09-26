"""Live zero-trust check over real MCP HTTP + real OPA + real Redis.

Run inside the orchestrator container (the only one that can reach mcp-server and holds the key):
    docker compose exec orchestrator python scripts/mcp_smoke.py
Covers the v2 plan §19.2 security cases. Exit 1 on any unexpected result.
"""

import asyncio
import sys
import uuid
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp import Client

from cloudscale.common.canonical import args_hash, idempotency_key
from cloudscale.common.config import get_settings
from cloudscale.common.gate import load_policy
from cloudscale.common.tokens import ApprovalClaim, StepClaim, TokenIssuer, load_private_key
from cloudscale.common.tool_args import normalize

META = "io.cloudscale/token"
S = get_settings()
BASE = S.mcp_url.removesuffix("/mcp")
INC = f"INC-SMOKE-{uuid.uuid4().hex[:6]}"
SCOPE = {"namespace": "production", "cloud_provider": "aws"}
ISSUER = TokenIssuer(load_private_key(Path(S.token_private_key_path)))
OPS = load_policy()["op_classes"]
failures = 0


def exec_args(tool, args, *, gate="APPROVAL", approver="sre1", step_id="s1", op=None):
    norm = normalize(tool, args)
    ah = args_hash(tool, norm)
    tok = ISSUER.mint_exec(incident_id=INC, namespace="production", cloud_provider="aws", plan_hash="smoke",
                           step=StepClaim(step_id=step_id, tool=tool, op_class=op or OPS[tool], args_hash=ah),
                           approval=ApprovalClaim(decision_id="d", gate=gate, approver=approver))
    norm["scope"]["idempotency_key"] = idempotency_key(INC, "smoke", step_id, ah)
    return norm, tok


async def check(client, label, tool, args, token, expect):
    global failures
    r = await client.call_tool(tool, args, meta={META: token} if token else None)
    text = r.content[0].text if r.content else ""
    ok = (not r.is_error) if expect == "allow" else (r.is_error and expect in text)
    failures += not ok
    outcome = "allowed" if not r.is_error else text[:90]
    print(f"[{'PASS' if ok else 'FAIL'}] {label:52} -> {outcome}")
    return r


async def main() -> int:
    sim = httpx.AsyncClient(base_url=BASE, headers={"x-sim-key": S.sim_control_key})
    (await sim.post("/sim/register", json={"incident_id": INC, "scenario_id": "s01-oom-orders"})).raise_for_status()
    read = ISSUER.mint_read(INC, "production", "aws")
    metrics = {"scope": SCOPE, "service": "orders-service"}
    hotfix = {"scope": SCOPE, "deployment": "orders-service", "patch": {"resources": {"limits": {"memory": "2Gi"}}}}

    async with Client(S.mcp_url) as c:
        tools = sorted(t.name for t in (await c.list_tools()).tools)
        print(f"tools/list over streamable HTTP: {tools}")
        await check(c, "READ token -> get_metrics", "get_metrics", metrics, read, "allow")
        await check(c, "no token", "get_metrics", metrics, None, "missing_token")
        forged = TokenIssuer(Ed25519PrivateKey.generate())
        await check(c, "forged token (wrong key)", "get_metrics", metrics,
                    forged.mint_read(INC, "production", "aws"), "invalid_token")
        await check(c, "cross-namespace call", "get_metrics",
                    {"scope": {"namespace": "kube-system", "cloud_provider": "aws"}, "service": "x"}, read,
                    "namespace_scope")
        await check(c, "cross-cloud call", "get_metrics",
                    {"scope": {"namespace": "production", "cloud_provider": "azure"}, "service": "x"}, read,
                    "cloud_scope")
        a, _ = exec_args("apply_hotfix", hotfix)
        await check(c, "READ token -> apply_hotfix", "apply_hotfix", a, read, "read_token_mutation")
        a, t = exec_args("apply_hotfix", hotfix, gate="AUTO", approver="system")
        await check(c, "DESTRUCTIVE on an AUTO decision", "apply_hotfix", a, t, "missing_approval")
        a, t = exec_args("apply_hotfix", hotfix, op="SAFE_MUTATION")
        await check(c, "token approved as SAFE_MUTATION used for hotfix", "apply_hotfix", a, t, "op_class_escalated")
        a, t = exec_args("apply_hotfix", hotfix)
        tampered = {**a, "patch": {"resources": {"limits": {"memory": "8Gi"}}}}
        await check(c, "EXEC token with changed args", "apply_hotfix", tampered, t, "exec_args_mismatch")
        await check(c, "approved hotfix", "apply_hotfix", a, t, "allow")
        r = await check(c, "same token replayed", "apply_hotfix", a, t, "allow")
        replay = (r.structured_content or {}).get("idempotent_replay")
        print(f"       replay served from idempotency store (no 2nd execution): {replay}")

    state = (await sim.get(f"/sim/state/{INC}")).json()
    await sim.aclose()
    print(f"simulator actions executed: {len(state['actions'])} (expected 1); "
          f"memory limit now {state['deployments']['orders-service']['resources']['limits']['memory']}")
    bad = failures + (len(state["actions"]) != 1) + (replay is not True)
    print("ALL PASS" if not bad else f"{bad} FAILURE(S)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
