"""
services/runbook_generator.py

Post-investigation runbook generation and recurring incident detection.

Design decisions:
- Runbooks are saved as JSON to runbooks/{app_name}-{timestamp_utc}.json.
  The file-per-incident structure makes diff tooling and git history natural.
- load_recent_runbooks() scans the runbooks/ directory for the same app name
  to detect recurring incidents — same root cause appearing multiple times
  is a signal that the fix didn't hold.
- ticket_ref is always None (Phase 2 placeholder per spec).
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from models.types import Runbook, SmokingGunReport

RUNBOOKS_DIR = Path("runbooks")
RUNBOOKS_DIR.mkdir(exist_ok=True)


def generate_runbook(
    app_name: str,
    bt_name: str,
    issue_summary: str,
    smoking_gun: SmokingGunReport,
    investigation_steps: list[str],
    tool_results: dict[str, Any],
    snapshots_archived: list[str],
    affected_users: str | None = None,
) -> Runbook:
    now_utc = datetime.now(tz=UTC).isoformat()
    runbook = Runbook(
        id=str(uuid.uuid4()),
        generated_at=now_utc,
        incident=f"{app_name} - {bt_name} - {issue_summary}",
        root_cause=(
            f"{smoking_gun.culprit_class}.{smoking_gun.culprit_method}"
            f":{smoking_gun.culprit_line} — {smoking_gun.deviation}"
        ),
        confidence_score=smoking_gun.confidence_score.value,
        investigation_steps=investigation_steps,
        tool_results=tool_results,
        resolution=smoking_gun.suggested_fix,
        prevention_recommendation=_prevention_recommendation(smoking_gun),
        snapshots_archived=snapshots_archived,
        affected_users=affected_users,
        ticket_ref=None,
    )
    _save(app_name, runbook)
    return runbook


def _prevention_recommendation(gun: SmokingGunReport) -> str:
    if gun.latency_deviations:
        method = gun.latency_deviations[0].get("method", "")
        return (
            f"Add alerting on p99 latency for {method}. "
            "Consider circuit breaker if this is an external dependency."
        )
    if gun.exclusive_methods:
        return (
            "Add unit test coverage for the new code path "
            f"introduced in {gun.exclusive_methods[0]}."
        )
    return "Review monitoring coverage for this component; add proactive alerts."


def _save(app_name: str, runbook: Runbook) -> Path:
    ts = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    safe_app = app_name.replace(" ", "_").replace("/", "-")
    path = RUNBOOKS_DIR / f"{safe_app}-{ts}.json"
    path.write_text(json.dumps(dataclasses.asdict(runbook), indent=2, default=str))
    return path


def load_recent_runbooks(app_name: str, limit: int = 5) -> list[dict[str, Any]]:
    """Return the most recent runbooks for an app to detect recurring incidents."""
    safe_app = app_name.replace(" ", "_").replace("/", "-")
    files = sorted(RUNBOOKS_DIR.glob(f"{safe_app}-*.json"), reverse=True)[:limit]
    results: list[dict[str, Any]] = []
    for f in files:
        try:
            results.append(json.loads(f.read_text()))
        except Exception:
            pass
    return results
