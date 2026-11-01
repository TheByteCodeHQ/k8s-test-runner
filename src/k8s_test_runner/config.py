"""Settings and the suite catalogue. Callers pick a suite by name; they never supply an image or command."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

SETTINGS_ENV = "KTR_CONFIG"
DEFAULT_FLAGS = ("-k", "-m", "-x", "--maxfail")
_NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?$")  # DNS-label safe and short enough for a Job name
_QUANTITY = re.compile(r"^\d+(\.\d+)?(m|Ki|Mi|Gi|Ti|k|M|G)?$")


class ConfigError(ValueError):
    pass


@dataclass
class Resources:
    cpu_request: str = "250m"
    memory_request: str = "256Mi"
    cpu_limit: str = "1"
    memory_limit: str = "512Mi"


@dataclass
class SecretRef:
    secret: str
    key: str


@dataclass
class Suite:
    name: str
    image: str
    command: list[str]
    description: str = ""
    allow_selector: bool = False
    allowed_flags: tuple[str, ...] = DEFAULT_FLAGS
    timeout_seconds: int = 600
    resources: Resources = field(default_factory=Resources)
    env: dict[str, str] = field(default_factory=dict)
    secret_env: dict[str, SecretRef] = field(default_factory=dict)
    read_only_root: bool = True
    run_as_user: int = 10001
    # Used only by the local executor (development without a cluster)
    local_command: list[str] = field(default_factory=list)
    local_cwd: str = "."


@dataclass
class Settings:
    suites: dict[str, Suite]
    executor: str = "kubernetes"  # kubernetes | local
    namespace: str = "test-runner"
    max_active_runs: int = 5
    ttl_seconds_after_finished: int = 86400
    image_pull_policy: str = "IfNotPresent"
    work_dir: Path = Path(".ktr")  # local executor only
    root: Path = Path(".")


def _resources(raw: dict, where: str) -> Resources:
    res = Resources(
        cpu_request=str(raw.get("requests", {}).get("cpu", "250m")),
        memory_request=str(raw.get("requests", {}).get("memory", "256Mi")),
        cpu_limit=str(raw.get("limits", {}).get("cpu", "1")),
        memory_limit=str(raw.get("limits", {}).get("memory", "512Mi")),
    )
    for value in (res.cpu_request, res.memory_request, res.cpu_limit, res.memory_limit):
        if not _QUANTITY.match(value):
            raise ConfigError(f"{where}: '{value}' is not a valid Kubernetes quantity (e.g. 500m, 1, 512Mi)")
    return res


def _suite(name: str, raw: dict) -> Suite:
    where = f"suites.{name}"
    if not _NAME.match(name):
        raise ConfigError(f"{where}: name must be lowercase letters, digits and '-', up to 30 characters")
    for required in ("image", "command"):
        if not raw.get(required):
            raise ConfigError(f"{where}: '{required}' is required")
    command = raw["command"]
    if not isinstance(command, list) or not all(isinstance(c, str) for c in command):
        raise ConfigError(f"{where}: 'command' must be a list of strings (no shell string)")
    if not any("{junit}" in c for c in command):
        raise ConfigError(f"{where}: the command must write JUnit XML to the '{{junit}}' placeholder")
    timeout = int(raw.get("timeout_seconds", 600))
    if not 10 <= timeout <= 6 * 3600:
        raise ConfigError(f"{where}: timeout_seconds must be between 10 and 21600")
    secrets = {}
    for var, ref in (raw.get("secret_env") or {}).items():
        if not isinstance(ref, dict) or "secret" not in ref or "key" not in ref:
            raise ConfigError(f"{where}: secret_env.{var} needs 'secret' and 'key'")
        secrets[var] = SecretRef(secret=ref["secret"], key=ref["key"])
    return Suite(
        name=name,
        image=str(raw["image"]),
        command=command,
        description=raw.get("description", ""),
        allow_selector=bool(raw.get("allow_selector", False)),
        allowed_flags=tuple(raw.get("allowed_flags", DEFAULT_FLAGS)),
        timeout_seconds=timeout,
        resources=_resources(raw.get("resources", {}), where),
        env={str(k): str(v) for k, v in (raw.get("env") or {}).items()},
        secret_env=secrets,
        read_only_root=bool(raw.get("read_only_root", True)),
        run_as_user=int(raw.get("run_as_user", 10001)),
        local_command=list(raw.get("local_command", [])),
        local_cwd=raw.get("local_cwd", "."),
    )


def load_settings(path: str | Path | None = None) -> Settings:
    location = Path(path or os.getenv(SETTINGS_ENV) or "suites.yaml")
    if not location.is_file():
        raise ConfigError(f"Config file not found: {location}. Copy suites.example.yaml or set {SETTINGS_ENV}.")
    raw = yaml.safe_load(location.read_text(encoding="utf-8")) or {}
    suites_raw = raw.get("suites") or {}
    if not suites_raw:
        raise ConfigError("Define at least one suite under 'suites:'")
    executor = raw.get("executor", "kubernetes")
    if executor not in ("kubernetes", "local"):
        raise ConfigError("executor must be 'kubernetes' or 'local'")
    base = location.resolve().parent
    return Settings(
        suites={name: _suite(name, body or {}) for name, body in suites_raw.items()},
        executor=executor,
        namespace=raw.get("namespace", "test-runner"),
        max_active_runs=int(raw.get("max_active_runs", 5)),
        ttl_seconds_after_finished=int(raw.get("ttl_seconds_after_finished", 86400)),
        image_pull_policy=raw.get("image_pull_policy", "IfNotPresent"),
        work_dir=(base / raw.get("work_dir", ".ktr")).resolve(),
        root=base,
    )
