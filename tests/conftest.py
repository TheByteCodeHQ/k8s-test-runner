from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from kubernetes import client as k8s

from k8s_test_runner.config import Settings, Suite
from k8s_test_runner.manifest import LABEL_RUN

ROOT = Path(__file__).resolve().parent.parent


def make_suite(**overrides) -> Suite:
    base = dict(
        name="sample", image="ktr-sample-suite:dev", description="sample", allow_selector=True,
        command=["python", "-m", "pytest", "-q", "--junitxml={junit}"], timeout_seconds=300,
        local_command=["{python}", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml={junit}"],
        local_cwd="examples/sample-suite")
    base.update(overrides)
    return Suite(**base)


def make_settings(tmp_path: Path, *, executor: str = "kubernetes", **overrides) -> Settings:
    base = dict(suites={"sample": make_suite()}, executor=executor, namespace="test-runner",
                work_dir=tmp_path / "work", root=ROOT, max_active_runs=3)
    base.update(overrides)
    return Settings(**base)


class FakeCluster:
    """Just enough of the Kubernetes batch and core APIs, with helpers to move a run through its life."""

    def __init__(self) -> None:
        self.jobs: dict[str, k8s.V1Job] = {}
        self.pods: dict[str, k8s.V1Pod] = {}
        self.logs: dict[str, str] = {}
        self.patches: list[tuple[str, dict]] = []
        self.batch = SimpleNamespace(
            create_namespaced_job=self._create, list_namespaced_job=self._list_jobs,
            patch_namespaced_job=self._patch)
        self.core = SimpleNamespace(
            list_namespaced_pod=self._list_pods, read_namespaced_pod_log=self._read_log)

    # -- API surface used by the executor --
    def _create(self, namespace, job):
        job.metadata.creation_timestamp = datetime.now(timezone.utc)
        job.status = k8s.V1JobStatus()
        self.jobs[job.metadata.name] = job
        return job

    def _matching(self, label_selector):
        key, _, value = (label_selector or "").partition("=")
        return [j for j in self.jobs.values() if not key or (j.metadata.labels or {}).get(key) == value]

    def _list_jobs(self, namespace, label_selector=None):
        return SimpleNamespace(items=self._matching(label_selector))

    def _patch(self, name, namespace, patch):
        self.patches.append((name, patch))
        job = self.jobs[name]
        job.metadata.annotations = {**(job.metadata.annotations or {}), **patch["metadata"]["annotations"]}
        job.spec.suspend = patch["spec"]["suspend"]

    def _list_pods(self, namespace, label_selector=None):
        key, _, value = (label_selector or "").partition("=")
        return SimpleNamespace(items=[p for p in self.pods.values() if (p.metadata.labels or {}).get(key) == value])

    def _read_log(self, name, namespace, container=None, tail_lines=None):
        text = self.logs.get(name, "")
        return "\n".join(text.splitlines()[-tail_lines:]) if tail_lines else text

    # -- test helpers --
    def job_for(self, run_id: str) -> k8s.V1Job:
        return next(j for j in self.jobs.values() if j.metadata.labels[LABEL_RUN] == run_id)

    def _ensure_pod(self, run_id: str, phase: str, waiting: str | None = None, exit_code: int | None = None):
        job = self.job_for(run_id)
        state = k8s.V1ContainerState()
        if waiting:
            state = k8s.V1ContainerState(waiting=k8s.V1ContainerStateWaiting(reason=waiting, message="back-off"))
        if exit_code is not None:
            state = k8s.V1ContainerState(terminated=k8s.V1ContainerStateTerminated(exit_code=exit_code, reason="Error"))
        pod = k8s.V1Pod(
            metadata=k8s.V1ObjectMeta(name=f"{job.metadata.name}-abcde", labels=dict(job.metadata.labels),
                                      creation_timestamp=datetime.now(timezone.utc)),
            status=k8s.V1PodStatus(phase=phase, container_statuses=[
                k8s.V1ContainerStatus(name="tests", image="x", image_id="x", ready=False, restart_count=0,
                                      state=state)]))
        self.pods[pod.metadata.name] = pod
        return pod

    def start(self, run_id, phase="Running", waiting=None):
        job = self.job_for(run_id)
        job.status = k8s.V1JobStatus(active=1, start_time=datetime.now(timezone.utc))
        return self._ensure_pod(run_id, phase, waiting)

    def finish(self, run_id, *, exit_code=0, log="", reason=None):
        job = self.job_for(run_id)
        now = datetime.now(timezone.utc)
        ok = exit_code == 0
        cond = k8s.V1JobCondition(type="Complete" if ok else "Failed", status="True", reason=reason,
                                  last_transition_time=now, message="Job has reached the specified backoff limit")
        job.status = k8s.V1JobStatus(start_time=now, conditions=[cond],
                                     completion_time=now if ok else None, succeeded=1 if ok else None,
                                     failed=None if ok else 1)
        pod = self._ensure_pod(run_id, "Succeeded" if ok else "Failed", exit_code=exit_code)
        self.logs[pod.metadata.name] = log


@pytest.fixture
def cluster() -> FakeCluster:
    return FakeCluster()
