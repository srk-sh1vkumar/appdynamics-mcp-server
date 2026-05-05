"""
tests/unit/test_trace_stitcher.py

Tests for services/trace_stitcher.py — service layer only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from services import trace_stitcher

CID = "corr-abc-123"

BASE_MS = 1_700_000_000_000


def _snap(app: str, start_ms: int, duration_ms: int = 50, error: bool = False, **extra) -> dict:
    return {
        "requestGUID": f"guid-{app}",
        "_app_name": app,
        "serverStartTime": start_ms,
        "timeTakenInMilliSecs": duration_ms,
        "errorOccurred": error,
        **extra,
    }


def _make_client(snap_map: dict[str, list[dict]], fail_apps: set[str] | None = None):
    client = AsyncMock()
    fail_apps = fail_apps or set()

    async def _list(app_name, *args, **kwargs):
        if app_name in fail_apps:
            raise RuntimeError("AppD timeout")
        return snap_map.get(app_name, [])

    client.list_snapshots.side_effect = _list
    return client


# ---------------------------------------------------------------------------
# Basic structure
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestTraceStitcherRun:

    async def test_returns_required_keys(self):
        client = _make_client({})
        result = await trace_stitcher.run(client=client, correlation_id=CID, app_names=[])
        for key in ("correlation_id", "ordered_trace", "coverage_percent"):
            assert key in result

    async def test_correlation_id_preserved(self):
        client = _make_client({})
        result = await trace_stitcher.run(client=client, correlation_id=CID, app_names=[])
        assert result["correlation_id"] == CID


# ---------------------------------------------------------------------------
# Correlation ID matching
# ---------------------------------------------------------------------------

    async def test_match_in_request_headers(self):
        snap = _snap("svc-a", BASE_MS, requestHeaders=f"X-Correlation-ID: {CID}")
        client = _make_client({"svc-a": [snap]})
        result = await trace_stitcher.run(client=client, correlation_id=CID, app_names=["svc-a"])
        assert len(result["ordered_trace"]) == 1

    async def test_match_in_user_data(self):
        snap = _snap("svc-a", BASE_MS, userData=f"cid={CID}")
        client = _make_client({"svc-a": [snap]})
        result = await trace_stitcher.run(client=client, correlation_id=CID, app_names=["svc-a"])
        assert len(result["ordered_trace"]) == 1

    async def test_match_in_correlation_info(self):
        snap = _snap("svc-a", BASE_MS, correlationInfo=CID)
        client = _make_client({"svc-a": [snap]})
        result = await trace_stitcher.run(client=client, correlation_id=CID, app_names=["svc-a"])
        assert len(result["ordered_trace"]) == 1

    async def test_match_in_exit_call_continuation_id(self):
        snap = _snap("svc-a", BASE_MS, exitCalls=[{"continuationID": CID}])
        client = _make_client({"svc-a": [snap]})
        result = await trace_stitcher.run(client=client, correlation_id=CID, app_names=["svc-a"])
        assert len(result["ordered_trace"]) == 1

    async def test_no_match_returns_missing_warning(self):
        snap = _snap("svc-a", BASE_MS)  # no correlation ID in any field
        client = _make_client({"svc-a": [snap]})
        result = await trace_stitcher.run(client=client, correlation_id=CID, app_names=["svc-a"])
        assert len(result["ordered_trace"]) == 0
        assert "warning" in result
        assert "svc-a" in result["warning"]


# ---------------------------------------------------------------------------
# Ordering and coverage
# ---------------------------------------------------------------------------

    async def test_trace_sorted_by_start_time(self):
        snaps = {
            "svc-b": [_snap("svc-b", BASE_MS + 200, requestHeaders=CID)],
            "svc-a": [_snap("svc-a", BASE_MS + 100, requestHeaders=CID)],
            "svc-c": [_snap("svc-c", BASE_MS + 300, requestHeaders=CID)],
        }
        client = _make_client(snaps)
        result = await trace_stitcher.run(
            client=client, correlation_id=CID, app_names=["svc-b", "svc-a", "svc-c"]
        )
        apps = [e["app"] for e in result["ordered_trace"]]
        assert apps == ["svc-a", "svc-b", "svc-c"]

    async def test_full_coverage_when_all_found(self):
        snaps = {
            "svc-a": [_snap("svc-a", BASE_MS, requestHeaders=CID)],
            "svc-b": [_snap("svc-b", BASE_MS + 100, requestHeaders=CID)],
        }
        client = _make_client(snaps)
        result = await trace_stitcher.run(
            client=client, correlation_id=CID, app_names=["svc-a", "svc-b"]
        )
        assert result["coverage_percent"] == 100.0

    async def test_partial_coverage_when_some_missing(self):
        snaps = {"svc-a": [_snap("svc-a", BASE_MS, requestHeaders=CID)]}
        client = _make_client(snaps)
        result = await trace_stitcher.run(
            client=client, correlation_id=CID, app_names=["svc-a", "svc-b"]
        )
        assert result["coverage_percent"] == 50.0


# ---------------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------------

    async def test_no_gap_on_first_segment(self):
        snaps = {"svc-a": [_snap("svc-a", BASE_MS, requestHeaders=CID)]}
        client = _make_client(snaps)
        result = await trace_stitcher.run(
            client=client, correlation_id=CID, app_names=["svc-a"]
        )
        assert "gap_from_previous_ms" not in result["ordered_trace"][0]

    async def test_gap_calculated_between_segments(self):
        # svc-a: starts BASE_MS, takes 50ms → ends at BASE_MS+50
        # svc-b: starts BASE_MS+200 → gap = 200-50 = 150ms
        snaps = {
            "svc-a": [_snap("svc-a", BASE_MS, duration_ms=50, requestHeaders=CID)],
            "svc-b": [_snap("svc-b", BASE_MS + 200, requestHeaders=CID)],
        }
        client = _make_client(snaps)
        result = await trace_stitcher.run(
            client=client, correlation_id=CID, app_names=["svc-a", "svc-b"]
        )
        second = result["ordered_trace"][1]
        assert second["gap_from_previous_ms"] == 150.0

    async def test_gap_warning_when_over_100ms(self):
        snaps = {
            "svc-a": [_snap("svc-a", BASE_MS, duration_ms=50, requestHeaders=CID)],
            "svc-b": [_snap("svc-b", BASE_MS + 200, requestHeaders=CID)],
        }
        client = _make_client(snaps)
        result = await trace_stitcher.run(
            client=client, correlation_id=CID, app_names=["svc-a", "svc-b"]
        )
        assert "gap_warning" in result["ordered_trace"][1]

    async def test_no_gap_warning_when_under_100ms(self):
        snaps = {
            "svc-a": [_snap("svc-a", BASE_MS, duration_ms=50, requestHeaders=CID)],
            "svc-b": [_snap("svc-b", BASE_MS + 100, requestHeaders=CID)],
        }
        client = _make_client(snaps)
        result = await trace_stitcher.run(
            client=client, correlation_id=CID, app_names=["svc-a", "svc-b"]
        )
        assert "gap_warning" not in result["ordered_trace"][1]


# ---------------------------------------------------------------------------
# Fault tolerance
# ---------------------------------------------------------------------------

    async def test_fetch_failure_treated_as_missing(self):
        client = _make_client({}, fail_apps={"svc-a"})
        result = await trace_stitcher.run(
            client=client, correlation_id=CID, app_names=["svc-a"]
        )
        assert len(result["ordered_trace"]) == 0
        assert "warning" in result

    async def test_all_fail_returns_zero_coverage(self):
        client = _make_client({}, fail_apps={"svc-a", "svc-b"})
        result = await trace_stitcher.run(
            client=client, correlation_id=CID, app_names=["svc-a", "svc-b"]
        )
        assert result["coverage_percent"] == 0.0
        assert result["ordered_trace"] == []
