"""ClaudeLLM routing, budgets, failover and cost — with a fake Anthropic client (no network, no spend)."""

from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from cloudscale.common.scenarios import load_scenario
from cloudscale.orchestrator.llm.base import TriageOut
from cloudscale.orchestrator.llm.claude import BudgetExceeded, ClaudeLLM, _Call, _Plan, _Step, _to_plan

DEEP, FAST = "claude-sonnet-5", "claude-haiku-4-5"


def usage(i=1000, o=200, cache_read=0):
    return SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=cache_read,
                           cache_creation_input_tokens=0)


class FakeClient:
    """Stands in for AsyncAnthropic: records every parse() call; `behaviour[model]` is a result or an exception."""

    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []
        self.messages = self

    def with_options(self, **_):
        return self

    async def parse(self, **kw):
        self.calls.append(kw)
        b = self.behaviour[kw["model"]]
        if isinstance(b, Exception):
            raise b
        return b


def ok(parsed, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, stop_details=None, parsed_output=parsed, usage=usage())


TRIAGE = TriageOut(root_cause="heap too small", root_cause_category="memory_limit_too_low", llm_confidence=1.4)
TIMEOUT = anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def ctx(scenario="s01-oom-orders", spent=0.0):
    inc = load_scenario(scenario).incident.model_dump()
    return {"incident": inc, "metrics": {}, "logs": "", "deployment": {"revision": 3}, "evidence": {},
            "spent_usd": spent}


@pytest.mark.parametrize(("scenario", "agent", "spent", "expected"), [
    ("s01-oom-orders", "triage", 0.0, DEEP),        # P1
    ("s02-cache-bloat", "triage", 0.0, FAST),       # P2, not tier 1
    ("s03-payment-pool", "planner", 0.0, DEEP),     # tier-1 service
    ("s01-oom-orders", "evaluator", 0.0, FAST),     # summaries always on Haiku
    ("s01-oom-orders", "triage", 0.55, FAST),       # budget downgrade at $0.50
])
def test_routing(scenario, agent, spent, expected):
    assert ClaudeLLM(FakeClient({}), DEEP, FAST).route(agent, ctx(scenario, spent)) == expected


def test_budget_cap_stops_llm_calls():
    with pytest.raises(BudgetExceeded):
        ClaudeLLM(FakeClient({}), DEEP, FAST).route("triage", ctx(spent=1.0))


async def test_sonnet_request_shape_and_cost():
    client = FakeClient({DEEP: ok(TRIAGE)})
    out, u = await ClaudeLLM(client, DEEP, FAST).triage(ctx())
    call = client.calls[0]
    assert call["output_config"] == {"effort": "medium"} and call["output_format"].__name__ == "_Triage"
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "<incident_data>" in call["messages"][0]["content"] and "temperature" not in call
    assert out.llm_confidence == 1.0                                   # clamped
    assert u.model == DEEP and u.cost_usd == pytest.approx((1000 * 2 + 200 * 10) / 1e6)


async def test_haiku_gets_no_effort_parameter():
    client = FakeClient({FAST: ok(TRIAGE)})
    await ClaudeLLM(client, DEEP, FAST).triage(ctx("s02-cache-bloat"))
    assert "output_config" not in client.calls[0]                      # Haiku 4.5 rejects effort


async def test_failover_sonnet_to_haiku():
    client = FakeClient({DEEP: TIMEOUT, FAST: ok(TRIAGE)})
    _, u = await ClaudeLLM(client, DEEP, FAST).triage(ctx())
    assert [c["model"] for c in client.calls] == [DEEP, FAST]
    assert u.model == FAST and "APITimeoutError" in u.failover


async def test_failover_all_the_way_to_scripted():
    client = FakeClient({DEEP: TIMEOUT, FAST: TIMEOUT})
    out, u = await ClaudeLLM(client, DEEP, FAST).triage(ctx())
    assert u.model == "scripted" and u.failover.endswith("-> scripted")
    assert out.root_cause_category == "memory_limit_too_low"


async def test_refusal_counts_as_failure():
    client = FakeClient({DEEP: ok(None, stop_reason="refusal"), FAST: ok(TRIAGE)})
    _, u = await ClaudeLLM(client, DEEP, FAST).triage(ctx())
    assert u.model == FAST and "LLMUnavailable" in u.failover


def test_plan_conversion_parses_args_and_keeps_bad_json_for_validation():
    good = _Call(tool_name="clear_pod_cache",
                 tool_args_json='{"scope": {"namespace": "media", "cloud_provider": "azure"}, "service": "x"}')
    bad = _Call(tool_name="restart_service", tool_args_json="{not json")
    p = _to_plan(_Plan(summary="s", artifacts=[], steps=[
        _Step(step_id="a", kind="tool", call=good, runbook_ref=None, llm_claims_destructive=False,
              estimated_impact="", rollback=None),
        _Step(step_id="b", kind="tool", call=bad, runbook_ref=None, llm_claims_destructive=False,
              estimated_impact="", rollback=None)]))
    assert p.steps[0].call.tool_args["service"] == "x"
    assert "_invalid_json" in p.steps[1].call.tool_args            # validate_plan rejects -> re-plan
