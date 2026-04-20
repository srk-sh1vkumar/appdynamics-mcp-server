"""
utils/cache.py

Two-layer caching: cachetools TTLCache (in-memory) + diskcache (file persistence).

Design decisions:
- Module-level API (get/set/delete/invalidate_prefix) provides backward-compatible
  access used by all 28 existing tools.
- TwoLayerCache class provides the richer get_or_fetch() interface with Pydantic
  validation, per-data-type TTL/maxsize, and structural eviction on corrupt entries.
- CachedSnapshotAnalysis stores parsed snapshot results (not raw JSON) — the most
  valuable cache entry in the system since parsing is expensive and GUIDs are immutable.
- Cache keys MUST include UPN as first segment to prevent cross-user data leakage.
- asyncio.Lock guards in-memory writes (single-process async server).
- Stats (hits/misses per data type, active UPNs) feed the health endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import diskcache
from cachetools import TTLCache
from pydantic import BaseModel, ValidationError

from utils import metrics as _metrics

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-data-type TTL and maxsize configuration
# ---------------------------------------------------------------------------

MEMORY_CACHE_CONFIG: dict[str, dict[str, int]] = {
    # One entry per controller; raised to cover federated multi-controller setups.
    "applications":          {"ttl": 300,  "maxsize": 200},
    # One entry per app_name; 3 000 apps across all controllers.
    "business_transactions": {"ttl": 300,  "maxsize": 3000},
    # One entry per app metric-tree; covers full fleet.
    "metric_tree":           {"ttl": 600,  "maxsize": 3000},
    # Specific metric ranges; active investigations stay well below this.
    "metric_values":         {"ttl": 60,   "maxsize": 2000},
    # Policy checks per app; realistic upper bound for concurrent investigations.
    "health_violations":     {"ttl": 30,   "maxsize": 500},
    # Per-UPN per-controller roles; covers large SRE orgs.
    "user_roles":            {"ttl": 1800, "maxsize": 1000},
    # Per-app snapshot lists; raised for broader concurrent investigations.
    "snapshot_list":         {"ttl": 30,   "maxsize": 500},
    # Expensive parsed analyses; GUIDs are immutable so high TTL justifies larger pool.
    "parsed_snapshot":       {"ttl": 3600, "maxsize": 500},
    # Infrastructure stats per node; changes slowly — 2 min TTL balances freshness vs load.
    "infrastructure_stats":  {"ttl": 120,  "maxsize": 2000},
    # Tier/node topology; deployment changes are rare — 5 min TTL, shared across investigations.
    "tiers_and_nodes":       {"ttl": 300,  "maxsize": 1000},
    # BT baselines are 7-day rolling averages; highly stable — 5 min TTL is conservative.
    "bt_baseline":           {"ttl": 300,  "maxsize": 3000},
}

# Data types that must NEVER be cached
NEVER_CACHE: frozenset[str] = frozenset({
    "raw_snapshot_json",
    "adql_query_results",
    "active_health_violations_realtime",
})

# ---------------------------------------------------------------------------
# Legacy TTL constants kept for backward compat (main.py uses CACHE_TTLS)
# ---------------------------------------------------------------------------

CACHE_TTLS: dict[str, int] = {dt: cfg["ttl"] for dt, cfg in MEMORY_CACHE_CONFIG.items()}
# Extra keys used by existing tools
CACHE_TTLS.setdefault("metrics", 60)

# ---------------------------------------------------------------------------
# Module-level Layer 1 + Layer 2 (used by legacy API)
# ---------------------------------------------------------------------------

_DISK_DIR = Path("data/diskcache")
_DISK_DIR.mkdir(parents=True, exist_ok=True)
_disk: diskcache.Cache = diskcache.Cache(str(_DISK_DIR))

_mem: TTLCache[str, Any] = TTLCache(maxsize=100_000, ttl=600)
_mem_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Stats (module-level, feeds health endpoint)
# ---------------------------------------------------------------------------

_stats: dict[str, Any] = {
    "hits": 0,
    "misses": 0,
    "active_users": set(),
    "request_timestamps": [],
    "per_type_hits": defaultdict(int),
    "per_type_misses": defaultdict(int),
}


def _record(upn: str) -> None:
    now = time.time()
    _stats["active_users"].add(upn)
    _stats["request_timestamps"].append(now)
    cutoff = now - 3600
    _stats["request_timestamps"] = [
        t for t in _stats["request_timestamps"] if t > cutoff
    ]


def _type_from_key(key: str) -> str:
    """Extract data_type from key format upn:controller:data_type[:id...]."""
    parts = key.split(":", 3)
    return parts[2] if len(parts) > 2 else "unknown"


def cache_hit_rate() -> str:
    total = _stats["hits"] + _stats["misses"]
    return f"{round(_stats['hits'] / total * 100)}%" if total else "0%"


def requests_last_hour() -> int:
    return len(_stats["request_timestamps"])


def active_user_count() -> int:
    return len(_stats["active_users"])


def disk_entry_count() -> int:
    return len(_disk)


def get_per_type_hit_rates() -> dict[str, str]:
    result: dict[str, str] = {}
    hits = _stats["per_type_hits"]
    misses = _stats["per_type_misses"]
    for dt in {**hits, **misses}:
        h = hits.get(dt, 0)
        m = misses.get(dt, 0)
        total = h + m
        result[dt] = f"{round(h / total * 100)}%" if total else "0%"
    return result


def get_stats() -> dict[str, Any]:
    """Return cache stats for the health endpoint."""
    return {
        "hit_rates": get_per_type_hit_rates(),
        "overall_hit_rate": cache_hit_rate(),
        "memory_entries": len(_mem),
        "disk_entries": disk_entry_count(),
    }


# ---------------------------------------------------------------------------
# Legacy module-level API (backward-compatible — used by existing 28 tools)
# ---------------------------------------------------------------------------


def make_key(upn: str, controller: str, data_type: str, identifier: str = "") -> str:
    """Build a namespaced key. UPN is always first to prevent leakage."""
    return f"{upn}:{controller}:{data_type}:{identifier}"


async def get(key: str, upn: str) -> Any | None:
    _record(upn)
    dt = _type_from_key(key)
    async with _mem_lock:
        value = _mem.get(key)
    if value is not None:
        _stats["hits"] += 1
        _stats["per_type_hits"][dt] += 1
        _metrics.record_cache_hit()
        return value

    disk_value = _disk.get(key)
    if disk_value is not None:
        _stats["hits"] += 1
        _stats["per_type_hits"][dt] += 1
        _metrics.record_cache_hit()
        async with _mem_lock:
            _mem[key] = disk_value
        return disk_value

    _stats["misses"] += 1
    _stats["per_type_misses"][dt] += 1
    _metrics.record_cache_miss()
    return None


async def set(key: str, value: Any, ttl: int, persist: bool = False) -> None:
    if ttl <= 0:
        return
    async with _mem_lock:
        _mem[key] = value
    if persist:
        _disk.set(key, value, expire=ttl * 10)


async def delete(key: str) -> None:
    async with _mem_lock:
        _mem.pop(key, None)
    _disk.delete(key)


async def invalidate_prefix(prefix: str) -> None:
    async with _mem_lock:
        to_del = [
            k for k in list(_mem.keys()) if isinstance(k, str) and k.startswith(prefix)
        ]
        for k in to_del:
            del _mem[k]
    for k in list(_disk.iterkeys()):
        if isinstance(k, str) and k.startswith(prefix):
            _disk.delete(k)


# ---------------------------------------------------------------------------
# CachedSnapshotAnalysis — parsed snapshot result model
# Raw snapshot JSON (~500 KB) is NEVER stored in cache.
# PII redaction has already been applied before storage.
# TTL: 3600s (GUIDs are immutable — content never changes).
# Storage: in-memory only (parsed_snapshot data_type, persist_to_disk=False).
# ---------------------------------------------------------------------------


class CachedSnapshotAnalysis(BaseModel):
    """Parsed snapshot output cached after first analysis.

    Raw snapshot JSON is intentionally excluded. Only the derived,
    PII-redacted analysis fields are stored.
    """

    snapshot_guid: str
    analyzed_at: datetime
    language_detected: str
    error_details: dict[str, Any] | None
    hot_path: dict[str, Any]
    top_call_segments: list[dict[str, Any]]
    culprit_frame: dict[str, Any] | None
    caused_by_chain: list[str]


# ---------------------------------------------------------------------------
# TwoLayerCache — class API for structured data with Pydantic validation
# ---------------------------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


class TwoLayerCache:
    """Two-layer cache with per-data-type TTLCache (L1) and diskcache (L2).

    Read strategy:  L1 → L2 → fetch_fn → populate both layers.
    Write strategy: Always write to L1. Write to L2 only when persist_to_disk=True.
    Validation:     Pydantic on every cache read. Corrupt dict entries are
                    auto-evicted; non-dict entries (lists, models) are passed through.

    Separate from the module-level cache: designed for single-model-per-key
    patterns (user roles, golden snapshots, parsed analyses).
    """

    def __init__(
        self,
        cache_dir: str = "data/two_layer_cache",
        mem_config: dict[str, dict[str, int]] | None = None,
    ) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        cfg = mem_config or MEMORY_CACHE_CONFIG
        self._l1: dict[str, TTLCache[str, Any]] = {
            dt: TTLCache(maxsize=c["maxsize"], ttl=c["ttl"])
            for dt, c in cfg.items()
        }
        self._l1_default: TTLCache[str, Any] = TTLCache(maxsize=500, ttl=300)
        self._disk: diskcache.Cache = diskcache.Cache(str(self._dir))
        self._lock: asyncio.Lock = asyncio.Lock()
        self._hits: dict[str, int] = defaultdict(int)
        self._misses: dict[str, int] = defaultdict(int)
        self._evictions: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def get_or_fetch(
        self,
        key: str,
        model: type[T],
        fetch_fn: Callable[[], Awaitable[Any]],
        data_type: str,
        persist_to_disk: bool = False,
    ) -> Any:
        """Return cached value or fetch fresh, validating with Pydantic.

        1. Try L1 (per-data-type TTLCache). Validate with Pydantic.
           - Dict that fails validation → evict + log + continue.
           - Non-dict (list, model) → return as-is (can't schema-validate).
        2. Try L2 (diskcache) if persist_to_disk=True. Same validation rules.
        3. Call fetch_fn() → validate → store in L1 (and L2 if persist_to_disk).
        """
        l1 = self._l1.get(data_type, self._l1_default)

        # L1
        async with self._lock:
            raw = l1.get(key)
        if raw is not None:
            validated, should_evict = self._try_validate(raw, model, key, data_type)
            if should_evict:
                async with self._lock:
                    l1.pop(key, None)
                self._evictions[data_type] += 1
            else:
                self._hits[data_type] += 1
                return validated if validated is not None else raw

        # L2
        if persist_to_disk:
            disk_raw = self._disk.get(key)
            if disk_raw is not None:
                validated, should_evict = self._try_validate(
                    disk_raw, model, key, data_type
                )
                if should_evict:
                    self._disk.delete(key)
                    self._evictions[data_type] += 1
                else:
                    value = validated if validated is not None else disk_raw
                    async with self._lock:
                        l1[key] = value
                    self._hits[data_type] += 1
                    return value

        # Fetch fresh
        self._misses[data_type] += 1
        result = await fetch_fn()
        validated, _ = self._try_validate(result, model, key, data_type)
        stored = validated if validated is not None else result

        async with self._lock:
            l1[key] = stored
        if persist_to_disk:
            ttl = MEMORY_CACHE_CONFIG.get(data_type, {}).get("ttl", 300)
            self._disk.set(key, stored, expire=ttl * 10)

        return stored

    def invalidate(self, key: str) -> None:
        """Remove key from both L1 and L2."""
        for l1 in self._l1.values():
            l1.pop(key, None)
        self._l1_default.pop(key, None)
        self._disk.delete(key)

    def invalidate_prefix(self, prefix: str) -> None:
        """Remove all keys starting with prefix from both layers."""
        for l1 in self._l1.values():
            to_del = [
                k for k in list(l1.keys())
                if isinstance(k, str) and k.startswith(prefix)
            ]
            for k in to_del:
                l1.pop(k, None)
        to_del_default = [
            k for k in list(self._l1_default.keys())
            if isinstance(k, str) and k.startswith(prefix)
        ]
        for k in to_del_default:
            self._l1_default.pop(k, None)
        for k in list(self._disk.iterkeys()):
            if isinstance(k, str) and k.startswith(prefix):
                self._disk.delete(k)

    def get_stats(self) -> dict[str, Any]:
        """Return hit rate, size, and eviction count per data type."""
        result: dict[str, Any] = {}
        all_types: dict[str, int] = {**self._hits, **self._misses}
        for dt in all_types:
            h = self._hits.get(dt, 0)
            m = self._misses.get(dt, 0)
            total = h + m
            result[dt] = {
                "hit_rate": f"{round(h / total * 100)}%" if total else "0%",
                "hits": h,
                "misses": m,
                "evictions": self._evictions.get(dt, 0),
                "l1_size": len(self._l1.get(dt, {})),
            }
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _try_validate(
        self,
        value: Any,
        model: type[T],
        key: str,
        data_type: str,
    ) -> tuple[T | None, bool]:
        """Attempt Pydantic validation.

        Returns (validated_value, should_evict).
        - Dict with valid schema   → (model_instance, False)
        - Dict with invalid schema → (None, True)  — caller should evict
        - Non-dict (list, model)   → (None, False) — pass through as-is
        """
        if isinstance(value, model):
            return value, False
        if isinstance(value, dict):
            try:
                return model.model_validate(value), False
            except (ValidationError, Exception) as exc:
                logger.warning(
                    "Cache validation failure: key=%s type=%s error=%s",
                    key,
                    data_type,
                    exc,
                )
                return None, True
        # List, raw string, or other type — cannot Pydantic-validate
        return None, False
