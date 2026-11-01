"""API and executor data contracts."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def finished(self) -> bool:
        return self in (Status.SUCCEEDED, Status.FAILED, Status.TIMED_OUT, Status.CANCELLED)


class RunRequest(BaseModel):
    suite: str = Field(min_length=1, max_length=64)
    selector: str | None = Field(default=None, max_length=1000)


class Run(BaseModel):
    id: str
    suite: str
    selector: str = ""
    status: Status
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    message: str = ""
    runner: str = ""  # job name or local process id


class TestResult(BaseModel):
    __test__ = False  # not a pytest test class

    test_id: str
    outcome: str  # passed | failed | error | skipped
    duration: float = 0.0
    message: str = ""
    details: str = ""


class Results(BaseModel):
    run_id: str
    total: int
    passed: int
    failed: int
    error: int
    skipped: int
    duration_seconds: float
    results: list[TestResult]
    truncated: bool = False


class SuiteInfo(BaseModel):
    name: str
    description: str
    image: str
    accepts_selector: bool
    allowed_options: list[str]
    timeout_seconds: int
