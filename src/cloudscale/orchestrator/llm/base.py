"""LLM contract used by the agents. `scripted` (dev/tests/offline demo) and `live` (Claude, M4)
implement it. Contexts carry only sanitized incident data plus tool observations — never the
scenario's simulation or ground truth."""

from typing import Any, Protocol

from pydantic import BaseModel

from cloudscale.common.schemas import RemediationPlan


class TriageOut(BaseModel):
    root_cause: str
    root_cause_category: str
    llm_confidence: float
    affected_services: list[str] = []


class LLMUsage(BaseModel):
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    avoided_cost_usd: float = 0.0     # semantic-cache hit: the original call's cost
    cache_tier: str | None = None     # None | "semantic"
    failover: str | None = None       # e.g. "claude-sonnet-5: APITimeoutError -> claude-haiku-4-5"


class LLM(Protocol):
    async def triage(self, ctx: dict[str, Any]) -> tuple[TriageOut, LLMUsage]: ...
    async def plan(self, ctx: dict[str, Any]) -> tuple[RemediationPlan, LLMUsage]: ...
    async def summarize(self, ctx: dict[str, Any]) -> tuple[str, LLMUsage]: ...
