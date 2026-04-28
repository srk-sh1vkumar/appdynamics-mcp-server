"""
tests/unit/test_event_analyzer.py

Unit tests for services/event_analyzer.py heuristics and the
list_application_events MCP tool wrapper.

Heuristics tested:
  - empty events → no change indicators, has_changes=False
  - explicit_deploy_marker (APPLICATION_DEPLOYMENT)
  - config_change (APPLICATION_CONFIG_CHANGE)
  - probable_rolling_deploy (≥2 nodes same tier within 10 min)
  - k8s_pod_turnover (DISCONNECT + CONNECT on new node names)
  - single_node_restart (1 isolated restart — LOW confidence)

Correlator integration:
  - include_deploys=True wires event_analyzer and prepends change_summary
  - include_deploys=False leaves deploys field as None
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from services.event_analyzer import _analyze, run as ea_run

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

START_MS = 1_700_000_000_000
END_MS   = 1_700_003_600_000   # +1 hour

_ROLLING_MS = 5 * 60 * 1000   # 5 min — well inside 10-min window


def _event(etype: str, ts: int, tier: str = "checkout", node: str = "node-1") -> dict:
    return {
        "type": etype,
        "eventTime": ts,
        "affectedEntityName": tier,
        "node": node,
    }


# ---------------------------------------------------------------------------
# _analyze: heuristics on raw event lists (synchronous)
# ---------------------------------------------------------------------------

class TestAnalyzeEmpty:

    def test_no_events_no_indicators(self):
        result = _analyze([], {})
        assert result["has_changes"] is False
        assert result["change_indicators"] == []
        assert "no change indicators" in result["change_summary"]


class TestExplicitDeployMarker:

    def test_single_deployment_event(self):
        events = [_event("APPLICATION_DEPLOYMENT", START_MS)]
        result = _analyze(events, {})
        assert result["has_changes"] is True
        types = [c["type"] for c in result["change_indicators"]]
        assert "explicit_deploy_marker" in types

    def test_confidence_is_high(self):
        events = [_event("APPLICATION_DEPLOYMENT", START_MS)]
        result = _analyze(events, {})
        deploy = next(c for c in result["change_indicators"] if c["type"] == "explicit_deploy_marker")
        assert deploy["confidence"] == "HIGH"

    def test_multiple_deployment_events_counted(self):
        events = [
            _event("APPLICATION_DEPLOYMENT", START_MS),
            _event("APPLICATION_DEPLOYMENT", START_MS + 60_000),
        ]
        result = _analyze(events, {})
        deploy = next(c for c in result["change_indicators"] if c["type"] == "explicit_deploy_marker")
        assert deploy["count"] == 2


class TestConfigChange:

    def test_config_change_event(self):
        events = [_event("APPLICATION_CONFIG_CHANGE", START_MS)]
        result = _analyze(events, {})
        assert result["has_changes"] is True
        types = [c["type"] for c in result["change_indicators"]]
        assert "config_change" in types

    def test_config_change_confidence_high(self):
        events = [_event("APPLICATION_CONFIG_CHANGE", START_MS)]
        result = _analyze(events, {})
        cc = next(c for c in result["change_indicators"] if c["type"] == "config_change")
        assert cc["confidence"] == "HIGH"


class TestProbableRollingDeploy:

    def test_two_nodes_same_tier_within_window(self):
        events = [
            _event("APP_SERVER_RESTART", START_MS, "checkout", "node-1"),
            _event("APP_SERVER_RESTART", START_MS + _ROLLING_MS, "checkout", "node-2"),
        ]
        result = _analyze(events, {"checkout": 4})
        types = [c["type"] for c in result["change_indicators"]]
        assert "probable_rolling_deploy" in types

    def test_high_confidence_when_half_tier_affected(self):
        # 2 out of 2 nodes → 100% → HIGH
        events = [
            _event("APP_SERVER_RESTART", START_MS, "checkout", "node-1"),
            _event("APP_SERVER_RESTART", START_MS + _ROLLING_MS, "checkout", "node-2"),
        ]
        result = _analyze(events, {"checkout": 2})
        rd = next(c for c in result["change_indicators"] if c["type"] == "probable_rolling_deploy")
        assert rd["confidence"] == "HIGH"

    def test_medium_confidence_when_less_than_half_affected(self):
        # 2 out of 10 nodes → 20% → MEDIUM
        events = [
            _event("APP_SERVER_RESTART", START_MS, "checkout", "node-1"),
            _event("APP_SERVER_RESTART", START_MS + _ROLLING_MS, "checkout", "node-2"),
        ]
        result = _analyze(events, {"checkout": 10})
        rd = next(c for c in result["change_indicators"] if c["type"] == "probable_rolling_deploy")
        assert rd["confidence"] == "MEDIUM"

    def test_two_nodes_different_tiers_not_rolled_up(self):
        # One restart per tier — neither qualifies as rolling
        events = [
            _event("APP_SERVER_RESTART", START_MS, "checkout", "node-1"),
            _event("APP_SERVER_RESTART", START_MS + _ROLLING_MS, "payments", "node-2"),
        ]
        result = _analyze(events, {})
        types = [c["type"] for c in result["change_indicators"]]
        assert "probable_rolling_deploy" not in types

    def test_no_rolling_deploy_for_single_restart(self):
        events = [_event("APP_SERVER_RESTART", START_MS, "checkout", "node-1")]
        result = _analyze(events, {})
        types = [c["type"] for c in result["change_indicators"]]
        assert "probable_rolling_deploy" not in types


class TestSingleNodeRestart:

    def test_single_restart_marked_low_confidence(self):
        events = [_event("APP_SERVER_RESTART", START_MS, "checkout", "node-1")]
        result = _analyze(events, {})
        assert result["has_changes"] is True
        types = [c["type"] for c in result["change_indicators"]]
        assert "single_node_restart" in types
        sn = next(c for c in result["change_indicators"] if c["type"] == "single_node_restart")
        assert sn["confidence"] == "LOW"

    def test_single_restart_suppressed_when_stronger_signal_present(self):
        # explicit deploy + single restart — single_node_restart should NOT appear
        events = [
            _event("APPLICATION_DEPLOYMENT", START_MS),
            _event("APP_SERVER_RESTART", START_MS + 60_000, "checkout", "node-1"),
        ]
        result = _analyze(events, {})
        types = [c["type"] for c in result["change_indicators"]]
        assert "single_node_restart" not in types, "single_node_restart must be suppressed when explicit_deploy_marker is present"


class TestK8sPodTurnover:

    def test_disconnect_connect_new_node_same_tier(self):
        events = [
            _event("AGENT_DISCONNECT", START_MS, "checkout", "pod-abc-old"),
            _event("AGENT_DISCONNECT", START_MS + 30_000, "checkout", "pod-def-old"),
            _event("AGENT_CONNECT", START_MS + 60_000, "checkout", "pod-xyz-new1"),
            _event("AGENT_CONNECT", START_MS + 90_000, "checkout", "pod-xyz-new2"),
        ]
        result = _analyze(events, {})
        types = [c["type"] for c in result["change_indicators"]]
        assert "k8s_pod_turnover" in types

    def test_k8s_turnover_high_when_two_or_more_new_pods(self):
        events = [
            _event("AGENT_DISCONNECT", START_MS, "checkout", "old-1"),
            _event("AGENT_CONNECT", START_MS + 60_000, "checkout", "new-a"),
            _event("AGENT_CONNECT", START_MS + 90_000, "checkout", "new-b"),
        ]
        result = _analyze(events, {})
        k8s = next((c for c in result["change_indicators"] if c["type"] == "k8s_pod_turnover"), None)
        assert k8s is not None
        assert k8s["confidence"] == "HIGH"

    def test_same_node_reconnect_no_turnover(self):
        # Same node name reconnects — not a pod replacement
        events = [
            _event("AGENT_DISCONNECT", START_MS, "checkout", "node-1"),
            _event("AGENT_CONNECT", START_MS + 60_000, "checkout", "node-1"),
        ]
        result = _analyze(events, {})
        types = [c["type"] for c in result["change_indicators"]]
        assert "k8s_pod_turnover" not in types


# ---------------------------------------------------------------------------
# run() — async integration (mocked client)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestEventAnalyzerRun:

    async def test_returns_raw_events_and_analysis(self):
        client = AsyncMock()
        client.get_application_events.return_value = [
            _event("APPLICATION_DEPLOYMENT", START_MS),
        ]
        result = await ea_run(client, "MyApp", START_MS, END_MS)
        assert result["app_name"] == "MyApp"
        assert result["event_count"] == 1
        assert len(result["raw_events"]) == 1
        assert result["has_changes"] is True

    async def test_empty_events_from_api(self):
        client = AsyncMock()
        client.get_application_events.return_value = []
        result = await ea_run(client, "MyApp", START_MS, END_MS)
        assert result["has_changes"] is False
        assert result["change_indicators"] == []

    async def test_raw_events_capped_at_50(self):
        client = AsyncMock()
        client.get_application_events.return_value = [
            _event("AGENT_EVENT", START_MS + i * 1000) for i in range(100)
        ]
        result = await ea_run(client, "MyApp", START_MS, END_MS)
        assert len(result["raw_events"]) == 50

    async def test_tier_node_counts_propagated(self):
        client = AsyncMock()
        client.get_application_events.return_value = [
            _event("APP_SERVER_RESTART", START_MS, "checkout", "node-1"),
            _event("APP_SERVER_RESTART", START_MS + _ROLLING_MS, "checkout", "node-2"),
        ]
        result = await ea_run(client, "MyApp", START_MS, END_MS, tier_node_counts={"checkout": 2})
        rd = next(c for c in result["change_indicators"] if c["type"] == "probable_rolling_deploy")
        assert rd["confidence"] == "HIGH"


# ---------------------------------------------------------------------------
# Correlator integration — include_deploys wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCorrelatorDeployWiring:

    def _mock_client(self, deploy_events: list | None = None) -> AsyncMock:
        client = AsyncMock()
        client.get_health_violations.return_value = []
        client.get_errors_and_exceptions.return_value = []
        client.get_business_transactions.return_value = []
        client.list_snapshots.return_value = []
        client.get_tiers.return_value = [{"name": "checkout", "numberOfNodes": 3}]
        client.get_application_events.return_value = deploy_events or []
        return client

    async def test_include_deploys_true_calls_event_analyzer(self):
        from services import incident_correlator

        client = self._mock_client([_event("APPLICATION_DEPLOYMENT", START_MS)])
        result = await incident_correlator.run(
            client=client,
            app_name="MyApp",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_deploys=True,
        )
        assert result["deploys"] is not None
        assert result["deploys"]["has_changes"] is True

    async def test_include_deploys_false_leaves_none(self):
        from services import incident_correlator

        client = self._mock_client()
        result = await incident_correlator.run(
            client=client,
            app_name="MyApp",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_deploys=False,
        )
        assert result["deploys"] is None

    async def test_change_summary_prepended_to_triage_summary(self):
        from services import incident_correlator

        client = self._mock_client([_event("APPLICATION_DEPLOYMENT", START_MS)])
        result = await incident_correlator.run(
            client=client,
            app_name="MyApp",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_deploys=True,
        )
        # triage_summary should start with the change context
        assert "HIGH" in result["triage_summary"] or "change" in result["triage_summary"].lower()

    async def test_no_change_events_does_not_prepend(self):
        from services import incident_correlator

        client = self._mock_client([])   # empty events
        result = await incident_correlator.run(
            client=client,
            app_name="MyApp",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_deploys=True,
        )
        # No changes — triage_summary starts with app name, not change context
        assert result["triage_summary"].startswith("MyApp")

    async def test_change_indicators_added_to_timeline(self):
        from services import incident_correlator

        client = self._mock_client([_event("APPLICATION_DEPLOYMENT", START_MS)])
        result = await incident_correlator.run(
            client=client,
            app_name="MyApp",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_deploys=True,
        )
        change_entries = [e for e in result["timeline"] if e.get("type") == "change_indicator"]
        assert len(change_entries) >= 1

    async def test_signals_summary_includes_change_indicators_count(self):
        from services import incident_correlator

        client = self._mock_client([_event("APPLICATION_DEPLOYMENT", START_MS)])
        result = await incident_correlator.run(
            client=client,
            app_name="MyApp",
            start_time_ms=START_MS,
            end_time_ms=END_MS,
            include_deploys=True,
        )
        assert result["signals_summary"]["change_indicators_count"] >= 1
