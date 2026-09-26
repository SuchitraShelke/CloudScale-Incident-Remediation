"""Deterministic evidence score from the runbook catalogue (no LLM involved)."""

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

CATALOGUE_PATH = Path(__file__).with_name("runbooks.yaml")


class Evidence(BaseModel):
    score: float
    runbook_id: str | None = None
    root_cause_category: str | None = None
    signature: str | None = None
    corroborated: bool = False
    manual: bool = False


@lru_cache
def load_catalogue(path: Path = CATALOGUE_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def slo_thresholds() -> dict[str, float]:
    return dict(load_catalogue()["slo_defaults"])


def score_evidence(log_text: str, metrics: dict[str, float]) -> Evidence:
    cat = load_catalogue()
    scores = cat["evidence_scores"]
    for rb in cat["runbooks"]:
        sig = next((p for p in rb["signatures"] if re.search(p, log_text)), None)
        if not sig:
            continue
        c = rb["corroborate"]
        corroborated = metrics.get(c["metric"], 0) > c["above"]
        return Evidence(score=scores["corroborated" if corroborated else "signature_only"],
                        runbook_id=rb["id"], root_cause_category=rb["root_cause_category"],
                        signature=sig, corroborated=corroborated, manual=rb.get("manual", False))
    return Evidence(score=scores["no_match"])
