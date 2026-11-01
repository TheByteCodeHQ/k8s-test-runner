"""What the API needs from a place where tests run."""

from __future__ import annotations

from typing import Protocol

from ..models import Run


class RunNotFound(LookupError):
    pass


class UnknownSuite(LookupError):
    pass


class TooManyRuns(RuntimeError):
    pass


class RunFinished(RuntimeError):
    pass


class Executor(Protocol):
    name: str

    def submit(self, suite: str, selector: str | None) -> Run: ...
    def get(self, run_id: str) -> Run: ...
    def list(self, limit: int = 20) -> list[Run]: ...
    def logs(self, run_id: str, tail_lines: int = 500) -> str: ...
    def cancel(self, run_id: str) -> Run: ...
