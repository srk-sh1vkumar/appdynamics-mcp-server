"""
services/bt_classifier.py

Business Transaction criticality scoring and type classification.

Design decisions:
- Healthcheck detection uses three independent signals (name pattern, path
  pattern, heuristic) so legitimate low-latency BTs aren't silently dropped.
  Failing healthchecks (error_rate > 0) are always shown — a broken
  healthcheck endpoint IS diagnostic information.
- Criticality and type are derived purely from BT metrics; no configuration
  needed. Adding a new criticality tier is a one-line change here.
"""

from __future__ import annotations

import re
from typing import Any

from models.types import BTType, BusinessTransaction, Criticality

_HEALTH_CHECK_NAMES = re.compile(
    r"health|ping|actuator|liveness|readiness|status|heartbeat",
    re.IGNORECASE,
)
_HEALTH_CHECK_PATHS = re.compile(r"^/actuator/|^/health/", re.IGNORECASE)


def is_health_check(bt: BusinessTransaction) -> bool:
    if _HEALTH_CHECK_NAMES.search(bt.name):
        return True
    if bt.tier_name and _HEALTH_CHECK_PATHS.match(bt.tier_name):
        return True
    # Heuristic: extremely fast and zero errors → likely a probe
    if bt.avg_response_time_ms < 10 and bt.error_rate == 0:
        return True
    return False


def classify_criticality(bt: BusinessTransaction) -> Criticality:
    if re.search(r"payment|checkout|order|auth", bt.name, re.IGNORECASE):
        return Criticality.CRITICAL
    if bt.error_rate > 1.0 or bt.avg_response_time_ms > 2000:
        return Criticality.HIGH
    if bt.calls_per_minute > 100:
        return Criticality.MEDIUM
    return Criticality.LOW


def classify_type(bt: BusinessTransaction) -> BTType:
    if bt.db_call_count > 5 and bt.avg_response_time_ms > 500:
        return BTType.DATA_HEAVY_READ
    if bt.error_rate > 2.0 and bt.external_call_count > 0:
        return BTType.EXTERNAL_DEPENDENCY_RISK
    if bt.calls_per_minute > 500 and bt.avg_response_time_ms < 100:
        return BTType.HIGH_FREQUENCY_LIGHTWEIGHT
    if bt.calls_per_minute < 10 and bt.avg_response_time_ms > 1000:
        return BTType.EXPENSIVE_INFREQUENT
    return BTType.STANDARD


def enrich_bt(bt: BusinessTransaction) -> dict[str, Any]:
    """Return BT as a dict enriched with classification fields."""
    return {
        "id": bt.id,
        "name": bt.name,
        "entry_point_type": bt.entry_point_type,
        "avg_response_time_ms": bt.avg_response_time_ms,
        "calls_per_minute": bt.calls_per_minute,
        "error_rate": bt.error_rate,
        "criticality": classify_criticality(bt).value,
        "type": classify_type(bt).value,
        "is_health_check": is_health_check(bt),
    }


def filter_and_sort_bts(
    bts: list[BusinessTransaction],
    include_health_checks: bool = False,
) -> list[dict[str, Any]]:
    """
    Filter healthcheck BTs (unless requested or failing) and sort by:
    CRITICAL first, then by error_rate descending.
    """
    results: list[dict[str, Any]] = []
    for bt in bts:
        hc = is_health_check(bt)
        # Always include failing healthchecks
        if hc and not include_health_checks and bt.error_rate == 0:
            continue
        results.append(enrich_bt(bt))

    _crit_order = {
        Criticality.CRITICAL.value: 0,
        Criticality.HIGH.value: 1,
        Criticality.MEDIUM.value: 2,
        Criticality.LOW.value: 3,
    }
    results.sort(
        key=lambda x: (_crit_order.get(x["criticality"], 4), -x["error_rate"])
    )
    return results
