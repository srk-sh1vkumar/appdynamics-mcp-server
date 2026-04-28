"""
services/incident_correlator.py

Composite triage call for correlate_incident_window.

Design:
- All sub-calls run in parallel via asyncio.gather(); individual failures are
  captured as {"_error": "..."} and never raise, so one bad signal can't kill
  the whole correlation.
- duration_mins is derived from the fixed time window for APIs that don't
  support absolute start/end timestamps.
- Tiers are fetched in the base pass and reused for infra/network fan-outs to
  avoid a redundant API call in optional paths.
- Timeline is sorted by time_ms; events without a timestamp sort to the front.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from utils.timezone import epoch_ms_to_utc


async def _safe(coro: Any, label: str) -> Any:
    """Run a coroutine and return {"_error": ...} on failure instead of raising."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001
        return {"_error": f"{label}: {exc}"}


def _is_error(val: Any) -> bool:
    return isinstance(val, dict) and "_error" in val


def _as_list(val: Any) -> list[Any]:
    if _is_error(val) or val is None:
        return []
    return val if isinstance(val, list) else []


async def run(
    client: Any,
    app_name: str,
    start_time_ms: int,
    end_time_ms: int,
    controller_name: str = "production",
    scope: str | None = None,
    include_deploys: bool = False,
    include_network: bool = False,
    include_security: bool = False,
    include_infra: bool = False,
) -> dict[str, Any]:
    """
    Fetch and correlate health, errors, snapshots, BTs, and optional infra/network
    signals for one application inside a fixed time window.

    Returns a structured dict with a `triage_summary` string the model can read
    directly before deciding which granular tools to call next.
    """
    duration_mins = max(1, math.ceil((end_time_ms - start_time_ms) / 60_000))

    # ── Base pass — always-on signals, all in parallel ────────────────────
    (
        raw_violations,
        raw_errors,
        raw_bts,
        raw_snapshots,
        raw_tiers,
    ) = await asyncio.gather(
        _safe(client.get_health_violations(app_name, duration_mins, True), "health_violations"),
        _safe(client.get_errors_and_exceptions(app_name, duration_mins), "errors"),
        _safe(client.get_business_transactions(app_name), "business_transactions"),
        _safe(client.list_snapshots(app_name, None, start_time_ms, end_time_ms, True, 20, 0), "error_snapshots"),
        _safe(client.get_tiers(app_name), "tiers"),
    )

    violations: list[Any] = _as_list(raw_violations)
    errors: list[Any] = _as_list(raw_errors)
    bts: list[Any] = _as_list(raw_bts)
    snapshots: list[Any] = _as_list(raw_snapshots)
    tiers: list[Any] = _as_list(raw_tiers)

    # ── Optional pass — infra + network (reuse tiers) ─────────────────────
    infra_results: list[dict[str, Any]] = []
    network_results: list[dict[str, Any]] = []

    optional_coros: list[Any] = []
    optional_labels: list[str] = []

    if include_infra:
        for tier in tiers[:3]:  # cap at 3 tiers to stay within token budget
            name = tier.get("name", "")
            if name:
                optional_coros.append(
                    _safe(client.get_infrastructure_stats(app_name, name, None, duration_mins), f"infra:{name}")
                )
                optional_labels.append(f"infra:{name}")

    if include_network:
        for tier in tiers[:3]:
            name = tier.get("name", "")
            if name:
                optional_coros.append(
                    _safe(client.get_network_kpis(app_name, name, None, duration_mins), f"network:{name}")
                )
                optional_labels.append(f"network:{name}")

    if optional_coros:
        optional_results = await asyncio.gather(*optional_coros)
        for label, val in zip(optional_labels, optional_results):
            tier_name = label.split(":", 1)[1]
            if label.startswith("infra:"):
                infra_results.append({"tier": tier_name, "stats": val if not _is_error(val) else None, "error": val.get("_error") if _is_error(val) else None})
            elif label.startswith("network:"):
                network_results.append({"tier": tier_name, "kpis": val if not _is_error(val) else None, "error": val.get("_error") if _is_error(val) else None})

    # ── Build timeline ────────────────────────────────────────────────────
    timeline: list[dict[str, Any]] = []

    for v in violations:
        ts = v.get("startTimeInMillis") or v.get("startTime") or 0
        timeline.append({
            "time_ms": ts,
            "time_utc": epoch_ms_to_utc(ts).isoformat() if ts else "unknown",
            "type": "health_violation",
            "severity": v.get("severity", "UNKNOWN"),
            "description": f"{v.get('name', '?')} — {v.get('affectedEntityName', '?')}",
        })

    for s in snapshots[:10]:
        ts = s.get("serverStartTime") or 0
        timeline.append({
            "time_ms": ts,
            "time_utc": epoch_ms_to_utc(ts).isoformat() if ts else "unknown",
            "type": "error_snapshot",
            "severity": "ERROR",
            "description": f"BT {s.get('businessTransactionId', '?')} — {s.get('errorSummary', s.get('summary', 'error'))}",
        })

    timeline.sort(key=lambda e: e.get("time_ms") or 0)

    # ── BT summary ────────────────────────────────────────────────────────
    error_bts = [b for b in bts if (b.get("errorPercentage") or 0) > 0]
    bt_summary = {
        "total": len(bts),
        "with_errors": len(error_bts),
        "top_error_bts": [
            {"name": b.get("name"), "error_pct": b.get("errorPercentage"), "calls_per_min": b.get("callsPerMinute")}
            for b in sorted(error_bts, key=lambda b: b.get("errorPercentage") or 0, reverse=True)[:5]
        ],
    }

    # ── Signal summary ────────────────────────────────────────────────────
    critical_count = sum(1 for e in timeline if e.get("severity") in ("CRITICAL", "ERROR"))
    warning_count = sum(1 for e in timeline if e.get("severity") == "WARNING")

    signals_summary = {
        "critical_count": critical_count,
        "warning_count": warning_count,
        "error_exception_types": len(errors),
        "error_snapshot_count": len(snapshots),
        "bts_with_errors": len(error_bts),
        "tiers_count": len(tiers),
        "infra_included": include_infra,
        "network_included": include_network,
        "security_included": include_security,
        "deploys_included": include_deploys,
    }

    # ── Triage summary (one-liner for the model) ──────────────────────────
    parts: list[str] = []
    if critical_count:
        parts.append(f"{critical_count} critical/error event(s)")
    if warning_count:
        parts.append(f"{warning_count} warning(s)")
    if len(snapshots):
        parts.append(f"{len(snapshots)} error snapshot(s)")
    if len(errors):
        parts.append(f"{len(errors)} exception type(s)")
    if len(error_bts):
        parts.append(f"{len(error_bts)} BT(s) with errors")
    if not parts:
        parts.append("no anomalies detected in window")

    scope_str = f" [{scope}]" if scope else ""
    start_utc = epoch_ms_to_utc(start_time_ms).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = epoch_ms_to_utc(end_time_ms).strftime("%Y-%m-%dT%H:%M:%SZ")
    triage_summary = (
        f"{app_name}{scope_str} | {start_utc} – {end_utc} ({duration_mins}m) | "
        + ", ".join(parts)
        + "."
    )

    return {
        "app_name": app_name,
        "controller_name": controller_name,
        "window": {
            "start_ms": start_time_ms,
            "end_ms": end_time_ms,
            "start_utc": start_utc,
            "end_utc": end_utc,
            "duration_mins": duration_mins,
        },
        "scope": scope,
        "triage_summary": triage_summary,
        "timeline": timeline,
        "health_violations": violations,
        "error_snapshots": {
            "count": len(snapshots),
            "samples": snapshots[:5],
        },
        "business_transactions": bt_summary,
        "errors_and_exceptions": errors[:20],
        "infra": infra_results if include_infra else None,
        "network": network_results if include_network else None,
        "security": None,   # placeholder — wire to analytics query when include_security=True
        "deploys": None,    # placeholder — wire to deployment event API when include_deploys=True
        "signals_summary": signals_summary,
    }
