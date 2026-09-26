"""Deterministic injection pre-filter.

STRONG signatures are high precision and quarantine on their own. CUES are ambiguous: they only
select which short windows of text get sent to LlamaGuard. Text with neither is passed without a
model call — on the dev host a LlamaGuard call costs 15-45 s, so most log text must skip it.
Deliberately NOT cues: OOMKilled, `kill -9`, sudo, /etc/passwd — they are normal in infra logs.
"""

import re
from dataclasses import dataclass, field

_F = re.IGNORECASE

STRONG: dict[str, re.Pattern[str]] = {
    "instruction_override": re.compile(
        r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|all|earlier|system)\b"
        r"[^.\n]{0,20}\b(instructions?|prompts?|rules|directives?)\b",
        _F,
    ),
    "role_reassignment": re.compile(
        r"\byou are now\b|\bact as (an? )?(admin|root|system|developer)\b|\bnew (system )?instructions?\s*:", _F
    ),
    "tag_spoofing": re.compile(
        r"</?\s*(incident_data|system|instructions?|assistant)\s*>|<\|(im_start|im_end|eot_id|start_header_id)\|>",
        _F,
    ),
    "alert_suppression": re.compile(r"\b(do not|don't|never)\s+(alert|escalate|notify|page|trigger|report)\b", _F),
    "system_override": re.compile(r"\bsystem override\b|\bdeveloper mode\b|\bjailbreak\b", _F),
}

CUES: dict[str, re.Pattern[str]] = {
    "addresses_agent": re.compile(
        r"\b(ai|remediation|llm|autonomous)\s+(agent|assistant|bot)\b|\bassistant\b|\blanguage model\b|\bllm\b", _F
    ),
    "imperative_to_reader": re.compile(
        r"\byou (must|should|need to|will)\b|\bplease (run|execute|delete|send|disable|scale)\b", _F
    ),
    "exfil_verb": re.compile(
        r"\b(send|post|upload|forward|exfiltrate|leak|email)\b[^\n]{0,60}(https?://|\bto [\w.-]+@|\bwebhook\b)", _F
    ),
    "pipe_to_shell": re.compile(
        r"\b(curl|wget)\b[^\n|]{0,120}\|\s*(ba|z)?sh\b|\bnc\s+-e\b|\bbase64\s+(-d|--decode)\b", _F
    ),
    "destructive_request": re.compile(
        r"\b(delete|drop|destroy|wipe|scale\s+\S+\s+to\s+(0|zero))\b[^\n]{0,40}"
        r"\b(namespace|cluster|database|all|production|prod)\b",
        _F,
    ),
    "secret_request": re.compile(
        r"\b(reveal|print|dump|show|send|share)\b[^\n]{0,40}"
        r"\b(api[_ -]?keys?|secrets?|credentials?|passwords?|tokens?|kubeconfig|/etc/shadow)\b",
        _F,
    ),
}

# Cues that quarantine on their own when LlamaGuard can't give a verdict (fail closed).
HIGH_RISK_CUES = {"exfil_verb", "pipe_to_shell", "secret_request"}

WINDOW_CHARS = 200    # context kept on each side of a cue match
MAX_SEGMENTS = 3       # bounds worst-case LlamaGuard time per text
MAX_SEGMENT_CHARS = 600


@dataclass
class HeuristicScan:
    strong: list[str] = field(default_factory=list)
    cues: list[str] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)


def scan(text: str) -> HeuristicScan:
    result = HeuristicScan(
        strong=[name for name, rx in STRONG.items() if rx.search(text)],
        cues=[name for name, rx in CUES.items() if rx.search(text)],
    )
    if result.strong or not result.cues:
        return result

    spans = sorted(
        (max(0, m.start() - WINDOW_CHARS), min(len(text), m.end() + WINDOW_CHARS))
        for rx in CUES.values()
        for m in rx.finditer(text)
    )
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    result.segments = [text[s:e][:MAX_SEGMENT_CHARS] for s, e in merged[:MAX_SEGMENTS]]
    return result
