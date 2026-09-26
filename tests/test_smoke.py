from fastapi.testclient import TestClient

from cloudscale.orchestrator.main import app


def test_orchestrator_health_reports_failed_deps_without_crashing(monkeypatch):
    monkeypatch.setenv("POSTGRES_DSN", "postgresql://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    monkeypatch.setenv("OLLAMA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("OPA_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("MCP_URL", "http://127.0.0.1:1/mcp")
    from cloudscale.common.config import get_settings

    get_settings.cache_clear()
    body = TestClient(app).get("/health").json()
    get_settings.cache_clear()
    assert body["service"] == "orchestrator"
    assert all(v.startswith("FAIL") for v in body["deps"].values())
