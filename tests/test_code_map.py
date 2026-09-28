"""The ADRs' file:line links must match the code, or a jury demo jumps to the wrong line."""

import subprocess
import sys
from pathlib import Path


def test_adr_code_maps_are_current():
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "scripts/code_map.py", "--check"], cwd=root, capture_output=True, text=True,
                       check=False)
    assert r.returncode == 0, r.stdout + r.stderr
