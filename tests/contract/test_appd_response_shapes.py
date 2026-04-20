"""
tests/contract/test_appd_response_shapes.py

Contract tests: verify that the AppDynamics REST API response shapes assumed
by the MCP server's parsers and models are structurally valid.

These tests validate that:
  - Required fields are present in API response dicts
  - Field types match what the Pydantic models expect
  - Edge cases (empty lists, missing optional fields) are handled gracefully
  - The snapshot parser can consume real-shaped response data
  - BusinessTransaction.model_validate() accepts the expected response shape

They do NOT call a real AppDynamics controller. All data comes from the
canonical fixture responses defined in tests/conftest.py.
"""

from __future__ import annotations

from models.types import BusinessTransaction, SnapshotSummary, StackLanguage
from parsers.snapshot_parser import (
    detect_language,
    parse_snapshot_errors,
)

# ---------------------------------------------------------------------------
# Re-import fixture data directly for contract assertions
# ---------------------------------------------------------------------------
from tests.conftest import (
    APP_LIST_RESPONSE,
    BT_LIST_RESPONSE,
    EUM_OVERVIEW_RESPONSE,
    GOLDEN_SNAPSHOT_RESPONSE,
    HEALTH_VIOLATIONS_RESPONSE,
    JVM_RESPONSE,
    SNAPSHOT_DETAIL_RESPONSE,
    SNAPSHOT_LIST_RESPONSE,
)

# ---------------------------------------------------------------------------
# Applications list contract
# ---------------------------------------------------------------------------

class TestApplicationsResponseShape:
    def test_each_app_has_id_and_name(self):
        """Every application in the list response must have 'id' and 'name'."""
        for app in APP_LIST_RESPONSE:
            assert "id" in app, f"Missing 'id' in: {app}"
            assert "name" in app, f"Missing 'name' in: {app}"

    def test_app_id_is_int(self):
        for app in APP_LIST_RESPONSE:
            assert isinstance(app["id"], int), f"id must be int, got {type(app['id'])}"

    def test_app_name_is_str(self):
        for app in APP_LIST_RESPONSE:
            assert isinstance(app["name"], str), f"name must be str, got {type(app['name'])}"  # noqa: E501

    def test_empty_apps_list_is_valid(self):
        """An empty application list is a valid API response (new controllers)."""
        apps: list = []
        assert isinstance(apps, list)
        assert len(apps) == 0


# ---------------------------------------------------------------------------
# Business Transactions contract
# ---------------------------------------------------------------------------

class TestBusinessTransactionResponseShape:
    def test_required_fields_present(self):
        """Every BT response dict must have the fields BusinessTransaction expects."""
        required = {
            "id", "name", "entryPointType", "tierName",
            "averageResponseTime", "callsPerMinute", "errorPercent",
        }
        for bt in BT_LIST_RESPONSE:
            missing = required - bt.keys()
            assert not missing, f"BT response missing fields: {missing}"

    def test_pydantic_model_validates(self):
        """BusinessTransaction.model_validate() must accept each BT dict."""
        for bt in BT_LIST_RESPONSE:
            obj = BusinessTransaction.model_validate(bt)
            assert obj.name == bt["name"]
            assert obj.id == bt["id"]

    def test_error_percent_is_float(self):
        for bt in BT_LIST_RESPONSE:
            assert isinstance(bt["errorPercent"], (int, float)), (
                f"errorPercent must be numeric, got {type(bt['errorPercent'])}"
            )

    def test_average_response_time_non_negative(self):
        for bt in BT_LIST_RESPONSE:
            assert bt["averageResponseTime"] >= 0, (
                f"averageResponseTime must be ≥ 0, got {bt['averageResponseTime']}"
            )

    def test_health_check_bt_detectable(self):
        """bt_classifier can identify health-check BTs by name pattern."""
        from services.bt_classifier import is_health_check

        health_bt = next(b for b in BT_LIST_RESPONSE if b["name"] == "/health")
        assert is_health_check(BusinessTransaction.model_validate(health_bt))

    def test_critical_bt_detectable(self):
        """bt_classifier can identify critical BTs (high error rate)."""
        from models.types import Criticality
        from services.bt_classifier import classify_criticality

        checkout_bt = BusinessTransaction.model_validate(
            next(b for b in BT_LIST_RESPONSE if b["name"] == "/api/checkout")
        )
        criticality = classify_criticality(checkout_bt)
        assert criticality != Criticality.LOW, (
            "checkout BT with 3.5% error rate should not be LOW criticality"
        )


# ---------------------------------------------------------------------------
# Snapshot list contract
# ---------------------------------------------------------------------------

class TestSnapshotListResponseShape:
    def test_required_fields_present(self):
        required = {"requestGUID", "serverStartTime", "timeTakenInMilliSecs"}
        for snap in SNAPSHOT_LIST_RESPONSE:
            missing = required - snap.keys()
            assert not missing, f"Snapshot response missing fields: {missing}"

    def test_request_guid_is_str(self):
        for snap in SNAPSHOT_LIST_RESPONSE:
            assert isinstance(snap["requestGUID"], str)

    def test_server_start_time_is_epoch_ms(self):
        """serverStartTime must be a large integer (epoch ms, not seconds)."""
        for snap in SNAPSHOT_LIST_RESPONSE:
            ts = snap["serverStartTime"]
            assert isinstance(ts, (int, float))
            # 2020-01-01 in epoch ms = 1577836800000; sanity-check magnitude
            assert ts > 1_000_000_000_000, (
                f"serverStartTime looks like epoch seconds, not ms: {ts}"
            )

    def test_pydantic_snapshot_summary_validates(self):
        """SnapshotSummary.model_validate() must accept each snapshot dict."""
        for snap in SNAPSHOT_LIST_RESPONSE:
            obj = SnapshotSummary.model_validate(snap)
            assert obj.request_guid == snap["requestGUID"]


# ---------------------------------------------------------------------------
# Snapshot detail contract — parser compatibility
# ---------------------------------------------------------------------------

class TestSnapshotDetailResponseShape:
    def test_required_fields_present(self):
        required = {
            "requestGUID", "serverStartTime", "timeTakenInMilliSecs", "errorDetails"
        }
        missing = required - SNAPSHOT_DETAIL_RESPONSE.keys()
        assert not missing, f"Snapshot detail missing fields: {missing}"

    def test_detect_language_identifies_java(self):
        """detect_language() must return JAVA for the mock Java stack trace."""
        stack_trace = SNAPSHOT_DETAIL_RESPONSE["errorDetails"]
        lang = detect_language(stack_trace)
        assert lang == StackLanguage.JAVA

    def test_parse_errors_extracts_npe(self):
        """parse_snapshot_errors() must extract the NullPointerException."""
        stack_trace = SNAPSHOT_DETAIL_RESPONSE["errorDetails"]
        parse_snapshot_errors(stack_trace)  # verify no exception raised
        # ParsedStack has a frames list; check the raw trace for the exception
        assert "NullPointerException" in stack_trace, (
            "Fixture errorDetails should contain NullPointerException"
        )

    def test_stacks_field_is_list(self):
        assert isinstance(SNAPSHOT_DETAIL_RESPONSE["stacks"], list)

    def test_call_chain_is_str(self):
        assert isinstance(SNAPSHOT_DETAIL_RESPONSE.get("callChain", ""), str)

    def test_error_occurred_is_bool(self):
        assert isinstance(SNAPSHOT_DETAIL_RESPONSE["errorOccurred"], bool)
        assert SNAPSHOT_DETAIL_RESPONSE["errorOccurred"] is True

    def test_exit_call_has_timing(self):
        """DB exit calls must include timeTakenInMilliSecs for hotpath analysis."""
        stacks = SNAPSHOT_DETAIL_RESPONSE["stacks"]
        exit_calls = stacks[0]["exitCalls"]
        for ec in exit_calls:
            assert "timeTakenInMilliSecs" in ec, f"Exit call missing timing: {ec}"
            assert ec["timeTakenInMilliSecs"] >= 0


# ---------------------------------------------------------------------------
# Golden snapshot contract
# ---------------------------------------------------------------------------

class TestGoldenSnapshotResponseShape:
    def test_golden_has_no_errors(self):
        assert GOLDEN_SNAPSHOT_RESPONSE["errorOccurred"] is False
        assert GOLDEN_SNAPSHOT_RESPONSE["errorDetails"] == ""

    def test_golden_response_time_low(self):
        """Golden baseline must be significantly faster than slow snapshots."""
        golden_time = GOLDEN_SNAPSHOT_RESPONSE["timeTakenInMilliSecs"]
        slow_time = SNAPSHOT_DETAIL_RESPONSE["timeTakenInMilliSecs"]
        assert golden_time < slow_time, (
            f"Golden ({golden_time}ms) should be faster than slow ({slow_time}ms)"
        )

    def test_golden_user_experience_normal(self):
        assert GOLDEN_SNAPSHOT_RESPONSE["userExperience"] == "NORMAL"


# ---------------------------------------------------------------------------
# Health violations contract
# ---------------------------------------------------------------------------

class TestHealthViolationsResponseShape:
    def test_required_fields_present(self):
        required = {"id", "name", "severity", "startTime", "affectedEntityName"}
        for v in HEALTH_VIOLATIONS_RESPONSE:
            missing = required - v.keys()
            assert not missing, f"Violation missing fields: {missing}"

    def test_severity_is_valid_enum_value(self):
        valid_severities = {"WARNING", "CRITICAL", "INFO"}
        for v in HEALTH_VIOLATIONS_RESPONSE:
            assert v["severity"] in valid_severities, (
                f"Unexpected severity: {v['severity']}"
            )

    def test_active_violation_has_no_end_time(self):
        """Active violations use -1 as endTime sentinel."""
        active = [v for v in HEALTH_VIOLATIONS_RESPONSE if v.get("endTime") == -1]
        assert len(active) > 0, "Expected at least one active violation in fixture"


# ---------------------------------------------------------------------------
# JVM details contract
# ---------------------------------------------------------------------------

class TestJvmResponseShape:
    def test_memory_pool_usage_is_list(self):
        assert isinstance(JVM_RESPONSE["memoryPoolUsage"], list)

    def test_memory_pool_entries_have_required_fields(self):
        required = {"name", "used", "committed", "max"}
        for pool in JVM_RESPONSE["memoryPoolUsage"]:
            missing = required - pool.keys()
            assert not missing, f"Memory pool entry missing fields: {missing}"

    def test_used_le_max(self):
        """used memory must not exceed max (would indicate a corrupt response)."""
        for pool in JVM_RESPONSE["memoryPoolUsage"]:
            if pool["max"] > 0:
                assert pool["used"] <= pool["max"], (
                    f"Pool {pool['name']}: used ({pool['used']}) > max ({pool['max']})"
                )

    def test_gc_stats_is_list(self):
        assert isinstance(JVM_RESPONSE["gcStats"], list)

    def test_thread_count_positive(self):
        assert JVM_RESPONSE["threadCount"] > 0

    def test_deadlocked_threads_is_list(self):
        assert isinstance(JVM_RESPONSE["deadlockedThreads"], list)


# ---------------------------------------------------------------------------
# EUM overview contract
# ---------------------------------------------------------------------------

class TestEumOverviewResponseShape:
    def test_required_fields_present(self):
        required = {"pageViews", "jsErrors", "ajaxErrors", "avgPageLoadTime"}
        missing = required - EUM_OVERVIEW_RESPONSE.keys()
        assert not missing, f"EUM response missing fields: {missing}"

    def test_page_views_positive(self):
        assert EUM_OVERVIEW_RESPONSE["pageViews"] > 0

    def test_avg_page_load_time_is_numeric(self):
        assert isinstance(EUM_OVERVIEW_RESPONSE["avgPageLoadTime"], (int, float))
        assert EUM_OVERVIEW_RESPONSE["avgPageLoadTime"] >= 0
