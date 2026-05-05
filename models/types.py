"""
models/types.py

Pydantic v2 models for all tool inputs/outputs and domain types.

Design decisions:
- Tool input models use Pydantic so FastMCP can auto-generate JSON Schema
  for the MCP protocol's tool-call validation.
- Domain output types (StackFrame, Runbook, HealthStatus) use @dataclass
  because they are internal structures passed between modules, not
  validated at API boundaries. Dataclasses are lighter and serialise
  cleanly via dataclasses.asdict().
- model_config extra="ignore" on all Pydantic models: AppD SaaS updates
  silently — extra fields must not crash parsing.
- All camelCase AppD JSON fields are mapped with Field(alias=...) so
  Python code uses snake_case throughout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class StackLanguage(StrEnum):
    JAVA = "java"
    NODEJS = "nodejs"
    PYTHON = "python"
    DOTNET = "dotnet"
    UNKNOWN = "unknown"


class AppDRole(StrEnum):
    VIEW = "VIEW"
    TROUBLESHOOT = "TROUBLESHOOT"
    CONFIGURE_ALERTING = "CONFIGURE_ALERTING"
    DENIED = "DENIED"


class Criticality(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class BTType(StrEnum):
    DATA_HEAVY_READ = "data-heavy-read"
    EXTERNAL_DEPENDENCY_RISK = "external-dependency-risk"
    HIGH_FREQUENCY_LIGHTWEIGHT = "high-frequency-lightweight"
    EXPENSIVE_INFREQUENT = "expensive-infrequent"
    STANDARD = "standard"


class ConfidenceScore(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class DegradationMode(StrEnum):
    FULL = "FULL"
    NO_ANALYTICS = "NO_ANALYTICS"
    NO_EUM = "NO_EUM"
    NO_SNAPSHOTS = "NO_SNAPSHOTS"
    READONLY_CACHE = "READONLY_CACHE"


# ---------------------------------------------------------------------------
# Internal dataclasses (not Pydantic — passed between modules)
# ---------------------------------------------------------------------------


@dataclass
class TokenCache:
    access_token: str
    expires_at: datetime
    refresh_scheduled_at: datetime


@dataclass
class StackFrame:
    class_name: str
    method_name: str
    file_name: str
    line_number: int
    is_app_frame: bool


@dataclass
class ParsedStack:
    language: StackLanguage
    culprit_frame: StackFrame | None       # First app-owned frame
    caused_by_chain: list[str]             # "Caused by:" lines
    top_app_frames: list[StackFrame]       # First 5 app-owned frames
    full_stack_preview: str                # Top 5 lines for context


@dataclass
class SmokingGunReport:
    culprit_class: str
    culprit_method: str
    culprit_line: int
    culprit_file: str
    deviation: str
    exception: str
    suggested_fix: str
    confidence_score: ConfidenceScore
    confidence_reasoning: str
    exclusive_methods: list[str]
    latency_deviations: list[dict[str, Any]]
    golden_snapshot_guid: str
    golden_selection_reason: str


@dataclass
class Runbook:
    id: str                                # uuid4
    generated_at: str                      # ISO8601 UTC
    incident: str                          # "{app} - {bt} - {issue}"
    root_cause: str
    confidence_score: str
    investigation_steps: list[str]
    tool_results: dict[str, Any]
    resolution: str
    prevention_recommendation: str
    snapshots_archived: list[str]
    affected_users: str | None = None
    ticket_ref: None = None                # Phase 2 — always None


@dataclass
class HealthStatus:
    status: str                            # healthy | degraded | unhealthy
    version: str
    vault: str                             # connected | unreachable
    controllers: dict[str, str]            # name → reachable | unreachable
    token_expiry: str                      # "2h 14m"
    degradation_mode: str
    cache_hit_rate: str
    requests_last_hour: int
    active_users: int
    licensed_modules: list[str]
    disabled_tools: list[str]


@dataclass
class LicenseState:
    eum: bool = False
    database_visibility: bool = False
    analytics: bool = False
    snapshots: bool = True


@dataclass
class ControllerConfig:
    name: str
    url: str
    account: str
    global_account: str
    timezone: str
    app_package_prefix: str
    analytics_url: str
    vault_path: str = ""        # enterprise: vault path for data account creds
    rbac_vault_path: str = ""   # enterprise: vault path for RBAC admin creds


# ---------------------------------------------------------------------------
# Pydantic models — AppD API response shapes (validated at boundary)
# ---------------------------------------------------------------------------


class AppDApplication(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    name: str
    description: str = ""
    account_guid: str = Field("", alias="accountGuid")


class BusinessTransaction(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    name: str
    entry_point_type: str = Field("", alias="entryPointType")
    avg_response_time_ms: float = Field(0.0, alias="avgResponseTime")
    calls_per_minute: float = Field(0.0, alias="callsPerMinute")
    error_rate: float = Field(0.0, alias="errorRate")
    db_call_count: int = Field(0, alias="dbCallCount")
    external_call_count: int = Field(0, alias="externalCallCount")
    tier_name: str = Field("", alias="tierName")


class MetricDataPoint(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    start_time_ms: int = Field(0, alias="startTimeInMillis")
    value: float = 0.0
    min_val: float = Field(0.0, alias="min")
    max_val: float = Field(0.0, alias="max")
    count: int = 0


class HealthViolation(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int = 0
    name: str = ""
    type: str = ""
    severity: str = "WARNING"
    affected_entity_name: str = Field("", alias="affectedEntityName")
    affected_entity_type: str = Field("", alias="affectedEntityType")
    start_time: int = Field(0, alias="startTime")
    end_time: int | None = Field(None, alias="endTime")
    resolved: bool = False


class SnapshotSummary(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    request_guid: str = Field("", alias="requestGUID")
    bt_name: str = Field("", alias="businessTransactionName")
    response_time_ms: float = Field(0.0, alias="timeTakenInMilliSecs")
    error_occurred: bool = Field(False, alias="errorOccurred")
    timestamp: int = Field(0, alias="serverStartTime")

    @property
    def snapshot_guid(self) -> str:
        return self.request_guid


class AppDException(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    exception_type: str = Field("", alias="name")
    count: int = 0
    first_occurrence: int = Field(0, alias="firstOccurrenceTime")
    last_occurrence: int = Field(0, alias="lastOccurrenceTime")

    @property
    def is_stale(self) -> bool:
        return self.count == 0


# ---------------------------------------------------------------------------
# Pydantic models — Tool inputs (used by FastMCP for JSON Schema)
# ---------------------------------------------------------------------------


class ListApplicationsInput(BaseModel):
    controller_name: str = "production"


class SearchMetricTreeInput(BaseModel):
    app_name: str
    path: str = ""
    controller_name: str = "production"


class GetMetricsInput(BaseModel):
    app_name: str
    metric_path: str
    duration_mins: int = 60
    controller_name: str = "production"


class GetBusinessTransactionsInput(BaseModel):
    app_name: str
    controller_name: str = "production"
    include_health_checks: bool = False
    page_size: int = 50
    page_offset: int = 0


class GetBtBaselineInput(BaseModel):
    app_name: str
    bt_name: str
    duration_mins: int = 60
    controller_name: str = "production"


class LoadApiSpecInput(BaseModel):
    spec_url: str
    app_name: str
    controller_name: str = "production"


class ListSnapshotsInput(BaseModel):
    app_name: str
    bt_name: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    error_only: bool = False
    max_results: int = 10
    page_size: int = 10
    page_offset: int = 0
    controller_name: str = "production"


class AnalyzeSnapshotInput(BaseModel):
    app_name: str
    snapshot_guid: str
    controller_name: str = "production"


class CompareSnapshotsInput(BaseModel):
    app_name: str
    failed_snapshot_guid: str
    healthy_snapshot_guid: str | None = None
    controller_name: str = "production"


class ArchiveSnapshotInput(BaseModel):
    app_name: str
    snapshot_guid: str
    reason: str
    archived_by: str
    alert_ref: str | None = None
    controller_name: str = "production"


class GetHealthViolationsInput(BaseModel):
    app_name: str
    duration_mins: int = 60
    include_resolved: bool = False
    controller_name: str = "production"


class GetPoliciesInput(BaseModel):
    app_name: str
    controller_name: str = "production"


class GetInfraStatsInput(BaseModel):
    app_name: str
    tier_name: str
    node_name: str | None = None
    duration_mins: int = 60
    controller_name: str = "production"


class GetJvmDetailsInput(BaseModel):
    app_name: str
    tier_name: str
    node_name: str
    duration_mins: int = 60
    controller_name: str = "production"


class GetErrorsInput(BaseModel):
    app_name: str
    duration_mins: int = 60
    controller_name: str = "production"
    page_size: int = 50
    page_offset: int = 0


class GetDatabasePerfInput(BaseModel):
    app_name: str
    db_name: str | None = None
    duration_mins: int = 60
    controller_name: str = "production"


class GetNetworkKPIsInput(BaseModel):
    app_name: str
    source_tier: str
    dest_tier: str | None = None
    duration_mins: int = 60
    controller_name: str = "production"


class QueryAnalyticsInput(BaseModel):
    adql_query: str
    start_time: str | None = None
    end_time: str | None = None
    controller_name: str = "production"


class StitchAsyncTraceInput(BaseModel):
    correlation_id: str
    app_names: list[str]
    duration_mins: int = 60
    controller_name: str = "production"


class EUMBaseInput(BaseModel):
    app_name: str
    duration_mins: int = 60
    controller_name: str = "production"


class GetEUMPagePerfInput(BaseModel):
    app_name: str
    page_url: str | None = None
    duration_mins: int = 60
    controller_name: str = "production"


class CorrelateEUMToBTInput(BaseModel):
    app_name: str
    bt_name: str
    duration_mins: int = 60
    controller_name: str = "production"
