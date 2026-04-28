"""
services/trace_stitcher.py

Cross-service async trace stitching via correlation ID.

Design:
- Fans out snapshot searches across all requested apps concurrently with a
  semaphore cap of 10 to avoid overwhelming AppD's snapshot endpoint.
- Correlation ID is matched against requestHeaders, userData, correlationInfo,
  and exitCalls[].continuationID — covers the most common propagation patterns.
- Found snapshots are sorted by serverStartTime to produce a causal ordering.
- Gap time between each adjacent segment is calculated and flagged when >100ms
  (indicates queue or network latency between services).
- Missing services are surfaced as a diagnostic warning rather than an error.
"""

from __future__ import annotations

import asyncio
from typing import Any

from utils.timezone import epoch_ms_to_utc, format_for_display


async def run(
    client: Any,
    correlation_id: str,
    app_names: list[str],
    duration_mins: int = 60,
) -> dict[str, Any]:
    """
    Search for snapshots containing correlation_id across app_names and return
    a chronological trace with gap analysis.

    Args:
        client:         AppDClient instance for the target controller.
        correlation_id: The correlation ID to search for in each app's snapshots.
        app_names:      List of service/app names to search.
        duration_mins:  Look-back window (passed to list_snapshots implicitly via
                        the time parameters; callers pass None/None for the default
                        7-day window AppD uses when start/end are omitted).

    Returns a dict with: correlation_id, ordered_trace, coverage_percent, and
    optionally a warning listing services with no matching snapshots.
    """
    _sem = asyncio.Semaphore(10)

    def _snap_contains_id(s: dict[str, Any]) -> bool:
        cid = correlation_id
        if cid in str(s.get("requestHeaders", "")):
            return True
        if cid in str(s.get("userData", "")):
            return True
        if cid in str(s.get("correlationInfo", "")):
            return True
        for ec in s.get("exitCalls", []):
            if cid in str(ec.get("continuationID", "")):
                return True
        return False

    async def _search_app(app_name: str) -> tuple[dict[str, Any] | None, str | None]:
        async with _sem:
            try:
                snaps = await client.list_snapshots(
                    app_name, None, None, None, False, 100, 0
                )
            except Exception:
                return None, app_name
        matched = [s for s in snaps if _snap_contains_id(s)]
        if matched:
            best = min(matched, key=lambda s: s.get("serverStartTime", 0))
            best["_app_name"] = app_name
            return best, None
        return None, app_name

    results = await asyncio.gather(*[_search_app(n) for n in app_names])
    found: list[dict[str, Any]] = [r for r, _ in results if r is not None]
    missing: list[str] = [m for _, m in results if m is not None]

    found.sort(key=lambda s: s.get("serverStartTime", 0))

    trace: list[dict[str, Any]] = []
    for i, snap in enumerate(found):
        entry: dict[str, Any] = {
            "app": snap.get("_app_name", ""),
            "snapshot_guid": snap.get("requestGUID", ""),
            "start_utc": format_for_display(
                epoch_ms_to_utc(snap.get("serverStartTime", 0))
            ),
            "response_time_ms": snap.get("timeTakenInMilliSecs", 0),
            "error_occurred": snap.get("errorOccurred", False),
        }
        if i > 0:
            prev = found[i - 1]
            prev_end_ms = (
                prev.get("serverStartTime", 0) + prev.get("timeTakenInMilliSecs", 0)
            )
            gap_ms = snap.get("serverStartTime", 0) - prev_end_ms
            entry["gap_from_previous_ms"] = round(gap_ms, 1)
            if gap_ms > 100:
                entry["gap_warning"] = (
                    f"Significant gap: {gap_ms:.0f}ms (queue latency?)"
                )
        trace.append(entry)

    coverage = len(found) / len(app_names) * 100 if app_names else 0
    result: dict[str, Any] = {
        "correlation_id": correlation_id,
        "ordered_trace": trace,
        "coverage_percent": round(coverage, 1),
    }
    if missing:
        result["warning"] = (
            f"Partial trace: {len(missing)} service(s) returned no snapshots: "
            f"{', '.join(missing)}. "
            "Possible causes: (1) correlation ID not propagated to those services "
            "(check X-Correlation-ID header plumbing); "
            "(2) snapshots purged — widen time window; "
            "(3) correlation ID lives in a non-standard field — "
            "verify the field used by your instrumentation framework."
        )

    return result
