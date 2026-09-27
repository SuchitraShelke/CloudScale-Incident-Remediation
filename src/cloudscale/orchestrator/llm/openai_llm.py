"""OpenAI provider for the routed LLM (Responses API + structured outputs).

Same routing, budgets, failover, prompts and output schemas as ClaudeLLM (they live in RoutedLLM);
only the API call and the cost math differ:
- structured outputs: responses.parse(text_format=<Pydantic model>) -> resp.output_parsed
- reasoning effort: medium for the deep model, low for the fast one
- prompt caching is automatic on OpenAI; prompt_cache_key keeps each agent's prefix on one cache
- store=False: OpenAI does not retain the incident data sent in these requests
"""

import logging

import openai
from pydantic import BaseModel

from cloudscale.common.telemetry import span
from cloudscale.orchestrator.llm.base import LLMUsage
from cloudscale.orchestrator.llm.claude import SYSTEM, LLMUnavailable, RoutedLLM
from cloudscale.orchestrator.llm.scripted import ScriptedLLM

log = logging.getLogger("cloudscale.llm")

Price = tuple[float, float, float]      # $ per 1M tokens: input, cached input, output


def parse_price(spec: str | None) -> Price | None:
    """'input,cached_input,output' in $ per 1M tokens, e.g. '2.50,0.25,15.00'."""
    if not spec:
        return None
    parts = [float(x) for x in spec.split(",")]
    if len(parts) != 3:
        raise ValueError(f"price must be 'input,cached_input,output', got {spec!r}")
    return parts[0], parts[1], parts[2]


class OpenAILLM(RoutedLLM):
    retryable = (openai.APIError, openai.LengthFinishReasonError, openai.ContentFilterFinishReasonError)

    def __init__(self, client: openai.AsyncOpenAI, deep: str, fast: str, prices: dict[str, Price | None],
                 fallback: ScriptedLLM | None = None):
        super().__init__(client, deep, fast, fallback)
        self.prices = prices
        for model, price in prices.items():
            if price is None:
                log.warning("No price configured for %s: the token ledger will record $0 for it "
                            "(set OPENAI_PRICE_DEEP / OPENAI_PRICE_FAST)", model)

    def _cost(self, model: str, u) -> float:
        price = self.prices.get(model)
        if price is None:
            return 0.0
        cin, ccached, cout = price
        cached = (u.input_tokens_details.cached_tokens or 0) if u.input_tokens_details else 0
        # input_tokens includes the cached part; output_tokens includes reasoning tokens
        return ((u.input_tokens - cached) * cin + cached * ccached + u.output_tokens * cout) / 1_000_000

    async def _parse(self, model: str, agent: str, user: str, schema: type[BaseModel]):
        deep = model == self.deep
        with span("llm.call", model=model, agent=agent, provider="openai") as s:
            resp = await self.client.with_options(timeout=60 if deep else 90).responses.parse(
                model=model,
                instructions=SYSTEM[agent],             # trusted: rules, runbooks, tool schemas
                input=user,                             # untrusted incident data inside <incident_data>
                text_format=schema,
                max_output_tokens=16000 if deep else 4000,
                reasoning={"effort": "medium" if deep else "low"},
                prompt_cache_key=f"cloudscale-{agent}",
                store=False,
            )
            u = resp.usage
            s.set_attribute("llm.input_tokens", u.input_tokens if u else 0)
            s.set_attribute("llm.output_tokens", u.output_tokens if u else 0)
            s.set_attribute("llm.status", str(resp.status))

        if resp.status != "completed":
            raise LLMUnavailable(f"{model} returned status {resp.status} ({resp.incomplete_details})")
        for item in resp.output:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", None) == "refusal":
                    raise LLMUnavailable(f"{model} refused: {part.refusal[:120]}")
        if resp.output_parsed is None:
            raise LLMUnavailable(f"{model} returned no parseable output")
        cached = (u.input_tokens_details.cached_tokens or 0) if u.input_tokens_details else 0
        usage = LLMUsage(model=model, input_tokens=u.input_tokens, output_tokens=u.output_tokens,
                         cache_read_tokens=cached, cost_usd=round(self._cost(model, u), 6))
        return resp.output_parsed, usage
