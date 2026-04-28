"""
services/snapshot_analyzer.py

Single-snapshot analysis: errors, hot path, language detection, exception hints.

Design:
- app_package_prefix is sourced by the main.py wrapper from ControllerConfig and
  passed in; this module has no dependency on main.py globals.
- EXCEPTION_STRATEGIES is a module constant here (moved from main.py).
  Callers can override via the exception_strategies parameter for testing.
- CPU-bound parsing (parse_snapshot_errors, find_hot_path) is offloaded to
  asyncio.to_thread (CONC-01).
- callChain string normalisation happens here — AppD returns a string in some
  API versions; we normalise to [] so find_hot_path always receives a list.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from parsers.snapshot_parser import find_hot_path, parse_snapshot_errors

EXCEPTION_STRATEGIES: dict[str, str] = {
    "NullPointerException": (
        "Focus on uninitialized object at culprit line. "
        "Check conditional logic before the failure point."
    ),
    "SSLHandshakeException": (
        "Check external calls in the snapshot. "
        "Which 3rd party URL failed the TLS handshake?"
    ),
    "SQLException": (
        "Correlate with get_database_performance results. "
        "Is the query slow or timing out?"
    ),
    "TimeoutException": (
        "Correlate with get_infrastructure_stats. "
        "Is CPU saturation delaying thread execution?"
    ),
    "ClassCastException": (
        "Deserialization mismatch between services. "
        "Check for recent schema changes across service boundaries."
    ),
    "OutOfMemoryError": (
        "Correlate with JVM heap metrics. "
        "Check for memory leak pattern in heap trend."
    ),
    "SocketException": (
        "Network layer failure. "
        "Check get_network_kpis between affected tiers."
    ),
    "ConcurrentModificationException": (
        "Thread safety issue. "
        "Check thread count and deadlocked threads in get_jvm_details."
    ),
    "ConnectionPoolExhaustedException": (
        "DB connection exhaustion. "
        "Correlate slow queries with active thread count."
    ),
}


async def run(
    client: Any,
    app_name: str,
    snapshot_guid: str,
    app_package_prefix: str = "",
    exception_strategies: dict[str, str] | None = None,
) -> dict[str, Any]:
    """
    Fetch a snapshot and return a structured analysis dict.

    Args:
        client:               AppDClient instance.
        app_name:             Application name.
        snapshot_guid:        GUID of the snapshot to analyse.
        app_package_prefix:   Package prefix for language-aware frame filtering
                              (e.g. "com.example."). Sourced from ControllerConfig.
        exception_strategies: Override the default EXCEPTION_STRATEGIES dict (for testing).

    Returns a dict suitable for sanitize_and_wrap.
    """
    strategies = exception_strategies if exception_strategies is not None else EXCEPTION_STRATEGIES

    snap = await client.get_snapshot_detail(app_name, snapshot_guid)

    error_details = snap.get("errorDetails", "")
    stack_trace = snap.get("errorStackTrace", error_details or "")
    call_segments = snap.get("callChain", [])

    # AppD sometimes returns callChain as a pipe-delimited string; normalise so
    # find_hot_path always receives a list.
    if isinstance(call_segments, str):
        call_segments = []

    parsed = (
        await asyncio.to_thread(parse_snapshot_errors, stack_trace, app_package_prefix)
        if stack_trace else None
    )
    hot_path = await asyncio.to_thread(find_hot_path, call_segments)

    strategy = ""
    if error_details:
        for exc_type, hint in strategies.items():
            if exc_type.lower() in str(error_details).lower():
                strategy = f"\n\nDiagnostic hint for {exc_type}: {hint}"
                break

    return {
        "snapshot_guid": snapshot_guid,
        "bt_name": snap.get("businessTransactionName", ""),
        "response_time_ms": snap.get("timeTakenInMilliSecs", 0),
        "error_occurred": snap.get("errorOccurred", False),
        "error_details": error_details,
        "hot_path": {
            "method": (
                f"{hot_path.get('className','')}.{hot_path.get('methodName','')}"
            ),
            "time_ms": hot_path.get("timeTakenInMilliSecs", 0),
        } if hot_path else None,
        "top_call_segments": call_segments[:10],
        "language": parsed.language.value if parsed else "unknown",
        "culprit_frame": (
            dataclasses.asdict(parsed.culprit_frame)
            if parsed and parsed.culprit_frame else None
        ),
        "caused_by_chain": parsed.caused_by_chain if parsed else [],
        "top_app_frames": (
            [dataclasses.asdict(f) for f in parsed.top_app_frames] if parsed else []
        ),
        "diagnostic_hint": strategy.strip(),
    }
