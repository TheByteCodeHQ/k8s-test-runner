"""Run suites as Kubernetes Jobs. Kubernetes is the only store: the control plane keeps no state.

That makes the API safe to scale to several replicas and to restart at any time. History is whatever
Jobs still exist, which ``ttl_seconds_after_finished`` bounds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from ..config import Settings
from ..manifest import (
    ANN_CANCELLED,
    ANN_SELECTOR,
    CONTAINER,
    LABEL_MANAGED,
    LABEL_RUN,
    LABEL_SUITE,
    MANAGED_BY,
    build_job,
    new_run_id,
)
from ..models import Run, Status
from ..selector import parse_selector
from .base import RunFinished, RunNotFound, TooManyRuns, UnknownSuite

MANAGED_SELECTOR = f"{LABEL_MANAGED}={MANAGED_BY}"


def _load_kube_config() -> None:
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()


def _time(value: Any) -> datetime | None:
    return value if isinstance(value, datetime) else None


def _condition(job: k8s.V1Job, kind: str) -> k8s.V1JobCondition | None:
    for cond in (job.status.conditions or []) if job.status else []:
        if cond.type == kind and cond.status == "True":
            return cond
    return None


class KubernetesExecutor:
    name = "kubernetes"

    def __init__(self, settings: Settings, batch: Any = None, core: Any = None) -> None:
        self.settings = settings
        if batch is None or core is None:
            _load_kube_config()
        self.batch = batch or k8s.BatchV1Api()
        self.core = core or k8s.CoreV1Api()

    # -- state ------------------------------------------------------------------------------

    def _to_run(self, job: k8s.V1Job, pod: k8s.V1Pod | None = None) -> Run:
        meta, status = job.metadata, job.status or k8s.V1JobStatus()
        annotations = meta.annotations or {}
        labels = meta.labels or {}
        state, message = Status.PENDING, ""

        failed, complete = _condition(job, "Failed"), _condition(job, "Complete")
        if annotations.get(ANN_CANCELLED) == "true":
            state, message = Status.CANCELLED, "Cancelled by request"
        elif complete:
            state = Status.SUCCEEDED
        elif failed:
            if failed.reason == "DeadlineExceeded":
                state, message = Status.TIMED_OUT, "Exceeded the suite's time limit"
            else:
                state, message = Status.FAILED, failed.message or ""
        elif status.active:
            state = Status.RUNNING
            if pod is not None and pod.status and pod.status.phase == "Pending":
                state = Status.PENDING
                waiting = self._waiting_reason(pod)
                message = waiting or "Waiting for the pod to start"
        elif status.failed:
            state = Status.FAILED

        exit_code = None
        if pod is not None and pod.status and pod.status.container_statuses:
            terminated = pod.status.container_statuses[0].state.terminated
            if terminated is not None:
                exit_code = terminated.exit_code
        if state == Status.FAILED and pod is not None:
            # "BackoffLimitExceeded" is Kubernetes' wording for any failed pod; say what actually happened
            if exit_code:
                message = f"The tests failed (exit code {exit_code})"
            elif self._waiting_reason(pod):
                message = self._waiting_reason(pod)

        finished = _time(status.completion_time) or (_time(failed.last_transition_time) if failed else None)
        return Run(
            id=labels.get(LABEL_RUN, ""), suite=labels.get(LABEL_SUITE, ""),
            selector=annotations.get(ANN_SELECTOR, ""), status=state,
            created_at=_time(meta.creation_timestamp), started_at=_time(status.start_time),
            finished_at=finished if state.finished else None, exit_code=exit_code, message=message,
            runner=meta.name)

    @staticmethod
    def _waiting_reason(pod: k8s.V1Pod) -> str:
        for cs in pod.status.container_statuses or []:
            waiting = cs.state.waiting if cs.state else None
            if waiting and waiting.reason:
                return f"{waiting.reason}: {waiting.message or ''}".strip(": ")
        return ""

    def _job(self, run_id: str) -> k8s.V1Job:
        jobs = self.batch.list_namespaced_job(self.settings.namespace, label_selector=f"{LABEL_RUN}={run_id}")
        if not jobs.items:
            raise RunNotFound(run_id)
        return jobs.items[0]

    def _pod(self, run_id: str) -> k8s.V1Pod | None:
        pods = self.core.list_namespaced_pod(self.settings.namespace, label_selector=f"{LABEL_RUN}={run_id}")
        items = sorted(pods.items, key=lambda p: p.metadata.creation_timestamp or datetime.min, reverse=True)
        return items[0] if items else None

    # -- Executor interface -----------------------------------------------------------------

    def _active_runs(self) -> int:
        jobs = self.batch.list_namespaced_job(self.settings.namespace, label_selector=MANAGED_SELECTOR)
        return sum(1 for j in jobs.items if self._to_run(j).status in (Status.PENDING, Status.RUNNING))

    def submit(self, suite: str, selector: str | None) -> Run:
        spec = self.settings.suites.get(suite)
        if spec is None:
            raise UnknownSuite(suite)
        args = parse_selector(selector, spec)
        active = self._active_runs()
        if active >= self.settings.max_active_runs:
            raise TooManyRuns(f"{active} runs are already active (limit {self.settings.max_active_runs})")
        job = build_job(new_run_id(), spec, args, selector or "", self.settings)
        created = self.batch.create_namespaced_job(self.settings.namespace, job)
        return self._to_run(created)

    def get(self, run_id: str) -> Run:
        return self._to_run(self._job(run_id), self._pod(run_id))

    def list(self, limit: int = 20) -> list[Run]:
        jobs = self.batch.list_namespaced_job(self.settings.namespace, label_selector=MANAGED_SELECTOR)
        ordered = sorted(jobs.items, key=lambda j: j.metadata.creation_timestamp or datetime.min, reverse=True)
        return [self._to_run(j) for j in ordered[:limit]]

    def logs(self, run_id: str, tail_lines: int = 500) -> str:
        self._job(run_id)  # raises RunNotFound
        pod = self._pod(run_id)
        if pod is None:
            return ""
        try:
            return self.core.read_namespaced_pod_log(
                pod.metadata.name, self.settings.namespace, container=CONTAINER, tail_lines=tail_lines)
        except ApiException as exc:
            if exc.status == 400:  # container not started yet
                return ""
            raise

    def cancel(self, run_id: str) -> Run:
        job = self._job(run_id)
        if self._to_run(job).status.finished:
            raise RunFinished(f"Run {run_id} has already finished")
        # Suspending a Job stops its pod but keeps the Job, so the run stays visible as cancelled.
        patch = {"metadata": {"annotations": {ANN_CANCELLED: "true"}}, "spec": {"suspend": True}}
        self.batch.patch_namespaced_job(job.metadata.name, self.settings.namespace, patch)
        return self.get(run_id)
