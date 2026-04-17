from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


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