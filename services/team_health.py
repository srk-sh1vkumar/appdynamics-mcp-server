"""
services/team_health.py

Fan-out health roll-up across all apps visible to a caller.

Design:
- Accepts a resolved list of app names so main.py owns the registry/live-fetch
  decision while this module owns the fan-out and aggregation.
- Semaphore caps concurrent AppD requests at 20 to stay within rate limits.
- Individual app failures are captured inline and surfaced in the result rather
  than aborting the entire summary.
- Output is sorted: degraded apps first, then warning, then healthy; fetch errors
  sort to front so they're immediately visible.
"""

from __future__ import annotations

import asyncio
from typing import Any


async def run(
    client: Any,
    app_names: list[str],
    duration_mins: int,
) -> dict[str, Any]:
    """
    Fetch health violations for each app in parallel and return an aggregated
    summary dict ready for sanitize_and_wrap.

    Args:
        client:        AppDClient instance for the target controller.
        app_names:     Pre-resolved list of application names to check.
        duration_mins: Look-back window for health violations.

    Returns a dict with keys: apps_checked, summary (healthy/warning/degraded/
    fetch_errors/total_open_violations), apps (per-app breakdown).
    """
    if not app_names:
        return {"apps_checked": 0, "warning": "No applications found on this controller."}

    _sem = asyncio.Semaphore(20)

    async def _fetch(app_name: str) -> dict[str, Any]:
        async with _sem:
            try:
                viols = await client.get_health_violations(
                    app_name, duration_mins, include_resolved=False
                )
            except Exception as exc:
                return {"app": app_name, "error": str(exc)}
        critical = [v for v in viols if v.get("severity") == "CRITICAL"]
        warning = [v for v in viols if v.get("severity") == "WARNING"]
        return {
            "app": app_name,
            "total_violations": len(viols),
            "critical": len(critical),
            "warning": len(warning),
            "status": "degraded" if critical else ("warning" if warning else "healthy"),
        }

    app_summaries: list[dict[str, Any]] = list(
        await asyncio.gather(*[_fetch(n) for n in app_names])
    )

    def _sort_key(a: dict[str, Any]) -> tuple[int, int]:
        if "error" in a:
            return (0, 0)
        return (
            1 if a["status"] == "healthy" else 0,
            -a["total_violations"],
        )

    app_summaries.sort(key=_sort_key)

    healthy = sum(1 for a in app_summaries if a.get("status") == "healthy")
    degraded = sum(1 for a in app_summaries if a.get("status") == "degraded")
    warning_count = sum(1 for a in app_summaries if a.get("status") == "warning")
    fetch_errors = sum(1 for a in app_summaries if "error" in a)
    total_viols = sum(a.get("total_violations", 0) for a in app_summaries)

    return {
        "apps_checked": len(app_names),
        "summary": {
            "healthy": healthy,
            "warning": warning_count,
            "degraded": degraded,
            "fetch_errors": fetch_errors,
            "total_open_violations": total_viols,
        },
        "apps": app_summaries,
    }
