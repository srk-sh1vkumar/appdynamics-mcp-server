"""
tests/conftest.py

Shared pytest fixtures for all unit and integration tests.

Scope notes:
- session-scoped fixtures (controllers_json, mock_vault_env) are set once
  per test run and never torn down — they are read-only config.
- function-scoped fixtures (mock_license_full, mock_license_no_eum, etc.)
  reset module-level state in services/license_check.py between tests.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.types import LicenseState

# ---------------------------------------------------------------------------
# Static fixture data
# ---------------------------------------------------------------------------

CONTROLLERS_JSON: dict[str, Any] = {
    "controllers": [
        {
            "name": "test",
            "url": "https://test.example.saas.appdynamics.com",
            "account": "testaccount",
            "globalAccount": "testaccount_abc123",
            "timezone": "UTC",
            "appPackagePrefix": "com.example",
            "analyticsUrl": "https://analytics.api.appdynamics.com",
            "vaultPath": "secret/appdynamics/test",
        }
    ]
}

# Minimal AppD API response fixtures
APP_LIST_RESPONSE: list[dict] = [
    {"id": 1, "name": "ecommerce-app", "description": "E-commerce platform"},
    {"id": 2, "name": "payment-service", "description": "Payment processing"},
]

BT_LIST_RESPONSE: list[dict] = [
    {
        "id": 101,
        "name": "/api/checkout",
        "entryPointType": "SERVLET",
        "tierName": "checkout-tier",
        "averageResponseTime": 1200,
        "callsPerMinute": 45,
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
        "callsPerMinute": 120,
        "errorPercent": 0.0,
        "numberOfCalls": 7200,
        "numberOfErrors": 0,
        "numberOfSlowCalls": 0,
        "numberOfVerySlowCalls": 0,
        "externalCallCount": 0,
        "dbCallCount": 0,
    },
]

SNAPSHOT_LIST_RESPONSE: list[dict] = [
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

SNAPSHOT_DETAIL_RESPONSE: dict = {
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

GOLDEN_SNAPSHOT_RESPONSE: dict = {
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

HEALTH_VIOLATIONS_RESPONSE: list[dict] = [
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

JVM_RESPONSE: dict = {
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

EUM_OVERVIEW_RESPONSE: dict = {
    "pageViews": 125000,
    "jsErrors": 340,
    "ajaxErrors": 87,
    "avgPageLoadTime": 2800,
    "crashCount": 0,
}


# ---------------------------------------------------------------------------
# Fixtures: configuration
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def controllers_json() -> dict[str, Any]:
    return CONTROLLERS_JSON


@pytest.fixture(scope="session")
def mock_vault_env(monkeypatch_session):
    """Inject mock Vault credentials into the environment (session-scoped)."""
    monkeypatch_session.setenv("SECRET_APPDYNAMICS_TEST_CLIENT_ID", "test-client-id")
    monkeypatch_session.setenv(
        "SECRET_APPDYNAMICS_TEST_CLIENT_SECRET", "test-client-secret"
    )
    monkeypatch_session.setenv("VAULT_MODE", "mock")


# pytest doesn't have a session-scoped monkeypatch by default — provide one.
@pytest.fixture(scope="session")
def monkeypatch_session(tmp_path_factory):
    """Session-scoped monkeypatch workaround."""
    import _pytest.monkeypatch

    mpatch = _pytest.monkeypatch.MonkeyPatch()
    yield mpatch
    mpatch.undo()


# ---------------------------------------------------------------------------
# Fixtures: license state
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_license_full():
    """All modules licensed."""
    state = LicenseState(eum=True, database_visibility=True, analytics=True, snapshots=True)  # noqa: E501
    with patch("services.license_check._state", state):
        yield state


@pytest.fixture
def mock_license_no_eum():
    state = LicenseState(eum=False, database_visibility=True, analytics=True, snapshots=True)  # noqa: E501
    with patch("services.license_check._state", state):
        yield state


@pytest.fixture
def mock_license_no_snapshots():
    state = LicenseState(eum=True, database_visibility=True, analytics=True, snapshots=False)  # noqa: E501
    with patch("services.license_check._state", state):
        yield state


@pytest.fixture
def mock_license_no_analytics():
    state = LicenseState(eum=True, database_visibility=True, analytics=False, snapshots=True)  # noqa: E501
    with patch("services.license_check._state", state):
        yield state


# ---------------------------------------------------------------------------
# Fixtures: AppDClient mock
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_appd_client():
    """Return a fully mocked AppDClient."""
    client = AsyncMock()
    client.list_applications.return_value = APP_LIST_RESPONSE
    client.get_business_transactions.return_value = BT_LIST_RESPONSE
    client.list_snapshots.return_value = SNAPSHOT_LIST_RESPONSE
    client.get_snapshot_detail.return_value = SNAPSHOT_DETAIL_RESPONSE
    client.get_health_violations.return_value = HEALTH_VIOLATIONS_RESPONSE
    client.get_jvm_details.return_value = JVM_RESPONSE
    client.get_eum_overview.return_value = EUM_OVERVIEW_RESPONSE
    client.ping.return_value = True
    return client


@pytest.fixture
def mock_client_registry(mock_appd_client):
    """Return a client registry dict with one 'test' controller."""
    return {"test": mock_appd_client}


# ---------------------------------------------------------------------------
# Fixtures: TokenManager mock
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_token_manager():
    tm = MagicMock()
    tm.get_token.return_value = "Bearer mock-token-xyz"
    tm.token_expiry_human.return_value = "2h 30m"
    tm.get_user_role = AsyncMock(return_value="VIEW")
    return tm


@pytest.fixture
def mock_token_managers(mock_token_manager):
    return {"test": mock_token_manager}
