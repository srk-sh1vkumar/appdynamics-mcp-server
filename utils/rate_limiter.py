"""
utils/rate_limiter.py

Token bucket rate limiter — global, per-user, and per-team.

Design decisions:
- Token bucket (not sliding window): idle users accumulate tokens up to burst
  capacity, then are smoothly throttled. Better for legitimate burst-then-idle
  SRE investigation patterns.
- Three layers: global (50 tok/s, burst 100), per-team (20 tok/s, burst 40),
  per-user (10 tok/s, burst 20). Global is the hard ceiling; team and user
  buckets enforce fair share below that ceiling.
- Tool weights: expensive operations (ADQL queries, snapshot analysis) consume
  multiple tokens per call so they are throttled more aggressively than cheap
  list operations.
- asyncio.Lock per bucket — this server is single-process async.
- Surface to user only if total wait > 5 seconds.
- Per-user and per-team bucket maps pruned every 5 minutes via background task.
"""

from __future__ import annotations

import asyncio
import time

# ---------------------------------------------------------------------------
# Capacity constants
# ---------------------------------------------------------------------------

GLOBAL_RATE = 50.0
GLOBAL_BURST = 100.0
TEAM_RATE = 20.0
TEAM_BURST = 40.0
USER_RATE = 10.0
USER_BURST = 20.0
SURFACE_THRESHOLD_S = 5.0
PRUNE_INTERVAL_S = 300
USER_IDLE_TTL_S = 300

# ---------------------------------------------------------------------------
# Tool weight multipliers — tokens consumed per call
# Heavy ops cost more so they are throttled proportionally to their AppD load.
# ---------------------------------------------------------------------------

TOOL_WEIGHTS: dict[str, int] = {
    # Analytics — ADQL queries hit a separate Events Service, expensive
    "query_analytics_logs": 3,
    # Snapshot ops — fetch full call chain + stack traces, medium cost
    "analyze_snapshot": 2,
    "compare_snapshots": 2,
    "get_exit_calls": 2,
    "stitch_async_trace": 2,
    # Aggregate tools — fan out across many apps
    "get_team_health_summary": 3,
    "get_bt_detection_rules": 2,
    # All other tools default to 1 token
}


def tool_weight(tool_name: str) -> int:
    return TOOL_WEIGHTS.get(tool_name, 1)


# ---------------------------------------------------------------------------
# TokenBucket
# ---------------------------------------------------------------------------


class TokenBucket:
    def __init__(self, rate: float, capacity: float) -> None:
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
        self._last_used = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(
            self._capacity, self._tokens + (now - self._last) * self._rate
        )
        self._last = now
        self._last_used = now

    async def acquire(self, tokens: float = 1.0) -> float:
        """
        Consume `tokens` from the bucket.
        Returns wait time in seconds (0 = immediate). Does NOT sleep.
        """
        async with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0
            wait = (tokens - self._tokens) / self._rate
            return wait

    @property
    def last_used(self) -> float:
        return self._last_used

    def fill_level(self) -> float:
        """Current token count (0.0 – capacity), for health reporting."""
        return round(self._tokens, 1)


# ---------------------------------------------------------------------------
# Module-level buckets
# ---------------------------------------------------------------------------

_global_bucket = TokenBucket(GLOBAL_RATE, GLOBAL_BURST)
_team_buckets: dict[str, TokenBucket] = {}
_user_buckets: dict[str, TokenBucket] = {}
_team_lock = asyncio.Lock()
_user_lock = asyncio.Lock()
_prune_task: asyncio.Task[None] | None = None


async def _get_team_bucket(team_name: str) -> TokenBucket:
    async with _team_lock:
        if team_name not in _team_buckets:
            _team_buckets[team_name] = TokenBucket(TEAM_RATE, TEAM_BURST)
        return _team_buckets[team_name]


async def _get_user_bucket(upn: str) -> TokenBucket:
    async with _user_lock:
        if upn not in _user_buckets:
            _user_buckets[upn] = TokenBucket(USER_RATE, USER_BURST)
        return _user_buckets[upn]


async def _prune_loop() -> None:
    while True:
        await asyncio.sleep(PRUNE_INTERVAL_S)
        cutoff = time.monotonic() - USER_IDLE_TTL_S
        async with _team_lock:
            stale = [k for k, b in _team_buckets.items() if b.last_used < cutoff]
            for k in stale:
                del _team_buckets[k]
        async with _user_lock:
            stale = [k for k, b in _user_buckets.items() if b.last_used < cutoff]
            for k in stale:
                del _user_buckets[k]


def start_rate_limiter() -> None:
    """Start background pruning task. Call once at server startup."""
    global _prune_task
    if _prune_task is None or _prune_task.done():
        _prune_task = asyncio.create_task(_prune_loop())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def check_and_wait(
    upn: str,
    tool_name: str = "",
    team_name: str | None = None,
) -> str | None:
    """
    Enforce rate limits for a given UPN and tool call.

    Checks three buckets in order: global → team (if known) → per-user.
    Tool weight multiplier is applied at each layer.

    Returns None if the request proceeds immediately.
    Returns a user-facing message string if total wait exceeded 5 seconds.
    """
    weight = float(tool_weight(tool_name)) if tool_name else 1.0
    t_start = time.monotonic()
    total_waited = 0.0

    # Global
    wait = await _global_bucket.acquire(weight)
    if wait > 0:
        await asyncio.sleep(wait)
        total_waited += wait

    # Per-team (if resolved)
    if team_name:
        bucket = await _get_team_bucket(team_name)
        wait = await bucket.acquire(weight)
        if wait > 0:
            await asyncio.sleep(wait)
            total_waited += wait

    # Per-user
    bucket = await _get_user_bucket(upn)
    wait = await bucket.acquire(weight)
    if wait > 0:
        await asyncio.sleep(wait)
        total_waited += wait

    elapsed = time.monotonic() - t_start
    if elapsed > SURFACE_THRESHOLD_S:
        return f"Rate limit applied. Request queued for {elapsed:.1f}s."
    return None


def get_stats() -> dict[str, object]:
    """Return current rate limiter state for health reporting."""
    return {
        "global_tokens_remaining": _global_bucket.fill_level(),
        "global_capacity": GLOBAL_BURST,
        "active_team_buckets": len(_team_buckets),
        "active_user_buckets": len(_user_buckets),
        "tool_weights": TOOL_WEIGHTS,
    }
