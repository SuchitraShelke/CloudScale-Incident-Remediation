"""Get the stack demo-ready: `uv run python scripts/demo_prep.py` (about 1 minute).

1. health check: every dependency reachable
2. reset circuit breakers and injected faults
3. empty the semantic cache, so the demo shows the first write and then the hit
4. check the embedding model is warm (the orchestrator loads it at startup; ~20 s on the dev VM)
5. print the demo URLs and the LLM mode

The audit log is deliberately NOT cleared: it is append-only by design, and "Verify chain" over the
rehearsal history is part of the demo.
"""

import subprocess
import sys
import time

import httpx

API = "http://localhost:8000"


def step(msg: str) -> None:
    print(f"\n== {msg}")


def compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", *args], capture_output=True, text=True, timeout=300, check=False)


def main() -> int:
    step("Health (waits up to 2 minutes for services that are still starting)")
    deadline = time.time() + 120
    while True:
        hc = subprocess.run([sys.executable, "scripts/health_check.py"], capture_output=True, text=True, check=False)
        if hc.returncode == 0 or time.time() > deadline:
            break
        time.sleep(5)
    print(hc.stdout.strip())
    if hc.returncode:
        print("Stack isn't healthy after 2 minutes. Try: docker compose up -d  (then re-run this script)")
        return 1

    step("Reset breakers and faults")
    print(compose("exec", "-T", "orchestrator", "python", "scripts/fault.py", "reset").stdout.strip())

    step("Empty the semantic cache")
    r = compose("exec", "-T", "postgres", "psql", "-U", "cloudscale", "-d", "cloudscale", "-tAc",
                "WITH d AS (DELETE FROM semantic_cache RETURNING 1) SELECT count(*) FROM d")
    print(f"removed {r.stdout.strip() or 0} cache entr(ies)" if r.returncode == 0 else r.stderr.strip())

    step("Embedding model (the orchestrator warms it in the background at startup)")
    for _ in range(30):
        if "embedding model warm: ok" in compose("logs", "orchestrator").stdout:
            print("warm")
            break
        time.sleep(2)
    else:
        print("not warm yet: the first cache lookup will take ~20 s. Wait a minute and re-run this script.")

    step("Ready")
    mode = httpx.get(f"{API}/health", timeout=30).json().get("llm_mode")
    print(f"LLM mode: {mode}" + ("  (real model: plans can vary between runs; falls back to scripted on errors)"
                                 if str(mode).startswith("live") else "  (deterministic, offline-safe)"))
    for name, url in (("Console", "http://localhost:8501  (sre1/sre1, lead1/lead1)"), ("Grafana", "http://localhost:3000"),
                      ("Jaeger", "http://localhost:16686"), ("API docs", "http://localhost:8000/docs")):
        print(f"  {name:9} {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
