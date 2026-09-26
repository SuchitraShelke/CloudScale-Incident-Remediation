"""Compile gate_policy.yaml into OPA data: `uv run python scripts/gen_opa_data.py`.

`--check` exits 1 if opa/data/data.json is stale (run it in CI / before the demo).
"""

import json
import sys
from pathlib import Path

from cloudscale.common.gate import load_policy

OUT = Path(__file__).resolve().parents[1] / "opa" / "data" / "data.json"


def build() -> str:
    p = load_policy()
    data = {"op_classes": p["op_classes"], "op_rank": p["op_rank"], "limits": p["limits"]}
    return json.dumps(data, indent=2, sort_keys=True) + "\n"


def main() -> int:
    content = build()
    if "--check" in sys.argv:
        stale = not OUT.exists() or OUT.read_text() != content
        print("opa data is STALE — run scripts/gen_opa_data.py" if stale else "opa data up to date")
        return int(stale)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(content)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
