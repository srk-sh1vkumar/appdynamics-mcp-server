"""
services/health.py

MCP server health status — used by get_server_health() tool and K8s liveness probe.

Design decisions:
- HealthStatus is computed on demand by aggregating state from TokenManager,
  controller pings, cache stats, and license_check module.
- The K8s liveness probe path is /health — handled by a minimal asyncio HTTP
  server (not FastAPI/Starlette) to avoid adding a full web framework
  dependency for a single endpoint.
- Graceful shutdown: SIGTERM/SIGINT set a stop event. The main loop checks
  this event after each tool call completes, allowing in-flight requests to
  finish before the process exits.
"""

from __future__ import annotations

import asyncio
import json
import signal
import sys
from typing import Any

from models.types import HealthStatus
from utils import cache as cache_module
from utils import metrics as metrics_module

# Module-level stop event for graceful shutdown
_stop_event = asyncio.Event()


def setup_signal_handlers() -> None:
    """Register SIGTERM and SIGINT handlers for graceful shutdown."""

    def _handle(sig: int, frame: object) -> None:
        print(
            f"[health] Received signal {sig}. Initiating graceful shutdown.",
            file=sys.stderr,
        )
        _stop_event.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def is_stopping() -> bool:
    return _stop_event.is_set()


async def compute_health(
    version: str,
    vault_ok: bool,
    token_managers: dict[str, Any],    # controller_name → TokenManager
    client_registry: dict[str, Any],   # controller_name → AppDClient
    licensed_modules: list[str],
    disabled_tools: list[str],
    degradation_mode: str,
) -> HealthStatus:
    """Compute current health status by probing all controllers."""
    controller_status: dict[str, str] = {}
    for name, client in client_registry.items():
        try:
            reachable = await asyncio.wait_for(client.ping(), timeout=5.0)
            controller_status[name] = "reachable" if reachable else "unreachable"
        except Exception:
            controller_status[name] = "unreachable"

    all_reachable = all(v == "reachable" for v in controller_status.values())
    vault_str = "connected" if vault_ok else "unreachable"

    # Token expiry from the first (primary) token manager
    token_expiry = "unknown"
    if token_managers:
        first_tm = next(iter(token_managers.values()))
        token_expiry = first_tm.token_expiry_human()

    # Determine overall status
    if all_reachable and vault_ok:
        status = "healthy"
    elif all_reachable or vault_ok:
        status = "degraded"
    else:
        status = "unhealthy"

    return HealthStatus(
        status=status,
        version=version,
        vault=vault_str,
        controllers=controller_status,
        token_expiry=token_expiry,
        degradation_mode=degradation_mode,
        cache_hit_rate=cache_module.cache_hit_rate(),
        requests_last_hour=cache_module.requests_last_hour(),
        active_users=cache_module.active_user_count(),
        licensed_modules=licensed_modules,
        disabled_tools=disabled_tools,
    )


# ---------------------------------------------------------------------------
# Minimal HTTP server for K8s liveness probe on /health
# ---------------------------------------------------------------------------

async def _handle_request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    raw = await reader.read(4096)
    first_line = raw.split(b"\n", 1)[0].decode(errors="replace")
    path = first_line.split(" ")[1] if " " in first_line else "/"

    if path.startswith("/metrics"):
        body = metrics_module.render().encode()
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"\r\n"
            + body
        )
    else:
        # /health and everything else
        payload = json.dumps({"status": "ok"}).encode()
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: application/json\r\n"
            + f"Content-Length: {len(payload)}\r\n".encode()
            + b"\r\n"
            + payload
        )

    writer.write(response)
    await writer.drain()
    writer.close()


async def start_liveness_server(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Start minimal HTTP server for K8s liveness probe and Prometheus metrics."""
    server = await asyncio.start_server(_handle_request, host, port)
    asyncio.create_task(server.serve_forever())
    print(
        f"[health] HTTP server on {host}:{port}"
        " — /health (liveness) /metrics (Prometheus)",
        file=sys.stderr,
    )
