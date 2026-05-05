"""
tests/unit/test_snapshot_comparator.py

Tests for services/snapshot_comparator.py — service layer only.

Uses a stub golden registry and AsyncMock client to avoid touching parsers
or AppD in unit tests. The _compare and score_golden_candidate functions are
patched so tests focus on selection logic, not diff algorithm correctness
(that is covered by tests/unit/test_snapshot_parser.py).
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.types import ConfidenceScore, SmokingGunReport
from services import snapshot_comparator


def _make_report(**overrides) -> SmokingGunReport:
    defaults = dict(
        culprit_class="com.example.Foo",
        culprit_method="process",
        culprit_line=42,
        culprit_file="Foo.java",
        deviation="Method took 3x longer",
        exception="NullPointerException",
        suggested_fix="Add null guard",
        confidence_score=ConfidenceScore.HIGH,
        confidence_reasoning="High timing deviation",
        exclusive_methods=[],
        latency_deviations=[],
        golden_snapshot_guid="healthy-guid",
        golden_selection_reason="",
    )
    defaults.update(overrides)
    return SmokingGunReport(**defaults)


def _make_snap(guid: str, bt: str = "checkout", error: bool = False) -> dict[str, Any]:
    return {
        "requestGUID": guid,
        "businessTransactionName": bt,
        "errorOccurred": error,
        "timeTakenInMilliSecs": 150,
        "callChain": [{"method": "process", "timeTakenInMilliSecs": 120}],
    }


def _make_registry(pinned=None):
    registry = MagicMock()
    registry.get.return_value = pinned
    return registry


def _make_client(snaps: dict[str, dict], candidates: list[dict] | None = None):
    client = AsyncMock()

    async def _get_detail(app_name, guid):
        return snaps.get(guid, {})

    client.get_snapshot_detail.side_effect = _get_detail
    client.list_snapshots.return_value = candidates or []
    return client


# ---------------------------------------------------------------------------
# Explicit healthy_snapshot_guid provided
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSnapshotComparatorRun:

    async def test_explicit_guid_uses_provided_healthy(self):
        failed = _make_snap("failed-guid")
        healthy = _make_snap("healthy-guid")
        client = _make_client({"failed-guid": failed, "healthy-guid": healthy})
        report = _make_report()

        with patch("services.snapshot_comparator._compare", return_value=report), \
             patch("services.snapshot_comparator.asyncio.to_thread", side_effect=lambda f, *a: f(*a) if callable(f) else f):
            result = await snapshot_comparator.run(
                client=client,
                app_name="ecommerce",
                failed_snapshot_guid="failed-guid",
                golden_registry=_make_registry(),
                healthy_snapshot_guid="healthy-guid",
            )

        assert result["golden_selection_reason"] == "Provided explicitly by caller."
        calls = [c.args[1] for c in client.get_snapshot_detail.call_args_list]
        assert "healthy-guid" in calls

    async def test_explicit_guid_skips_registry(self):
        failed = _make_snap("failed-guid")
        healthy = _make_snap("healthy-guid")
        client = _make_client({"failed-guid": failed, "healthy-guid": healthy})
        registry = _make_registry()
        report = _make_report()

        with patch("services.snapshot_comparator._compare", return_value=report), \
             patch("services.snapshot_comparator.asyncio.to_thread", side_effect=lambda f, *a: f(*a) if callable(f) else f):
            await snapshot_comparator.run(
                client=client,
                app_name="ecommerce",
                failed_snapshot_guid="failed-guid",
                golden_registry=registry,
                healthy_snapshot_guid="healthy-guid",
            )

        registry.get.assert_not_called()


# ---------------------------------------------------------------------------
# Pinned golden from registry
# ---------------------------------------------------------------------------

    async def test_pinned_golden_used_when_available(self):
        failed = _make_snap("failed-guid")
        healthy = _make_snap("pinned-guid")
        client = _make_client({"failed-guid": failed, "pinned-guid": healthy})

        pinned = MagicMock()
        pinned.snapshot_guid = "pinned-guid"
        pinned.promoted_by = "alice"
        pinned.confidence = "HIGH"
        pinned.selection_score = 95.0
        registry = _make_registry(pinned=pinned)
        report = _make_report()

        with patch("services.snapshot_comparator._compare", return_value=report), \
             patch("services.snapshot_comparator.asyncio.to_thread", side_effect=lambda f, *a: f(*a) if callable(f) else f):
            result = await snapshot_comparator.run(
                client=client,
                app_name="ecommerce",
                failed_snapshot_guid="failed-guid",
                golden_registry=registry,
            )

        assert "Pinned golden baseline" in result["golden_selection_reason"]
        assert "alice" in result["golden_selection_reason"]

    async def test_pinned_golden_skipped_when_same_as_failed(self):
        # If pinned guid == failed guid, fall through to auto-select
        failed = _make_snap("same-guid")
        healthy = _make_snap("auto-guid")
        client = _make_client(
            {"same-guid": failed, "auto-guid": healthy},
            candidates=[healthy],
        )

        pinned = MagicMock()
        pinned.snapshot_guid = "same-guid"  # same as failed
        registry = _make_registry(pinned=pinned)
        report = _make_report()

        with patch("services.snapshot_comparator._compare", return_value=report), \
             patch("services.snapshot_comparator.score_golden_candidate", return_value=90.0), \
             patch("services.snapshot_comparator.asyncio.to_thread") as mock_thread:

            async def _run_in_thread(fn, *args):
                return fn(*args)
            mock_thread.side_effect = _run_in_thread

            result = await snapshot_comparator.run(
                client=client,
                app_name="ecommerce",
                failed_snapshot_guid="same-guid",
                golden_registry=registry,
            )

        assert "Auto-selected" in result["golden_selection_reason"]


# ---------------------------------------------------------------------------
# Auto-selection via scoring
# ---------------------------------------------------------------------------

    async def test_no_candidates_returns_message_dict(self):
        failed = _make_snap("failed-guid")
        client = _make_client({"failed-guid": failed}, candidates=[])
        registry = _make_registry(pinned=None)

        with patch("services.snapshot_comparator.asyncio.to_thread") as mock_thread:
            async def _run_in_thread(fn, *args):
                return fn(*args)
            mock_thread.side_effect = _run_in_thread

            result = await snapshot_comparator.run(
                client=client,
                app_name="ecommerce",
                failed_snapshot_guid="failed-guid",
                golden_registry=registry,
            )

        assert "message" in result
        assert "No suitable golden baseline" in result["message"]

    async def test_all_zero_scores_returns_message_dict(self):
        failed = _make_snap("failed-guid")
        candidate = _make_snap("cand-guid")
        client = _make_client(
            {"failed-guid": failed, "cand-guid": candidate},
            candidates=[candidate],
        )
        registry = _make_registry(pinned=None)

        with patch("services.snapshot_comparator.score_golden_candidate", return_value=0), \
             patch("services.snapshot_comparator.asyncio.to_thread") as mock_thread:
            async def _run_in_thread(fn, *args):
                return fn(*args)
            mock_thread.side_effect = _run_in_thread

            result = await snapshot_comparator.run(
                client=client,
                app_name="ecommerce",
                failed_snapshot_guid="failed-guid",
                golden_registry=registry,
            )

        assert "message" in result

    async def test_auto_select_confidence_high_above_80(self):
        failed = _make_snap("failed-guid")
        candidate = _make_snap("cand-guid")
        client = _make_client(
            {"failed-guid": failed, "cand-guid": candidate},
            candidates=[candidate],
        )
        registry = _make_registry(pinned=None)
        report = _make_report()

        with patch("services.snapshot_comparator.score_golden_candidate", return_value=85.0), \
             patch("services.snapshot_comparator._compare", return_value=report), \
             patch("services.snapshot_comparator.asyncio.to_thread") as mock_thread:
            async def _run_in_thread(fn, *args):
                return fn(*args)
            mock_thread.side_effect = _run_in_thread

            result = await snapshot_comparator.run(
                client=client,
                app_name="ecommerce",
                failed_snapshot_guid="failed-guid",
                golden_registry=registry,
            )

        assert "HIGH" in result["golden_selection_reason"]

    async def test_auto_select_confidence_low_at_or_below_50(self):
        failed = _make_snap("failed-guid")
        candidate = _make_snap("cand-guid")
        client = _make_client(
            {"failed-guid": failed, "cand-guid": candidate},
            candidates=[candidate],
        )
        registry = _make_registry(pinned=None)
        report = _make_report()

        with patch("services.snapshot_comparator.score_golden_candidate", return_value=30.0), \
             patch("services.snapshot_comparator._compare", return_value=report), \
             patch("services.snapshot_comparator.asyncio.to_thread") as mock_thread:
            async def _run_in_thread(fn, *args):
                return fn(*args)
            mock_thread.side_effect = _run_in_thread

            result = await snapshot_comparator.run(
                client=client,
                app_name="ecommerce",
                failed_snapshot_guid="failed-guid",
                golden_registry=registry,
            )

        assert "LOW" in result["golden_selection_reason"]


# ---------------------------------------------------------------------------
# callChain normalisation
# ---------------------------------------------------------------------------

    async def test_callchain_string_normalised_to_list(self):
        failed = _make_snap("failed-guid")
        failed["callChain"] = "tier1|tier2|tier3"  # string form
        healthy = _make_snap("healthy-guid")

        client = _make_client({"failed-guid": failed, "healthy-guid": healthy})
        report = _make_report()
        captured: list[dict] = []

        def _fake_compare(h, f):
            captured.append(f)
            return report

        with patch("services.snapshot_comparator._compare", side_effect=_fake_compare), \
             patch("services.snapshot_comparator.asyncio.to_thread") as mock_thread:
            async def _run_in_thread(fn, *args):
                return fn(*args)
            mock_thread.side_effect = _run_in_thread

            await snapshot_comparator.run(
                client=client,
                app_name="ecommerce",
                failed_snapshot_guid="failed-guid",
                golden_registry=_make_registry(),
                healthy_snapshot_guid="healthy-guid",
            )

        assert captured[0]["callChain"] == []


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------

    async def test_result_is_dict_not_dataclass(self):
        failed = _make_snap("failed-guid")
        healthy = _make_snap("healthy-guid")
        client = _make_client({"failed-guid": failed, "healthy-guid": healthy})
        report = _make_report()

        with patch("services.snapshot_comparator._compare", return_value=report), \
             patch("services.snapshot_comparator.asyncio.to_thread") as mock_thread:
            async def _run_in_thread(fn, *args):
                return fn(*args)
            mock_thread.side_effect = _run_in_thread

            result = await snapshot_comparator.run(
                client=client,
                app_name="ecommerce",
                failed_snapshot_guid="failed-guid",
                golden_registry=_make_registry(),
                healthy_snapshot_guid="healthy-guid",
            )

        assert isinstance(result, dict)
        assert "culprit_class" in result
