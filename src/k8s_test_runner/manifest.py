"""Build the Kubernetes Job for one run.

Built with the official typed models, so a misspelt field fails immediately instead of at the API
server. The pod is locked down: non-root, no privilege escalation, all capabilities dropped,
read-only root filesystem, no service-account token, and hard CPU, memory and time limits.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from kubernetes import client as k8s

from .config import Settings, Suite

MANAGED_BY = "k8s-test-runner"
LABEL_RUN = "ktr/run-id"
LABEL_SUITE = "ktr/suite"
LABEL_MANAGED = "app.kubernetes.io/managed-by"
ANN_SELECTOR = "ktr/selector"
ANN_CANCELLED = "ktr/cancelled"
CONTAINER = "tests"
JUNIT_PATH = "/results/junit.xml"

# Runs the suite command, then prints the JUnit file to the log so the control plane can read it back,
# and exits with the suite's own exit code. Needs only /bin/sh, base64 and tr, which busybox has too.
WRAPPER = r"""
"$@"
code=$?
if [ -f "$KTR_JUNIT" ]; then
  echo "::ktr-junit-begin::"
  base64 "$KTR_JUNIT" | tr -d '\n'
  echo
  echo "::ktr-junit-end::"
fi
exit $code
""".strip()


def new_run_id() -> str:
    # 22 characters; with a 30-character suite name the Job name is at most 57 of the 63 Kubernetes allows
    return f"r{datetime.now(timezone.utc):%y%m%d%H%M%S}-{secrets.token_hex(4)}"


def job_name(suite: str, run_id: str) -> str:
    return f"ktr-{suite}-{run_id}"


def build_job(run_id: str, suite: Suite, selector_args: list[str], selector_text: str, settings: Settings) -> k8s.V1Job:
    labels = {LABEL_MANAGED: MANAGED_BY, LABEL_RUN: run_id, LABEL_SUITE: suite.name}
    command = [token.replace("{junit}", JUNIT_PATH) for token in suite.command] + selector_args

    env = [k8s.V1EnvVar(name="KTR_JUNIT", value=JUNIT_PATH), k8s.V1EnvVar(name="HOME", value="/tmp")]
    env += [k8s.V1EnvVar(name=k, value=v) for k, v in sorted(suite.env.items())]
    for var, ref in sorted(suite.secret_env.items()):
        selector = k8s.V1SecretKeySelector(name=ref.secret, key=ref.key)
        env.append(k8s.V1EnvVar(name=var, value_from=k8s.V1EnvVarSource(secret_key_ref=selector)))

    res = suite.resources
    container = k8s.V1Container(
        name="tests",
        image=suite.image,
        image_pull_policy=settings.image_pull_policy,
        command=["/bin/sh", "-c"],
        args=[WRAPPER, "ktr", *command],
        env=env,
        resources=k8s.V1ResourceRequirements(
            requests={"cpu": res.cpu_request, "memory": res.memory_request},
            limits={"cpu": res.cpu_limit, "memory": res.memory_limit}),
        security_context=k8s.V1SecurityContext(
            allow_privilege_escalation=False,
            read_only_root_filesystem=suite.read_only_root,
            capabilities=k8s.V1Capabilities(drop=["ALL"])),
        volume_mounts=[k8s.V1VolumeMount(name="results", mount_path="/results"),
                       k8s.V1VolumeMount(name="tmp", mount_path="/tmp")],
    )
    pod = k8s.V1PodSpec(
        restart_policy="Never",
        automount_service_account_token=False,
        enable_service_links=False,
        containers=[container],
        security_context=k8s.V1PodSecurityContext(
            run_as_non_root=True, run_as_user=suite.run_as_user, run_as_group=suite.run_as_user,
            fs_group=suite.run_as_user, seccomp_profile=k8s.V1SeccompProfile(type="RuntimeDefault")),
        volumes=[
            k8s.V1Volume(name="results", empty_dir=k8s.V1EmptyDirVolumeSource(size_limit="64Mi")),
            k8s.V1Volume(name="tmp", empty_dir=k8s.V1EmptyDirVolumeSource(size_limit="256Mi")),
        ],
    )
    return k8s.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=k8s.V1ObjectMeta(
            name=job_name(suite.name, run_id), namespace=settings.namespace, labels=labels,
            annotations={ANN_SELECTOR: selector_text}),
        spec=k8s.V1JobSpec(
            backoff_limit=0,  # a failing test is a result, not something to retry
            active_deadline_seconds=suite.timeout_seconds,
            ttl_seconds_after_finished=settings.ttl_seconds_after_finished,
            template=k8s.V1PodTemplateSpec(metadata=k8s.V1ObjectMeta(labels=labels), spec=pod)),
    )
