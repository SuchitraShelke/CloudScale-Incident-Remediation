"""Write the "Show it in the code" table into each ADR: clickable file:line links for the jury demo.

Each entry names a pattern, not a line number, so the links follow the code as it changes:
    uv run python scripts/code_map.py          # regenerate the tables
    uv run python scripts/code_map.py --check  # exit 1 if any table is stale (tests/test_code_map.py runs this)
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADRS = ROOT / "docs" / "adrs"
START, END = "<!-- code-map:start -->", "<!-- code-map:end -->"
G, S, A = "src/cloudscale/orchestrator/graph.py", "src/cloudscale/orchestrator/service.py", "src/cloudscale/orchestrator/api.py"
ENF, SRV = "src/cloudscale/mcp_server/enforcement.py", "src/cloudscale/mcp_server/server.py"
LLM, POL = "src/cloudscale/orchestrator/llm/claude.py", "src/cloudscale/common/gate_policy.yaml"

# ADR file prefix -> [(what to show, file, regex matching the line to jump to)]
MAP: dict[str, list[tuple[str, str, str]]] = {
    "ADR-001": [
        ("Graph wiring: the deterministic supervisor (nodes + routing)", G, r"b\.add_node\(\"intake\""),
        ("Intake agent: scrub + guard before anything else", G, r"async def intake\("),
        ("Triage agent: read tools, evidence, cache, LLM diagnosis", G, r"async def triage\("),
        ("Planner agent", G, r"async def planner\("),
        ("Plan validation (schema, patch bounds, artifacts)", G, r"def validate_plan\("),
        ("HITL gate node", G, r"async def hitl_gate\("),
        ("Executor: one exec token per step, rollback on failure", G, r"async def execute\("),
        ("Evaluator: metrics decide the outcome, not the LLM", G, r"async def evaluate\("),
        ("Close: hand-off + verified-only cache write", G, r"async def close\("),
        ("Crash recovery: startup sweeper resumes checkpoints", S, r"async def resume_in_flight\("),
        ("Postgres checkpointer wired in", "src/cloudscale/orchestrator/main.py", r"AsyncPostgresSaver\.from_conn_string"),
    ],
    "ADR-002": [
        ("The 7 MCP tools (3 read, 4 mutating; no shell tool)", SRV, r"async def get_metrics\("),
        ("Enforcement pipeline, step by step", ENF, r"async def _run\("),
        ("Token signature + expiry check", ENF, r"claims = self\.verifier\.verify"),
        ("OPA decision (default deny)", ENF, r"if reasons := await self\._opa"),
        ("Idempotency key must match the token", ENF, r"expected = idempotency_key\("),
        ("Single-use exec token (jti, SET NX)", ENF, r"f\"jti:\{claims\.jti\}\", 1, nx=True"),
        ("Exec token minted per step, bound to args + approval", "src/cloudscale/common/tokens.py", r"def mint_exec\("),
        ("Token verification (EdDSA, aud, iss, exp, jti)", "src/cloudscale/common/tokens.py", r"def verify\(self, token"),
        ("OPA policy: default deny", "opa/policies/mcp.rego", r"^default allow := false"),
        ("OPA: destructive needs a named human", "opa/policies/mcp.rego", r"^approved_by_human if"),
        ("Client side: retries (reads only) + breaker", "src/cloudscale/orchestrator/tools.py", r"async for attempt in AsyncRetrying"),
        ("Circuit breaker states (CLOSED/OPEN/HALF_OPEN)", "src/cloudscale/orchestrator/breaker.py", r"^class CircuitBreaker"),
        ("Breaker: one probe after cooldown", "src/cloudscale/orchestrator/breaker.py", r"async def before_call\("),
        ("Pre-flight: an OPEN breaker blocks the plan before any change", G, r"if b and await b\.blocking\(\)"),
        ("Rollback of completed steps, in reverse order", G, r"# Compensate completed steps in reverse order"),
    ],
    "ADR-003": [
        ("Semantic cache: filtered pgvector lookup, similarity >= 0.92", "src/cloudscale/orchestrator/cache.py", r"async def lookup\("),
        ("Cache write (called only for verified fixes)", "src/cloudscale/orchestrator/cache.py", r"async def store\("),
        ("Cache hit used in triage (no LLM call)", G, r"hit = await deps\.cache\.lookup"),
        ("Only verified fixes are cached", G, r"# only verified fixes are ever cached"),
        ("Cached diagnosis capped at 0.79 in the gate", "src/cloudscale/common/gate.py", r"cache_confidence_cap\"\]\) if ctx\.from_cache"),
        ("Checkpoints in Postgres (durable state)", "src/cloudscale/orchestrator/main.py", r"AsyncPostgresSaver\.from_conn_string"),
        ("Audit hash chain", "src/cloudscale/common/audit.py", r"def chain_hash\("),
        ("Audit append-only triggers (UPDATE/DELETE/TRUNCATE blocked)", "src/cloudscale/common/sql/001_core.sql", r"CREATE TRIGGER audit_no_modify"),
        ("Token ledger (cost per incident)", "src/cloudscale/orchestrator/ledger.py", r"async def record\("),
    ],
    "ADR-004": [
        ("Gate policy: confidence tiers + matrix", POL, r"^confidence_tiers:"),
        ("Gate policy: financial thresholds ($10k / $50k)", POL, r"^financial:"),
        ("Per-step gate + overrides that only raise it", "src/cloudscale/common/gate.py", r"def step_gate\("),
        ("Plan gate = most restrictive step", "src/cloudscale/common/gate.py", r"def evaluate_plan\("),
        ("Composite confidence = min(LLM, evidence)", G, r"confidence = min\(out\.llm_confidence, evidence\.score\)"),
        ("Evidence score from the runbook catalogue", "src/cloudscale/common/runbooks.py", r"def score_evidence\("),
        ("Gate node: interrupt, role check, plan-hash binding", G, r"decision = interrupt\("),
        ("Roles per gate", A, r"^DECIDERS ="),
        ("Decision endpoint (approver from the session)", A, r"^async def decide\("),
        ("One decision per gate (claim before any await)", S, r"async def resume\("),
        ("Timeout ladder: promote, then expire; never approves", S, r"async def sweep_deadlines\("),
        ("Expiry path: nothing runs, metrics re-checked", G, r"if outcome == \"EXPIRE\" and"),
        ("Escalation hand-off", G, r"^def _handoff\("),
        ("Record a human fix, re-verify, propose a runbook", S, r"async def record_resolution\("),
    ],
    "ADR-005": [
        ("Strong injection signatures (quarantine on their own)", "src/cloudscale/common/safety/heuristics.py", r"^STRONG:"),
        ("LlamaGuard on cue windows", "src/cloudscale/common/safety/llama_guard.py", r"async def check\("),
        ("Fail closed on high-risk cues when the model can't answer", "src/cloudscale/common/safety/llama_guard.py", r"# No model verdict\. High-risk cues"),
        ("Intake guard (direct injection, s05)", G, r"g = await deps\.guard\.check\(f\"\{title\}"),
        ("Tool-output guard (indirect injection, s06)", G, r"# tool output is untrusted too"),
        ("Secret scrubber + IP pseudonyms", "src/cloudscale/common/safety/scrubber.py", r"^def scrub\("),
        ("Patch bounds validator", "src/cloudscale/common/safety/validators.py", r"^def validate_patch_bounds\("),
        ("Strict schemas: unknown fields rejected", "src/cloudscale/common/schemas.py", r"^class Strict\("),
        ("Fixed tool list: the LLM can't name another tool", "src/cloudscale/common/schemas.py", r"^ToolName = "),
        ("Session login + role check", "src/cloudscale/orchestrator/auth.py", r"^def require\("),
        ("Zero-trust: every tool call enforced in the MCP server", ENF, r"async def _run\("),
        ("OPA: destructive needs a named human", "opa/policies/mcp.rego", r"^approved_by_human if"),
    ],
    "ADR-006": [
        ("Routing: deep vs fast model, budget downgrade + stop", LLM, r"def route\("),
        ("Budget thresholds ($0.50 downgrade, $1.00 stop)", LLM, r"^DOWNGRADE_AT_USD, STOP_AT_USD"),
        ("Failover: routed model -> fast model -> scripted", LLM, r"async def _call\("),
        ("LLM output forced into the plan schema", LLM, r"^def _to_plan\("),
        ("OpenAI provider: structured outputs, prompt cache key", "src/cloudscale/orchestrator/llm/openai_llm.py", r"async def _parse\("),
        ("Model names per tier", "src/cloudscale/common/config.py", r"openai_model_deep:"),
        ("Provider selection at startup", "src/cloudscale/orchestrator/main.py", r"^def select_llm\("),
        ("Token ledger (measured cost)", "src/cloudscale/orchestrator/ledger.py", r"async def record\("),
    ],
    "ADR-007": [
        ("Local stand-in for the deployment: the compose stack", "docker-compose.yml", r"^services:"),
        ("Cloud scope carried on every tool call (aws / azure)", "src/cloudscale/common/schemas.py", r"^class Incident\("),
        ("OPA: token scoped to one cloud + namespace", "opa/policies/mcp.rego", r"deny contains \"cloud_scope\""),
    ],
}


def locate(path: str, pattern: str) -> int:
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
    rx = re.compile(pattern)
    for n, line in enumerate(lines, 1):
        if rx.search(line):
            return n
    raise SystemExit(f"code_map: pattern {pattern!r} not found in {path}")


def table(entries: list[tuple[str, str, str]]) -> str:
    rows = [f"| {what} | [{Path(p).name}:{(n := locate(p, rx))}](../../{p}#L{n}) |" for what, p, rx in entries]
    return "\n".join([START, "**Show it in the code** (generated by `scripts/code_map.py`; ctrl+click to jump):", "",
                      "| What | Where |", "|---|---|", *rows, END])


def render(text: str, block: str) -> str:
    if START in text:
        return re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, text, flags=re.DOTALL)
    return text.rstrip("\n") + "\n\n" + block + "\n"


def main(check: bool) -> int:
    stale = []
    for prefix, entries in MAP.items():
        f = next(ADRS.glob(f"{prefix}-*.md"))
        old = f.read_text(encoding="utf-8")
        new = render(old, table(entries))
        if new != old:
            stale.append(f.name)
            if not check:
                f.write_text(new, encoding="utf-8")
    if check and stale:
        print("stale code maps (run scripts/code_map.py):", ", ".join(stale))
        return 1
    print(("up to date: " if check else "written: ") + str(len(MAP)) + " ADRs")
    return 0


if __name__ == "__main__":
    sys.exit(main("--check" in sys.argv))
