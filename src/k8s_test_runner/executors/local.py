"""Run suites as local subprocesses, for development and demos without a cluster.

It writes the same log format as the Kubernetes wrapper (JUnit between marker lines), so the API,
the results parser and the dashboard behave identically. It is not isolated: use it only for
suites you trust.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..config import Settings
from ..manifest import new_run_id
from ..models import Run, Status
from ..results import BEGIN, END
from ..selector import parse_selector
from .base import RunFinished, RunNotFound, TooManyRuns, UnknownSuite

_ENV_KEEP = ("PATH", "SYSTEMROOT", "SystemRoot", "HOME", "USERPROFILE", "TEMP", "TMP", "LANG", "PYTHONIOENCODING")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


class LocalExecutor:
    name = "local"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.settings.work_dir.mkdir(parents=True, exist_ok=True)
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def _dir(self, run_id: str) -> Path:
        path = (self.settings.work_dir / run_id).resolve()
        if path.parent != self.settings.work_dir.resolve():
            raise RunNotFound(run_id)
        return path

    def _meta(self, run_id: str) -> dict:
        path = self._dir(run_id) / "meta.json"
        if not path.is_file():
            raise RunNotFound(run_id)
        return json.loads(path.read_text(encoding="utf-8"))

    def _save(self, run_id: str, meta: dict) -> None:
        path = self._dir(run_id) / "meta.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta), encoding="utf-8")
        tmp.replace(path)

    def _run(self, meta: dict) -> Run:
        status = Status(meta["status"])
        if status in (Status.PENDING, Status.RUNNING) and meta["id"] not in self._procs:
            status, meta["message"] = Status.UNKNOWN, "The server restarted while this run was in progress"
        return Run(**{**meta, "status": status})

    def _active(self) -> int:
        with self._lock:
            return sum(1 for p in self._procs.values() if p.poll() is None)

    def submit(self, suite: str, selector: str | None) -> Run:
        spec = self.settings.suites.get(suite)
        if spec is None:
            raise UnknownSuite(suite)
        if not spec.local_command:
            raise UnknownSuite(f"{suite} (no local_command configured for the local executor)")
        args = parse_selector(selector, spec)
        if self._active() >= self.settings.max_active_runs:
            raise TooManyRuns(f"Run limit reached ({self.settings.max_active_runs})")

        run_id = new_run_id()
        run_dir = self._dir(run_id)
        run_dir.mkdir(parents=True)
        junit = run_dir / "junit.xml"
        argv = [t.replace("{python}", sys.executable).replace("{junit}", str(junit)) for t in spec.local_command] + args
        meta = {"id": run_id, "suite": suite, "selector": selector or "", "status": "pending",
                "created_at": _now().isoformat(), "started_at": None, "finished_at": None,
                "exit_code": None, "message": "", "runner": ""}
        log = (run_dir / "output.log").open("wb")
        env = {k: v for k, v in os.environ.items() if k in _ENV_KEEP} | spec.env
        kwargs: dict = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
                        else {"start_new_session": True})
        try:
            proc = subprocess.Popen(argv, cwd=(self.settings.root / spec.local_cwd), env=env,
                                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, **kwargs)
        except OSError as exc:
            log.close()
            meta.update(status="failed", finished_at=_now().isoformat(), message=f"Could not start: {exc}")
            self._save(run_id, meta)
            return self._run(meta)
        meta.update(status="running", started_at=_now().isoformat(), runner=str(proc.pid))
        self._save(run_id, meta)
        with self._lock:
            self._procs[run_id] = proc
        threading.Thread(target=self._watch, args=(run_id, proc, log, junit, spec.timeout_seconds),
                         daemon=True).start()
        return self._run(meta)

    def _watch(self, run_id: str, proc: subprocess.Popen, log, junit: Path, timeout: int) -> None:
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            proc.wait()
        log.close()
        if junit.is_file():  # same log format as the Kubernetes wrapper
            payload = base64.b64encode(junit.read_bytes()).decode()
            with (self._dir(run_id) / "output.log").open("a", encoding="utf-8") as fh:
                fh.write(f"\n{BEGIN}\n{payload}\n{END}\n")
        meta = self._meta(run_id)
        if run_id in self._cancelled:
            status, message = Status.CANCELLED, "Cancelled by request"
        elif timed_out:
            status, message = Status.TIMED_OUT, "Exceeded the suite's time limit"
        elif proc.returncode == 0:
            status, message = Status.SUCCEEDED, ""
        else:
            status, message = Status.FAILED, f"The tests failed (exit code {proc.returncode})"
        meta.update(status=status.value, finished_at=_now().isoformat(), exit_code=proc.returncode, message=message)
        self._save(run_id, meta)

    def get(self, run_id: str) -> Run:
        return self._run(self._meta(run_id))

    def list(self, limit: int = 20) -> list[Run]:
        ids = sorted((p.name for p in self.settings.work_dir.iterdir() if (p / "meta.json").is_file()), reverse=True)
        return [self._run(self._meta(i)) for i in ids[:limit]]

    def logs(self, run_id: str, tail_lines: int = 500) -> str:
        self._meta(run_id)
        path = self._dir(run_id) / "output.log"
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        return "\n".join(text.splitlines()[-tail_lines:])

    def cancel(self, run_id: str) -> Run:
        run = self.get(run_id)
        if run.status.finished:
            raise RunFinished(f"Run {run_id} has already finished")
        self._cancelled.add(run_id)
        proc = self._procs.get(run_id)
        if proc is not None:
            _kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            for _ in range(40):  # let the watcher thread record the final state
                if self.get(run_id).status.finished:
                    break
                time.sleep(0.05)
        return self.get(run_id)
