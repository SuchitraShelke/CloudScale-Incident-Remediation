"""Shared contracts between orchestrator, mcp-server and console."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

K8sName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")]
CloudProvider = Literal["aws", "azure"]
Severity = Literal["P1", "P2"]
OpClass = Literal["READ", "SAFE_MUTATION", "DISRUPTIVE", "DESTRUCTIVE", "MANUAL"]
Gate = Literal["AUTO", "APPROVAL", "ESCALATION"]
RiskLevel = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
ToolName = Literal[
    "get_metrics", "fetch_k8s_logs", "get_deployment_status",
    "clear_pod_cache", "restart_service", "apply_hotfix", "rollback_deployment",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------- incident (the only part of a scenario the pipeline sees) ----------

class Telemetry(Strict):
    source: Literal["prometheus", "datadog"]
    cpu_usage_pct: float
    memory_usage_pct: float
    error_rate_pct: float
    request_latency_p99_ms: float
    pod_restarts_last_1h: int = 0


class Incident(Strict):
    incident_id: str = Field(pattern=r"^INC-[A-Za-z0-9-]{1,40}$")
    title: str = Field(max_length=200)
    severity: Severity
    alert_name: str = Field(max_length=100)
    affected_service: K8sName
    namespace: K8sName
    cloud_provider: CloudProvider
    cloud_region: str = Field(max_length=40)
    telemetry: Telemetry
    log_excerpt: str = Field(max_length=100_000)
    tags: list[str] = Field(default_factory=list, max_length=20)


# ---------- plan ----------

class ToolCall(Strict):
    tool_name: ToolName
    tool_args: dict[str, Any]


class RecoveryArtifact(Strict):
    artifact_type: Literal["k8s_patch", "terraform", "ansible", "runbook"]
    filename: str
    content: str = Field(max_length=20_000)   # displayed + audited, never executed
    target_cloud: CloudProvider


class RemediationStep(Strict):
    step_id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    kind: Literal["tool", "manual_runbook"]
    call: ToolCall | None = None
    runbook_ref: str | None = None
    llm_claims_destructive: bool = False       # used only for mismatch detection
    estimated_impact: str = ""
    rollback: ToolCall | None = None


class RemediationPlan(Strict):
    summary: str
    steps: list[RemediationStep] = Field(min_length=1, max_length=10)
    artifacts: list[RecoveryArtifact] = Field(default_factory=list)


class TriageResult(Strict):
    root_cause: str
    root_cause_category: str
    llm_confidence: float = Field(ge=0, le=1)
    evidence_score: float = Field(ge=0, le=1)
    affected_services: list[str] = Field(default_factory=list)

    @property
    def confidence(self) -> float:
        """Composite: an LLM can never be more confident than the evidence allows."""
        return min(self.llm_confidence, self.evidence_score)


# ---------- gate ----------

class GateContext(Strict):
    incident_id: str
    severity: Severity
    service: str
    alert_name: str
    confidence: float
    from_cache: bool = False
    guard_degraded: bool = False
    tag_mismatch: bool = False
    policy_violation: bool = False


class StepGate(Strict):
    step_id: str
    op_class: OpClass
    gate: Gate
    impact_usd: float
    reasons: list[str]


class GateEvaluation(Strict):
    plan_hash: str
    gate: Gate
    risk_level: RiskLevel
    required_role: Literal["sre", "senior_sre"] | None
    steps: list[StepGate]
