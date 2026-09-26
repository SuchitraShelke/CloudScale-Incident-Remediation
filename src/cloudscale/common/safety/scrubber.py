"""Secret scrubber + IP pseudonymization (data-exfiltration defence, OWASP LLM02).

Secrets are redacted outright. IPs are replaced with stable per-incident pseudonyms (IP_A, IP_B, ...)
so topology reasoning still works ("IP_A cannot reach IP_B") without leaking addresses. The mapping
lives in the incident state, so the alert payload and logs fetched later share the same pseudonyms.
"""

import re

PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.DOTALL), "[PEM_REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[JWT_REDACTED]"),
    (re.compile(r"\b((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s/@]+):[^@\s]+@"), r"\1:[REDACTED]@"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[AWS_KEY_REDACTED]"),
    (re.compile(r"(?i)\baws_secret_access_key\s*[:=]\s*\S+"), "aws_secret_access_key=[REDACTED]"),
    (re.compile(r"(?i)\b(AccountKey|SharedAccessKey)=[A-Za-z0-9+/=]{20,}"), r"\1=[REDACTED]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"), "[ANTHROPIC_KEY_REDACTED]"),
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|api[_-]?secret|token|bearer)(\s*[:=]\s*|\s+)[A-Za-z0-9._\-]{12,}"),
     r"\1=[REDACTED]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[EMAIL_REDACTED]"),
]
IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")


def _label(n: int) -> str:
    letters = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        letters = chr(65 + r) + letters
    return f"IP_{letters}"                     # IP_A ... IP_Z, IP_AA, ...


def scrub(text: str, pseudonyms: dict[str, str] | None = None) -> tuple[str, dict[str, str], int]:
    """Returns (scrubbed text, updated IP mapping, number of secrets redacted)."""
    mapping = dict(pseudonyms or {})
    redacted = 0
    for rx, repl in PATTERNS:
        text, n = rx.subn(repl, text)
        redacted += n

    def pseudo(m: re.Match[str]) -> str:
        ip = m.group(0)
        if ip not in mapping:
            mapping[ip] = _label(len(mapping))
        return mapping[ip]

    return IPV4.sub(pseudo, text), mapping, redacted
