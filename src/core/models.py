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