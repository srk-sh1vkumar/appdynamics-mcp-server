"""
parsers/snapshot_parser.py

Snapshot error parsing and snapshot comparison (Smoking Gun analysis).

Design decisions:
- detect_language() uses regex patterns from the spec — first match wins.
  Parser dispatch is a simple dict lookup, not a class hierarchy.
- compare_snapshots() uses a RELATIVE threshold (>30% AND >20ms) not a flat
  threshold. Flat thresholds cause false positives on fast methods (where
  60ms is meaningful) and false negatives on slow methods (where 60ms is noise).
- Confidence scoring: 3+ corroborating signals → HIGH, 2 → MEDIUM, 1 → LOW.
  "Corroborating signals" means independent evidence types, not just count of
  deviations (so 10 latency deviations = 1 signal, not 10).
- Golden baseline scoring algorithm is implemented here so compare_snapshots
  can auto-select when healthy_snapshot_guid is not provided.
"""

from __future__ import annotations

import re
from typing import Any

from models.types import (
    ConfidenceScore,
    ParsedStack,
    SmokingGunReport,
    StackLanguage,
)
from utils.timezone import epoch_ms_to_utc, same_hour, same_weekday

# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def detect_language(stack_trace: str) -> StackLanguage:
    if re.search(r"at\s+[\w\.]+\([\w]+\.java:\d+\)", stack_trace):
        return StackLanguage.JAVA
    if re.search(r"at\s+\w+\s+\(.*\.js:\d+:\d+\)", stack_trace):
        return StackLanguage.NODEJS
    if re.search(r'File ".*\.py", line \d+', stack_trace):
        return StackLanguage.PYTHON
    if re.search(r"at\s+.*\s+in\s+.*\.cs:line \d+", stack_trace):
        return StackLanguage.DOTNET
    return StackLanguage.UNKNOWN


# ---------------------------------------------------------------------------
# Parser dispatch
# ---------------------------------------------------------------------------


def parse_snapshot_errors(
    stack_trace: str, app_package_prefix: str = ""
) -> ParsedStack:
    """Detect language and dispatch to the appropriate parser."""
    lang = detect_language(stack_trace)

    if lang == StackLanguage.JAVA:
        from parsers.stack.java import parse
    elif lang == StackLanguage.NODEJS:
        from parsers.stack.nodejs import parse
    elif lang == StackLanguage.PYTHON:
        from parsers.stack.python_parser import parse
    elif lang == StackLanguage.DOTNET:
        from parsers.stack.dotnet import parse
    else:
        return _unknown_parse(stack_trace)

    return parse(stack_trace, app_package_prefix)


def _unknown_parse(stack_trace: str) -> ParsedStack:
    """Best-effort extraction for unknown languages."""
    lines = stack_trace.strip().splitlines()
    preview = "\n".join(lines[:5])
    caused_by = [lines[0].strip()] if lines else []
    return ParsedStack(
        language=StackLanguage.UNKNOWN,
        culprit_frame=None,
        caused_by_chain=caused_by,
        top_app_frames=[],
        full_stack_preview=preview,
    )


# ---------------------------------------------------------------------------
# Hot path identification
# ---------------------------------------------------------------------------


def find_hot_path(call_segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the call segment with the highest % of total time."""
    if not call_segments:
        return None
    total = sum(s.get("timeTakenInMilliSecs", 0) for s in call_segments)
    if total == 0:
        return None
    return max(call_segments, key=lambda s: s.get("timeTakenInMilliSecs", 0))


# ---------------------------------------------------------------------------
# Golden baseline auto-selection
# ---------------------------------------------------------------------------


def score_golden_candidate(
    candidate: dict[str, Any],
    failed: dict[str, Any],
    bt_baseline_ms: float,
) -> int:
    score = 100

    if candidate.get("errorOccurred", False):
        score -= 50

    candidate_rt = candidate.get("timeTakenInMilliSecs", 0)
    if bt_baseline_ms > 0 and candidate_rt > bt_baseline_ms * 1.5:
        score -= 30

    c_ts = epoch_ms_to_utc(int(candidate.get("serverStartTime", 0)))
    f_ts = epoch_ms_to_utc(int(failed.get("serverStartTime", 0)))

    if same_hour(c_ts, f_ts, tolerance_s=3600):
        score += 20
    if same_weekday(c_ts, f_ts):
        score += 10

    return max(0, score)


def confidence_from_score(score: int) -> ConfidenceScore:
    if score > 80:
        return ConfidenceScore.HIGH
    if score > 50:
        return ConfidenceScore.MEDIUM
    return ConfidenceScore.LOW


# ---------------------------------------------------------------------------
# compare_snapshots (Smoking Gun analysis)
# ---------------------------------------------------------------------------


def compare_snapshots(
    healthy: dict[str, Any],
    failed: dict[str, Any],
    threshold_percent: float = 30.0,
    threshold_ms: float = 20.0,
) -> SmokingGunReport:
    """
    Differential analysis between a failed snapshot and a golden healthy baseline.

    Threshold: delta > 30% AND delta > 20ms absolute (relative, not flat).
    Confidence: 3+ independent signal types = HIGH, 2 = MEDIUM, 1 = LOW.
    """
    healthy_segments: list[dict[str, Any]] = healthy.get("callChain", [])
    failed_segments: list[dict[str, Any]] = failed.get("callChain", [])

    healthy_by_name = {
        f"{s.get('className', '')}.{s.get('methodName', '')}": s
        for s in healthy_segments
    }
    failed_by_name = {
        f"{s.get('className', '')}.{s.get('methodName', '')}": s
        for s in failed_segments
    }

    # Signal 1: latency deviations (relative threshold)
    latency_deviations: list[dict[str, Any]] = []
    for name, f_seg in failed_by_name.items():
        h_seg = healthy_by_name.get(name)
        if not h_seg:
            continue
        h_ms = float(h_seg.get("timeTakenInMilliSecs", 0))
        f_ms = float(f_seg.get("timeTakenInMilliSecs", 0))
        if h_ms == 0:
            continue
        delta_ms = f_ms - h_ms
        delta_pct = (delta_ms / h_ms) * 100
        if delta_pct > threshold_percent and delta_ms > threshold_ms:
            latency_deviations.append({
                "method": name,
                "delta_ms": round(delta_ms, 1),
                "delta_percent": round(delta_pct, 1),
            })

    # Signal 2: exclusive methods (in failed only)
    exclusive_methods = [n for n in failed_by_name if n not in healthy_by_name]

    # Signal 3: premature exits (method appears at lower index in failed)
    failed_names = list(failed_by_name.keys())
    healthy_names = list(healthy_by_name.keys())
    premature_exits: list[str] = []
    for name in failed_names:
        if name in healthy_names:
            if failed_names.index(name) < healthy_names.index(name):
                premature_exits.append(name)

    # Identify culprit: worst latency deviation, or first exclusive method
    culprit_name = ""
    culprit_frame_data: dict[str, Any] = {}
    if latency_deviations:
        worst = max(latency_deviations, key=lambda x: x["delta_ms"])
        culprit_name = worst["method"]
        culprit_frame_data = failed_by_name.get(culprit_name, {})
    elif exclusive_methods:
        culprit_name = exclusive_methods[0]
        culprit_frame_data = failed_by_name.get(culprit_name, {})

    # Parse culprit class/method
    if "." in culprit_name:
        parts = culprit_name.rsplit(".", 1)
        culprit_class, culprit_method = parts[0], parts[1]
    else:
        culprit_class, culprit_method = "", culprit_name

    # Error details from failed snapshot
    error_details = failed.get("errorDetails", "") or ""
    error_stack = failed.get("errorOccurred", False)

    # Confidence scoring
    signal_count = (
        (1 if latency_deviations else 0)
        + (1 if exclusive_methods else 0)
        + (1 if premature_exits else 0)
        + (1 if error_stack else 0)
    )
    if signal_count >= 3:
        confidence = ConfidenceScore.HIGH
    elif signal_count == 2:
        confidence = ConfidenceScore.MEDIUM
    else:
        confidence = ConfidenceScore.LOW

    _candidates = [
        f"{len(latency_deviations)} latency deviation(s)" if latency_deviations else "",
        f"{len(exclusive_methods)} exclusive method(s)" if exclusive_methods else "",
        f"{len(premature_exits)} premature exit(s)" if premature_exits else "",
        "error occurred in failed snapshot" if error_stack else "",
    ]
    signal_parts: list[str] = [x for x in _candidates if x]
    confidence_reasoning = (
        f"{signal_count} corroborating signal(s): " + ", ".join(signal_parts)
    )

    deviation = (
        f"Failed path took significantly longer in {culprit_name}. "
        if latency_deviations
        else f"Method {culprit_name} appeared only in the failed path. "
        if exclusive_methods
        else "Execution path differed from golden baseline."
    )

    return SmokingGunReport(
        culprit_class=culprit_class,
        culprit_method=culprit_method,
        culprit_line=int(culprit_frame_data.get("lineNumber", 0)),
        culprit_file=culprit_frame_data.get("fileName", ""),
        deviation=deviation,
        exception=str(error_details)[:500],
        suggested_fix=_suggest_fix(culprit_name, error_details, latency_deviations),
        confidence_score=confidence,
        confidence_reasoning=confidence_reasoning,
        exclusive_methods=exclusive_methods[:10],
        latency_deviations=latency_deviations[:10],
        golden_snapshot_guid=healthy.get("requestGUID", ""),
        golden_selection_reason="",  # filled in by the tool handler
    )


def _suggest_fix(culprit: str, error: str, deviations: list[dict[str, Any]]) -> str:
    error_lower = str(error).lower()
    if "nullpointer" in error_lower or "nullreference" in error_lower:
        return (
            f"Check for uninitialized object in {culprit}. "
            "Add null guard before the failure point."
        )
    if "sql" in error_lower or "database" in error_lower:
        return "Correlate with get_database_performance. Check for slow/missing index."
    if "timeout" in error_lower:
        return "Correlate with get_infrastructure_stats. Check for CPU saturation."
    if "connection" in error_lower and "pool" in error_lower:
        return "DB connection pool exhausted. Increase pool size or find connection leak."  # noqa: E501
    if "outofmemory" in error_lower or "heap" in error_lower:
        return "Check JVM heap via get_jvm_details. Look for memory leak pattern."
    if deviations:
        worst = max(deviations, key=lambda x: x["delta_ms"])
        return (
            f"Investigate {worst['method']} — {worst['delta_ms']}ms above baseline."
            f" {worst['delta_percent']}% regression. Check dependencies called within."
        )
    return f"Review {culprit} and its downstream calls for the root cause."
