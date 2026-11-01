import re

from kubernetes import client as k8s

from k8s_test_runner.config import SecretRef
from k8s_test_runner.manifest import JUNIT_PATH, WRAPPER, build_job, job_name, new_run_id

from .conftest import make_settings, make_suite

DNS_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def build(tmp_path, selector=None, **suite_overrides):
    suite = make_suite(**suite_overrides)
    settings = make_settings(tmp_path, suites={"sample": suite})
    return build_job("r261101093012-ab12", suite, selector or [], " ".join(selector or []), settings), suite


def test_job_is_a_valid_dns_name_and_labelled(tmp_path):
    job, _ = build(tmp_path)
    assert DNS_LABEL.match(job.metadata.name) and len(job.metadata.name) <= 63
    assert job.metadata.labels == {"app.kubernetes.io/managed-by": "k8s-test-runner",
                                   "ktr/run-id": "r261101093012-ab12", "ktr/suite": "sample"}
    assert job.spec.template.metadata.labels == job.metadata.labels


def test_run_ids_are_unique_and_name_safe():
    ids = {new_run_id() for _ in range(200)}
    assert len(ids) == 200 and all(DNS_LABEL.match(job_name("a" * 30, i)) and len(job_name("a" * 30, i)) <= 63
                                   for i in ids)


def test_pod_is_locked_down(tmp_path):
    job, suite = build(tmp_path)
    pod = job.spec.template.spec
    container = pod.containers[0]
    assert pod.restart_policy == "Never" and pod.automount_service_account_token is False
    assert pod.enable_service_links is False
    assert pod.security_context.run_as_non_root is True and pod.security_context.run_as_user == suite.run_as_user
    assert pod.security_context.seccomp_profile.type == "RuntimeDefault"
    assert container.security_context.allow_privilege_escalation is False
    assert container.security_context.read_only_root_filesystem is True
    assert container.security_context.capabilities.drop == ["ALL"]
    assert container.security_context.privileged is None


def test_limits_deadline_and_cleanup(tmp_path):
    job, suite = build(tmp_path, timeout_seconds=120)
    spec = job.spec
    assert spec.backoff_limit == 0 and spec.active_deadline_seconds == 120
    assert spec.ttl_seconds_after_finished == 86400
    resources = spec.template.spec.containers[0].resources
    assert resources.limits == {"cpu": "1", "memory": "512Mi"} and resources.requests["cpu"] == "250m"
    assert {v.name: v.empty_dir.size_limit for v in spec.template.spec.volumes} == {"results": "64Mi", "tmp": "256Mi"}


def test_command_wraps_the_suite_and_substitutes_the_junit_path(tmp_path):
    job, _ = build(tmp_path, selector=["-k", "checkout"])
    container = job.spec.template.spec.containers[0]
    assert container.command == ["/bin/sh", "-c"]
    assert container.args[0] == WRAPPER and container.args[1] == "ktr"
    assert container.args[2:] == ["python", "-m", "pytest", "-q", f"--junitxml={JUNIT_PATH}", "-k", "checkout"]
    assert "$@" in WRAPPER and "exit $code" in WRAPPER


def test_selector_text_cannot_reach_the_shell(tmp_path):
    """Arguments are passed as separate argv entries after the script, never interpolated into it."""
    job, _ = build(tmp_path, selector=["-k", "a; rm -rf /"])
    container = job.spec.template.spec.containers[0]
    assert "rm -rf" not in container.args[0]
    assert container.args[-1] == "a; rm -rf /"


def test_environment_and_secret_references(tmp_path):
    job, _ = build(tmp_path, env={"BASE_URL": "https://staging.example.com"},
                   secret_env={"API_TOKEN": SecretRef(secret="suite-secrets", key="api-token")})
    env = {e.name: e for e in job.spec.template.spec.containers[0].env}
    assert env["KTR_JUNIT"].value == JUNIT_PATH and env["BASE_URL"].value == "https://staging.example.com"
    ref = env["API_TOKEN"].value_from.secret_key_ref
    assert (ref.name, ref.key) == ("suite-secrets", "api-token") and env["API_TOKEN"].value is None


def test_serialises_to_the_api_schema(tmp_path):
    job, _ = build(tmp_path)
    body = k8s.ApiClient().sanitize_for_serialization(job)
    assert body["apiVersion"] == "batch/v1" and body["kind"] == "Job"
    pod = body["spec"]["template"]["spec"]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["containers"][0]["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert body["spec"]["activeDeadlineSeconds"] == 300
