"""
services/event_analyzer.py

Fetch application events and apply heuristics to produce structured
change_indicators alongside the raw event list.

Heuristics (in priority order):
  explicit_deploy_marker  — APPLICATION_DEPLOYMENT event present (HIGH)
  config_change           — APPLICATION_CONFIG_CHANGE event present (HIGH)
  probable_rolling_deploy — ≥2 nodes in same tier with APP_SERVER_RESTART
                            within 10 min; HIGH if ≥50% of tier nodes,
                            MEDIUM otherwise
  k8s_pod_turnover        — AGENT_DISCONNECT + AGENT_CONNECT pairs on
                            different node names in same tier within 10 min;
                            HIGH if ≥2 pairs
  single_node_restart     — 1 node restart with no tier pattern (LOW)
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

_ROLLING_WINDOW_MS = 10 * 60 * 1000   # 10 minutes
_MAX_RAW_EVENTS = 50


def _node_tier(event: dict[str, Any]) -> str:
    return (
        event.get("affectedEntityName", "")
        or event.get("tier", "")
        or "unknown"
    )


def _node_name(event: dict[str, Any]) -> str:
    return event.get("node", "") or event.get("nodeName", "") or ""


def _event_type(event: dict[str, Any]) -> str:
    return event.get("type", "") or event.get("eventType", "")


def _ts(event: dict[str, Any]) -> int:
    return int(event.get("eventTime", 0) or event.get("time", 0) or 0)


def _analyze(events: list[dict[str, Any]], tier_node_counts: dict[str, int]) -> dict[str, Any]:
    """Apply heuristics to raw events. Returns the analysis dict."""
    change_indicators: list[dict[str, Any]] = []
    has_changes = False

    # ── Explicit deploy / config markers ────────────────────────────────
    deploy_events = [e for e in events if _event_type(e) == "APPLICATION_DEPLOYMENT"]
    config_events = [e for e in events if _event_type(e) == "APPLICATION_CONFIG_CHANGE"]

    if deploy_events:
        has_changes = True
        change_indicators.append({
            "type": "explicit_deploy_marker",
            "confidence": "HIGH",
            "count": len(deploy_events),
            "first_time_ms": min(_ts(e) for e in deploy_events),
            "detail": f"{len(deploy_events)} APPLICATION_DEPLOYMENT event(s) in window",
        })

    if config_events:
        has_changes = True
        change_indicators.append({
            "type": "config_change",
            "confidence": "HIGH",
            "count": len(config_events),
            "first_time_ms": min(_ts(e) for e in config_events),
            "detail": f"{len(config_events)} APPLICATION_CONFIG_CHANGE event(s) in window",
        })

    # ── Restart pattern analysis ─────────────────────────────────────────
    restart_events = [e for e in events if _event_type(e) in ("APP_SERVER_RESTART", "AGENT_EVENT")]
    restart_events.sort(key=_ts)

    # Group restarts by tier
    tier_restarts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in restart_events:
        tier = _node_tier(e)
        tier_restarts[tier].append(e)

    rolling_found = False
    single_node: list[dict[str, Any]] = []

    for tier, tier_evs in tier_restarts.items():
        if len(tier_evs) < 2:
            single_node.extend(tier_evs)
            continue

        # Check for ≥2 restarts within 10 min window
        clustered: list[dict[str, Any]] = []
        for ev in tier_evs:
            t = _ts(ev)
            # Keep events within rolling window of the first in the cluster
            if not clustered or (t - _ts(clustered[0])) <= _ROLLING_WINDOW_MS:
                clustered.append(ev)
            else:
                clustered = [ev]

        if len(clustered) >= 2:
            rolling_found = True
            has_changes = True
            # Confidence: HIGH if ≥50% of nodes in tier affected
            total_tier_nodes = tier_node_counts.get(tier, 0)
            affected = len({_node_name(e) for e in clustered})
            if total_tier_nodes > 0 and affected >= total_tier_nodes * 0.5:
                confidence = "HIGH"
            else:
                confidence = "MEDIUM"

            change_indicators.append({
                "type": "probable_rolling_deploy",
                "confidence": confidence,
                "tier": tier,
                "nodes_affected": affected,
                "nodes_in_tier": total_tier_nodes or "unknown",
                "first_time_ms": _ts(clustered[0]),
                "detail": (
                    f"{affected} node restart(s) in tier '{tier}' within 10 min window"
                    + (f" ({affected}/{total_tier_nodes} nodes)" if total_tier_nodes else "")
                ),
            })
        else:
            single_node.extend(tier_evs)

    if single_node and not rolling_found and not deploy_events:
        # Only report as single_node_restart if no stronger signal already present
        has_changes = True
        change_indicators.append({
            "type": "single_node_restart",
            "confidence": "LOW",
            "count": len(single_node),
            "detail": f"{len(single_node)} isolated node restart(s) — ambiguous, may not indicate a deploy",
        })

    # ── K8s pod turnover: DISCONNECT + CONNECT pairs on different node names ──
    disconnect_evs = [e for e in events if "DISCONNECT" in _event_type(e) or "disconnect" in str(e.get("summary", "")).lower()]
    connect_evs = [e for e in events if "CONNECT" in _event_type(e) and "DISCONNECT" not in _event_type(e)]

    if disconnect_evs and connect_evs:
        # Group by tier; look for pairs where node name changed
        tier_disconnects: dict[str, list[str]] = defaultdict(list)
        tier_connects: dict[str, list[str]] = defaultdict(list)
        for e in disconnect_evs:
            tier_disconnects[_node_tier(e)].append(_node_name(e))
        for e in connect_evs:
            tier_connects[_node_tier(e)].append(_node_name(e))

        for tier in tier_disconnects:
            if tier not in tier_connects:
                continue
            old_names = set(tier_disconnects[tier])
            new_names = set(tier_connects[tier])
            pairs = new_names - old_names   # new node names that weren't there before
            if pairs:
                has_changes = True
                confidence = "HIGH" if len(pairs) >= 2 else "MEDIUM"
                change_indicators.append({
                    "type": "k8s_pod_turnover",
                    "confidence": confidence,
                    "tier": tier,
                    "new_pod_count": len(pairs),
                    "detail": (
                        f"{len(pairs)} new node name(s) appeared in tier '{tier}' "
                        "after DISCONNECT events — K8s rolling update pattern"
                    ),
                })

    # ── Change summary ───────────────────────────────────────────────────
    if change_indicators:
        high = [c for c in change_indicators if c["confidence"] == "HIGH"]
        medium = [c for c in change_indicators if c["confidence"] == "MEDIUM"]
        low = [c for c in change_indicators if c["confidence"] == "LOW"]
        parts = []
        if high:
            parts.append(f"{len(high)} HIGH-confidence change(s)")
        if medium:
            parts.append(f"{len(medium)} MEDIUM-confidence change(s)")
        if low:
            parts.append(f"{len(low)} LOW-confidence signal(s)")
        change_summary = "; ".join(parts) + " — review change_indicators for details"
    else:
        change_summary = "no change indicators detected in window"

    return {
        "change_indicators": change_indicators,
        "change_summary": change_summary,
        "has_changes": has_changes,
    }


async def run(
    client: Any,
    app_name: str,
    start_time_ms: int,
    end_time_ms: int,
    event_types: list[str] | None = None,
    tier_node_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Fetch events and return raw_events + change_indicators analysis."""
    raw_events: list[dict[str, Any]] = await client.get_application_events(
        app_name, start_time_ms, end_time_ms, event_types
    )

    analysis = _analyze(raw_events, tier_node_counts or {})

    return {
        "app_name": app_name,
        "event_count": len(raw_events),
        "raw_events": raw_events[:_MAX_RAW_EVENTS],
        **analysis,
    }
