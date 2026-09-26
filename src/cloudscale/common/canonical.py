"""Canonical JSON + the hashes that bind approvals, tokens and idempotency to exact arguments."""

import hashlib
import json
from typing import Any

from pydantic import BaseModel


def canonical_json(obj: Any) -> str:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def args_hash(tool: str, args: dict[str, Any]) -> str:
    """Hash of a tool call. `scope.idempotency_key` is excluded: it is derived from this hash."""
    clean = json.loads(canonical_json(args))
    if isinstance(clean.get("scope"), dict):
        clean["scope"].pop("idempotency_key", None)
    return sha256_hex(canonical_json({"tool": tool, "args": clean}))


def plan_hash(plan: BaseModel | dict) -> str:
    return sha256_hex(canonical_json(plan))


def idempotency_key(incident_id: str, plan_hash_: str, step_id: str, args_hash_: str) -> str:
    """Unique per incident + plan version + step + exact args (fixes the v1/v2 `step_id`-only key)."""
    return sha256_hex(f"{incident_id}|{plan_hash_}|{step_id}|{args_hash_}")
