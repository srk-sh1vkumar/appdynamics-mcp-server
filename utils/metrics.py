"""
utils/metrics.py

In-process Prometheus metrics for the AppDynamics MCP server.

Exposed counters / gauges (Prometheus text format on /metrics):
  appd_mcp_tool_calls_total{tool, status}   — total tool invocations by status
  appd_mcp_tool_duration_ms{tool}           — cumulative duration (ms) per tool
  appd_mcp_rate_limit_hits_total            — times check_and_wait had to wait
  appd_mcp_cache_hits_total                 — L1/L2 cache hits
  appd_mcp_cache_misses_total               — cache misses
  appd_mcp_active_users                     — distinct UPNs seen (rolling 1h)
  appd_mcp_requests_last_hour               — tool calls in the last 60 minutes

No external dependencies — counters are plain dicts guarded by a threading.Lock
so they are safe from concurrent asyncio callbacks.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

_lock = threading.Lock()

# tool_name → {"success": int, "error": int}
_tool_calls: dict[str, dict[str, int]] = defaultdict(lambda: {"success": 0, "error": 0})
# tool_name → cumulative ms
_tool_duration: dict[str, float] = defaultdict(float)

_rate_limit_hits: int = 0
_cache_hits: int = 0
_cache_misses: int = 0

# Rolling 1-hour request timestamps
_request_times: list[float] = []
_active_upns: set[str] = set()


# ---------------------------------------------------------------------------
# Record helpers (called from main.py and utils/cache.py hooks)
# ---------------------------------------------------------------------------


def record_tool_call(tool: str, status: str, duration_ms: int) -> None:
    with _lock:
        _tool_calls[tool][status] += 1
        _tool_duration[tool] += duration_ms
        now = time.time()
        _request_times.append(now)
        # Trim entries older than 1 hour
        cutoff = now - 3600
        while _request_times and _request_times[0] < cutoff:
            _request_times.pop(0)


def record_rate_limit_hit() -> None:
    global _rate_limit_hits
    with _lock:
        _rate_limit_hits += 1


def record_cache_hit() -> None:
    global _cache_hits
    with _lock:
        _cache_hits += 1


def record_cache_miss() -> None:
    global _cache_misses
    with _lock:
        _cache_misses += 1


def record_upn(upn: str) -> None:
    with _lock:
        _active_upns.add(upn)


# ---------------------------------------------------------------------------
# Prometheus text format renderer
# ---------------------------------------------------------------------------


def render() -> str:
    with _lock:
        lines: list[str] = []

        # tool_calls_total
        lines.append("# HELP appd_mcp_tool_calls_total Total MCP tool invocations")
        lines.append("# TYPE appd_mcp_tool_calls_total counter")
        for tool, counts in sorted(_tool_calls.items()):
            for status, val in counts.items():
                label = f'tool="{tool}",status="{status}"'
                lines.append(f"appd_mcp_tool_calls_total{{{label}}} {val}")

        # tool_duration_ms
        lines.append(
            "# HELP appd_mcp_tool_duration_ms Cumulative tool duration in milliseconds"
        )
        lines.append("# TYPE appd_mcp_tool_duration_ms counter")
        for tool, ms in sorted(_tool_duration.items()):
            lines.append(f'appd_mcp_tool_duration_ms{{tool="{tool}"}} {ms:.0f}')

        # rate_limit_hits
        lines.append(
            "# HELP appd_mcp_rate_limit_hits_total Times rate limiter throttled request"
        )
        lines.append("# TYPE appd_mcp_rate_limit_hits_total counter")
        lines.append(f"appd_mcp_rate_limit_hits_total {_rate_limit_hits}")

        # cache
        lines.append("# HELP appd_mcp_cache_hits_total Cache hits (L1 + L2)")
        lines.append("# TYPE appd_mcp_cache_hits_total counter")
        lines.append(f"appd_mcp_cache_hits_total {_cache_hits}")

        lines.append("# HELP appd_mcp_cache_misses_total Cache misses")
        lines.append("# TYPE appd_mcp_cache_misses_total counter")
        lines.append(f"appd_mcp_cache_misses_total {_cache_misses}")

        # active users + request rate
        lines.append("# HELP appd_mcp_active_users Distinct UPNs seen")
        lines.append("# TYPE appd_mcp_active_users gauge")
        lines.append(f"appd_mcp_active_users {len(_active_upns)}")

        now = time.time()
        cutoff = now - 3600
        recent = sum(1 for t in _request_times if t >= cutoff)
        lines.append(
            "# HELP appd_mcp_requests_last_hour Tool calls in the last 60 minutes"
        )
        lines.append("# TYPE appd_mcp_requests_last_hour gauge")
        lines.append(f"appd_mcp_requests_last_hour {recent}")

        return "\n".join(lines) + "\n"
