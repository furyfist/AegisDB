from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field
import uuid

class FailureSeverity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

class FailureCategory(str, Enum):
    NULL_VIOLATION = "null_violation"
    RANGE_VIOLATION = "range_violation"
    UNIQUENESS_VIOLATION = "uniqueness_violation"
    REFERENTIAL_INTEGRITY = "referential_integrity"
    FORMAT_VIOLATION = "format_violation"
    SCHEMA_DRIFT = "schema_drift"
    UNKNOWN = "unknown"

class OMWebhookPayload(BaseModel):
    eventType: str
    entityType: str
    entityFQN: str | None = None
    entity: dict[str, Any] | None = None
    changeDescription: dict[str, Any] | None = None
    timestamp: int = Field(default_factory=lambda: int(datetime.now().timestamp()))

class ColumnInfo(BaseModel):
    name: str
    dataType: str
    nullable: bool = True
    description: str | None = None

class TableContext(BaseModel):
    table_fqn: str
    table_name: str
    database: str
    schema_name: str
    columns: list[ColumnInfo] = []
    upstream_tables: list[str] = []
    downstream_tables: list[str] = []
    row_count: int | None = None

class FailedTest(BaseModel):
    test_name: str
    test_fqn: str
    column_name: str | None = None
    failure_reason: str | None = None
    status: TestStatus


class TestStatus(str, Enum):
    SUCCESS = "Success"
    FAILED = "Failed"
    ABORTED = "Aborted"


# Raw payload shape OpenMetadata sends on webhook
class OMTestCaseResult(BaseModel):
    testCaseName: str
    testCaseFQN: str
    status: TestStatus
    result: str | None = None
    sampleData: str | None = None
    timestamp: int


class OMWebhookPayload(BaseModel):
    """Raw OpenMetadata webhook payload — we only care about test failure events."""
    eventType: str
    entityType: str
    entityFQN: str | None = None
    entity: dict[str, Any] | None = None
    changeDescription: dict[str, Any] | None = None
    timestamp: int = Field(default_factory=lambda: int(datetime.now().timestamp()))


# Enriched event — what we publish to Redis after OM API enrichment
class ColumnInfo(BaseModel):
    name: str
    dataType: str
    nullable: bool = True
    description: str | None = None


class TableContext(BaseModel):
    """Schema + lineage context pulled from OM API."""
    table_fqn: str
    table_name: str
    database: str
    schema_name: str
    columns: list[ColumnInfo] = []
    upstream_tables: list[str] = []
    downstream_tables: list[str] = []
    row_count: int | None = None


class FailedTest(BaseModel):
    test_name: str
    test_fqn: str
    column_name: str | None = None
    failure_reason: str | None = None
    status: TestStatus


class EnrichedFailureEvent(BaseModel):
    """
    The canonical event shape published to Redis Streams.
    Every downstream agent (Detector, Diagnosis, Repair) reads this.
    """
    event_id: str
    received_at: datetime = Field(default_factory=datetime.now)

    # What failed
    failed_tests: list[FailedTest] = []
    table_fqn: str

    # Enriched context from OM API
    table_context: TableContext | None = None

    # Severity assigned by detector
    severity: FailureSeverity = FailureSeverity.MEDIUM

    # Raw payload preserved for debugging
    raw_payload: dict[str, Any] = {}

    # Processing state
    enrichment_success: bool = False
    enrichment_error: str | None = None

class DetectorResult(BaseModel):
    """Output of the Detector agent."""
    event_id: str
    failure_categories: list[FailureCategory]
    affected_columns: list[str]
    severity: FailureSeverity
    is_actionable: bool           # False → skip LLM, go straight to escalation
    detector_notes: str = ""


class SimilarFix(BaseModel):
    """A past fix retrieved from ChromaDB."""
    fix_id: str
    table_fqn: str
    failure_category: str
    problem_description: str
    fix_sql: str
    was_successful: bool
    similarity_score: float


class RepairProposal(BaseModel):
    """
    Structured repair plan produced by the Diagnosis agent.
    Consumed by the Repair agent in Phase 4.
    """
    fix_sql: str                  # executable SQL
    fix_description: str          # human-readable explanation
    affected_columns: list[str]
    is_reversible: bool           # does a safe rollback exist?
    rollback_sql: str | None = None
    estimated_rows_affected: int | None = None


class DiagnosisResult(BaseModel):
    """
    Full output of the Diagnosis agent.
    Published to aegisdb:repair or aegisdb:escalation stream.
    """
    event_id: str
    diagnosed_at: datetime = Field(default_factory=datetime.now)

    # Classification
    failure_categories: list[FailureCategory]
    root_cause: str               # plain English explanation

    # Decision gate
    confidence: float             # 0.0 – 1.0
    is_repairable: bool           # confidence >= threshold

    # Repair plan (only if is_repairable=True)
    repair_proposal: RepairProposal | None = None

    # Context used
    similar_fixes_used: list[SimilarFix] = []
    llm_reasoning: str = ""       # raw LLM chain-of-thought

    # Escalation reason (only if is_repairable=False)
    escalation_reason: str | None = None

class TestAssertionResult(BaseModel):
    """Result of re-running one test assertion inside the sandbox."""
    test_name: str
    column_name: str | None
    passed: bool
    detail: str = ""


class DataDiff(BaseModel):
    """Before/after snapshot for audit trail."""
    rows_before: int
    rows_after: int
    rows_deleted: int = 0
    rows_updated: int = 0
    sample_before: list[dict] = []
    sample_after: list[dict] = []


class SandboxResult(BaseModel):
    """Full output of the sandbox execution."""
    event_id: str
    executed_at: datetime = Field(default_factory=datetime.now)

    # What was run
    fix_sql_executed: str
    table_name: str

    # Did it work?
    sandbox_passed: bool
    test_assertions: list[TestAssertionResult] = []
    data_diff: DataDiff | None = None

    # Retry tracking
    attempt: int = 1
    error: str | None = None

    # Ready to apply?
    approved_for_production: bool = False


class RepairDecision(BaseModel):
    """
    Final decision record — what gets published to aegisdb:apply.
    The apply agent in Phase 5 reads this.
    """
    event_id: str
    decided_at: datetime = Field(default_factory=datetime.now)

    table_fqn: str
    table_name: str

    fix_sql: str
    rollback_sql: str | None = None
    fix_description: str

    sandbox_result: SandboxResult
    diagnosis_result_json: str  # serialized DiagnosisResult

    approved: bool
    rejection_reason: str | None = None

    # Data snapshots for UI/audit
    sample_before: list[dict] = []
    sample_after: list[dict] = []

    # Audit
    dry_run: bool = True

# ── Phase 5 models ────────────────────────────────────────────────────────────

class ApplyAction(str, Enum):
    APPLIED = "applied"
    DRY_RUN = "dry_run"
    ROLLED_BACK = "rolled_back"
    SKIPPED = "skipped"
    FAILED = "failed"


class AuditEntry(BaseModel):
    """
    Persisted to _aegisdb_audit table in the target DB.
    One row per fix attempt — the full decision trail.
    """
    event_id: str
    table_fqn: str
    table_name: str
    action: ApplyAction
    fix_sql: str = ""
    rollback_sql: str | None = None
    rows_affected: int = 0
    dry_run: bool = True
    sandbox_passed: bool = False
    confidence: float = 0.0
    failure_categories: list[str] = []
    applied_at: datetime = Field(default_factory=datetime.now)
    post_apply_assertions: list[dict] = []
    error: str | None = None


class ApplyResult(BaseModel):
    """Final outcome of the apply agent for one event."""
    event_id: str
    table_fqn: str
    action: ApplyAction
    rows_affected: int = 0
    post_apply_passed: bool = False
    post_apply_assertions: list[TestAssertionResult] = []
    audit_id: int | None = None
    error: str | None = None
    completed_at: datetime = Field(default_factory=datetime.now)

# ── Profiling Engine 

class AnomalySeverity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"


class ColumnAnomaly(BaseModel):
    """
    One detected anomaly on one column.
    Maps directly to a FailureCategory so the existing
    Detector → Diagnosis → Repair pipeline can process it.
    """
    column_name:    str
    anomaly_type:   FailureCategory
    severity:       AnomalySeverity
    affected_rows:  int
    total_rows:     int
    rate:           float            # affected_rows / total_rows
    description:    str
    sample_values:  list[Any] = []  # up to 5 example bad values


class TableProfile(BaseModel):
    """Profiling result for a single table."""
    table_name:            str
    schema_name:           str
    total_rows:            int
    total_columns:         int
    anomalies:             list[ColumnAnomaly] = []
    profiled_at:           datetime = Field(default_factory=datetime.now)
    profiling_duration_ms: int = 0


class ProfilingReport(BaseModel):
    """
    Full profiling output for a database connection.
    Returned by POST /api/v1/profile and stored in _aegisdb_profiling_reports.
    """
    report_id:       str = Field(default_factory=lambda: str(uuid.uuid4()) if True else "")
    connection_hint: str = ""        # host:port/db — no credentials stored
    profiled_at:     datetime = Field(default_factory=datetime.now)
    tables_scanned:  int = 0
    total_anomalies: int = 0
    critical_count:  int = 0
    warning_count:   int = 0
    tables:          list[TableProfile] = []
    status:          str = "completed"  # completed | failed | partial
    error:           str | None = None
    duration_ms:     int = 0

# ── Onboarding 

class ConnectionStatus(str, Enum):
    PENDING    = "pending"
    CONNECTED  = "connected"
    INGESTING  = "ingesting"
    PROFILING  = "profiling"
    READY      = "ready"
    FAILED     = "failed"


class DatabaseConnection(BaseModel):
    """
    A registered database connection in AegisDB.
    Credentials are never stored — only the connection URL hint.
    """
    connection_id:    str = Field(
        default_factory=lambda: str(uuid.uuid4())
    )
    service_name:     str           # name registered in OpenMetadata
    connection_hint:  str           # host:port/db — no credentials
    db_name:          str
    schema_names:     list[str] = ["public"]
    status:           ConnectionStatus = ConnectionStatus.PENDING
    om_service_fqn:   str = ""      # FQN assigned by OM
    profiling_report_id: str | None = None
    tables_found:     int = 0
    total_anomalies:  int = 0
    critical_count:   int = 0
    registered_at:    datetime = Field(default_factory=datetime.now)
    last_profiled_at: datetime | None = None
    error:            str | None = None


class ConnectRequest(BaseModel):
    """Body for POST /api/v1/connect"""
    host:         str
    port:         int = 5432
    database:     str
    username:     str
    password:     str
    schemas:      list[str] = ["public"]
    service_name: str | None = None  # auto-generated if not provided


class ConnectResponse(BaseModel):
    connection_id:   str
    service_name:    str
    connection_hint: str
    status:          ConnectionStatus
    tables_found:    int
    total_anomalies: int
    critical_count:  int
    profiling_report_id: str | None
    message:         str

# ── Phase D models — Approval Workflow 

class ProposalStatus(str, Enum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED         = "approved"
    REJECTED         = "rejected"
    EXECUTING        = "executing"   # repair agent picked it up
    COMPLETED        = "completed"   # apply agent finished
    FAILED           = "failed"


class RepairProposalRecord(BaseModel):
    """
    Created by the diagnosis consumer when confidence >= threshold.
    Sits pending until a user explicitly approves or rejects it.
    On approval the full DiagnosisResult is published to aegisdb:repair
    and the existing repair → sandbox → apply pipeline runs unchanged.
    """
    proposal_id:        str = Field(
        default_factory=lambda: str(uuid.uuid4())
    )
    event_id:           str
    table_fqn:          str
    table_name:         str
    failure_categories: list[str] = []
    root_cause:         str
    confidence:         float
    fix_sql:            str
    fix_description:    str
    rollback_sql:       str | None = None
    estimated_rows:     int | None = None

    # Sandbox preview — shown to user before they approve
    sandbox_passed:     bool = False
    rows_before:        int = 0
    rows_after:         int = 0
    rows_affected:      int = 0
    sample_before:      list[dict] = []
    sample_after:       list[dict] = []

    status:             ProposalStatus = ProposalStatus.PENDING_APPROVAL
    created_at:         datetime = Field(default_factory=datetime.now)
    decided_at:         datetime | None = None
    decision_by:        str = "system"
    rejection_reason:   str | None = None

    # Full serialized objects for pipeline re-entry on approval
    diagnosis_json:     str = ""   # serialized DiagnosisResult
    event_json:         str = ""   # serialized EnrichedFailureEvent