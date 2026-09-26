# CloudScale Incident Remediation — working notes for Claude

Solo developer + Claude, 2-day capstone build. Graded by `Capstone_evalutation_rubric.txt`.

- **Build spec:** `PROTOTYPE_SCOPE.md` (what we build; milestone checklist at the bottom).
- **Blueprint:** `CapstoneProjectPlan_v2.md` (target architecture for docs; do NOT build
  everything in it — only what PROTOTYPE_SCOPE.md lists).

## Layout
- `src/cloudscale/common/` — shared schemas, gate policy, safety, audit client, telemetry
- `src/cloudscale/orchestrator/` — FastAPI + LangGraph (agents, HITL, token minting)
- `src/cloudscale/mcp_server/` — FastMCP tools + simulator + token/OPA enforcement
- `src/cloudscale/console/` — Streamlit ops console
- `mock-data/scenarios/` — scenario files (`incident` / `simulation` / `ground_truth`)
- `opa/` — Rego policy + tests · `db/init/` — Postgres init SQL · `observability/` — Grafana
- `tests/` — pytest · `docs/` — ADRs, diagrams, financials

## Commands
- Install / sync: `uv sync`
- Tests: `uv run pytest -q`
- Lint: `uv run ruff check src tests`
- Stack: `docker compose up -d --build` · health: `uv run python scripts/health_check.py`
- Logs: `docker compose logs -f orchestrator mcp-server`

## Conventions
- Python 3.12, Pydantic v2, async everywhere in services.
- Config via `cloudscale.common.config.Settings` (pydantic-settings, env vars) — no ad-hoc `os.environ`.
- `LLM_MODE=scripted` is the default for dev and tests; never require an API key for tests.
- Security-critical code (gate, tokens, OPA input, audit chain) gets table-driven tests first.
- Nothing from the scenario `simulation` or `ground_truth` blocks may reach an LLM prompt.
- Keep code small: the developer must be able to explain every module to the jury.
