"""Host-side stack check: `uv run python scripts/health_check.py`. Exit 1 if anything is down."""

import sys

import httpx

ENDPOINTS = {
    "orchestrator": "http://localhost:8000/health",
    "console": "http://localhost:8501/_stcore/health",
    "grafana": "http://localhost:3000/api/health",
}


def main() -> int:
    failed = False
    for name, url in ENDPOINTS.items():
        try:
            resp = httpx.get(url, timeout=5)
            resp.raise_for_status()
            print(f"[ok]   {name}")
            if name == "orchestrator":
                for dep, status in resp.json()["deps"].items():
                    bad = status.startswith("FAIL")
                    failed |= bad
                    print(f"  [{'FAIL' if bad else 'ok'}] {dep}: {status}")
        except (httpx.HTTPError, KeyError, ValueError) as e:
            failed = True
            print(f"[FAIL] {name}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
