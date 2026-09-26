"""Fault injection + breaker control for demos. Runs inside the orchestrator container (the only one that
can reach the simulator and shares Redis with the breakers):

    docker compose exec orchestrator python scripts/fault.py inject restart_service 3
    docker compose exec orchestrator python scripts/fault.py status
    docker compose exec orchestrator python scripts/fault.py reset
"""

import sys

import httpx
import redis

from cloudscale.common.config import get_settings

S = get_settings()
SIM = S.mcp_url.removesuffix("/mcp")


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    r = redis.from_url(S.redis_url)
    if cmd == "inject":
        tool, times = sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 1
        resp = httpx.post(f"{SIM}/sim/faults", headers={"x-sim-key": S.sim_control_key},
                          json={"tool": tool, "fail_times": times}, timeout=10)
        print(f"next {times} call(s) to {tool} will fail: {resp.json()}")
    elif cmd == "reset":
        keys = list(r.scan_iter("breaker:*"))
        if keys:
            r.delete(*keys)
        for tool in ("restart_service", "apply_hotfix", "clear_pod_cache", "rollback_deployment", "get_metrics",
                     "fetch_k8s_logs", "get_deployment_status"):
            httpx.post(f"{SIM}/sim/faults", headers={"x-sim-key": S.sim_control_key},
                       json={"tool": tool, "fail_times": 0}, timeout=10)
        print(f"cleared {len(keys)} breaker key(s) and all injected faults")
    else:
        rows = [(k.decode(), {f.decode(): v.decode() for f, v in r.hgetall(k).items()})
                for k in sorted(r.scan_iter("breaker:*")) if not k.endswith(b":probe")]
        print("\n".join(f"{k:40} {v}" for k, v in rows) or "no breakers tripped yet (all CLOSED)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
