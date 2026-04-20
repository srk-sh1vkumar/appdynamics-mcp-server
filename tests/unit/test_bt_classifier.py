"""
tests/unit/test_bt_classifier.py

Unit tests for services/bt_classifier.py.

Coverage targets:
- is_health_check: name regex, path regex, heuristic (avg<10ms + zero errors)
- classify_criticality: CRITICAL / HIGH / MEDIUM / LOW
- classify_type: DATA_HEAVY_READ / EXTERNAL_DEPENDENCY_RISK /
                 HIGH_FREQUENCY_LIGHTWEIGHT / EXPENSIVE_INFREQUENT / STANDARD
- filter_and_sort_bts: healthcheck filtering, failing healthcheck included, sort order
- enrich_bt: output shape
"""

from __future__ import annotations

from models.types import BTType, BusinessTransaction, Criticality
from services.bt_classifier import (
    classify_criticality,
    classify_type,
    enrich_bt,
    filter_and_sort_bts,
    is_health_check,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bt(
    name: str = "/api/checkout",
    tier_name: str = "checkout-tier",
    avg_rt_ms: float = 500,
    cpm: float = 50,
    error_rate: float = 0.0,
    db_calls: int = 0,
    ext_calls: int = 0,
    entry_point: str = "SERVLET",
) -> BusinessTransaction:
    return BusinessTransaction(
        id=1,
        name=name,
        entry_point_type=entry_point,
        tier_name=tier_name,
        avg_response_time_ms=avg_rt_ms,
        calls_per_minute=cpm,
        error_rate=error_rate,
        db_call_count=db_calls,
        external_call_count=ext_calls,
    )


# ---------------------------------------------------------------------------
# is_health_check
# ---------------------------------------------------------------------------

class TestIsHealthCheck:
    def test_name_contains_health(self):
        assert is_health_check(_bt(name="/health")) is True

    def test_name_contains_ping(self):
        assert is_health_check(_bt(name="/ping")) is True

    def test_name_contains_actuator(self):
        assert is_health_check(_bt(name="/actuator/info")) is True

    def test_name_contains_liveness(self):
        assert is_health_check(_bt(name="/liveness")) is True

    def test_name_contains_readiness(self):
        assert is_health_check(_bt(name="/readiness")) is True

    def test_name_contains_heartbeat(self):
        assert is_health_check(_bt(name="/heartbeat")) is True

    def test_path_actuator_prefix(self):
        assert is_health_check(
            _bt(name="/checkout", tier_name="/actuator/health")
        ) is True

    def test_path_health_prefix(self):
        assert is_health_check(_bt(name="/checkout", tier_name="/health/live")) is True

    def test_heuristic_fast_zero_errors(self):
        # avg < 10ms AND error_rate == 0 → heuristic healthcheck
        assert is_health_check(_bt(name="/probe", avg_rt_ms=5, error_rate=0.0)) is True

    def test_heuristic_fast_with_errors_not_healthcheck(self):
        # Fast but has errors — real failing endpoint
        assert is_health_check(_bt(name="/probe", avg_rt_ms=5, error_rate=0.5)) is False

    def test_normal_bt_not_healthcheck(self):
        assert is_health_check(
            _bt(name="/api/checkout", avg_rt_ms=500, error_rate=1.0)
        ) is False

    def test_case_insensitive_name(self):
        assert is_health_check(_bt(name="/HEALTH/live")) is True


# ---------------------------------------------------------------------------
# classify_criticality
# ---------------------------------------------------------------------------

class TestClassifyCriticality:
    def test_payment_is_critical(self):
        assert classify_criticality(_bt(name="/payment/charge")) == Criticality.CRITICAL

    def test_checkout_is_critical(self):
        assert classify_criticality(_bt(name="/checkout")) == Criticality.CRITICAL

    def test_order_is_critical(self):
        assert (
            classify_criticality(_bt(name="/api/order/create")) == Criticality.CRITICAL
        )

    def test_auth_is_critical(self):
        assert classify_criticality(_bt(name="/auth/login")) == Criticality.CRITICAL

    def test_high_error_rate_is_high(self):
        # error_rate > 1.0 → HIGH
        bt = _bt(name="/api/products", error_rate=2.0)
        assert classify_criticality(bt) == Criticality.HIGH

    def test_high_rt_is_high(self):
        # avg_rt > 2000ms → HIGH
        bt = _bt(name="/api/products", avg_rt_ms=2500)
        assert classify_criticality(bt) == Criticality.HIGH

    def test_high_cpm_is_medium(self):
        # cpm > 100 → MEDIUM (assuming no other triggers)
        bt = _bt(name="/api/products", cpm=150, error_rate=0.5, avg_rt_ms=200)
        assert classify_criticality(bt) == Criticality.MEDIUM

    def test_low_everything_is_low(self):
        bt = _bt(name="/api/products", cpm=10, error_rate=0.1, avg_rt_ms=100)
        assert classify_criticality(bt) == Criticality.LOW

    def test_critical_takes_precedence_over_high(self):
        # Name matches "payment" + also has high error rate
        bt = _bt(name="/payment", error_rate=5.0, avg_rt_ms=3000)
        assert classify_criticality(bt) == Criticality.CRITICAL


# ---------------------------------------------------------------------------
# classify_type
# ---------------------------------------------------------------------------

class TestClassifyType:
    def test_data_heavy_read(self):
        # db > 5 AND avg_rt > 500
        bt = _bt(db_calls=8, avg_rt_ms=800)
        assert classify_type(bt) == BTType.DATA_HEAVY_READ

    def test_external_dependency_risk(self):
        # error_rate > 2.0 AND ext_calls > 0
        bt = _bt(error_rate=3.5, ext_calls=2)
        assert classify_type(bt) == BTType.EXTERNAL_DEPENDENCY_RISK

    def test_high_frequency_lightweight(self):
        # cpm > 500 AND avg_rt < 100
        bt = _bt(cpm=600, avg_rt_ms=50)
        assert classify_type(bt) == BTType.HIGH_FREQUENCY_LIGHTWEIGHT

    def test_expensive_infrequent(self):
        # cpm < 10 AND avg_rt > 1000
        bt = _bt(cpm=5, avg_rt_ms=1500)
        assert classify_type(bt) == BTType.EXPENSIVE_INFREQUENT

    def test_standard_default(self):
        bt = _bt(cpm=50, avg_rt_ms=300, error_rate=0.5, db_calls=2, ext_calls=0)
        assert classify_type(bt) == BTType.STANDARD


# ---------------------------------------------------------------------------
# enrich_bt output shape
# ---------------------------------------------------------------------------

class TestEnrichBt:
    def test_required_keys_present(self):
        bt = _bt()
        result = enrich_bt(bt)
        for key in (
            "id", "name", "entry_point_type", "avg_response_time_ms",
            "calls_per_minute", "error_rate", "criticality", "type", "is_health_check",
        ):
            assert key in result, f"Missing key: {key}"

    def test_criticality_is_string(self):
        result = enrich_bt(_bt(name="/payment"))
        assert isinstance(result["criticality"], str)
        assert result["criticality"] == Criticality.CRITICAL.value

    def test_type_is_string(self):
        result = enrich_bt(_bt(db_calls=10, avg_rt_ms=900))
        assert isinstance(result["type"], str)


# ---------------------------------------------------------------------------
# filter_and_sort_bts
# ---------------------------------------------------------------------------

class TestFilterAndSortBts:
    def _bts(self) -> list[BusinessTransaction]:
        return [
            _bt(name="/payment", error_rate=0.1, avg_rt_ms=200, cpm=30),     # CRITICAL
            _bt(name="/api/products", error_rate=2.5, avg_rt_ms=2500, cpm=80),  # HIGH
            _bt(name="/health", avg_rt_ms=5, error_rate=0.0, cpm=120),  # healthcheck
            _bt(name="/api/user", error_rate=0.0, avg_rt_ms=100, cpm=60),      # MEDIUM
        ]

    def test_healthcheck_excluded_by_default(self):
        results = filter_and_sort_bts(self._bts())
        names = [r["name"] for r in results]
        assert "/health" not in names

    def test_healthcheck_included_when_requested(self):
        results = filter_and_sort_bts(self._bts(), include_health_checks=True)
        names = [r["name"] for r in results]
        assert "/health" in names

    def test_failing_healthcheck_always_included(self):
        bts = self._bts()
        # Make the healthcheck fail
        bts[2] = _bt(name="/health", avg_rt_ms=5, error_rate=5.0, cpm=120)
        results = filter_and_sort_bts(bts)
        names = [r["name"] for r in results]
        assert "/health" in names

    def test_critical_first(self):
        results = filter_and_sort_bts(self._bts())
        assert results[0]["criticality"] == Criticality.CRITICAL.value

    def test_sorted_by_error_rate_within_tier(self):
        bts = [
            _bt(name="/api/a", error_rate=5.0, avg_rt_ms=2500),  # HIGH, 5% error
            _bt(name="/api/b", error_rate=1.5, avg_rt_ms=2500),  # HIGH, 1.5% error
        ]
        results = filter_and_sort_bts(bts)
        assert results[0]["name"] == "/api/a"
