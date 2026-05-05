"""
tests/unit/test_snapshot_analyzer.py

Tests for services/snapshot_analyzer.py — service layer only.

parse_snapshot_errors and find_hot_path are patched so tests focus on the
orchestration logic (field extraction, normalisation, strategy matching),
not parser algorithm correctness (that is covered by test_snapshot_parser.py).
"""

from __future__ import annotations

import dataclasses
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.types import StackFrame, StackLanguage
from services import snapshot_analyzer


def _make_parsed(language=StackLanguage.JAVA, culprit=None, caused_by=None, top_frames=None):
    mock = MagicMock()
    mock.language = language
    mock.culprit_frame = culprit
    mock.caused_by_chain = caused_by or []
    mock.top_app_frames = top_frames or []
    return mock


def _make_culprit(cls="com.example.Foo", method="process", file="Foo.java", line=42):
    return StackFrame(
        class_name=cls,
        method_name=method,
        file_name=file,
        line_number=line,
        is_app_frame=True,
    )


def _make_client(snap: dict[str, Any]):
    client = AsyncMock()
    client.get_snapshot_detail.return_value = snap
    return client


def _snap(**kwargs) -> dict[str, Any]:
    defaults = {
        "requestGUID": "test-guid",
        "businessTransactionName": "/api/checkout",
        "timeTakenInMilliSecs": 250,
        "errorOccurred": True,
        "errorDetails": "NullPointerException at line 42",
        "errorStackTrace": "Exception in thread main java.lang.NullPointerException\n\tat com.example.Foo.process(Foo.java:42)",
        "callChain": [{"className": "Foo", "methodName": "process", "timeTakenInMilliSecs": 200}],
    }
    defaults.update(kwargs)
    return defaults


# ---------------------------------------------------------------------------
# Required output keys
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSnapshotAnalyzerRun:

    async def test_returns_required_keys(self):
        client = _make_client(_snap())
        parsed = _make_parsed()

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return parsed
                return None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        for key in ("snapshot_guid", "bt_name", "response_time_ms", "error_occurred",
                    "error_details", "hot_path", "top_call_segments", "language",
                    "culprit_frame", "caused_by_chain", "top_app_frames", "diagnostic_hint"):
            assert key in result, f"missing key: {key}"

    async def test_snapshot_guid_preserved(self):
        client = _make_client(_snap())
        parsed = _make_parsed()

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                return parsed if call_count == 1 else None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert result["snapshot_guid"] == "test-guid"


# ---------------------------------------------------------------------------
# callChain normalisation
# ---------------------------------------------------------------------------

    async def test_callchain_string_normalised_to_empty_list(self):
        snap = _snap(callChain="tier1|tier2|tier3")
        client = _make_client(snap)
        parsed = _make_parsed()

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                return parsed if call_count == 1 else None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert result["top_call_segments"] == []

    async def test_callchain_list_preserved(self):
        segments = [{"className": "Foo", "methodName": "bar"}]
        snap = _snap(callChain=segments)
        client = _make_client(snap)
        parsed = _make_parsed()

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                return parsed if call_count == 1 else None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert result["top_call_segments"] == segments


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------

    async def test_language_from_parsed_stack(self):
        client = _make_client(_snap())
        parsed = _make_parsed(language=StackLanguage.PYTHON)

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                return parsed if call_count == 1 else None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert result["language"] == "python"

    async def test_language_unknown_when_no_stack_trace(self):
        snap = _snap(errorStackTrace="", errorDetails="")
        client = _make_client(snap)

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            async def _thread_side(fn, *args):
                return None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert result["language"] == "unknown"


# ---------------------------------------------------------------------------
# Culprit frame
# ---------------------------------------------------------------------------

    async def test_culprit_frame_included_when_present(self):
        client = _make_client(_snap())
        culprit = _make_culprit()
        parsed = _make_parsed(culprit=culprit)

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                return parsed if call_count == 1 else None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert result["culprit_frame"] is not None
        assert result["culprit_frame"]["class_name"] == "com.example.Foo"

    async def test_culprit_frame_none_when_absent(self):
        client = _make_client(_snap())
        parsed = _make_parsed(culprit=None)

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                return parsed if call_count == 1 else None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert result["culprit_frame"] is None


# ---------------------------------------------------------------------------
# Exception strategy hints
# ---------------------------------------------------------------------------

    async def test_strategy_hint_for_known_exception(self):
        snap = _snap(errorDetails="NullPointerException at line 42")
        client = _make_client(snap)
        parsed = _make_parsed()

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                return parsed if call_count == 1 else None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert "NullPointerException" in result["diagnostic_hint"]

    async def test_no_strategy_hint_when_no_error(self):
        snap = _snap(errorDetails="", errorStackTrace="")
        client = _make_client(snap)

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            async def _thread_side(fn, *args):
                return None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert result["diagnostic_hint"] == ""

    async def test_custom_exception_strategies_used(self):
        snap = _snap(errorDetails="CustomException: something failed")
        client = _make_client(snap)
        parsed = _make_parsed()
        custom = {"CustomException": "Check the custom handler at line X."}

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                return parsed if call_count == 1 else None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client,
                app_name="ecommerce",
                snapshot_guid="test-guid",
                exception_strategies=custom,
            )

        assert "Check the custom handler" in result["diagnostic_hint"]


# ---------------------------------------------------------------------------
# Hot path
# ---------------------------------------------------------------------------

    async def test_hot_path_included_when_present(self):
        snap = _snap(callChain=[
            {"className": "Checkout", "methodName": "process", "timeTakenInMilliSecs": 200}
        ])
        client = _make_client(snap)
        parsed = _make_parsed()
        hot = {"className": "Checkout", "methodName": "process", "timeTakenInMilliSecs": 200}

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return parsed
                return hot

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert result["hot_path"] is not None
        assert "Checkout.process" in result["hot_path"]["method"]

    async def test_hot_path_none_when_not_found(self):
        snap = _snap(callChain=[])
        client = _make_client(snap)
        parsed = _make_parsed()

        with patch("services.snapshot_analyzer.asyncio.to_thread") as mock_thread:
            call_count = 0

            async def _thread_side(fn, *args):
                nonlocal call_count
                call_count += 1
                return parsed if call_count == 1 else None

            mock_thread.side_effect = _thread_side

            result = await snapshot_analyzer.run(
                client=client, app_name="ecommerce", snapshot_guid="test-guid"
            )

        assert result["hot_path"] is None
