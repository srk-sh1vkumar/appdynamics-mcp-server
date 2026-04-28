"""
tests/unit/test_incident_correlator.py

Tests for services/incident_correlator.py and the correlate_incident_window MCP tool.

Two test layers:
  1. Service layer (incident_correlator.run) — tests logic and output shape
     directly without MCP plumbing. Uses AsyncMock client.
  2. Tool layer (correlate_incident_window in main) — tests MCP wrapper
     including rate limit, RBAC, audit log, and sanitize_and_wrap output.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services import incident_correlator

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

START_MS = 1_700_000_000_000
END_MS   = 1_700_003_600_000  # +1 hour = 60 mins


@pytest.fixture
def mock_client():
    client = AsyncMock()
    client.get_health_violations.return_value = [
        {
            "id": 1,
            "name": "Response time is too slow",
            "severity": "CRITICAL",
            "startTimeInMillis": START_MS + 60_000,
            "affectedEntityName": "/api/checkout",
        }
    ]
    client.get_errors_and_exceptions.return_value = [
        {"errorName": "NullPointerException", "occurrences": 42},
        {"errorName": "TimeoutException", "occurrences": 7},
    ]
    client.get_business_transactions.return_value = [
        {"id": 101, "name": "/api/checkout", "errorPercentage": 5.2, "callsPerMinute": 45},
        {"id": 102, "name": "/health",        "errorPercentage": 0.0, "callsPerMinute": 120},
    ]
    client.list_snapshots.return_value = [
        {"serverStartTime": START_MS + 120_000, "businessTransactionId": 101, "errorSummary": "NPE in CheckoutService"},
        {"serverStartTime": START_MS + 180_000, "businessTransactionId": 101, "errorSummary": "Timeout"},
    ]
    client.get_tiers.return_value = [
        {"name": "checkout-tier", "numberOfNodes": 3},
        {"name": "payment-tier",  "numberOfNodes": 2},
    ]
    client.get_infrastructure_stats.return_value = [
        {"nodeName": "node-1", "cpuUsage": 87.5, "memoryUsage": 72.1},
    ]
    client.get_network_kpis.return_value = [
        {"source": "checkout-tier", "dest": "payment-tier", "errorRate": 0.02, "avgLatencyMs": 45},
    ]
    return client


# ---------------------------------------------------------------------------
# Service layer tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestIncidentCorrelatorRun:

    async def test_returns_required_keys(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        for key in ("app_name", "controller_name", "window", "triage_summary",
                    "timeline", "health_violations", "error_snapshots",
                    "business_transactions", "errors_and_exceptions",
                    "signals_summary"):
            assert key in result, f"missing key: {key}"

    async def test_triage_summary_is_non_empty_string(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        assert isinstance(result["triage_summary"], str)
        assert "ecommerce-app" in result["triage_summary"]
        assert len(result["triage_summary"]) > 20

    async def test_timeline_is_sorted(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        times = [e["time_ms"] for e in result["timeline"] if e.get("time_ms")]
        assert times == sorted(times)

    async def test_timeline_contains_violation_and_snapshot_events(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        types = {e["type"] for e in result["timeline"]}
        assert "health_violation" in types
        assert "error_snapshot" in types

    async def test_window_duration_mins_derived_correctly(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        assert result["window"]["duration_mins"] == 60

    async def test_infra_none_when_not_requested(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_infra=False,
        )
        assert result["infra"] is None
        mock_client.get_infrastructure_stats.assert_not_called()

    async def test_infra_included_when_requested(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_infra=True,
        )
        assert result["infra"] is not None
        assert len(result["infra"]) > 0
        mock_client.get_infrastructure_stats.assert_called()

    async def test_network_none_when_not_requested(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_network=False,
        )
        assert result["network"] is None
        mock_client.get_network_kpis.assert_not_called()

    async def test_network_included_when_requested(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_network=True,
        )
        assert result["network"] is not None
        mock_client.get_network_kpis.assert_called()

    async def test_scope_appears_in_triage_summary(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            scope="us-east-1",
        )
        assert "us-east-1" in result["triage_summary"]

    async def test_partial_failure_does_not_raise(self, mock_client):
        mock_client.get_errors_and_exceptions.side_effect = RuntimeError("AppD 503")
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        # Should still return a valid result with other signals intact
        assert "triage_summary" in result
        assert result["errors_and_exceptions"] == []

    async def test_all_failures_returns_empty_signals(self, mock_client):
        mock_client.get_health_violations.side_effect = RuntimeError("timeout")
        mock_client.get_errors_and_exceptions.side_effect = RuntimeError("timeout")
        mock_client.get_business_transactions.side_effect = RuntimeError("timeout")
        mock_client.list_snapshots.side_effect = RuntimeError("timeout")
        mock_client.get_tiers.side_effect = RuntimeError("timeout")
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        assert result["timeline"] == []
        assert "no anomalies" in result["triage_summary"]

    async def test_bt_summary_identifies_error_bts(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        bt = result["business_transactions"]
        assert bt["total"] == 2
        assert bt["with_errors"] == 1
        assert bt["top_error_bts"][0]["name"] == "/api/checkout"

    async def test_signals_summary_counts_match_timeline(self, mock_client):
        result = await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        ss = result["signals_summary"]
        critical = sum(1 for e in result["timeline"] if e.get("severity") in ("CRITICAL", "ERROR"))
        assert ss["critical_count"] == critical

    async def test_infra_capped_at_three_tiers(self, mock_client):
        mock_client.get_tiers.return_value = [
            {"name": f"tier-{i}"} for i in range(10)
        ]
        await incident_correlator.run(
            client=mock_client,
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_infra=True,
        )
        assert mock_client.get_infrastructure_stats.call_count <= 3


# ---------------------------------------------------------------------------
# MCP tool wrapper tests
# ---------------------------------------------------------------------------

def _tool(name: str):
    import main as m
    return getattr(m, name)


@pytest.fixture
def patched_correlator(mock_appd_client):
    mock_appd_client.get_health_violations.return_value = []
    mock_appd_client.get_errors_and_exceptions.return_value = []
    mock_appd_client.get_business_transactions.return_value = []
    mock_appd_client.list_snapshots.return_value = []
    mock_appd_client.get_tiers.return_value = []
    from models.types import AppDRole
    with (
        patch("main.get_client", return_value=mock_appd_client),
        patch("main.check_and_wait", new=AsyncMock(return_value=None)),
        patch("main._get_role", new=AsyncMock(return_value=AppDRole.TROUBLESHOOT)),
        patch("main.require_permission"),
        patch("main.audit_log"),
    ):
        yield mock_appd_client


@pytest.mark.asyncio
class TestCorrelateIncidentWindowTool:

    async def test_returns_appd_data_wrapper(self, patched_correlator):
        result = await _tool("correlate_incident_window")(
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        assert "<appd_data>" in result

    async def test_triage_summary_in_output(self, patched_correlator):
        result = await _tool("correlate_incident_window")(
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        assert "triage_summary" in result
        assert "ecommerce-app" in result

    async def test_optional_flags_default_false(self, patched_correlator):
        # Tool should complete without calling infra/network APIs when flags are False
        await _tool("correlate_incident_window")(
            app_name="ecommerce-app",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
        )
        patched_correlator.get_infrastructure_stats.assert_not_called()
        patched_correlator.get_network_kpis.assert_not_called()
