"""Scenario files: `incident` goes to the pipeline; `simulation` and `ground_truth` never reach an LLM."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cloudscale.common.schemas import Incident, Strict

DEFAULT_DIR = Path("mock-data/scenarios")


class GroundTruth(Strict):
    root_cause_category: str
    expected_actions: list[str]
    expected_gate: str
    expected_final_status: str


class Scenario(BaseModel):
    scenario_id: str
    description: str
    incident: Incident
    simulation: dict[str, Any]
    ground_truth: GroundTruth


def load_scenario(scenario_id: str, directory: Path = DEFAULT_DIR) -> Scenario:
    return Scenario.model_validate_json((directory / f"{scenario_id}.json").read_text(encoding="utf-8"))


def list_scenarios(directory: Path = DEFAULT_DIR) -> list[str]:
    return sorted(p.stem for p in directory.glob("s*.json"))
