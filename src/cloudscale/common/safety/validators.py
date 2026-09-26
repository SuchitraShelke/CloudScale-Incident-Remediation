"""Deterministic output validators (second guard layer). They check what the LLM PRODUCES —
recovery artifacts and hotfix arguments — not raw logs, so they never block benign telemetry."""

import re
from urllib.parse import urlparse

ALLOWED_EGRESS_HOSTS = {"registry.cloudscale.internal", "docs.cloudscale.internal", "kubernetes.io",
                        "learn.microsoft.com", "docs.aws.amazon.com"}

ARTIFACT_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("destructive_shell", re.compile(r"\brm\s+-[a-z]*r[a-z]*f?[a-z]*\s+/(\s|$|\*)")),
    ("remote_code_exec", re.compile(r"\b(curl|wget)\b[^\n|]*\|\s*(ba|z)?sh\b")),
    ("reverse_shell", re.compile(r"\bnc\s+(-\w+\s+)*-e\b|/dev/tcp/")),
    ("world_writable", re.compile(r"\bchmod\s+(-R\s+)?777\b")),
    ("privileged_container", re.compile(r"(?i)\bprivileged:\s*true\b")),
    ("host_network", re.compile(r"(?i)\bhost(Network|PID|IPC):\s*true\b")),
    ("host_path_mount", re.compile(r"(?i)\bhostPath:")),
    ("run_as_root", re.compile(r"(?i)\brunAsUser:\s*0\b")),
    ("cluster_admin_binding", re.compile(r"(?i)\bkind:\s*ClusterRoleBinding\b")),
    ("inline_secret", re.compile(r"(?is)\bkind:\s*Secret\b.*\b(data|stringData):")),
    ("iam_wildcard", re.compile(r'(?i)"Action"\s*:\s*"\*"|actions\s*=\s*\["\*"\]')),
]
URL = re.compile(r"https?://[^\s'\"<>)]+")
RAW_IP_URL = re.compile(r"https?://\d{1,3}(\.\d{1,3}){3}")


def validate_artifact(content: str) -> list[str]:
    violations = [name for name, rx in ARTIFACT_RULES if rx.search(content)]
    if RAW_IP_URL.search(content):
        violations.append("egress_raw_ip")
    for url in URL.findall(content):
        host = (urlparse(url).hostname or "").lower()
        if host and not any(host == h or host.endswith("." + h) for h in ALLOWED_EGRESS_HOSTS):
            violations.append(f"egress_not_allowlisted:{host}")
    return violations


def _quantity_mib(q: str) -> float:
    n, unit = int(q[:-2]), q[-2:]
    return n * 1024 if unit == "Gi" else n


def _cpu_millicores(q: str) -> float:
    return int(q[:-1]) if q.endswith("m") else int(q) * 1000


def validate_patch_bounds(patch: dict) -> list[str]:
    """HotfixPatch is already allowlisted by schema; this bounds its values."""
    out = []
    for kind in ("limits", "requests"):
        spec = ((patch.get("resources") or {}).get(kind)) or {}
        if spec.get("memory") and _quantity_mib(spec["memory"]) > 8 * 1024:
            out.append(f"memory_{kind}_above_8Gi")
        if spec.get("cpu") and _cpu_millicores(spec["cpu"]) > 4000:
            out.append(f"cpu_{kind}_above_4_cores")
    return out
