"""
tests/mocks/appd_server.py

httpx MockTransport that replays fixture JSON responses for AppDynamics API calls.

Usage in tests:
    from tests.mocks.appd_server import build_mock_transport
    transport = build_mock_transport()
    client = httpx.AsyncClient(transport=transport, base_url="https://test.example.saas.appdynamics.com")

Design:
- Routes are matched by (method, path_prefix) in insertion order.
- The RESPONSES dict maps URL path fragments to JSON payloads.
- Unknown routes return 404 with a diagnostic body.
- Error scenario helpers (make_401, make_403, make_429, make_500) are
  provided for test parameterization.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Fixture payloads (duplicated from conftest for import-independence)
# ---------------------------------------------------------------------------

_APPS = [
    {"id": 1, "name": "ecommerce-app", "description": "E-commerce platform"},
    {"id": 2, "name": "payment-service", "description": "Payment processing"},
]

_BTS = [
    {
        "id": 101,
        "name": "/api/checkout",
        "entryPointType": "SERVLET",
        "tierName": "checkout-tier",
        "averageResponseTime": 1200,
        "callsPerMinute": 45.0,
        "errorPercent": 3.5,
        "numberOfCalls": 2700,
        "numberOfErrors": 94,
        "numberOfSlowCalls": 12,
        "numberOfVerySlowCalls": 3,
        "externalCallCount": 2,
        "dbCallCount": 8,
    },
    {
        "id": 102,
        "name": "/health",
        "entryPointType": "SERVLET",
        "tierName": "checkout-tier",
        "averageResponseTime": 5,
        "callsPerMinute": 120.0,
        "errorPercent": 0.0,
        "numberOfCalls": 7200,
        "numberOfErrors": 0,
        "numberOfSlowCalls": 0,
        "numberOfVerySlowCalls": 0,
        "externalCallCount": 0,
        "dbCallCount": 0,
    },
]

_SNAPSHOTS = [
    {
        "requestGUID": "abc-123-guid",
        "serverStartTime": 1700000000000,
        "timeTakenInMilliSecs": 3500,
        "summary": "Exception occurred",
        "errorOccurred": True,
        "errorDetails": "NullPointerException in com.example.CheckoutService",
        "url": "/api/checkout",
        "userExperience": "SLOW",
        "stacks": [],
        "callChain": "",
    }
]

_SNAPSHOT_DETAIL = {
    "requestGUID": "abc-123-guid",
    "serverStartTime": 1700000000000,
    "timeTakenInMilliSecs": 3500,
    "summary": "Exception occurred",
    "errorOccurred": True,
    "errorDetails": (
        "java.lang.NullPointerException\n"
        "\tat com.example.CheckoutService.processPayment(CheckoutService.java:142)\n"
        "\tat com.example.OrderController.checkout(OrderController.java:67)\n"
        "\tat sun.reflect.NativeMethodAccessorImpl.invoke0(Native Method)\n"
    ),
    "url": "/api/checkout",
    "userExperience": "SLOW",
    "stacks": [
        {
            "exitCalls": [
                {
                    "exitPointType": "DB",
                    "toComponentId": "MySQL",
                    "timeTakenInMilliSecs": 2100,
                    "stackTrace": "",
                }
            ]
        }
    ],
    "callChain": "CUSTOM_EXIT_POINT|SERVLET",
}

_GOLDEN_SNAPSHOT = {
    "requestGUID": "golden-snap-guid",
    "serverStartTime": 1699913600000,
    "timeTakenInMilliSecs": 350,
    "summary": "",
    "errorOccurred": False,
    "errorDetails": "",
    "url": "/api/checkout",
    "userExperience": "NORMAL",
    "stacks": [],
    "callChain": "SERVLET",
}

_HEALTH_VIOLATIONS = [
    {
        "id": 5001,
        "name": "Business Transaction response time is too slow",
        "type": "APPLICATION",
        "severity": "CRITICAL",
        "startTime": 1700000000000,
        "endTime": -1,
        "affectedEntityName": "/api/checkout",
        "affectedEntityType": "BUSINESS_TRANSACTION",
        "description": "P99 latency exceeded 2000ms threshold",
    }
]

_METRIC_DATA = {
    "metricValues": [
        {"startTimeInMillis": 1700000000000, "occurrences": 1, "current": 1250, "min": 800, "max": 3500, "count": 60, "sum": 75000, "value": 1250, "standardDeviation": 220.5}  # noqa: E501
    ],
    "metricPath": "Business Transaction Performance|Business Transactions|checkout-tier|/api/checkout|Average Response Time (ms)",  # noqa: E501
    "frequency": "ONE_MIN",
}

_JVM = {
    "memoryPoolUsage": [
        {"name": "Eden Space", "used": 134217728, "committed": 268435456, "max": 536870912},  # noqa: E501
        {"name": "Old Gen", "used": 805306368, "committed": 1073741824, "max": 1073741824},  # noqa: E501
    ],
    "gcStats": [
        {"name": "G1 Young Generation", "collectionCount": 42, "collectionTime": 630},
        {"name": "G1 Old Generation", "collectionCount": 1, "collectionTime": 15000},
    ],
    "threadCount": 248,
    "deadlockedThreads": [],
}

_INFRA = [
    {
        "tierId": 10,
        "tierName": "checkout-tier",
        "nodeId": 100,
        "nodeName": "checkout-node-01",
        "cpuUsage": 72.3,
        "memoryUsed": 3221225472,
        "memoryTotal": 8589934592,
        "diskReadKbps": 12.5,
        "diskWriteKbps": 8.3,
    }
]

_ERRORS = [
    {
        "id": 9001,
        "name": "NullPointerException",
        "type": "APPLICATION_ERROR",
        "count": 94,
        "message": "Cannot invoke method getPaymentToken() on null object",
        "stackTrace": (
            "java.lang.NullPointerException\n"
            "\tat com.example.CheckoutService.processPayment(CheckoutService.java:142)\n"  # noqa: E501
        ),
        "firstOccurrence": 1700000100000,
        "lastOccurrence": 1700003500000,
    }
]

_EUM_OVERVIEW = {
    "pageViews": 125000,
    "jsErrors": 340,
    "ajaxErrors": 87,
    "avgPageLoadTime": 2800,
    "crashCount": 0,
}

_EUM_PAGE_PERF = [
    {"page": "/checkout", "avgLoadTime": 3200, "percentile95": 5800, "views": 12000, "bounceRate": 0.08}  # noqa: E501
]

_EUM_JS_ERRORS = [
    {"message": "TypeError: Cannot read property 'price' of undefined", "count": 145, "page": "/checkout"}  # noqa: E501
]

_EUM_AJAX = [
    {"url": "/api/payment", "avgTime": 1800, "errorRate": 2.5, "count": 8400}
]

_EUM_GEO = [
    {"country": "United States", "avgLoadTime": 2600, "views": 78000},
    {"country": "United Kingdom", "avgLoadTime": 3100, "views": 18000},
]

_DB_PERF = {
    "queries": [
        {"sql": "SELECT * FROM orders WHERE status=?", "avgTime": 1200, "callCount": 2700, "totalTime": 3240000}  # noqa: E501
    ],
    "connections": {"active": 45, "idle": 5, "max": 100},
}

_NETWORK_KPIS = [
    {
        "tierId": 10,
        "tierName": "checkout-tier",
        "bytesSentPerSec": 524288,
        "bytesReceivedPerSec": 262144,
        "tcpLossPercent": 0.02,
        "latencyMs": 1.2,
    }
]

_ANALYTICS = {
    "results": [
        {"transactionName": "/api/checkout", "count": 2700, "avgResponseTime": 1250}
    ],
    "total": 1,
}

_USER = {"userName": "alice", "roles": [{"name": "Account Administrator"}]}

_LICENSE_PROBES = {
    "eum": True,
    "database_visibility": True,
    "analytics": True,
    "snapshots": True,
}

_API_VERSION = {"major": 4, "minor": 1, "patch": 0, "build": "22.11.0.33581"}

_POLICIES = [
    {
        "id": 1,
        "name": "Checkout SLA Policy",
        "enabled": True,
        "actionNames": ["Page team"],
        "events": {"healthRuleEvents": {"healthRuleViolationEvents": {"violationOpenEnabled": True}}},  # noqa: E501
    }
]

_TOKEN_RESPONSE = {
    "access_token": "mock-oauth2-token",
    "token_type": "Bearer",
    "expires_in": 3600,
}


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

# (method, path_substring) → response body (dict/list) or callable
_ROUTES: list[tuple[str, str, Any]] = [
    # Auth
    ("POST", "/controller/api/oauth/access_token", _TOKEN_RESPONSE),
    # API version
    ("GET", "/controller/rest/version", _API_VERSION),
    # Applications
    ("GET", "/controller/rest/applications", _APPS),
    # BTs
    ("GET", "/controller/rest/applications/1/business-transactions", _BTS),
    ("GET", "/controller/rest/applications/ecommerce-app/business-transactions", _BTS),
    # Metrics
    ("GET", "/controller/rest/applications/1/metric-data", _METRIC_DATA),
    ("GET", "/controller/rest/applications/ecommerce-app/metric-data", _METRIC_DATA),
    # Snapshots — list (before detail to avoid substring collision)
    ("GET", "/controller/rest/applications/1/request-snapshots", _SNAPSHOTS),
    ("GET", "/controller/rest/applications/ecommerce-app/request-snapshots", _SNAPSHOTS),  # noqa: E501
    # Snapshot detail
    ("GET", "/controller/rest/applications/1/request-snapshots/abc-123-guid", _SNAPSHOT_DETAIL),  # noqa: E501
    # Golden snapshot (separate GUID)
    ("GET", "/controller/rest/applications/1/request-snapshots/golden-snap-guid", _GOLDEN_SNAPSHOT),  # noqa: E501
    # Health violations
    ("GET", "/controller/rest/applications/1/problems/healthrule-violations", _HEALTH_VIOLATIONS),  # noqa: E501
    ("GET", "/controller/rest/applications/ecommerce-app/problems/healthrule-violations", _HEALTH_VIOLATIONS),  # noqa: E501
    # Policies
    ("GET", "/controller/rest/applications/1/policies", _POLICIES),
    # Infra
    ("GET", "/controller/rest/applications/1/nodes", _INFRA),
    # JVM
    ("GET", "/controller/rest/applications/1/nodes/100/jvm", _JVM),
    # Errors
    ("GET", "/controller/rest/applications/1/problems/errors", _ERRORS),
    # DB
    ("GET", "/controller/rest/applications/1/data-collectors/database", _DB_PERF),
    # Network
    ("GET", "/controller/rest/applications/1/network-requests", _NETWORK_KPIS),
    # EUM
    ("GET", "/restui/v1/eum/apps", _EUM_OVERVIEW),
    ("GET", "/restui/v1/eum/pagelist", _EUM_PAGE_PERF),
    ("GET", "/restui/v1/eum/jserrors", _EUM_JS_ERRORS),
    ("GET", "/restui/v1/eum/ajaxlist", _EUM_AJAX),
    ("GET", "/restui/v1/eum/geo", _EUM_GEO),
    # User
    ("GET", "/controller/rest/users", _USER),
    # Analytics
    ("POST", "/events/query", _ANALYTICS),
    # Archive snapshot (no-op 200)
    ("POST", "/controller/rest/applications/1/request-snapshots/abc-123-guid/archive", {}),  # noqa: E501
]


def _make_response(status_code: int, body: Any) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        content=json.dumps(body).encode(),
    )


class _MockTransport(httpx.AsyncBaseTransport):
    """Route requests against _ROUTES; return 404 for unmatched paths."""

    def __init__(self, overrides: dict[tuple[str, str], Any] | None = None) -> None:
        self._overrides = overrides or {}

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()

        # Check overrides first (allows injecting error responses per-test)
        override_key = (method, path)
        if override_key in self._overrides:
            override = self._overrides[override_key]
            if isinstance(override, httpx.Response):
                return override
            return _make_response(200, override)

        # Match against route table
        for route_method, route_path, body in _ROUTES:
            if route_method == method and route_path in path:
                return _make_response(200, body)

        # 404 fallback
        return _make_response(
            404, {"error": f"MockTransport: no route for {method} {path}"}
        )


def build_mock_transport(
    overrides: dict[tuple[str, str], Any] | None = None,
) -> _MockTransport:
    """
    Build an httpx AsyncBaseTransport replaying fixture data.

    Args:
        overrides: dict mapping (HTTP_METHOD, url_path) to either an
                   httpx.Response (for error scenarios) or a dict/list
                   (replaces the default fixture payload).

    Example — inject a 401:
        transport = build_mock_transport({
            ("GET", "/controller/rest/applications"): make_401()
        })
    """
    return _MockTransport(overrides=overrides)


# ---------------------------------------------------------------------------
# Error response helpers
# ---------------------------------------------------------------------------


def make_401() -> httpx.Response:
    return _make_response(401, {"message": "Unauthorized"})


def make_403() -> httpx.Response:
    return _make_response(403, {"message": "Forbidden: insufficient privileges"})


def make_404() -> httpx.Response:
    return _make_response(404, {"message": "Resource not found"})


def make_429() -> httpx.Response:
    return _make_response(429, {"message": "Rate limit exceeded"})


def make_500() -> httpx.Response:
    return _make_response(500, {"message": "Internal server error"})
