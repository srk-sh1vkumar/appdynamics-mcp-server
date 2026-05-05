"""
tests/unit/test_team_health.py

Tests for services/team_health.py — service layer only (no MCP plumbing).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services import team_health


def _make_client(violation_map: dict[str, list[dict]] | None = None, error_apps: set[str] | None = None):
    """Build an AsyncMock client where each app returns violations or raises."""
    client = AsyncMock()
    violation_map = violation_map or {}
    error_apps = error_apps or set()

    async def _get_violations(app_name, duration_mins, include_resolved=False):
        if app_name in error_apps:
            raise RuntimeError(f"AppD 503 for {app_name}")
        return violation_map.get(app_name, [])

    client.get_health_violations.side_effect = _get_violations
    return client


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTeamHealthRun:

    async def test_returns_required_keys(self):
        client = _make_client()
        result = await team_health.run(client=client, app_names=["app-a"], duration_mins=15)
        for key in ("apps_checked", "summary", "apps"):
            assert key in result

    async def test_empty_app_names_returns_warning(self):
        client = _make_client()
        result = await team_health.run(client=client, app_names=[], duration_mins=15)
        assert result["apps_checked"] == 0
        assert "warning" in result

    async def test_apps_checked_matches_input(self):
        client = _make_client()
        result = await team_health.run(client=client, app_names=["a", "b", "c"], duration_mins=15)
        assert result["apps_checked"] == 3


# ---------------------------------------------------------------------------
# Violation classification
# ---------------------------------------------------------------------------

    async def test_healthy_app_when_no_violations(self):
        client = _make_client(violation_map={"app-a": []})
        result = await team_health.run(client=client, app_names=["app-a"], duration_mins=15)
        assert result["apps"][0]["status"] == "healthy"
        assert result["summary"]["healthy"] == 1

    async def test_degraded_app_when_critical_violation(self):
        client = _make_client(violation_map={"app-a": [{"severity": "CRITICAL"}]})
        result = await team_health.run(client=client, app_names=["app-a"], duration_mins=15)
        assert result["apps"][0]["status"] == "degraded"
        assert result["summary"]["degraded"] == 1

    async def test_warning_app_when_only_warning_violation(self):
        client = _make_client(violation_map={
            "app-a": [{"severity": "WARNING"}, {"severity": "WARNING"}]
        })
        result = await team_health.run(client=client, app_names=["app-a"], duration_mins=15)
        assert result["apps"][0]["status"] == "warning"
        assert result["summary"]["warning"] == 1

    async def test_critical_overrides_warning(self):
        client = _make_client(violation_map={
            "app-a": [{"severity": "WARNING"}, {"severity": "CRITICAL"}]
        })
        result = await team_health.run(client=client, app_names=["app-a"], duration_mins=15)
        assert result["apps"][0]["status"] == "degraded"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

    async def test_total_open_violations_sum(self):
        client = _make_client(violation_map={
            "app-a": [{"severity": "CRITICAL"}, {"severity": "CRITICAL"}],
            "app-b": [{"severity": "WARNING"}],
        })
        result = await team_health.run(client=client, app_names=["app-a", "app-b"], duration_mins=15)
        assert result["summary"]["total_open_violations"] == 3

    async def test_mixed_statuses_all_counted(self):
        client = _make_client(violation_map={
            "app-a": [],
            "app-b": [{"severity": "WARNING"}],
            "app-c": [{"severity": "CRITICAL"}],
        })
        result = await team_health.run(client=client, app_names=["app-a", "app-b", "app-c"], duration_mins=15)
        s = result["summary"]
        assert s["healthy"] == 1
        assert s["warning"] == 1
        assert s["degraded"] == 1


# ---------------------------------------------------------------------------
# Sort order
# ---------------------------------------------------------------------------

    async def test_degraded_apps_sort_before_healthy(self):
        client = _make_client(violation_map={
            "healthy-app": [],
            "degraded-app": [{"severity": "CRITICAL"}],
        })
        result = await team_health.run(
            client=client, app_names=["healthy-app", "degraded-app"], duration_mins=15
        )
        apps = result["apps"]
        statuses = [a["status"] for a in apps]
        assert statuses.index("degraded") < statuses.index("healthy")

    async def test_fetch_error_apps_sort_to_front(self):
        client = _make_client(
            violation_map={"good-app": []},
            error_apps={"bad-app"},
        )
        result = await team_health.run(
            client=client, app_names=["good-app", "bad-app"], duration_mins=15
        )
        assert "error" in result["apps"][0]


# ---------------------------------------------------------------------------
# Partial failures
# ---------------------------------------------------------------------------

    async def test_single_fetch_error_captured_not_raised(self):
        client = _make_client(
            violation_map={"ok-app": [{"severity": "CRITICAL"}]},
            error_apps={"bad-app"},
        )
        result = await team_health.run(
            client=client, app_names=["ok-app", "bad-app"], duration_mins=15
        )
        assert result["summary"]["fetch_errors"] == 1
        assert result["summary"]["degraded"] == 1

    async def test_all_fetch_errors_still_returns_result(self):
        client = _make_client(error_apps={"a", "b", "c"})
        result = await team_health.run(client=client, app_names=["a", "b", "c"], duration_mins=15)
        assert result["apps_checked"] == 3
        assert result["summary"]["fetch_errors"] == 3
        assert result["summary"]["healthy"] == 0


# ---------------------------------------------------------------------------
# Concurrency cap
# ---------------------------------------------------------------------------

    async def test_semaphore_cap_does_not_block_completion(self):
        # 25 apps > semaphore(20) — all should still complete
        apps = [f"app-{i}" for i in range(25)]
        client = _make_client(violation_map={a: [] for a in apps})
        result = await team_health.run(client=client, app_names=apps, duration_mins=15)
        assert result["apps_checked"] == 25
        assert result["summary"]["healthy"] == 25
