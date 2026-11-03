"""Static checks on the Coolify deployment files: no published ports, no dev defaults, documented variables."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from vreview.config import Settings

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "deploy" / "coolify" / "docker-compose.yml"
README_PATH = ROOT / "deploy" / "coolify" / "README.md"

# Read by code that does not go through Settings.
NON_SETTINGS_VREVIEW_VARS = {"VREVIEW_RUN_MIGRATIONS", "VREVIEW_WORKER_SLOTS"}


@pytest.fixture(scope="module")
def compose_text() -> str:
    return COMPOSE_PATH.read_text()


@pytest.fixture(scope="module")
def services(compose_text) -> dict:
    return yaml.safe_load(compose_text)["services"]


def _env(service: dict) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for entry in service.get("environment", []):
        key, sep, value = str(entry).partition("=")
        out[key] = value if sep else None
    return out


def test_expected_services(services):
    assert set(services) == {"postgres", "minio", "minio-init", "hatchet", "api", "worker", "web"}


def test_no_service_publishes_a_host_port(services):
    assert [name for name, svc in services.items() if "ports" in svc] == []


def test_exactly_the_public_services_declare_a_domain(services):
    declared = {
        name: [k for k in _env(svc) if k.startswith(("SERVICE_FQDN_", "SERVICE_URL_"))] for name, svc in services.items()
    }
    assert {n: v for n, v in declared.items() if v} == {
        "api": ["SERVICE_FQDN_API_8000"],
        "web": ["SERVICE_FQDN_WEB_3000"],
        "minio": ["SERVICE_FQDN_MINIO_9000"],
    }


@pytest.mark.parametrize(
    "forbidden",
    ["minioadmin", "Admin123", "admin@example.com", "vreview:vreview", "ALLOW_ORIGIN=*", "COOKIE_INSECURE=t",
     "ALLOW_SIGNUP=1", "localhost"],
)
def test_no_dev_defaults(compose_text, forbidden):
    code = "\n".join(line for line in compose_text.splitlines() if not line.lstrip().startswith("#"))
    assert forbidden not in code


def test_every_secret_comes_from_coolify_or_is_required(services):
    secret_keys = re.compile(r"PASSWORD|SECRET|TOKEN|API_KEY|ACCESS_KEY")
    for name, svc in services.items():
        for key, value in _env(svc).items():
            if not secret_keys.search(key) or value is None:
                continue
            assert re.fullmatch(
                r".*\$\{(SERVICE_[A-Z0-9_]+|[A-Z0-9_]+:\?|HATCHET_CLIENT_TOKEN:-|DUBEVAL_LLM_API_KEY:-)\}.*", value
            ), f"{name}.{key} is a literal secret: {value!r}"


def test_hatchet_uses_secure_cookies_and_non_default_admin(services):
    env = _env(services["hatchet"])
    assert env["SERVER_AUTH_COOKIE_INSECURE"] == "f"
    assert "${SERVICE_LOWERCASEUSER_HATCHETADMIN}" in env["ADMIN_EMAIL"]
    assert "${SERVICE_PASSWORD_HATCHETADMIN}" in env["ADMIN_PASSWORD"]
    assert services["hatchet"]["image"].endswith("hatchet-lite:v0.114.2")


def test_minio_cors_is_limited_to_the_web_origin_and_the_bucket_is_private(services):
    assert _env(services["minio"])["MINIO_API_CORS_ALLOW_ORIGIN"] == "${SERVICE_URL_WEB}"
    script = services["minio-init"]["entrypoint"][-1]
    assert "mc mb --ignore-existing local/vreview" in script
    assert "mc anonymous set private local/vreview" in script


def test_only_the_api_runs_migrations(services):
    assert {n for n, s in services.items() if "VREVIEW_RUN_MIGRATIONS" in _env(s)} == {"api"}
    assert _env(services["api"])["VREVIEW_RUN_MIGRATIONS"] == "1"


def test_worker_limits(services):
    worker = services["worker"]
    assert worker["command"] == ["vreview-worker"]
    assert (worker["mem_limit"], worker["cpus"], worker["stop_grace_period"]) == ("6g", 4, "120s")
    assert _env(worker)["VREVIEW_WORKER_SLOTS"] == "2"
    assert any(v.endswith(":/data") for v in worker["volumes"])


def test_api_and_worker_agree_on_the_shared_settings(services):
    api, worker = _env(services["api"]), _env(services["worker"])
    shared = [k for k in api if k in worker and k.startswith(("VREVIEW_", "HATCHET_CLIENT_")) and k != "VREVIEW_RUN_MIGRATIONS"]
    assert {"VREVIEW_DATABASE_URL", "VREVIEW_R2_ENDPOINT_URL", "VREVIEW_WEBHOOK_SECRET", "HATCHET_CLIENT_TOKEN"} <= set(shared)
    assert {k: api[k] for k in shared} == {k: worker[k] for k in shared}


def test_public_and_internal_object_store_urls(services):
    env = _env(services["api"])
    assert env["VREVIEW_R2_ENDPOINT_URL"] == "http://minio:9000"
    assert env["VREVIEW_R2_PUBLIC_ENDPOINT_URL"] == "${SERVICE_URL_MINIO}"
    assert env["VREVIEW_STORE"] == "r2"


def test_auth_values_follow_the_web_url(services):
    env = _env(services["api"])
    assert env["VREVIEW_AUTH_ISSUER"] == env["VREVIEW_AUTH_AUDIENCE"] == "${SERVICE_URL_WEB}"
    assert env["VREVIEW_CORS_ORIGINS"] == "${SERVICE_URL_WEB}"
    assert _env(services["web"])["BETTER_AUTH_URL"] == "${SERVICE_URL_WEB}"
    assert "ALLOW_SIGNUP" not in _env(services["web"])


def test_web_api_url_is_a_build_arg(services):
    assert services["web"]["build"]["args"] == ["NEXT_PUBLIC_VREVIEW_API_URL=${SERVICE_URL_API}"]


def test_vreview_variables_exist_in_settings(services):
    fields = {f"VREVIEW_{name.upper()}" for name in Settings.model_fields}
    used = {k for s in services.values() for k in _env(s) if k.startswith("VREVIEW_")}
    assert used - fields - NON_SETTINGS_VREVIEW_VARS == set()


def test_every_variable_in_the_compose_file_is_documented(compose_text):
    readme = README_PATH.read_text()
    names = set(re.findall(r"\$\{([A-Z][A-Z0-9_]*)", compose_text))
    names |= set(re.findall(r"^\s*- (SERVICE_FQDN_[A-Z0-9_]+)\s*$", compose_text, re.M))
    missing = sorted(n for n in names if n not in readme)
    assert missing == []


def test_postgres_init_creates_the_three_databases_and_the_vector_extension():
    sql = (ROOT / "deploy" / "coolify" / "init-db.sql").read_text()
    for db in ("vreview", "hatchet", "vreview_auth"):
        assert f"CREATE DATABASE {db};" in sql
    assert sql.index("\\connect vreview\n") < sql.index("CREATE EXTENSION IF NOT EXISTS vector")


def test_dockerignore_keeps_the_build_context_small():
    entries = {line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines() if line.strip()}
    for required in ("web/", "docs/", "tests/", "scratch/", "raw-plans/", "out/", "data/", "projects/", ".venv", ".git", ".env*"):
        assert required in entries
    assert "docker/" not in entries  # docker/entrypoint.sh is copied into the image


def test_dockerfile_runs_unprivileged_with_an_api_healthcheck():
    text = (ROOT / "Dockerfile").read_text()
    assert re.search(r"^USER vreview$", text, re.M)
    assert "HEALTHCHECK" in text and "/healthz" in text
    assert 'ENTRYPOINT ["vreview-entrypoint"]' in text
    for var in ("HF_HOME=", "TORCH_HOME=", "VREVIEW_SCRATCH_DIR="):
        assert var in text
