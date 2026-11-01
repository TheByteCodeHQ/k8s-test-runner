import base64

import pytest

from k8s_test_runner.executors.base import RunFinished, RunNotFound, TooManyRuns, UnknownSuite
from k8s_test_runner.executors.kubernetes import KubernetesExecutor
from k8s_test_runner.models import Status
from k8s_test_runner.results import BEGIN, END
from k8s_test_runner.selector import SelectorError

from .conftest import make_settings


@pytest.fixture
def executor(tmp_path, cluster):
    return KubernetesExecutor(make_settings(tmp_path), batch=cluster.batch, core=cluster.core)


def test_submit_creates_a_labelled_job_and_reports_pending(executor, cluster):
    run = executor.submit("sample", "-k checkout")
    assert run.status == Status.PENDING and run.suite == "sample" and run.selector == "-k checkout"
    assert cluster.job_for(run.id).metadata.name == run.runner


def test_run_moves_through_pending_running_succeeded(executor, cluster):
    run = executor.submit("sample", None)
    cluster.start(run.id, phase="Pending", waiting="ImagePullBackOff")
    pending = executor.get(run.id)
    assert pending.status == Status.PENDING and "ImagePullBackOff" in pending.message

    cluster.start(run.id)
    assert executor.get(run.id).status == Status.RUNNING

    cluster.finish(run.id, exit_code=0)
    done = executor.get(run.id)
    assert done.status == Status.SUCCEEDED and done.exit_code == 0 and done.finished_at is not None


def test_failed_tests_are_a_failed_run_with_the_exit_code(executor, cluster):
    run = executor.submit("sample", None)
    cluster.finish(run.id, exit_code=1)
    failed = executor.get(run.id)
    assert failed.status == Status.FAILED and failed.exit_code == 1
    assert failed.message == "The tests failed (exit code 1)"   # not Kubernetes' "backoff limit" wording


def test_deadline_exceeded_is_a_timeout(executor, cluster):
    run = executor.submit("sample", None)
    cluster.finish(run.id, exit_code=137, reason="DeadlineExceeded")
    assert executor.get(run.id).status == Status.TIMED_OUT


def test_cancel_suspends_the_job_and_keeps_it_visible(executor, cluster):
    run = executor.submit("sample", None)
    cluster.start(run.id)
    cancelled = executor.cancel(run.id)
    assert cancelled.status == Status.CANCELLED
    name, patch = cluster.patches[0]
    assert patch["spec"] == {"suspend": True} and patch["metadata"]["annotations"] == {"ktr/cancelled": "true"}
    assert executor.list()[0].status == Status.CANCELLED


def test_cannot_cancel_a_finished_run(executor, cluster):
    run = executor.submit("sample", None)
    cluster.finish(run.id, exit_code=0)
    with pytest.raises(RunFinished):
        executor.cancel(run.id)


def test_active_run_limit(executor, cluster):
    for _ in range(3):
        executor.submit("sample", None)
    with pytest.raises(TooManyRuns):
        executor.submit("sample", None)


def test_finished_runs_do_not_count_against_the_limit(executor, cluster):
    runs = [executor.submit("sample", None) for _ in range(3)]
    cluster.finish(runs[0].id)
    assert executor.submit("sample", None).status == Status.PENDING


def test_rejects_unknown_suites_and_bad_selectors_before_touching_the_cluster(executor, cluster):
    with pytest.raises(UnknownSuite):
        executor.submit("nope", None)
    with pytest.raises(SelectorError):
        executor.submit("sample", "--junitxml=/etc/x")
    assert cluster.jobs == {}


def test_unknown_run(executor):
    with pytest.raises(RunNotFound):
        executor.get("r000000000000-ffff")


def test_logs_come_from_the_runs_pod(executor, cluster):
    run = executor.submit("sample", None)
    assert executor.logs(run.id) == ""            # no pod yet
    cluster.finish(run.id, log="line1\nline2\nline3")
    assert executor.logs(run.id, tail_lines=2) == "line2\nline3"


def test_list_is_newest_first(executor, cluster):
    first = executor.submit("sample", None)
    second = executor.submit("sample", None)
    assert [r.id for r in executor.list()] == [second.id, first.id]


def test_results_payload_round_trip_through_the_log(executor, cluster):
    from k8s_test_runner.results import extract_junit

    run = executor.submit("sample", None)
    xml = '<testsuite><testcase name="t"/></testsuite>'
    cluster.finish(run.id, log=f"out\n{BEGIN}\n{base64.b64encode(xml.encode()).decode()}\n{END}")
    assert extract_junit(executor.logs(run.id, tail_lines=20)) == xml
