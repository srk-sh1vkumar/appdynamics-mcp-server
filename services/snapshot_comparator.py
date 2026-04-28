"""
services/snapshot_comparator.py

Differential snapshot analysis with golden baseline selection.

Design:
- Caller may provide an explicit healthy_snapshot_guid; if not, baseline is
  resolved in priority order:
    1. User-pinned golden from the GoldenRegistry (most trustworthy)
    2. Auto-selected by scoring up to 100 recent snapshots (score_golden_candidate)
- Scoring is CPU-bound — offloaded to asyncio.to_thread (CONC-01).
- callChain normalisation (string → []) happens here before the diff so
  the parser always receives a list regardless of AppD version differences.
- golden_selection_reason is populated here and stored on the returned report
  so callers can surface provenance to the LLM.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from parsers.snapshot_parser import (
    compare_snapshots as _compare,
    score_golden_candidate,
)


async def run(
    client: Any,
    app_name: str,
    failed_snapshot_guid: str,
    golden_registry: Any,
    healthy_snapshot_guid: str | None = None,
    controller_name: str = "production",
) -> dict[str, Any]:
    """
    Fetch failed and healthy snapshots, resolve the golden baseline, and return
    a SmokingGunReport as a plain dict.

    Args:
        client:                AppDClient instance for the target controller.
        app_name:              Application name.
        failed_snapshot_guid:  GUID of the failing snapshot to analyse.
        golden_registry:       GoldenRegistry instance (from main.py) for pinned lookups.
        healthy_snapshot_guid: Explicit healthy snapshot GUID; if None, auto-selected.
        controller_name:       Controller name used to scope the golden registry lookup.

    Returns a dict (dataclasses.asdict of SmokingGunReport) or a dict with a
    "message" key when no suitable golden baseline can be found.
    """
    failed = await client.get_snapshot_detail(app_name, failed_snapshot_guid)

    bt_name = failed.get("businessTransactionName", "")
    golden_reason = ""

    if healthy_snapshot_guid:
        healthy = await client.get_snapshot_detail(app_name, healthy_snapshot_guid)
        golden_reason = "Provided explicitly by caller."
    else:
        # Priority 1: user-pinned golden in registry
        pinned = golden_registry.get(controller_name, app_name, bt_name)
        if pinned and pinned.snapshot_guid != failed_snapshot_guid:
            healthy_snapshot_guid = pinned.snapshot_guid
            healthy = await client.get_snapshot_detail(app_name, healthy_snapshot_guid)
            golden_reason = (
                f"Pinned golden baseline (promoted by {pinned.promoted_by}, "
                f"confidence={pinned.confidence}, score={pinned.selection_score})."
            )
        else:
            # Priority 2: auto-select by scoring recent candidates
            candidates = await client.list_snapshots(
                app_name,
                bt_name=bt_name,
                start_time_ms=None,
                end_time_ms=None,
                error_only=False,
                page_size=100,
                page_offset=0,
            )

            def _score_all() -> list[tuple[dict[str, Any], float]]:
                return sorted(
                    [
                        (s, score_golden_candidate(s, failed, 500))
                        for s in candidates
                        if s.get("requestGUID") != failed_snapshot_guid
                    ],
                    key=lambda x: x[1],
                    reverse=True,
                )

            scored = await asyncio.to_thread(_score_all)

            if not scored or scored[0][1] <= 0:
                return {
                    "message": (
                        "No suitable golden baseline found in the last 7 days. "
                        "Use set_golden_snapshot to designate a healthy baseline, "
                        "or provide healthy_snapshot_guid explicitly."
                    ),
                    "failed_snapshot": failed_snapshot_guid,
                }

            best_candidate, best_score = scored[0]
            healthy_snapshot_guid = best_candidate.get("requestGUID", "")
            healthy = await client.get_snapshot_detail(app_name, healthy_snapshot_guid)
            conf = "HIGH" if best_score > 80 else "MEDIUM" if best_score > 50 else "LOW"
            golden_reason = (
                f"Auto-selected (score={best_score}, confidence={conf}). "
                "No errors, response time within baseline, "
                "similar time-of-day/weekday."
            )

    # Normalise callChain — AppD returns a string in some API versions
    for snap in (healthy, failed):
        if isinstance(snap.get("callChain"), str):
            snap["callChain"] = []

    report = await asyncio.to_thread(_compare, healthy, failed)
    report.golden_selection_reason = golden_reason
    return dataclasses.asdict(report)
