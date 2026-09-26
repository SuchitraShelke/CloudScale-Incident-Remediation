"""MCP server end to end in-process: real MCPServer + Client, fakeredis, OPA mocked with respx.
The Rego rules themselves are tested by `opa test` (opa/tests/mcp_test.rego)."""

from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from mcp import Client

from cloudscale.common.canonical import args_hash, idempotency_key
from cloudscale.common.gate import load_policy
from cloudscale.common.tokens import ApprovalClaim, StepClaim, TokenIssuer, TokenVerifier
from cloudscale.common.tool_args import normalize
from cloudscale.mcp_server.enforcement import TOKEN_META_KEY, Enforcer
from cloudscale.mcp_server.server import build_server
from cloudscale.mcp_server.simulator import Simulator

OPA = "http://opa.test:8181"
DECISION = f"{OPA}/v1/data/cloudscale/mcp/decision"
INC = "INC-S01"
SCOPE = {"namespace": "production", "cloud_provider": "aws"}
HOTFIX = {"scope": SCOPE, "deployment": "orders-service",
          "patch": {"resources": {"limits": {"memory": "2Gi"}}}}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def env():
    key = Ed25519PrivateKey.generate()
    redis = fakeredis.aioredis.FakeRedis()
    clock = Clock()
    sim = Simulator(Path("mock-data/scenarios"), clock=clock)
    sim.register(INC, "s01-oom-orders")
    enforcer = Enforcer(TokenVerifier(key.public_key()), redis, OPA, load_policy()["op_classes"])
    server = build_server(enforcer, sim, "k")
    return {"issuer": TokenIssuer(key), "server": server, "sim": sim, "clock": clock,
            "enforcer": enforcer, "redis": redis}


def opa(allow=True, reasons=()):
    return respx.post(DECISION).mock(return_value=httpx.Response(
        200, json={"result": {"allow": allow, "reasons": list(reasons)}}))


def exec_call(issuer, tool, args, *, gate="APPROVAL", approver="sre1", plan="ph1", step_id="s1", op=None):
    """Mint an exec token bound to `args` and return (args_with_idem_key, token)."""
    norm = normalize(tool, args)
    ah = args_hash(tool, norm)
    op = op or load_policy()["op_classes"][tool]
    tok = issuer.mint_exec(incident_id=INC, namespace="production", cloud_provider="aws", plan_hash=plan,
                           step=StepClaim(step_id=step_id, tool=tool, op_class=op, args_hash=ah),
                           approval=ApprovalClaim(decision_id="d1", gate=gate, approver=approver, role="sre"))
    norm["scope"]["idempotency_key"] = idempotency_key(INC, plan, step_id, ah)
    return norm, tok


async def call(server, tool, args, token):
    async with Client(server) as c:
        return await c.call_tool(tool, args, meta={TOKEN_META_KEY: token} if token else None)


@respx.mock
async def test_lists_seven_tools(env):
    async with Client(env["server"]) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == set(load_policy()["op_classes"])


@respx.mock
async def test_read_token_reads_metrics(env):
    route = opa(allow=True)
    tok = env["issuer"].mint_read(INC, "production", "aws")
    r = await call(env["server"], "get_metrics", {"scope": SCOPE, "service": "orders-service"}, tok)
    assert not r.is_error and r.structured_content["metrics"]["memory_usage_pct"] == 98.7
    sent = route.calls.last.request.read().decode()
    assert '"op_class":"READ"' in sent.replace(" ", "") and '"typ":"read"' in sent.replace(" ", "")


@respx.mock
async def test_missing_and_forged_tokens_denied_before_opa(env):
    route = opa(allow=True)
    r = await call(env["server"], "get_metrics", {"scope": SCOPE, "service": "orders-service"}, None)
    assert r.is_error and "missing_token" in r.content[0].text
    forged = TokenIssuer(Ed25519PrivateKey.generate()).mint_read(INC, "production", "aws")
    r = await call(env["server"], "get_metrics", {"scope": SCOPE, "service": "orders-service"}, forged)
    assert r.is_error and "invalid_token" in r.content[0].text
    assert not route.called


@respx.mock
async def test_opa_deny_reasons_returned(env):
    opa(allow=False, reasons=["namespace_scope"])
    tok = env["issuer"].mint_read(INC, "production", "aws")
    r = await call(env["server"], "get_metrics", {"scope": SCOPE, "service": "orders-service"}, tok)
    assert r.is_error and "namespace_scope" in r.content[0].text
    assert env["enforcer"].decisions[-1]["outcome"] == "deny"


@pytest.mark.parametrize("side_effect", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")])
@respx.mock
async def test_opa_unreachable_fails_closed(env, side_effect):
    respx.post(DECISION).mock(side_effect=side_effect)
    tok = env["issuer"].mint_read(INC, "production", "aws")
    r = await call(env["server"], "get_metrics", {"scope": SCOPE, "service": "orders-service"}, tok)
    assert r.is_error and "opa_unavailable" in r.content[0].text


@respx.mock
async def test_opa_empty_result_fails_closed(env):
    respx.post(DECISION).mock(return_value=httpx.Response(200, json={}))
    tok = env["issuer"].mint_read(INC, "production", "aws")
    r = await call(env["server"], "get_metrics", {"scope": SCOPE, "service": "orders-service"}, tok)
    assert r.is_error and "opa_no_result" in r.content[0].text


@respx.mock
async def test_approved_hotfix_executes_and_metrics_recover(env):
    opa(allow=True)
    args, tok = exec_call(env["issuer"], "apply_hotfix", HOTFIX)
    r = await call(env["server"], "apply_hotfix", args, tok)
    assert not r.is_error
    inc = env["sim"].get(INC)
    assert inc.deployments["orders-service"]["resources"]["limits"]["memory"] == "2Gi"
    assert inc.deployments["orders-service"]["revision"] == 15
    env["clock"].t += 20
    assert inc.metrics()["memory_usage_pct"] == 55


@respx.mock
async def test_retry_with_same_token_returns_stored_result_without_reexecuting(env):
    opa(allow=True)
    args, tok = exec_call(env["issuer"], "apply_hotfix", HOTFIX)
    await call(env["server"], "apply_hotfix", args, tok)
    r = await call(env["server"], "apply_hotfix", args, tok)          # transport retry / replay
    assert not r.is_error and r.structured_content["idempotent_replay"] is True
    assert len(env["sim"].get(INC).actions) == 1


@respx.mock
async def test_resume_with_fresh_token_for_same_step_is_idempotent(env):
    opa(allow=True)
    args, tok1 = exec_call(env["issuer"], "apply_hotfix", HOTFIX)
    await call(env["server"], "apply_hotfix", args, tok1)
    _, tok2 = exec_call(env["issuer"], "apply_hotfix", HOTFIX)         # re-minted after a crash
    r = await call(env["server"], "apply_hotfix", args, tok2)
    assert not r.is_error and r.structured_content["idempotent_replay"] is True
    assert len(env["sim"].get(INC).actions) == 1


@respx.mock
async def test_same_step_id_in_another_plan_does_not_collide(env):
    opa(allow=True)
    a1, t1 = exec_call(env["issuer"], "restart_service", {"scope": SCOPE, "service": "orders-service"}, plan="p1")
    a2, t2 = exec_call(env["issuer"], "restart_service", {"scope": SCOPE, "service": "orders-service"}, plan="p2")
    await call(env["server"], "restart_service", a1, t1)
    r = await call(env["server"], "restart_service", a2, t2)
    assert "idempotent_replay" not in r.structured_content
    assert len(env["sim"].get(INC).actions) == 2


@respx.mock
async def test_idempotency_key_not_bound_to_token_is_denied(env):
    opa(allow=True)
    args, tok = exec_call(env["issuer"], "apply_hotfix", HOTFIX)
    args["scope"]["idempotency_key"] = "0" * 64
    r = await call(env["server"], "apply_hotfix", args, tok)
    assert r.is_error and "bad_idempotency_key" in r.content[0].text


@respx.mock
async def test_concurrent_duplicate_without_stored_result_is_replay_denied(env):
    opa(allow=True)
    args, tok = exec_call(env["issuer"], "apply_hotfix", HOTFIX)
    claims = env["enforcer"].verifier.verify(tok)
    await env["redis"].set(f"jti:{claims.jti}", 1)                  # first call in flight, no result yet
    r = await call(env["server"], "apply_hotfix", args, tok)
    assert r.is_error and "token_replayed" in r.content[0].text


@respx.mock
async def test_injected_fault_fails_then_recovers(env):
    opa(allow=True)
    env["sim"].inject_fault("restart_service", 1)
    a1, t1 = exec_call(env["issuer"], "restart_service", {"scope": SCOPE, "service": "orders-service"}, step_id="r1")
    r = await call(env["server"], "restart_service", a1, t1)
    assert r.is_error and "tool_failed" in r.content[0].text
    a2, t2 = exec_call(env["issuer"], "restart_service", {"scope": SCOPE, "service": "orders-service"}, step_id="r2")
    assert not (await call(env["server"], "restart_service", a2, t2)).is_error


@respx.mock
async def test_restart_alone_relapses(env):
    opa(allow=True)
    args, tok = exec_call(env["issuer"], "restart_service", {"scope": SCOPE, "service": "orders-service"})
    await call(env["server"], "restart_service", args, tok)
    inc = env["sim"].get(INC)
    env["clock"].t += 10
    assert inc.metrics()["error_rate_pct"] == 8
    env["clock"].t += 60
    assert inc.metrics()["error_rate_pct"] == 23.4


@respx.mock
async def test_schema_rejects_wildcards_and_unlisted_patch_keys(env):
    opa(allow=True)
    tok = env["issuer"].mint_read(INC, "production", "aws")
    r = await call(env["server"], "get_metrics", {"scope": {"namespace": "*", "cloud_provider": "aws"},
                                                  "service": "orders-service"}, tok)
    assert r.is_error
    bad = {**HOTFIX, "patch": {"securityContext": {"privileged": True}}}
    r = await call(env["server"], "apply_hotfix", bad, tok)
    assert r.is_error


def test_simulation_block_is_separate_from_incident():
    from cloudscale.common.scenarios import list_scenarios, load_scenario
    ids = list_scenarios()
    assert len(ids) == 6
    for sid in ids:
        sc = load_scenario(sid)
        dumped = sc.incident.model_dump_json()
        assert "recover_to" not in dumped and "root_cause_category" not in dumped
