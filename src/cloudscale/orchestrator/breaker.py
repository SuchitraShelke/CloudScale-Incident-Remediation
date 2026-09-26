"""Redis-backed circuit breaker, one per (tool, cloud). State is shared by replicas, survives restarts,
and is read by the MCP server's OPA input — so an OPEN breaker is enforced server-side as well.

CLOSED --(threshold consecutive failed logical calls)--> OPEN --(cooldown)--> HALF_OPEN
HALF_OPEN: exactly one probe (SET NX) decides CLOSED (success) or OPEN (failure).
Policy denials are not failures: the tool is healthy, the request was refused.
"""

import time


class CircuitOpenError(Exception):
    def __init__(self, key: str, retry_in_s: float):
        self.key, self.retry_in_s = key, retry_in_s
        super().__init__(f"circuit open for {key}; retry in {retry_in_s:.0f}s")


def _s(v) -> str | None:
    return v.decode() if isinstance(v, bytes) else v


class CircuitBreaker:
    def __init__(self, redis, tool: str, cloud: str, threshold: int = 3, cooldown_s: float = 30.0,
                 clock=time.time):
        self.r, self.key = redis, f"breaker:{tool}:{cloud}"
        self.threshold, self.cooldown, self.clock = threshold, cooldown_s, clock

    async def state(self) -> str:
        return _s(await self.r.hget(self.key, "state")) or "CLOSED"

    async def blocking(self) -> bool:
        """True while OPEN and still inside the cooldown. After the cooldown a call may go ahead as the probe."""
        if await self.state() != "OPEN":
            return False
        opened_at = float(_s(await self.r.hget(self.key, "opened_at")) or 0)
        return self.clock() - opened_at < self.cooldown

    async def before_call(self) -> None:
        state = await self.state()
        if state == "CLOSED":
            return
        if state == "OPEN":
            opened_at = float(_s(await self.r.hget(self.key, "opened_at")) or 0)
            wait = self.cooldown - (self.clock() - opened_at)
            if wait > 0:
                raise CircuitOpenError(self.key, wait)
        # OPEN past cooldown, or HALF_OPEN: only the holder of the probe lock may call. The lock expires,
        # so a prober that crashed mid-call can't leave the breaker stuck in HALF_OPEN.
        if not await self.r.set(f"{self.key}:probe", 1, nx=True, ex=int(self.cooldown) or 1):
            raise CircuitOpenError(self.key, self.cooldown)      # another caller is probing
        await self.r.hset(self.key, "state", "HALF_OPEN")

    async def on_success(self) -> str | None:
        prev = await self.state()
        await self.r.hset(self.key, mapping={"state": "CLOSED", "failures": 0})
        await self.r.delete(f"{self.key}:probe")
        return "CLOSED" if prev != "CLOSED" else None           # returns the new state when it changed

    async def on_failure(self) -> str | None:
        failures = await self.r.hincrby(self.key, "failures", 1)
        prev = await self.state()
        if prev == "HALF_OPEN" or failures >= self.threshold:
            await self.r.hset(self.key, mapping={"state": "OPEN", "opened_at": self.clock()})
            await self.r.delete(f"{self.key}:probe")
            return "OPEN" if prev != "OPEN" else None
        return None
