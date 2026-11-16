"""接口请求与领域错误的数据契约。"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pydantic import BaseModel, Field, field_validator, model_validator

class DomainError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, detail: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.status, self.detail = code, message, status, detail or {}

def utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise DomainError("NAIVE_TIME", "Timestamp requires UTC offset")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")

def parse_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise DomainError("BAD_TIME", "Timestamp must be ISO 8601") from None
    if result.tzinfo is None:
        raise DomainError("NAIVE_TIME", "Timestamp requires UTC offset")
    return result.astimezone(timezone.utc)

class EventKind(str, Enum):
    ASSIGN = "assigned"
    RESPONSE = "responded"
    EXPLAIN = "explained"
    ACK = "acknowledged"
    CLOSE = "closed"
    REOPEN = "reopened"

class Resolution(str, Enum):
    SOLVED = "solved"
    EXPLAINED = "explained"
    OUT_OF_SCOPE = "out_of_scope"

class AwareModel(BaseModel):
    @field_validator("occurred_at", "submitted_at", "as_of", check_fields=False)
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Timestamp requires UTC offset")
        return value.astimezone(timezone.utc)

class CaseInput(AwareModel):
    external_id: str = Field(min_length=1, max_length=100)
    grid_id: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=5, max_length=400)
    occurred_at: datetime
    source: str = Field(default="resident", min_length=1, max_length=40)
    priority: int = Field(default=2, ge=1, le=3)
    resident_token: str = Field(min_length=4, max_length=100)

    @field_validator("external_id", "grid_id", "category", "source", "resident_token")
    @classmethod
    def clean_key(cls, value: str) -> str:
        if value != value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Identifier must be trimmed and printable")
        return value

class BatchInput(AwareModel):
    batch_key: str = Field(min_length=1, max_length=100)
    cases: list[CaseInput] = Field(min_length=1, max_length=500)
    submitted_at: datetime

class GridInput(BaseModel):
    grid_id: str = Field(min_length=1, max_length=80)
    district: str = Field(min_length=1, max_length=80)
    active: bool = True

class RouteRuleInput(BaseModel):
    category: str = Field(min_length=1, max_length=80)
    department: str = Field(min_length=1, max_length=80)
    priority: int = Field(default=100, ge=0, le=1000)
    response_hours: int = Field(default=72, ge=1, le=720)
    resolution_hours: int = Field(default=336, ge=1, le=2160)

    @model_validator(mode="after")
    def deadlines(self):
        if self.resolution_hours < self.response_hours:
            raise ValueError("Resolution deadline precedes response deadline")
        return self

class EventInput(AwareModel):
    event_key: str = Field(min_length=1, max_length=100)
    kind: EventKind
    occurred_at: datetime
    actor: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=1)
    department: str | None = Field(default=None, max_length=80)
    note: str | None = Field(default=None, max_length=500)
    resolution: Resolution | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.kind == EventKind.ASSIGN and not self.department:
            raise ValueError("Assignment requires department")
        if self.kind == EventKind.CLOSE and self.resolution is None:
            raise ValueError("Closure requires resolution")
        if self.kind in (EventKind.RESPONSE, EventKind.EXPLAIN) and not self.note:
            raise ValueError("Response requires note")
        if self.kind != EventKind.CLOSE and self.resolution is not None:
            raise ValueError("Resolution only applies to closure")
        return self

class ReportInput(AwareModel):
    report_key: str = Field(min_length=1, max_length=100)
    as_of: datetime
    district: str | None = None
    category: str | None = None
    group_by: str = "district"

    @field_validator("group_by")
    @classmethod
    def grouping(cls, value: str) -> str:
        if value not in {"district", "grid", "category", "department", "problem"}:
            raise ValueError("Unsupported grouping")
        return value

class ProblemInput(AwareModel):
    """Open a governance problem, optionally grouping existing cases."""

    title: str = Field(min_length=1, max_length=400)
    grid_id: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    department: str | None = Field(default=None, max_length=80)
    case_ids: list[int] = Field(default_factory=list, max_length=500)
    occurred_at: datetime
    actor: str = Field(default="analyst", min_length=1, max_length=80)
    operation_key: str | None = Field(default=None, min_length=1, max_length=100)

class ProblemCaseInput(AwareModel):
    case_id: int = Field(ge=1)
    occurred_at: datetime
    expected_version: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=80)
    operation_key: str | None = Field(default=None, min_length=1, max_length=100)

class AssociationInput(AwareModel):
    """A hint, never an automatic merge: similar content across sources."""

    case_id: int = Field(ge=1)
    other_case_id: int = Field(ge=1)
    reason: str = Field(default="similar_content", min_length=1, max_length=120)
    similarity: float = Field(default=1.0, ge=0, le=1)
    occurred_at: datetime
    actor: str = Field(default="system", min_length=1, max_length=80)
    operation_key: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_pair(self):
        if self.case_id == self.other_case_id:
            raise ValueError("Association needs two distinct cases")
        return self

class AssociationScanInput(AwareModel):
    occurred_at: datetime
    grid_id: str | None = Field(default=None, max_length=80)
    category: str | None = Field(default=None, max_length=80)
    threshold: float = Field(default=0.5, ge=0, le=1)
    actor: str = Field(default="system", min_length=1, max_length=80)

class AssociationResolveInput(AwareModel):
    occurred_at: datetime
    actor: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=1)
    problem_id: int | None = Field(default=None, ge=1)
    operation_key: str | None = Field(default=None, min_length=1, max_length=100)

class ProblemMergeInput(AwareModel):
    into_problem_id: int = Field(ge=1)
    occurred_at: datetime
    actor: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=1)
    expected_target_version: int | None = Field(default=None, ge=1)
    suggestion_id: int | None = Field(default=None, ge=1)
    operation_key: str | None = Field(default=None, min_length=1, max_length=100)

class ProblemSplitInput(AwareModel):
    case_ids: list[int] = Field(min_length=1, max_length=500)
    occurred_at: datetime
    actor: str = Field(min_length=1, max_length=80)
    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=400)
    department: str | None = Field(default=None, max_length=80)
    operation_key: str | None = Field(default=None, min_length=1, max_length=100)
    # Completed requests keep the record (and problem) they were handled
    # under; only obligations that are still outstanding follow the split.
    # An explicit override is possible when re-organising closed archives.
    move_completed: bool = False

class MergeUndoInput(AwareModel):
    occurred_at: datetime
    actor: str = Field(min_length=1, max_length=80)
    operation_key: str | None = Field(default=None, min_length=1, max_length=100)
    # Completed residents keep the problem they were handled under; their
    # still-outstanding neighbours return to the pre-merge problem.
    move_completed: bool = False
