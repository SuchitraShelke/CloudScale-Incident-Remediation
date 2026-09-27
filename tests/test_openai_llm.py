"""OpenAILLM: request shape, cost with cached tokens, refusal/incomplete handling, failover, provider selection.
Fake OpenAI client: no network, no spend."""

from types import SimpleNamespace

import httpx
import openai
import pytest

from cloudscale.common.scenarios import load_scenario
from cloudscale.orchestrator.llm.claude import _Triage
from cloudscale.orchestrator.llm.openai_llm import OpenAILLM, parse_price

DEEP, FAST = "gpt-5.4", "gpt-5.4-mini"
PRICES = {DEEP: (2.0, 0.2, 10.0), FAST: (0.5, 0.05, 2.0)}      # test values, not real prices


def usage(i=1000, o=200, cached=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, input_tokens_details=SimpleNamespace(cached_tokens=cached))


def resp(parsed, status="completed", refusal=None, u=None):
    content = [SimpleNamespace(type="refusal", refusal=refusal)] if refusal else [SimpleNamespace(type="output_text")]
    return SimpleNamespace(status=status, incomplete_details=None, output=[SimpleNamespace(content=content)],
                           output_parsed=parsed, usage=u or usage())


class FakeClient:
    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []
        self.responses = self

    def with_options(self, **_):
        return self

    async def parse(self, **kw):
        self.calls.append(kw)
        b = self.behaviour[kw["model"]]
        if isinstance(b, Exception):
            raise b
        return b


TRIAGE = _Triage(root_cause="heap too small", root_cause_category="memory_limit_too_low", llm_confidence=0.9,
                 affected_services=["orders-service"])
TIMEOUT = openai.APITimeoutError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))


def ctx(scenario="s01-oom-orders"):
    return {"incident": load_scenario(scenario).incident.model_dump(), "metrics": {}, "logs": "",
            "deployment": {"revision": 3}, "evidence": {}, "spent_usd": 0.0}


def llm(client):
    return OpenAILLM(client, DEEP, FAST, PRICES)


async def test_deep_request_shape_and_cost_with_cached_tokens():
    client = FakeClient({DEEP: resp(TRIAGE, u=usage(i=1000, o=200, cached=400))})
    out, u = await llm(client).triage(ctx())
    call = client.calls[0]
    assert call["model"] == DEEP and call["text_format"] is _Triage
    assert call["reasoning"] == {"effort": "medium"} and call["store"] is False
    assert call["prompt_cache_key"] == "cloudscale-triage"
    assert "<incident_data>" in call["input"] and "untrusted" in call["instructions"]
    assert out.root_cause_category == "memory_limit_too_low"
    assert u.cache_read_tokens == 400
    assert u.cost_usd == pytest.approx((600 * 2.0 + 400 * 0.2 + 200 * 10.0) / 1e6)


async def test_p2_routes_to_fast_model_with_low_effort():
    client = FakeClient({FAST: resp(TRIAGE)})
    await llm(client).triage(ctx("s02-cache-bloat"))
    assert client.calls[0]["model"] == FAST and client.calls[0]["reasoning"] == {"effort": "low"}


@pytest.mark.parametrize("bad", [resp(None, status="incomplete"), resp(None, refusal="I can't help with that"),
                                 resp(None), TIMEOUT])
async def test_unusable_deep_answer_fails_over_to_fast(bad):
    client = FakeClient({DEEP: bad, FAST: resp(TRIAGE)})
    _, u = await llm(client).triage(ctx())
    assert [c["model"] for c in client.calls] == [DEEP, FAST] and u.model == FAST and u.failover


async def test_both_models_down_falls_back_to_scripted():
    _, u = await llm(FakeClient({DEEP: TIMEOUT, FAST: TIMEOUT})).triage(ctx())
    assert u.model == "scripted" and u.failover.endswith("-> scripted")


async def test_missing_price_records_zero_cost_instead_of_guessing():
    client = FakeClient({DEEP: resp(TRIAGE)})
    _, u = await OpenAILLM(client, DEEP, FAST, {DEEP: None, FAST: None}).triage(ctx())
    assert u.cost_usd == 0.0 and u.input_tokens == 1000


def test_parse_price():
    assert parse_price("2.50,0.25,15") == (2.5, 0.25, 15.0)
    assert parse_price("") is None
    with pytest.raises(ValueError):
        parse_price("2.50,15")


@pytest.mark.parametrize(("env", "expected"), [
    ({"LLM_MODE": "live", "LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"}, "OpenAILLM"),
    ({"LLM_MODE": "live", "LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-test"}, "ClaudeLLM"),
    ({"LLM_MODE": "live", "LLM_PROVIDER": "openai", "OPENAI_API_KEY": ""}, "ScriptedLLM"),
    ({"LLM_MODE": "scripted", "LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sk-test"}, "ScriptedLLM"),
])
def test_provider_selection(monkeypatch, env, expected):
    from cloudscale.common.config import Settings
    from cloudscale.orchestrator.main import select_llm
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(k, "")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert type(select_llm(Settings(_env_file=None))).__name__ == expected


async def test_summary_gets_requested_changes_not_raw_tool_output(mocked_backends):
    """A live run reported unchanged deployment fields as changes when the summarizer saw raw tool output."""
    from tests.test_graph import approve, build_env

    class Spy:
        def __init__(self):
            from cloudscale.orchestrator.llm.scripted import ScriptedLLM
            self.inner, self.summary_ctx = ScriptedLLM(), None

        async def triage(self, ctx):
            return await self.inner.triage(ctx)

        async def plan(self, ctx):
            return await self.inner.plan(ctx)

        async def summarize(self, ctx):
            self.summary_ctx = ctx
            return await self.inner.summarize(ctx)

    spy = Spy()
    env = build_env(llm=spy)
    iid = await env["service"].start("s01-oom-orders", wait=True)
    await env["service"].resume(iid, approve(await env["service"].get(iid)), wait=True)
    actions = spy.summary_ctx["actions"]
    assert actions[0]["requested_change"] == {"deployment": "orders-service",
                                              "patch": {"resources": {"limits": {"memory": "2Gi", "cpu": None},
                                                                      "requests": None},
                                                        "connection_pool_max_size": None, "tls_secret_ref": None,
                                                        "image_tag": None}}
    assert "output" not in actions[0] and "v2.8.1" not in repr(actions)


def test_model_chosen_step_ids_are_normalized_and_unique():
    """Live OpenAI named a step 'S1'; step IDs must match ^[a-z0-9-]{1,40}$ (they go into tokens and keys)."""
    from cloudscale.orchestrator.llm.claude import _Call, _Plan, _Step, _to_plan
    call = _Call(tool_name="clear_pod_cache",
                 tool_args_json='{"scope": {"namespace": "media", "cloud_provider": "azure"}, "service": "x"}')

    def step(sid):
        return _Step(step_id=sid, kind="tool", call=call, runbook_ref=None, llm_claims_destructive=False,
                     estimated_impact="", rollback=None)
    plan = _to_plan(_Plan(summary="s", artifacts=[], steps=[step("S1"), step("Step 2: Restart!"), step("s1"), step("__")]))
    assert [s.step_id for s in plan.steps] == ["s1", "step-2-restart", "s1-3", "step-4"]


async def test_plan_that_cannot_be_converted_fails_over_instead_of_crashing():
    from cloudscale.orchestrator.llm.claude import _Plan
    bad = _Plan(summary="s", artifacts=[], steps=[])           # RemediationPlan needs at least one step
    client = FakeClient({DEEP: resp(bad), FAST: resp(bad)})
    plan, u = await llm(client).plan({**ctx(), "triage": {}, "validation_errors": []})
    assert u.model == "scripted" and "ValidationError" in u.failover and plan.steps


async def test_planner_sees_the_guarded_pod_logs(mocked_backends):
    """The traffic growth needed to size a pool fix was only in the fetched logs, which the planner didn't get."""
    from cloudscale.orchestrator.llm.scripted import ScriptedLLM
    from tests.test_graph import build_env

    seen = {}

    class Spy(ScriptedLLM):
        async def plan(self, ctx):
            seen.update(ctx)
            return await super().plan(ctx)

    env = build_env(llm=Spy())
    await env["service"].start("s03-payment-pool", wait=True)
    assert "traffic 2.3x baseline" in seen["logs"]
