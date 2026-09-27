from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_mode: Literal["scripted", "live"] = "scripted"
    llm_provider: Literal["anthropic", "openai"] = "anthropic"
    openai_api_key: str = ""
    openai_model_deep: str = "gpt-5.4"          # P1 / tier-1 triage + planning
    openai_model_fast: str = "gpt-5.4-mini"     # P2 + evaluator summaries + failover
    openai_price_deep: str = ""                 # "input,cached_input,output" $ per 1M tokens
    openai_price_fast: str = ""
    anthropic_api_key: str = ""
    model_deep: str = "claude-sonnet-5"
    model_fast: str = "claude-haiku-4-5"

    postgres_dsn: str = "postgresql://cloudscale:cloudscale@localhost:5432/cloudscale"
    audit_db_password: str = "audit-writer-dev"
    audit_dsn: str = "postgresql://audit_writer:audit-writer-dev@localhost:5432/cloudscale"   # INSERT/SELECT only
    session_secret: str = "dev-session-secret-change-me-32-bytes-min"
    demo_users: str = "viewer:viewer:viewer,sre1:sre1:sre,sre2:sre2:sre,lead1:lead1:senior_sre"
    redis_url: str = "redis://localhost:6379/0"
    opa_url: str = "http://localhost:8181"
    ollama_url: str = "http://localhost:11434"
    guard_model: str = "llama-guard3:1b"
    guard_timeout_s: float = 60.0   # dev host runs LlamaGuard at 15-45 s/call; over budget -> degraded
    mcp_url: str = "http://localhost:8001/mcp"
    orchestrator_url: str = "http://localhost:8000"

    token_private_key_path: str = "keys/signing_private.pem"   # orchestrator only
    token_public_key_path: str = "keys/signing_public.pem"
    sim_control_key: str = "dev-sim-key"    # shared secret for the simulator control routes
    scenarios_dir: str = "mock-data/scenarios"
    breaker_threshold: int = 3        # consecutive failed calls before a tool's breaker opens
    breaker_cooldown_s: float = 30.0
    fastembed_cache_dir: str | None = None   # image bakes the model into /opt/fastembed

    otel_enabled: bool = False             # compose sets OTEL_ENABLED=true
    otel_endpoint: str = "http://localhost:4317"
    otel_metrics_endpoint: str = "http://localhost:9090/api/v1/otlp/v1/metrics"   # Prometheus OTLP receiver
    prometheus_url: str = "http://localhost:9090"
    service_name: str = "cloudscale"


@lru_cache
def get_settings() -> Settings:
    return Settings()
