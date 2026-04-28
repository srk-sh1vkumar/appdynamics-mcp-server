"""
tests/unit/test_permissions.py

Tests for the tool permission sets in auth/appd_auth.py.

These tests are structural regression tests. In single-user mode require_permission
is a no-op (everyone is CONFIGURE_ALERTING), but the permission sets define the
intended access tiers that will be enforced when multi-user mode is activated.

The tests verify:
  1. Every tool registered in main.py appears in at least one permission set.
  2. The set hierarchy is correct (VIEW ⊆ TROUBLESHOOT ⊆ CONFIGURE_ALERTING).
  3. Tools are in the right tier (read-only in VIEW, triage in TROUBLESHOOT, etc.).
  4. The DENIED role cannot access any tool.
"""

from __future__ import annotations

import pytest

from auth.appd_auth import _CONFIGURE_ALERTING_TOOLS, _ROLE_TOOLS, _TROUBLESHOOT_TOOLS, _VIEW_TOOLS
from models.types import AppDRole

# ---------------------------------------------------------------------------
# Every tool in main.py — kept in sync manually; add new tools here.
# ---------------------------------------------------------------------------

ALL_TOOLS: frozenset[str] = frozenset({
    # Discovery
    "list_controllers", "list_applications", "search_metric_tree",
    # Metrics
    "get_metrics",
    # Business transactions
    "get_business_transactions", "get_bt_baseline", "get_bt_detection_rules",
    # API spec
    "load_api_spec",
    # Snapshots
    "list_snapshots", "analyze_snapshot", "compare_snapshots",
    "archive_snapshot", "set_golden_snapshot",
    # Health
    "get_health_violations", "get_policies",
    # Infrastructure
    "get_infrastructure_stats", "get_jvm_details", "get_tiers_and_nodes",
    "get_exit_calls", "get_agent_status",
    # Errors
    "get_errors_and_exceptions",
    # Database / network
    "get_database_performance", "get_network_kpis",
    # Analytics
    "query_analytics_logs",
    # Async trace
    "stitch_async_trace",
    # EUM
    "get_eum_overview", "get_eum_page_performance", "get_eum_js_errors",
    "get_eum_ajax_requests", "get_eum_geo_performance", "correlate_eum_to_bt",
    # System / admin
    "get_server_health",
    # Runbook
    "save_runbook",
    # Team health
    "get_team_health_summary",
    # Composite triage
    "correlate_incident_window",
})


# ---------------------------------------------------------------------------
# Coverage: every tool must be in at least one set
# ---------------------------------------------------------------------------

class TestPermissionCoverage:

    def test_all_tools_covered_by_configure_alerting(self):
        missing = ALL_TOOLS - _CONFIGURE_ALERTING_TOOLS
        assert not missing, f"Tools not in any permission set: {sorted(missing)}"

    def test_no_unknown_tools_in_view(self):
        extra = _VIEW_TOOLS - ALL_TOOLS
        assert not extra, f"VIEW set contains unregistered tools: {sorted(extra)}"

    def test_no_unknown_tools_in_troubleshoot(self):
        extra = _TROUBLESHOOT_TOOLS - ALL_TOOLS
        assert not extra, f"TROUBLESHOOT set contains unregistered tools: {sorted(extra)}"

    def test_no_unknown_tools_in_configure_alerting(self):
        extra = _CONFIGURE_ALERTING_TOOLS - ALL_TOOLS
        assert not extra, f"CONFIGURE_ALERTING set contains unregistered tools: {sorted(extra)}"


# ---------------------------------------------------------------------------
# Hierarchy: VIEW ⊆ TROUBLESHOOT ⊆ CONFIGURE_ALERTING
# ---------------------------------------------------------------------------

class TestPermissionHierarchy:

    def test_view_is_subset_of_troubleshoot(self):
        assert _VIEW_TOOLS <= _TROUBLESHOOT_TOOLS

    def test_troubleshoot_is_subset_of_configure_alerting(self):
        assert _TROUBLESHOOT_TOOLS <= _CONFIGURE_ALERTING_TOOLS

    def test_view_is_subset_of_configure_alerting(self):
        assert _VIEW_TOOLS <= _CONFIGURE_ALERTING_TOOLS


# ---------------------------------------------------------------------------
# Role-to-toolset mapping
# ---------------------------------------------------------------------------

class TestRoleToolMapping:

    def test_denied_role_has_no_tools(self):
        assert _ROLE_TOOLS[AppDRole.DENIED] == frozenset()

    def test_view_role_maps_to_view_set(self):
        assert _ROLE_TOOLS[AppDRole.VIEW] == _VIEW_TOOLS

    def test_troubleshoot_role_maps_to_troubleshoot_set(self):
        assert _ROLE_TOOLS[AppDRole.TROUBLESHOOT] == _TROUBLESHOOT_TOOLS

    def test_configure_alerting_role_maps_to_full_set(self):
        assert _ROLE_TOOLS[AppDRole.CONFIGURE_ALERTING] == _CONFIGURE_ALERTING_TOOLS


# ---------------------------------------------------------------------------
# Specific tool tier placement
# ---------------------------------------------------------------------------

class TestToolTierPlacement:

    # VIEW-only tools — read-only, no snapshot or alert access
    @pytest.mark.parametrize("tool", [
        "list_controllers", "list_applications", "get_metrics",
        "get_health_violations", "get_infrastructure_stats", "get_tiers_and_nodes",
        "get_team_health_summary", "get_server_health",
    ])
    def test_view_tool_in_view_set(self, tool):
        assert tool in _VIEW_TOOLS, f"{tool} should be in VIEW"

    # TROUBLESHOOT tools — snapshot access, triage, runbooks
    @pytest.mark.parametrize("tool", [
        "list_snapshots", "analyze_snapshot", "compare_snapshots",
        "get_errors_and_exceptions", "stitch_async_trace",
        "correlate_incident_window", "save_runbook",
    ])
    def test_troubleshoot_tool_not_in_view(self, tool):
        assert tool not in _VIEW_TOOLS, f"{tool} should NOT be in VIEW"
        assert tool in _TROUBLESHOOT_TOOLS, f"{tool} should be in TROUBLESHOOT"

    # CONFIGURE_ALERTING-only tools — alert policies, golden baseline changes
    @pytest.mark.parametrize("tool", [
        "get_policies", "archive_snapshot", "set_golden_snapshot",
    ])
    def test_configure_alerting_tool_not_in_troubleshoot(self, tool):
        assert tool not in _TROUBLESHOOT_TOOLS, f"{tool} should NOT be in TROUBLESHOOT"
        assert tool in _CONFIGURE_ALERTING_TOOLS, f"{tool} should be in CONFIGURE_ALERTING"

    # correlate_incident_window specifically — the missing-from-sets bug
    def test_correlate_incident_window_in_troubleshoot(self):
        assert "correlate_incident_window" in _TROUBLESHOOT_TOOLS

    def test_correlate_incident_window_in_configure_alerting(self):
        assert "correlate_incident_window" in _CONFIGURE_ALERTING_TOOLS

    def test_correlate_incident_window_not_in_view(self):
        assert "correlate_incident_window" not in _VIEW_TOOLS
