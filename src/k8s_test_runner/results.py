"""Get JUnit results out of a finished run.

A pod's filesystem is gone once it ends, so the Job's wrapper prints the JUnit XML to the end of the
container log between two marker lines (base64, on one line). The control plane reads it back from
the log. That needs no volume, object store or sidecar, and the control plane stays stateless.
"""

from __future__ import annotations

import base64
import binascii
import xml.etree.ElementTree as ET

from .models import Results, TestResult

BEGIN = "::ktr-junit-begin::"
END = "::ktr-junit-end::"
MAX_RESULTS = 2000
MAX_TEXT = 4000


def extract_junit(log: str) -> str | None:
    """Return the JUnit XML embedded in a log, or None if the run never produced one."""
    lines = log.splitlines()
    try:
        start = max(i for i, line in enumerate(lines) if line.strip() == BEGIN)
        end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == END)
    except ValueError:
        return None
    except StopIteration:
        return None
    payload = "".join(lines[start + 1:end]).strip()
    try:
        return base64.b64decode(payload, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return None


def strip_junit(log: str) -> str:
    """Hide the base64 payload so people reading the log see the test output, not an encoded blob."""
    kept, hidden = [], False
    for line in log.splitlines():
        marker = line.strip()
        if marker == BEGIN:
            hidden = True
            kept.append("[JUnit results captured]")
        elif marker == END:
            hidden = False
        elif not hidden:
            kept.append(line)
    return "\n".join(kept)


def _clip(text: str) -> str:
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT] + f"\n[... {len(text) - MAX_TEXT} characters omitted ...]"


def parse_junit(xml_text: str, run_id: str) -> Results:
    root = ET.fromstring(xml_text)
    results: list[TestResult] = []
    for case in root.iter("testcase"):
        classname, name = case.get("classname", ""), case.get("name", "")
        outcome, message, details = "passed", "", ""
        for tag, label in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            child = case.find(tag)
            if child is not None:
                outcome, message, details = label, child.get("message", ""), (child.text or "").strip()
                break
        results.append(TestResult(
            test_id=f"{classname}::{name}" if classname else name, outcome=outcome,
            duration=float(case.get("time", "0") or 0), message=_clip(message), details=_clip(details)))
    counts = {k: sum(1 for r in results if r.outcome == k) for k in ("passed", "failed", "error", "skipped")}
    return Results(
        run_id=run_id, total=len(results), **counts,
        duration_seconds=round(sum(r.duration for r in results), 3),
        results=results[:MAX_RESULTS], truncated=len(results) > MAX_RESULTS)
