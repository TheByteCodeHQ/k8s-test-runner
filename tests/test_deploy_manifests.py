"""The deployment files are code too: check them for the properties that matter."""

import shutil
import subprocess

import pytest
import yaml

from .conftest import ROOT

DEPLOY = ROOT / "deploy"


def load(name):
    return [d for d in yaml.safe_load_all((DEPLOY / name).read_text(encoding="utf-8")) if d]


def test_every_listed_resource_exists_and_parses():
    kustomization = yaml.safe_load((DEPLOY / "kustomization.yaml").read_text(encoding="utf-8"))
    for name in kustomization["resources"]:
        assert load(name), name
    assert (DEPLOY / kustomization["configMapGenerator"][0]["files"][0]).is_file()


def test_rbac_is_minimal():
    role = next(d for d in load("rbac.yaml") if d["kind"] == "Role")
    granted = {(g, r, v) for rule in role["rules"] for g in rule["apiGroups"] for r in rule["resources"]
               for v in rule["verbs"]}
    assert ("batch", "jobs", "create") in granted and ("", "pods/log", "get") in granted
    assert not any("*" in part for item in granted for part in item)
    assert not any(r in ("secrets", "configmaps", "pods/exec") for _, r, _ in granted)
    assert not any(v in ("delete", "deletecollection") for _, _, v in granted)
    assert not any(d["kind"] == "ClusterRole" for d in load("rbac.yaml"))   # namespace-scoped only


def test_control_plane_is_hardened_and_stateless_friendly():
    deployment = next(d for d in load("deployment.yaml") if d["kind"] == "Deployment")
    pod = deployment["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert deployment["spec"]["replicas"] >= 2
    env = {e["name"]: e for e in container["env"]}
    assert "secretKeyRef" in env["KTR_API_TOKEN"]["valueFrom"]        # token from a Secret, never inline
    assert "value" not in env["KTR_API_TOKEN"]


def test_namespace_enforces_the_restricted_pod_standard():
    namespace = load("namespace.yaml")[0]
    assert namespace["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_runner_network_policy_blocks_ingress_and_internal_ranges():
    policy = next(d for d in load("policies.yaml") if d["kind"] == "NetworkPolicy")
    assert policy["spec"]["ingress"] == [] and "Egress" in policy["spec"]["policyTypes"]
    blocked = next(r["to"][0]["ipBlock"]["except"] for r in policy["spec"]["egress"] if "ipBlock" in r["to"][0])
    assert "169.254.0.0/16" in blocked and "10.0.0.0/8" in blocked      # metadata service and private ranges


def test_no_secret_values_are_committed():
    text = "".join(p.read_text(encoding="utf-8") for p in DEPLOY.glob("*.yaml"))
    assert "kind: Secret" not in text


def test_the_incluster_suites_file_loads():
    from k8s_test_runner.config import load_settings
    assert "sample" in load_settings(DEPLOY / "suites.yaml").suites
    assert "sample" in load_settings(ROOT / "suites.example.yaml").suites


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl is not installed")
def test_kustomize_renders_offline():
    out = subprocess.run(["kubectl", "kustomize", str(DEPLOY)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    kinds = {d["kind"] for d in yaml.safe_load_all(out.stdout) if d}
    assert {"Namespace", "ServiceAccount", "Role", "RoleBinding", "Deployment", "Service", "ConfigMap",
            "NetworkPolicy", "ResourceQuota", "LimitRange"} <= kinds
