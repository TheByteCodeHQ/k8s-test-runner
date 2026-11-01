import time

import pytest
from fastapi.testclient import TestClient

from k8s_test_runner.api import create_app
from k8s_test_runner.executors.kubernetes import KubernetesExecutor

from .conftest import make_settings, make_suite

AUTH = {"Authorization": "Bearer secret-token"}


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path, executor="local", suites={
        "sample": make_suite(),
        "slow": make_suite(name="slow", allow_selector=False, timeout_seconds=30,
                           local_command=["{python}", "-c", "import time; time.sleep(60)"]),
    })


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings, api_token="secret-token"))


def wait_for(client, run_id, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        run = client.get(f"/api/runs/{run_id}", headers=AUTH).json()
        if run["status"] not in ("pending", "running"):
            return run
        time.sleep(0.3)
    raise AssertionError("run did not finish")


def test_refuses_to_start_without_a_token_unless_explicitly_allowed(settings):
    with pytest.raises(RuntimeError, match="KTR_API_TOKEN"):
        create_app(settings)
    TestClient(create_app(settings, allow_no_auth=True))  # explicit opt-in for local development


def test_api_requires_the_token_but_health_and_dashboard_do_not(client):
    assert client.get("/healthz").json() == {"status": "ok", "executor": "local"}
    assert client.get("/").status_code == 200 and "Test Runner" in client.get("/").text
    assert client.get("/api/suites").status_code == 401
    assert client.get("/api/suites", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/suites", headers=AUTH).status_code == 200


def test_security_headers_are_set(client):
    headers = client.get("/").headers
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"


def test_list_suites(client):
    suites = {s["name"]: s for s in client.get("/api/suites", headers=AUTH).json()}
    assert suites["sample"]["accepts_selector"] is True and "-k" in suites["sample"]["allowed_options"]
    assert suites["slow"]["allowed_options"] == []


def test_run_a_real_suite_and_read_results_and_logs(client):
    started = client.post("/api/runs", json={"suite": "sample"}, headers=AUTH)
    assert started.status_code == 202 and started.headers["location"] == f"/api/runs/{started.json()['id']}"
    run = wait_for(client, started.json()["id"])
    assert run["status"] == "failed" and run["exit_code"] == 1

    results = client.get(f"/api/runs/{run['id']}/results", headers=AUTH).json()
    assert (results["total"], results["passed"], results["failed"]) == (6, 4, 2)
    only_failed = client.get(f"/api/runs/{run['id']}/results?outcome=failed", headers=AUTH).json()
    assert {r["test_id"].split("::")[1] for r in only_failed["results"]} == {
        "test_negative_quantity_is_rejected", "test_discount_codes"}

    log = client.get(f"/api/runs/{run['id']}/logs", headers=AUTH).text
    assert "2 failed, 4 passed" in log and "::ktr-junit-begin::" not in log


def test_selector_narrows_the_run(client):
    run = wait_for(client, client.post("/api/runs", json={"suite": "sample", "selector": '-k "total or empty"'},
                                       headers=AUTH).json()["id"])
    assert run["status"] == "succeeded"
    assert client.get(f"/api/runs/{run['id']}/results", headers=AUTH).json()["total"] == 3


def test_validation_errors(client):
    assert client.post("/api/runs", json={"suite": "nope"}, headers=AUTH).status_code == 404
    bad = client.post("/api/runs", json={"suite": "sample", "selector": "--junitxml=/etc/x"}, headers=AUTH)
    assert bad.status_code == 422 and "not allowed" in bad.json()["detail"]
    refused = client.post("/api/runs", json={"suite": "slow", "selector": "-k a"}, headers=AUTH)
    assert refused.status_code == 422
    assert client.post("/api/runs", json={"suite": ""}, headers=AUTH).status_code == 422
    assert client.get("/api/runs/r000000000000-ffff", headers=AUTH).status_code == 404
    assert client.get("/api/runs/..%2F..%2Fetc", headers=AUTH).status_code == 404


def test_cancel_a_running_run_and_conflicts(client):
    run_id = client.post("/api/runs", json={"suite": "slow"}, headers=AUTH).json()["id"]
    assert client.get(f"/api/runs/{run_id}/results", headers=AUTH).status_code == 409   # not finished
    cancelled = client.post(f"/api/runs/{run_id}/cancel", headers=AUTH)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.post(f"/api/runs/{run_id}/cancel", headers=AUTH).status_code == 409   # already finished
    assert client.get(f"/api/runs/{run_id}/results", headers=AUTH).status_code == 404   # no results file


def test_run_limit_returns_429(tmp_path):
    settings = make_settings(tmp_path, executor="local", max_active_runs=1, suites={
        "slow": make_suite(name="slow", allow_selector=False,
                           local_command=["{python}", "-c", "import time; time.sleep(60)"])})
    client = TestClient(create_app(settings, api_token="secret-token"))
    first = client.post("/api/runs", json={"suite": "slow"}, headers=AUTH).json()["id"]
    try:
        second = client.post("/api/runs", json={"suite": "slow"}, headers=AUTH)
        assert second.status_code == 429 and second.headers["retry-after"] == "30"
    finally:
        client.post(f"/api/runs/{first}/cancel", headers=AUTH)


def test_api_runs_on_the_kubernetes_executor_too(tmp_path, cluster):
    settings = make_settings(tmp_path)
    app = create_app(settings, KubernetesExecutor(settings, batch=cluster.batch, core=cluster.core),
                     api_token="secret-token")
    client = TestClient(app)
    run = client.post("/api/runs", json={"suite": "sample"}, headers=AUTH).json()
    assert run["status"] == "pending"
    assert client.get(f"/api/runs/{run['id']}/results", headers=AUTH).status_code == 409
    cluster.finish(run["id"], exit_code=1, log="no results written")
    assert client.get(f"/api/runs/{run['id']}", headers=AUTH).json()["status"] == "failed"
    assert client.get(f"/api/runs/{run['id']}/results", headers=AUTH).status_code == 404
    assert client.get("/api/runs", headers=AUTH).json()[0]["id"] == run["id"]
