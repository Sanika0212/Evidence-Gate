"""Per-tenant token bucket, atomic in Redis so every gateway replica shares it."""

from __future__ import annotations

import time
from typing import Any

_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1]) or burst
local ts = tonumber(data[2]) or now
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = (1 - tokens) / rate
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, math.ceil(burst / rate) + 1)
return {allowed, tostring(retry)}
"""


class RedisRateLimiter:
    def __init__(self, client: Any, per_minute: int, burst: int) -> None:
        self.client = client
        self.rate = per_minute / 60.0
        self.burst = burst
        self._script = client.register_script(_LUA)

    def allow(self, tenant: str) -> tuple[bool, float]:
        allowed, retry = self._script(
            keys=[f"eg:rl:{tenant}"], args=[self.rate, self.burst, time.time()]
        )
        return bool(int(allowed)), float(retry)


class MemoryRateLimiter:
    def __init__(self, per_minute: int, burst: int, clock: Any = time.monotonic) -> None:
        self.rate = per_minute / 60.0
        self.burst = burst
        self.clock = clock
        self._state: dict[str, tuple[float, float]] = {}

    def allow(self, tenant: str) -> tuple[bool, float]:
        now = self.clock()
        tokens, ts = self._state.get(tenant, (float(self.burst), now))
        tokens = min(self.burst, tokens + max(0.0, now - ts) * self.rate)
        if tokens >= 1:
            self._state[tenant] = (tokens - 1, now)
            return True, 0.0
        self._state[tenant] = (tokens, now)
        return False, (1 - tokens) / self.rate
