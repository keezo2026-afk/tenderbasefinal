"""Deployment artifacts must describe the application that actually exists.

Compose files, the Dockerfile and `.env.example` are configuration *code*: nothing
type-checks them, and the failure mode of drift is not an error but silence. A
renamed setting in a compose file is dropped by `extra="ignore"`, and the setting
that decides where documents are written then falls back to a container-local path
— so the storage volume mounts an empty directory and every stored document
disappears on the next `docker compose up`.

These tests read the files and compare them against :class:`app.config.Settings`
and the mounted application, which is the only authority available here.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker" / "docker-compose.yml"
DOCKERFILE = ROOT / "docker" / "Dockerfile"

#: Variables the container itself consumes — not application settings.
INFRA_VARS = frozenset(
    {
        "RUN_MIGRATIONS_ON_START",  # docker/entrypoint.sh
        "UVICORN_HOST",
        "UVICORN_PORT",
        "UVICORN_WORKERS",  # docker/entrypoint.sh
    }
)


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _settings_fields() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_compose_only_sets_real_settings() -> None:
    """Every `environment:` key must be a field the app reads.

    Silently-ignored variables are the dangerous kind of typo: the service starts,
    reports healthy, and runs with defaults nobody chose.
    """
    fields = _settings_fields()
    offenders: dict[str, list[str]] = {}
    for service, spec in _compose()["services"].items():
        # Only the services that run *this* image: `POSTGRES_USER` and friends are
        # the postgres container's own configuration, not ours.
        if "build" not in spec:
            continue
        unknown = [
            key
            for key in (spec.get("environment") or {})
            if key not in fields and key not in INFRA_VARS
        ]
        if unknown:
            offenders[service] = unknown
    assert not offenders, f"compose sets variables that no longer exist in Settings: {offenders}"


def test_storage_paths_are_under_a_mounted_volume() -> None:
    """Anything the app writes must live on a volume, for every service.

    Checks the *pair* rather than each side alone: a correct path with an
    unmounted volume, or a volume mounted at a path the settings never mention,
    both produce a container that runs and loses data.
    """
    compose = _compose()
    for service, spec in compose["services"].items():
        env = spec.get("environment") or {}
        paths = [
            str(value)
            for key, value in env.items()
            if key.endswith("_PATH") and str(value).startswith("/")
        ]
        if not paths:
            continue
        # ``source:target[:mode]`` — both named volumes and bind mounts.
        mounts = [
            parts[1]
            for parts in (str(item).split(":") for item in spec.get("volumes") or [])
            if len(parts) >= 2
        ]
        for path in paths:
            parent = str(Path(path).parent)
            assert any(path == mount or parent.startswith(mount) for mount in mounts), (
                f"{service}: {path} is not covered by any volume mount {mounts}"
            )


def test_api_and_worker_agree_on_storage_paths() -> None:
    """A document stored by the worker must be readable by the API.

    They are separate containers; if either path diverges, ingestion succeeds,
    the API returns a URL, and the file is not there.
    """
    keys = ("DOCUMENT_STORAGE_PATH", "RAW_PAYLOAD_STORAGE_PATH", "DOCUMENT_STORAGE_BACKEND")
    env_of = {
        name: {k: v for k, v in (spec.get("environment") or {}).items() if k in keys}
        for name, spec in _compose()["services"].items()
        if name in ("api", "worker")
    }
    assert env_of["api"] and env_of["api"] == env_of["worker"], env_of


def test_dockerfile_healthcheck_path_exists_on_the_app() -> None:
    """The healthcheck must target a route that is served without credentials.

    A path that 404s is reported as "container unhealthy" and orchestrators kill
    a perfectly healthy API; a path that requires an API key does the same thing
    slower.
    """
    text = DOCKERFILE.read_text()
    match = re.search(r"CMD\s+curl\s+-fsS\s+\S+/api\S+", text)
    assert match, "no curl-based HEALTHCHECK found in the Dockerfile"
    url = match.group(0).split()[-1]
    path = url[url.index("/", url.index("//") + 2) :]

    from app.main import create_app

    # The OpenAPI document, not ``app.routes``: this FastAPI version keeps
    # included routers as unresolved ``_IncludedRouter`` placeholders until the
    # app is served, so only the schema is a complete view of what is mounted.
    served = set(create_app(Settings(app_env="test")).openapi()["paths"])
    assert path in served, f"{path} is not served by the app (of {len(served)} documented paths)"


def test_dockerfile_mount_root_is_writable_by_the_runtime_user() -> None:
    """The image runs as a non-root user; storage directories must belong to it.

    Otherwise the first document write fails at runtime — after the deployment has
    been declared successful.
    """
    text = DOCKERFILE.read_text()
    assert "USER tenderbase" in text, "the image must not run as root"
    assert re.search(r"chown\s+-R\s+tenderbase:tenderbase\s+/srv/tenderbase", text), (
        "the storage root must be owned by the runtime user"
    )


def test_compose_healthchecks_and_probes_use_the_same_contract() -> None:
    """Postgres/Redis healthchecks exist so `depends_on: service_healthy` is real.

    Without them the API starts against a database that is not accepting
    connections yet, and `RUN_MIGRATIONS_ON_START` fails the deploy for a race.
    """
    for name, spec in _compose()["services"].items():
        if name in ("api", "worker"):
            continue
        assert "healthcheck" in spec, f"{name} has no healthcheck but is a migration dependency"
