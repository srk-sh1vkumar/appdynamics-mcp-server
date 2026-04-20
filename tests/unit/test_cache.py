"""
tests/unit/test_cache.py

Unit tests for the caching layer:
  - TwoLayerCache (get_or_fetch, invalidate, invalidate_prefix, validation)
  - CacheKeys (all key functions, UPN enforcement)
  - GoldenRegistry (get/set/invalidate/TTL)
  - CacheInvalidator (all four event handlers)
  - ParsedSnapshotCache behaviour (in-memory only, no raw JSON)
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import BaseModel

from registries.apps_registry import AppEntry, AppsRegistry
from registries.bt_registry import BTEntry, BTRegistry
from registries.golden_registry import GoldenRegistry, GoldenSnapshot
from services.cache_invalidator import CacheInvalidator
from utils import cache_keys
from utils.cache import (
    CachedSnapshotAnalysis,
    TwoLayerCache,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _Item(BaseModel):
    id: int
    value: str


def _make_golden(
    controller: str = "prod",
    app: str = "myapp",
    bt: str = "/checkout",
    guid: str = "abc-123",
    promoted_by: str = "auto",
    hours_ago: float = 0.0,
) -> GoldenSnapshot:
    ts = datetime.now(tz=UTC) - timedelta(hours=hours_ago)
    return GoldenSnapshot(
        snapshot_guid=guid,
        bt_name=bt,
        app_name=app,
        controller_name=controller,
        response_time_ms=350.0,
        captured_at=ts,
        selected_at=ts,
        selection_score=85,
        confidence="HIGH",
        promoted_by=promoted_by,
    )


@pytest.fixture
def tmp_cache(tmp_path: Any) -> TwoLayerCache:
    return TwoLayerCache(cache_dir=str(tmp_path / "two_layer"))


@pytest.fixture
def golden_reg(tmp_path: Any) -> GoldenRegistry:
    return GoldenRegistry(disk_dir=str(tmp_path / "golden"))


@pytest.fixture
def bt_reg(tmp_path: Any) -> BTRegistry:
    return BTRegistry(disk_dir=str(tmp_path / "bts"))


@pytest.fixture
def apps_reg(tmp_path: Any) -> AppsRegistry:
    return AppsRegistry(disk_dir=str(tmp_path / "apps"))


@pytest.fixture
def invalidator(golden_reg: GoldenRegistry, bt_reg: BTRegistry) -> CacheInvalidator:
    return CacheInvalidator(golden_registry=golden_reg, bt_registry=bt_reg)


# ===========================================================================
# CacheKeys
# ===========================================================================


class TestCacheKeys:
    def test_make_key_includes_upn_first(self) -> None:
        key = cache_keys.make_key("alice@co.com", "prod", "applications")
        assert key.startswith("alice@co.com:")

    def test_make_key_is_deterministic(self) -> None:
        k1 = cache_keys.make_key("alice@co.com", "prod", "bt_list", "myapp")
        k2 = cache_keys.make_key("alice@co.com", "prod", "bt_list", "myapp")
        assert k1 == k2

    def test_different_upns_different_keys(self) -> None:
        ka = cache_keys.make_key("alice@co.com", "prod", "applications")
        kb = cache_keys.make_key("bob@co.com", "prod", "applications")
        assert ka != kb

    def test_snapshot_list_key_includes_upn(self) -> None:
        key = cache_keys.snapshot_list_key("u@x.com", "prod", "app")
        assert key.startswith("u@x.com:")

    def test_snapshot_list_key_encodes_error_only(self) -> None:
        k_all = cache_keys.snapshot_list_key("u@x.com", "prod", "app", error_only=False)
        k_err = cache_keys.snapshot_list_key("u@x.com", "prod", "app", error_only=True)
        assert k_all != k_err
        assert "errors_only" in k_err

    def test_snapshot_list_key_with_bt(self) -> None:
        k_no_bt = cache_keys.snapshot_list_key("u@x.com", "prod", "app")
        k_bt = cache_keys.snapshot_list_key("u@x.com", "prod", "app", bt="checkout")
        assert k_no_bt != k_bt

    def test_parsed_snapshot_key_includes_upn(self) -> None:
        key = cache_keys.parsed_snapshot_key("u@x.com", "prod", "guid-abc")
        assert key.startswith("u@x.com:")
        assert "guid-abc" in key

    def test_golden_key_has_no_upn(self) -> None:
        key = cache_keys.golden_key("prod", "app", "bt")
        assert "alice" not in key
        assert key.startswith("__golden__:")

    def test_bt_list_key_includes_upn(self) -> None:
        key = cache_keys.bt_list_key("u@x.com", "prod", "myapp")
        assert key.startswith("u@x.com:")
        assert ":business_transactions:" in key

    def test_app_list_key_includes_upn(self) -> None:
        key = cache_keys.app_list_key("u@x.com", "prod")
        assert key.startswith("u@x.com:")
        assert ":applications" in key

    def test_user_roles_key_includes_upn(self) -> None:
        key = cache_keys.user_roles_key("u@x.com", "prod")
        assert key.startswith("u@x.com:")

    def test_metric_values_key_includes_upn(self) -> None:
        key = cache_keys.metric_values_key("u@x.com", "prod", "app", "calls/min")
        assert key.startswith("u@x.com:")

    def test_keys_normalised_lowercase(self) -> None:
        key = cache_keys.make_key("Alice@Corp.COM", "Production", "BT_List")
        assert key == key.lower()


# ===========================================================================
# TwoLayerCache
# ===========================================================================


@pytest.mark.asyncio
class TestTwoLayerCache:
    async def test_l1_hit_does_not_call_fetch(self, tmp_cache: TwoLayerCache) -> None:
        item = _Item(id=1, value="hello")

        call_count = 0

        async def fetch() -> _Item:
            nonlocal call_count
            call_count += 1
            return item

        # First call — cold, hits fetch
        await tmp_cache.get_or_fetch("key1", _Item, fetch, "applications")
        assert call_count == 1

        # Second call — L1 hit, no fetch
        result = await tmp_cache.get_or_fetch("key1", _Item, fetch, "applications")
        assert call_count == 1
        assert isinstance(result, _Item)
        assert result.id == 1

    async def test_l2_hit_populates_l1(self, tmp_path: Any) -> None:
        cache = TwoLayerCache(cache_dir=str(tmp_path / "c"))
        item = _Item(id=2, value="from_disk")

        # Seed L2 directly
        key = "key_disk"
        cache._disk.set(key, {"id": 2, "value": "from_disk"})

        call_count = 0

        async def fetch() -> _Item:
            nonlocal call_count
            call_count += 1
            return item

        result = await cache.get_or_fetch(
            key, _Item, fetch, "applications", persist_to_disk=True
        )
        # Should be a L2 hit — fetch NOT called
        assert call_count == 0
        assert result.id == 2

        # Now L1 should be populated — fetch still not called on third call
        result2 = await cache.get_or_fetch(
            key, _Item, fetch, "applications", persist_to_disk=True
        )
        assert call_count == 0
        assert result2.id == 2

    async def test_both_miss_calls_fetch_and_populates(  # noqa: E501
        self, tmp_cache: TwoLayerCache
    ) -> None:
        item = _Item(id=3, value="fresh")
        calls: list[int] = []

        async def fetch() -> _Item:
            calls.append(1)
            return item

        result = await tmp_cache.get_or_fetch(
            "k3", _Item, fetch, "applications", persist_to_disk=True
        )
        assert len(calls) == 1
        assert result.id == 3

        # Check L1 populated — second call should NOT invoke fetch
        result2 = await tmp_cache.get_or_fetch(
            "k3", _Item, fetch, "applications", persist_to_disk=True
        )
        assert len(calls) == 1
        assert result2.id == 3

    async def test_pydantic_validation_failure_evicts_and_refetches(
        self, tmp_cache: TwoLayerCache
    ) -> None:
        # Seed L1 with a corrupt dict (missing required 'value' field)
        l1 = tmp_cache._l1.get("applications", tmp_cache._l1_default)
        l1["bad_key"] = {"id": 99}  # Missing 'value' — will fail _Item validation

        item = _Item(id=99, value="recovered")
        calls: list[int] = []

        async def fetch() -> _Item:
            calls.append(1)
            return item

        result = await tmp_cache.get_or_fetch("bad_key", _Item, fetch, "applications")
        # Corrupt entry should be evicted; fetch_fn called once
        assert len(calls) == 1
        assert result.id == 99

    async def test_invalidate_removes_from_both_layers(self, tmp_path: Any) -> None:
        cache = TwoLayerCache(cache_dir=str(tmp_path / "inv"))
        item = _Item(id=5, value="to_remove")
        calls: list[int] = []

        async def fetch() -> _Item:
            calls.append(1)
            return item

        # Populate both layers
        await cache.get_or_fetch(
            "rem_key", _Item, fetch, "applications", persist_to_disk=True
        )
        assert len(calls) == 1

        # Invalidate
        cache.invalidate("rem_key")

        # Next call must re-fetch
        await cache.get_or_fetch(
            "rem_key", _Item, fetch, "applications", persist_to_disk=True
        )
        assert len(calls) == 2

    async def test_invalidate_prefix_removes_matching_keys(
        self, tmp_cache: TwoLayerCache
    ) -> None:
        item_a = _Item(id=10, value="a")
        item_b = _Item(id=11, value="b")
        item_c = _Item(id=12, value="c")

        async def fa() -> _Item:
            return item_a

        async def fb() -> _Item:
            return item_b

        async def fc() -> _Item:
            return item_c

        await tmp_cache.get_or_fetch(
            "alice:prod:applications", _Item, fa, "applications"
        )
        await tmp_cache.get_or_fetch(
            "alice:prod:bt_list:app1", _Item, fb, "business_transactions"
        )
        await tmp_cache.get_or_fetch("bob:prod:applications", _Item, fc, "applications")

        # Invalidate all of alice's entries
        tmp_cache.invalidate_prefix("alice:")

        calls: list[int] = []

        async def fetch_new() -> _Item:
            calls.append(1)
            return _Item(id=99, value="new")

        await tmp_cache.get_or_fetch(
            "alice:prod:applications", _Item, fetch_new, "applications"
        )
        await tmp_cache.get_or_fetch(
            "alice:prod:bt_list:app1", _Item, fetch_new,
            "business_transactions"
        )
        # Both alice keys should re-fetch
        assert len(calls) == 2

        # Bob's key should still be in L1 — no re-fetch
        bob_calls: list[int] = []

        async def fetch_bob() -> _Item:
            bob_calls.append(1)
            return _Item(id=12, value="c")

        await tmp_cache.get_or_fetch(
            "bob:prod:applications", _Item, fetch_bob, "applications"
        )
        assert len(bob_calls) == 0

    async def test_list_values_passed_through_without_validation_failure(
        self, tmp_cache: TwoLayerCache
    ) -> None:
        """Lists stored in cache are returned as-is (no schema eviction)."""
        data = [{"id": 1}, {"id": 2}]

        async def fetch() -> Any:
            return data

        # First call — cache miss, fetch called
        result = await tmp_cache.get_or_fetch("list_key", _Item, fetch, "applications")
        assert result == data

        calls: list[int] = []

        async def fetch2() -> Any:
            calls.append(1)
            return data

        # Second call — list should be in L1, NOT evicted
        result2 = await tmp_cache.get_or_fetch(
            "list_key", _Item, fetch2, "applications"
        )
        assert len(calls) == 0  # No re-fetch
        assert result2 == data

    async def test_get_stats_reports_hit_miss_counts(
        self, tmp_cache: TwoLayerCache
    ) -> None:
        item = _Item(id=1, value="x")

        async def fetch() -> _Item:
            return item

        await tmp_cache.get_or_fetch("sk1", _Item, fetch, "applications")  # miss
        await tmp_cache.get_or_fetch("sk1", _Item, fetch, "applications")  # hit

        stats = tmp_cache.get_stats()
        assert "applications" in stats
        assert stats["applications"]["hits"] >= 1
        assert stats["applications"]["misses"] >= 1


# ===========================================================================
# GoldenRegistry
# ===========================================================================


class TestGoldenRegistry:
    def test_returns_none_for_missing_entry(self, golden_reg: GoldenRegistry) -> None:
        assert golden_reg.get("prod", "app", "/checkout") is None

    def test_set_and_get_round_trip(self, golden_reg: GoldenRegistry) -> None:
        g = _make_golden()
        golden_reg.set(g)
        result = golden_reg.get("prod", "myapp", "/checkout")
        assert result is not None
        assert result.snapshot_guid == "abc-123"

    def test_returns_none_when_older_than_24h(self, tmp_path: Any) -> None:
        reg = GoldenRegistry(disk_dir=str(tmp_path / "old_golden"))
        g = _make_golden()
        reg.set(g)

        # Fake the stored_at timestamp to be 25 hours ago
        key = list(reg._registry.keys())[0]
        golden, _ = reg._registry[key]
        reg._registry[key] = (golden, time.time() - 90_001)

        result = reg.get("prod", "myapp", "/checkout")
        assert result is None

    def test_set_persists_to_disk(self, tmp_path: Any) -> None:
        reg = GoldenRegistry(disk_dir=str(tmp_path / "persist"))
        g = _make_golden(guid="persist-guid")
        reg.set(g)

        # Create a fresh registry instance pointing to same disk
        reg2 = GoldenRegistry(disk_dir=str(tmp_path / "persist"))
        result = reg2.get("prod", "myapp", "/checkout")
        assert result is not None
        assert result.snapshot_guid == "persist-guid"

    def test_invalidate_removes_from_memory_and_disk(
        self, golden_reg: GoldenRegistry
    ) -> None:
        g = _make_golden()
        golden_reg.set(g)
        assert golden_reg.get("prod", "myapp", "/checkout") is not None

        golden_reg.invalidate("prod", "myapp", "/checkout", reason="test")
        assert golden_reg.get("prod", "myapp", "/checkout") is None

    def test_invalidate_app_removes_all_bts(self, golden_reg: GoldenRegistry) -> None:
        golden_reg.set(_make_golden(bt="/checkout", guid="g1"))
        golden_reg.set(_make_golden(bt="/payment", guid="g2"))
        golden_reg.set(_make_golden(app="other_app", bt="/checkout", guid="g3"))

        golden_reg.invalidate_app("prod", "myapp", reason="deployment")

        assert golden_reg.get("prod", "myapp", "/checkout") is None
        assert golden_reg.get("prod", "myapp", "/payment") is None
        # Other app should be untouched
        assert golden_reg.get("prod", "other_app", "/checkout") is not None

    def test_get_stats_counts_entries(self, golden_reg: GoldenRegistry) -> None:
        golden_reg.set(_make_golden(bt="/a", guid="g1", promoted_by="auto"))
        golden_reg.set(_make_golden(bt="/b", guid="g2", promoted_by="sre@corp.com"))

        stats = golden_reg.get_stats()
        assert stats["total_entries"] == 2
        assert stats["manually_promoted"] == 1

    def test_get_stats_entries_expiring_soon(self, golden_reg: GoldenRegistry) -> None:
        g = _make_golden()
        golden_reg.set(g)
        # Set stored_at to 23h ago (< 2h before 24h expiry)
        key = list(golden_reg._registry.keys())[0]
        golden, _ = golden_reg._registry[key]
        golden_reg._registry[key] = (golden, time.time() - 82_800)  # 23 hours ago

        stats = golden_reg.get_stats()
        assert stats["entries_expiring_soon"] == 1


# ===========================================================================
# CacheInvalidator
# ===========================================================================


class TestCacheInvalidator:
    def test_on_deployment_clears_bt_and_golden(
        self,
        invalidator: CacheInvalidator,
        golden_reg: GoldenRegistry,
        bt_reg: BTRegistry,
    ) -> None:
        # Pre-populate golden for two BTs
        golden_reg.set(_make_golden(bt="/checkout"))
        golden_reg.set(_make_golden(bt="/payment"))
        # Pre-populate bt_registry
        bt_reg.update("prod", "myapp", [BTEntry(name="/checkout")])

        invalidator.on_deployment_detected("prod", "myapp")

        assert golden_reg.get("prod", "myapp", "/checkout") is None
        assert golden_reg.get("prod", "myapp", "/payment") is None
        assert bt_reg.get_all("prod", "myapp") == []

    def test_on_deployment_does_not_affect_other_apps(
        self,
        invalidator: CacheInvalidator,
        golden_reg: GoldenRegistry,
    ) -> None:
        golden_reg.set(_make_golden(app="other_app", bt="/x"))
        invalidator.on_deployment_detected("prod", "myapp")
        assert golden_reg.get("prod", "other_app", "/x") is not None

    def test_on_restart_clears_golden_but_not_bt(
        self,
        invalidator: CacheInvalidator,
        golden_reg: GoldenRegistry,
        bt_reg: BTRegistry,
    ) -> None:
        golden_reg.set(_make_golden(bt="/checkout"))
        bt_reg.update("prod", "myapp", [BTEntry(name="/checkout")])

        invalidator.on_app_restart_detected("prod", "myapp")

        # Golden must be gone
        assert golden_reg.get("prod", "myapp", "/checkout") is None
        # BT registry must survive
        assert len(bt_reg.get_all("prod", "myapp")) == 1

    def test_on_manual_override_clears_specific_bt_only(
        self,
        invalidator: CacheInvalidator,
        golden_reg: GoldenRegistry,
    ) -> None:
        golden_reg.set(_make_golden(bt="/checkout", guid="old"))
        golden_reg.set(_make_golden(bt="/payment", guid="pay"))

        invalidator.on_manual_golden_override(
            "prod", "myapp", "/checkout", "new-guid", "sre@x.com"
        )

        assert golden_reg.get("prod", "myapp", "/checkout") is None
        assert golden_reg.get("prod", "myapp", "/payment") is not None

    def test_on_cache_validation_failure_never_raises(
        self,
        invalidator: CacheInvalidator,
    ) -> None:
        # Should not raise even if key doesn't exist
        invalidator.on_cache_validation_failure(
            key="nonexistent:key",
            data_type="applications",
            error="ValidationError: missing field",
        )

    def test_get_stats_counts_events_in_last_hour(
        self,
        invalidator: CacheInvalidator,
    ) -> None:
        invalidator.on_deployment_detected("prod", "app1")
        invalidator.on_app_restart_detected("prod", "app2")
        invalidator.on_manual_golden_override("prod", "app3", "/bt", "g", "u@x.com")
        invalidator.on_cache_validation_failure("k", "applications", "err")

        stats = invalidator.get_stats()
        assert stats["deployment_triggered"] == 1
        assert stats["restart_triggered"] == 1
        assert stats["manual_override"] == 1
        assert stats["validation_failure"] == 1

    def test_get_stats_excludes_old_events(
        self,
        invalidator: CacheInvalidator,
    ) -> None:
        # Add an event then backdate it by 2 hours
        invalidator._record("deployment")
        invalidator._log[-1] = ("deployment", time.time() - 7201)

        stats = invalidator.get_stats()
        assert stats["deployment_triggered"] == 0


# ===========================================================================
# ParsedSnapshotCache (TwoLayerCache with parsed_snapshot data_type)
# ===========================================================================


@pytest.mark.asyncio
class TestParsedSnapshotCache:
    async def test_same_guid_returns_cached_without_fetch(
        self, tmp_cache: TwoLayerCache
    ) -> None:
        analysis = CachedSnapshotAnalysis(
            snapshot_guid="guid-abc",
            analyzed_at=datetime.now(tz=UTC),
            language_detected="java",
            error_details={"exception": "NPE"},
            hot_path={"method": "checkout", "time_ms": 2100},
            top_call_segments=[],
            culprit_frame=None,
            caused_by_chain=["NullPointerException"],
        )
        calls: list[int] = []

        async def fetch() -> CachedSnapshotAnalysis:
            calls.append(1)
            return analysis

        # First call — cache miss
        await tmp_cache.get_or_fetch(
            "guid-abc", CachedSnapshotAnalysis, fetch, "parsed_snapshot"
        )
        assert len(calls) == 1

        # Second call — cache hit, no fetch
        await tmp_cache.get_or_fetch(
            "guid-abc", CachedSnapshotAnalysis, fetch, "parsed_snapshot"
        )
        assert len(calls) == 1

    async def test_different_guids_cached_independently(
        self, tmp_cache: TwoLayerCache
    ) -> None:
        def _analysis(guid: str) -> CachedSnapshotAnalysis:
            return CachedSnapshotAnalysis(
                snapshot_guid=guid,
                analyzed_at=datetime.now(tz=UTC),
                language_detected="java",
                error_details=None,
                hot_path={},
                top_call_segments=[],
                culprit_frame=None,
                caused_by_chain=[],
            )

        calls: list[str] = []

        async def fetch_a() -> CachedSnapshotAnalysis:
            calls.append("a")
            return _analysis("guid-a")

        async def fetch_b() -> CachedSnapshotAnalysis:
            calls.append("b")
            return _analysis("guid-b")

        await tmp_cache.get_or_fetch(
            "guid-a", CachedSnapshotAnalysis, fetch_a, "parsed_snapshot"
        )
        await tmp_cache.get_or_fetch(
            "guid-b", CachedSnapshotAnalysis, fetch_b, "parsed_snapshot"
        )
        # Each fetched exactly once
        assert calls.count("a") == 1
        assert calls.count("b") == 1

        # Second accesses — no additional fetches
        await tmp_cache.get_or_fetch(
            "guid-a", CachedSnapshotAnalysis, fetch_a, "parsed_snapshot"
        )
        await tmp_cache.get_or_fetch(
            "guid-b", CachedSnapshotAnalysis, fetch_b, "parsed_snapshot"
        )
        assert calls.count("a") == 1
        assert calls.count("b") == 1

    async def test_raw_snapshot_json_not_in_cached_result(
        self, tmp_cache: TwoLayerCache
    ) -> None:
        """CachedSnapshotAnalysis must not contain raw snapshot fields."""
        analysis = CachedSnapshotAnalysis(
            snapshot_guid="guid-raw",
            analyzed_at=datetime.now(tz=UTC),
            language_detected="java",
            error_details=None,
            hot_path={},
            top_call_segments=[],
            culprit_frame=None,
            caused_by_chain=[],
        )

        async def fetch() -> CachedSnapshotAnalysis:
            return analysis

        result = await tmp_cache.get_or_fetch(
            "guid-raw", CachedSnapshotAnalysis, fetch, "parsed_snapshot"
        )
        assert isinstance(result, CachedSnapshotAnalysis)

        # Raw JSON fields must NOT be present
        raw_fields = {"raw_json", "rawSnapshot", "fullBody", "requestPayload"}
        result_dict = (
            result.model_dump() if isinstance(result, CachedSnapshotAnalysis) else {}
        )
        for field in raw_fields:
            assert field not in result_dict, (
                f"Raw field '{field}' found in cached result"
            )

    async def test_parsed_snapshot_not_persisted_to_disk(self, tmp_path: Any) -> None:
        """Parsed snapshots must stay in-memory only (never persisted to disk)."""
        cache = TwoLayerCache(cache_dir=str(tmp_path / "snap_cache"))
        analysis = CachedSnapshotAnalysis(
            snapshot_guid="no-disk",
            analyzed_at=datetime.now(tz=UTC),
            language_detected="python",
            error_details=None,
            hot_path={},
            top_call_segments=[],
            culprit_frame=None,
            caused_by_chain=[],
        )

        async def fetch() -> CachedSnapshotAnalysis:
            return analysis

        # persist_to_disk=False — default for parsed_snapshot
        await cache.get_or_fetch(
            "no-disk", CachedSnapshotAnalysis, fetch, "parsed_snapshot",
            persist_to_disk=False,
        )
        # Disk should have no entries for this key
        assert cache._disk.get("no-disk") is None


# ===========================================================================
# Registry round-trips
# ===========================================================================


class TestAppsRegistryRoundTrip:
    def test_update_and_get_all(self, apps_reg: AppsRegistry) -> None:
        apps = [
            AppEntry(id=1, name="myapp", controller_name="prod"),
            AppEntry(id=2, name="other", controller_name="prod"),
        ]
        apps_reg.update("prod", apps)
        result = apps_reg.get_all("prod")
        assert len(result) == 2
        assert result[0].name == "myapp"

    def test_empty_controller_returns_empty_list(self, apps_reg: AppsRegistry) -> None:
        assert apps_reg.get_all("unknown_ctrl") == []

    def test_invalidate_clears_both_layers(self, apps_reg: AppsRegistry) -> None:
        apps_reg.update("prod", [AppEntry(id=1, name="a", controller_name="prod")])
        apps_reg.invalidate("prod")
        assert apps_reg.get_all("prod") == []

    def test_l2_fallback_on_mem_miss(self, tmp_path: Any) -> None:
        reg = AppsRegistry(disk_dir=str(tmp_path / "apps_l2"))
        reg.update("prod", [AppEntry(id=5, name="fallback", controller_name="prod")])

        # Clear L1 only
        reg._mem.clear()

        # Should fall back to L2
        result = reg.get_all("prod")
        assert len(result) == 1
        assert result[0].name == "fallback"


class TestBTRegistryRoundTrip:
    def test_update_and_get_all(self, bt_reg: BTRegistry) -> None:
        bts = [BTEntry(name="/checkout"), BTEntry(name="/health", is_health_check=True)]
        bt_reg.update("prod", "myapp", bts)
        result = bt_reg.get_all("prod", "myapp")
        assert len(result) == 2

    def test_empty_returns_empty_list(self, bt_reg: BTRegistry) -> None:
        assert bt_reg.get_all("prod", "unknown") == []

    def test_invalidate_clears_entries(self, bt_reg: BTRegistry) -> None:
        bt_reg.update("prod", "myapp", [BTEntry(name="/a")])
        bt_reg.invalidate("prod", "myapp")
        assert bt_reg.get_all("prod", "myapp") == []
