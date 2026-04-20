"""
registries/golden_registry.py

Golden snapshot registry.

Caches the result of the expensive 7-day golden baseline auto-selection so it
is not repeated on every compare_snapshots call.

Design:
  - In-memory dict mirror for O(1) lookups during active investigations.
  - diskcache for crash-safe persistence.
  - TTL: 24 hours per entry.
  - Invalidated by deployment detection, app restart, manual override, or age.

Cache key format (no UPN — golden baseline is shared across users):
  "__golden__:{controller}:{app}:{bt}"
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import diskcache
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

_DISK_DIR = Path("data/registry/golden")
_GOLDEN_TTL_SECONDS = 86_400      # 24 hours
_EXPIRING_SOON_THRESHOLD = 7_200  # 2 hours before expiry


class GoldenSnapshot(BaseModel):
    """A known-good baseline snapshot for a specific BT."""

    model_config = ConfigDict(extra="ignore")

    snapshot_guid: str
    bt_name: str
    app_name: str
    controller_name: str
    response_time_ms: float
    captured_at: datetime
    selected_at: datetime
    selection_score: int
    confidence: str           # HIGH | MEDIUM | LOW
    promoted_by: str          # "auto" or UPN for manual promotions
    invalidation_reason: str | None = None


def _golden_key(controller: str, app: str, bt: str) -> str:
    app_norm = app.lower().replace(" ", "_")
    bt_norm = bt.lower().replace(" ", "_")
    return f"__golden__:{controller.lower()}:{app_norm}:{bt_norm}"


class GoldenRegistry:
    """Persisted golden snapshot registry with in-memory mirror.

    Thread-safe for single-process asyncio usage.
    """

    def __init__(self, disk_dir: str | None = None) -> None:
        dir_path = Path(disk_dir) if disk_dir else _DISK_DIR
        dir_path.mkdir(parents=True, exist_ok=True)
        self._disk: diskcache.Cache = diskcache.Cache(str(dir_path))
        # In-memory mirror: key → (GoldenSnapshot, stored_at_epoch)
        self._registry: dict[str, tuple[GoldenSnapshot, float]] = {}
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Warm the in-memory mirror from disk at startup."""
        now = time.time()
        for key in list(self._disk.iterkeys()):
            if not isinstance(key, str) or not key.startswith("__golden__"):
                continue
            raw = self._disk.get(key)
            if raw is None:
                continue
            try:
                entry = GoldenSnapshot.model_validate(raw)
                stored_at = raw.get("_stored_at", now)
                if now - stored_at < _GOLDEN_TTL_SECONDS:
                    self._registry[key] = (entry, stored_at)
                else:
                    self._disk.delete(key)
            except Exception:
                logger.warning("golden_registry: corrupt entry %s — evicting", key)
                self._disk.delete(key)

    def get(self, controller: str, app: str, bt: str) -> GoldenSnapshot | None:
        """Return golden snapshot or None if absent / expired (> 24h)."""
        key = _golden_key(controller, app, bt)
        entry = self._registry.get(key)
        if entry is None:
            return None
        golden, stored_at = entry
        if time.time() - stored_at > _GOLDEN_TTL_SECONDS:
            del self._registry[key]
            self._disk.delete(key)
            logger.debug("golden_registry: expired %s/%s/%s", controller, app, bt)
            return None
        return golden

    def set(self, golden: GoldenSnapshot) -> None:
        """Persist a golden snapshot to memory and disk."""
        key = _golden_key(golden.controller_name, golden.app_name, golden.bt_name)
        now = time.time()
        self._registry[key] = (golden, now)
        payload = golden.model_dump()
        payload["_stored_at"] = now
        self._disk.set(key, payload, expire=_GOLDEN_TTL_SECONDS)
        logger.info(
            "golden_registry: set %s/%s/%s guid=%s promoted_by=%s score=%d",
            golden.controller_name,
            golden.app_name,
            golden.bt_name,
            golden.snapshot_guid,
            golden.promoted_by,
            golden.selection_score,
        )

    def invalidate(self, controller: str, app: str, bt: str, reason: str) -> None:
        """Remove golden for a specific BT."""
        key = _golden_key(controller, app, bt)
        self._registry.pop(key, None)
        self._disk.delete(key)
        logger.info(
            "golden_registry: invalidated %s/%s/%s reason=%s",
            controller, app, bt, reason,
        )

    def invalidate_app(self, controller: str, app: str, reason: str) -> None:
        """Remove all golden entries for every BT in an app."""
        prefix = f"__golden__:{controller.lower()}:{app.lower().replace(' ', '_')}:"
        to_del = [k for k in self._registry if k.startswith(prefix)]
        for k in to_del:
            del self._registry[k]
        for k in list(self._disk.iterkeys()):
            if isinstance(k, str) and k.startswith(prefix):
                self._disk.delete(k)
        if to_del:
            logger.info(
                "golden_registry: invalidated %d entries for %s/%s reason=%s",
                len(to_del), controller, app, reason,
            )

    def get_stats(self) -> dict[str, Any]:
        """Return stats for the health endpoint."""
        now = time.time()
        total = len(self._registry)
        expiring_soon = sum(
            1
            for _, stored_at in self._registry.values()
            if (_GOLDEN_TTL_SECONDS - (now - stored_at)) < _EXPIRING_SOON_THRESHOLD
        )
        manually_promoted = sum(
            1
            for golden, _ in self._registry.values()
            if golden.promoted_by != "auto"
        )
        return {
            "total_entries": total,
            "entries_expiring_soon": expiring_soon,
            "manually_promoted": manually_promoted,
        }
