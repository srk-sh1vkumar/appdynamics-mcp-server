"""
registries/bt_registry.py

Persisted business-transaction registry.

Two-layer design:
  L1: TTLCache (300s in-memory) — fast reads during active investigations
  L2: diskcache — survives MCP process restarts mid-incident

Updated on every successful get_business_transactions call.
Invalidated when a deployment is detected for the app.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import diskcache
from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

_DISK_DIR = Path("data/registry/bts")
_MEM_TTL = 300
_MEM_MAXSIZE = 500
_DISK_TTL_MULTIPLIER = 10


class BTEntry(BaseModel):
    """Enriched BT record as produced by bt_classifier.enrich_bt()."""

    model_config = ConfigDict(extra="ignore")

    id: int = 0
    name: str
    entry_point_type: str = ""
    http_method: str | None = None
    avg_response_time_ms: float = 0.0
    calls_per_minute: float = 0.0
    error_rate: float = 0.0
    criticality: str = "LOW"      # CRITICAL | HIGH | MEDIUM | LOW
    bt_type: str = "standard"     # see bt_classifier.BTType values
    is_health_check: bool = False
    swagger_operation_name: str | None = None
    baseline_response_time_ms: float | None = None
    last_updated: datetime = None  # type: ignore[assignment]

    def model_post_init(self, __context: Any) -> None:
        if self.last_updated is None:
            object.__setattr__(self, "last_updated", datetime.now(tz=UTC))

    @classmethod
    def from_enriched(cls, enriched: dict[str, Any]) -> BTEntry:
        """Construct from bt_classifier.enrich_bt() output."""
        return cls.model_validate(enriched)


class BTRegistry:
    """Two-layer BT registry keyed by (controller, app)."""

    def __init__(self, disk_dir: str | None = None) -> None:
        dir_path = Path(disk_dir) if disk_dir else _DISK_DIR
        dir_path.mkdir(parents=True, exist_ok=True)
        self._disk: diskcache.Cache = diskcache.Cache(str(dir_path))
        self._mem: TTLCache[str, list[BTEntry]] = TTLCache(
            maxsize=_MEM_MAXSIZE, ttl=_MEM_TTL
        )

    def _key(self, controller: str, app: str) -> str:
        return f"bts:{controller.lower()}:{app.lower().replace(' ', '_')}"

    def get_all(self, controller: str, app: str) -> list[BTEntry]:
        """Return cached BT list, checking L1 then L2. Returns [] if absent."""
        key = self._key(controller, app)
        value: list[BTEntry] | None = self._mem.get(key)
        if value is not None:
            return value
        raw = self._disk.get(key)
        if raw is not None:
            try:
                entries = [BTEntry.model_validate(e) for e in raw]
                self._mem[key] = entries
                return entries
            except Exception:
                logger.warning(
                    "bt_registry: corrupt L2 entry for %s/%s — evicting",
                    controller, app,
                )
                self._disk.delete(key)
        return []

    def update(self, controller: str, app: str, bts: list[BTEntry]) -> None:
        """Persist fresh BT list to both layers."""
        key = self._key(controller, app)
        self._mem[key] = bts
        serialized = [b.model_dump() for b in bts]
        self._disk.set(key, serialized, expire=_MEM_TTL * _DISK_TTL_MULTIPLIER)
        logger.debug("bt_registry: updated %d BTs for %s/%s", len(bts), controller, app)

    def invalidate(self, controller: str, app: str) -> None:
        """Remove BT list for a specific app from both layers."""
        key = self._key(controller, app)
        self._mem.pop(key, None)
        self._disk.delete(key)
        logger.info("bt_registry: invalidated %s/%s", controller, app)
