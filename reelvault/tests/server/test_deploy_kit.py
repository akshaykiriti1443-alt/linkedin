"""WP1 second pass: the deploy kit (DESIGN-SAAS.md 12.5) and deploy/build_extension.py (5.2).

Docker itself is exercised in CI (.github/workflows/reelvault-members.yml: build, compose boot, /healthz, sign-in,
backup/restore). These tests check the files so a broken kit fails fast on any machine.
"""
import base64
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]          # the project folder (pyproject.toml)
DEPLOY = ROOT / "deploy"
WORKFLOW = ROOT.parent / ".github" / "workflows" / "reelvault-members.yml"


def load_builder():
    spec = importlib.util.spec_from_file_location("rv_build_extension_t", DEPLOY / "build_extension.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_ext(tmp_path):
    d = tmp_path / "ext"
    (d / "icons").mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "manifest_version": 3, "name": "ReelVault Members Reader", "version": "2.0.0",
        "permissions": ["scripting", "storage", "alarms", "declarativeNetRequestWithHostAccess"],
        "host_permissions": ["https://www.instagram.com/*", "https://*.cdninstagram.com/*"],
        "background": {"service_worker": "sw.js", "type": "module"}}))
    (d / "sw.js").write_text("import { SERVERS } from './config.js';\n")
    (d / "config.js").write_text('// template\nexport const SERVERS = ["https://app.example.com"];\n'
                                 'export const BUILD = "zip";\nexport const DEV = false;\n')
    (d / "icons" / "16.png").write_bytes(b"\x89PNG fake")
    (d / ".DS_Store").write_bytes(b"junk")
    (d / "tests").mkdir()
    (d / "tests" / "x.js").write_text("test")
    (d / "sw.js.map").write_text("{}")
    return d


# ============================================================================================== build_extension.py

def test_build_zip_writes_config_and_skips_junk(fake_ext):
    b = load_builder()
    data = b.build_zip(["https://App.Example.com/", "https://app.example.com"], build="store", src=fake_ext)
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = sorted(zf.namelist())
    assert names == ["config.js", "icons/16.png", "manifest.json", "sw.js"]
    cfg = zf.read("config.js").decode()
    assert 'export const SERVERS = ["https://app.example.com"];' in cfg
    assert 'export const BUILD = "store";' in cfg
    assert "export const DEV = false;" in cfg and "// template" in cfg       # WP2's other lines are kept
    assert data == b.build_zip(["https://app.example.com"], build="store", src=fake_ext)  # reproducible


def test_build_zip_server_rules(fake_ext):
    b = load_builder()
    ok = b.build_zip(["https://a.example.com:8443", "http://localhost:8000"], build="zip", src=fake_ext)
    cfg = zipfile.ZipFile(io.BytesIO(ok)).read("config.js").decode()
    assert '["https://a.example.com:8443", "http://localhost:8000"]' in cfg
    for bad in ("http://app.example.com", "https://app.example.com/path", "ftp://x.example.com", "app.example.com",
                "https://user:pw@app.example.com"):
        with pytest.raises(b.BuildError):
            b.build_zip([bad], build="zip", src=fake_ext)
    with pytest.raises(b.BuildError):
        b.build_zip(["http://localhost:8000"], build="store", src=fake_ext)
    with pytest.raises(b.BuildError):
        b.build_zip([], src=fake_ext)


@pytest.mark.parametrize("change", [
    {"permissions": ["cookies", "storage"]},
    {"permissions": ["tabs"]},
    {"host_permissions": ["<all_urls>"]},
    {"optional_host_permissions": ["https://*/*"]},
    {"manifest_version": 2},
])
def test_build_zip_refuses_manifests_that_break_the_rules(fake_ext, change):
    b = load_builder()
    m = json.loads((fake_ext / "manifest.json").read_text())
    m.update(change)
    (fake_ext / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(b.BuildError):
        b.build_zip(["https://app.example.com"], src=fake_ext)


def test_build_extension_cli(fake_ext, tmp_path):
    out = tmp_path / "dist"
    r = subprocess.run(["python", str(DEPLOY / "build_extension.py"), "--server", "https://app.example.com",
                        "--src", str(fake_ext), "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (out / "reelvault-members-extension-2.0.0-store.zip").exists()
    r = subprocess.run(["python", str(DEPLOY / "build_extension.py"), "--server", "http://evil.example.com",
                        "--src", str(fake_ext), "--out", str(out)], capture_output=True, text=True)
    assert r.returncode == 1 and "https://" in r.stderr


def test_download_route_packs_the_extension_for_this_server(client, fake_ext, monkeypatch):
    from reelvault.server import api_ext

    if getattr(api_ext, "extension_zip", None) is not None:
        pytest.skip("WP2 provides api_ext.extension_zip(); its own tests cover the route")
    monkeypatch.setenv("RV_EXTENSION_DIR", str(fake_ext))
    r = client.get("/download/reelvault-members-extension.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    cfg = zipfile.ZipFile(io.BytesIO(r.content)).read("config.js").decode()
    assert 'export const SERVERS = ["http://localhost"];' in cfg and 'BUILD = "zip"' in cfg
    from reelvault.server import api_files

    monkeypatch.setattr(api_files, "load_extension_builder", lambda: None)
    r = client.get("/download/reelvault-members-extension.zip")
    assert r.status_code == 404 and r.json()["error"] == "The extension download is not ready yet."


def test_the_real_extension_folder_builds_when_present():
    if not (ROOT / "extension-members" / "sw.js").exists():
        pytest.skip("extension-members/ is not complete yet (WP2)")
    b = load_builder()
    data = b.build_zip(["https://app.example.com"], build="store")
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "manifest.json" in names and "config.js" in names and "sw.js" in names


# ============================================================================================== Dockerfile

def test_dockerfile_essentials():
    s = (DEPLOY / "Dockerfile").read_text()
    assert s.count("FROM ") >= 3 and "AS deps" in s and "AS models" in s and "AS runtime" in s
    assert "python:3.13-slim" in s
    assert re.search(r"apt-get install[^\n]*ffmpeg[^\n]*ca-certificates[^\n]*sqlite3[^\n]*zstd", s)
    assert "--require-hashes -r /tmp/requirements.lock" in s
    assert "fetch_models.sh" in s
    for env in ("DATA_DIR=/data", "RV_MODELS_DIR=/opt/reelvault/models", "HF_HOME=/opt/reelvault/models/whisper",
                "HF_HUB_OFFLINE=1", "RV_MIGRATIONS_DIR=/opt/reelvault/migrations",
                "RV_EXTENSION_DIR=/opt/reelvault/extension-members"):
        assert env in s, env
    assert "COPY migrations /opt/reelvault/migrations" in s
    assert "USER reelvault" in s and 'VOLUME ["/data"]' in s
    assert 'CMD ["reelvault-start"]' in s and "HEALTHCHECK" in s and '"reelvault-start", "health"' in s
    assert "ADMIN_PASSWORD" not in s and "RV_MASTER_KEYS" not in s     # secrets come from the host, never baked


def test_dockerignore_keeps_private_files_out():
    lines = [x.strip() for x in (DEPLOY / "Dockerfile.dockerignore").read_text().splitlines()]
    assert lines[lines.index("*")] == "*"                       # allowlist style
    assert "**/.env" in lines
    allowed = {x[1:] for x in lines if x.startswith("!")}
    assert {"pyproject.toml", "reelvault/", "migrations/", "deploy/requirements.lock"} <= allowed
    for private in ("vault/", "work/", "profile.md", "reelvault.toml", "deploy/.env", "tests/"):
        assert private not in allowed


def test_lock_pins_every_server_dependency_with_hashes():
    lock = (DEPLOY / "requirements.lock").read_text()
    names = {m.group(1).lower().replace("_", "-") for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==", lock, re.M)}
    pyproject = (ROOT / "pyproject.toml").read_text()
    server = re.search(r"server = \[(.*?)\]", pyproject, re.S).group(1)
    wanted = {re.split(r"[\[<>=]", d.strip().strip('"'))[0].lower() for d in server.split(",") if d.strip()}
    assert wanted - names == set()
    for pkg in ("anthropic", "pydantic", "faster-whisper"):
        assert pkg in names
    assert lock.count("--hash=sha256:") >= len(names)
    assert "/home/" not in lock and "/tmp/" not in lock


def test_fetch_models_pins_checksums():
    s = (DEPLOY / "fetch_models.sh").read_text()
    for name in ("CED_MODEL_SHA", "CED_LABELS_SHA", "YUNET_SHA"):
        assert re.search(name + r'="[0-9a-f]{64}"', s), name
    assert re.findall(r'"(small|base)=[0-9a-f]{40}"', s) == ["small", "base"]
    assert "ced-tiny/model.int8.onnx" in s or '"$CED/"' in s
    assert "face_detection_yunet_2023mar.onnx" in s


# ============================================================================================== compose + Caddy

def test_compose_files():
    c = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
    app = c["services"]["app"]
    assert app["build"] == {"context": "..", "dockerfile": "deploy/Dockerfile"}
    assert app["env_file"] == ".env" and "rvdata:/data" in app["volumes"]
    assert app["ports"] == ["127.0.0.1:8000:8000"]                 # only Caddy and this host reach it
    caddy = c["services"]["caddy"]
    assert caddy["profiles"] == ["https"] and "./Caddyfile:/etc/caddy/Caddyfile:ro" in caddy["volumes"]
    assert re.fullmatch(r"caddy:\d+\.\d+\.\d+-alpine", caddy["image"])
    assert c["services"]["postgres"]["profiles"] == ["postgres"]
    subnet = c["networks"]["rvnet"]["ipam"]["config"][0]["subnet"]
    import ipaddress

    assert ipaddress.ip_network(subnet).subnet_of(ipaddress.ip_network("172.16.0.0/12"))
    split = yaml.safe_load((DEPLOY / "docker-compose.split.yml").read_text())
    assert split["services"]["app"]["environment"]["RV_ROLE"] == "web"
    assert split["services"]["worker"]["environment"]["RV_ROLE"] == "worker"
    assert "rvdata:/data" in split["services"]["worker"]["volumes"]


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not installed")
def test_compose_config_is_valid(tmp_path):
    work = tmp_path / "deploy"
    shutil.copytree(DEPLOY, work, ignore=shutil.ignore_patterns(".env", "__pycache__"))
    shutil.copy(work / ".env.example", work / ".env")
    for files in ("docker-compose.yml", "docker-compose.yml:docker-compose.split.yml"):
        r = subprocess.run(["docker", "compose", "config", "-q"], cwd=work, capture_output=True, text=True,
                           env={**os.environ, "COMPOSE_FILE": files})
        if "unknown command" in r.stderr or "is not a docker command" in r.stderr:
            pytest.skip("docker compose plugin not installed")
        assert r.returncode == 0, r.stderr


def test_caddyfile():
    s = (DEPLOY / "Caddyfile").read_text()
    assert "{$RV_DOMAIN}" in s and "max_size 100MB" in s and "reverse_proxy app:8000" in s
    assert "flush_interval -1" in s                       # MCP streams pass straight through
    assert "@compress not path /mcp /mcp/*" in s


# ============================================================================================== .env.example

def test_env_example_has_placeholders_only_and_every_setting():
    s = (DEPLOY / ".env.example").read_text()
    values = dict(line.split("=", 1) for line in s.splitlines() if line and not line.startswith("#") and "=" in line)
    keys = values["RV_MASTER_KEYS"].split(":", 1)[1]
    assert "REPLACE" in keys
    assert "REPLACE" in values["ADMIN_PASSWORD"]
    for k, v in values.items():
        if re.fullmatch(r"[A-Za-z0-9+/]{40,}={0,2}", v):
            raise AssertionError(f"{k} looks like a real secret")
    for k in ("SMTP_URL", "RESEND_API_KEY", "SKOOL_HOOK_SECRET", "POSTGRES_PASSWORD"):
        assert values[k] == "", k
    settings_src = (ROOT / "reelvault" / "server" / "settings.py").read_text()
    env_names = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', settings_src)) | set(
        re.findall(r'_(?:int|float|flag)\("([A-Z_]+)"', settings_src))
    test_only = {"RV_TEST_PACING", "RV_TEST_ALLOW_HTTP_CIMD", "RV_ROLE", "RV_MODELS_DIR"}
    documented = set(re.findall(r"^#?\s*([A-Z_]+)=", s, re.M))
    assert env_names - test_only - documented == set()


# ============================================================================================== shell scripts

@pytest.mark.parametrize("script", ["setup.sh", "backup.sh", "fetch_models.sh"])
def test_scripts_parse_and_are_executable(script):
    p = DEPLOY / script
    assert os.access(p, os.X_OK), f"chmod +x deploy/{script}"
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert p.read_text().startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in p.read_text()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_setup_writes_a_private_env_with_fresh_secrets(tmp_path):
    shutil.copy(DEPLOY / "setup.sh", tmp_path / "setup.sh")
    shutil.copy(DEPLOY / ".env.example", tmp_path / ".env.example")
    r = subprocess.run(["bash", "-c", 'RV_SETUP_SOURCE_ONLY=1 source ./setup.sh && '
                        'write_env https://app.example.org app.example.org owner@example.org https && '
                        'echo "PW=$ADMIN_PASSWORD_SHOWN"'], cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    env = tmp_path / ".env"
    assert oct(env.stat().st_mode & 0o777) == "0o600"
    vals = dict(line.split("=", 1) for line in env.read_text().splitlines() if line and not line.startswith("#"))
    assert vals["PUBLIC_URL"] == "https://app.example.org" and vals["RV_DOMAIN"] == "app.example.org"
    assert vals["ADMIN_EMAIL"] == "owner@example.org" and vals["COMPOSE_PROFILES"] == "https"
    kid, b64 = vals["RV_MASTER_KEYS"].split(":", 1)
    assert kid == "k1" and len(base64.b64decode(b64)) == 32
    assert len(vals["ADMIN_PASSWORD"]) >= 16 and f"PW={vals['ADMIN_PASSWORD']}" in r.stdout
    assert re.fullmatch(r"[0-9a-f]{48}", vals["SKOOL_HOOK_SECRET"])
    assert vals["RV_MASTER_KEYS"] not in r.stdout                   # the master key is never printed
    # the server accepts what setup wrote
    from reelvault.server.settings import _parse_keys

    assert len(_parse_keys(vals["RV_MASTER_KEYS"])) == 1


def test_backup_script_only_accepts_backup_file_names(tmp_path):
    src = (DEPLOY / "backup.sh").read_text()
    fn = src[src.index("safe_name() {"):src.index("\n}\n", src.index("safe_name() {")) + 3]
    for name, ok in [("db-2026-11-09.sqlite", True), ("users-2026-45.tar.zst", True),
                     ("before-restore-2026-11-10T005730.sqlite", True), ("../../etc/passwd", False),
                     ("/data/db/reelvault.sqlite", False), ("db-2026-11-09.sqlite; rm -rf /", False)]:
        r = subprocess.run(["bash", "-c", fn + f'\nsafe_name "$1"', "_", name])
        assert (r.returncode == 0) is ok, name


# ============================================================================================== docs + CI

def test_deploy_docs_exist_and_cover_the_host_box():
    d = (DEPLOY / "DEPLOY.md").read_text()
    for must in ("Host must have", "local", "always-on", "100 MB", "setup.sh", "backup.sh", "restore",
                 "build_extension.py", "cloudflared", "bench"):
        assert must in d, must
    assert (DEPLOY / "hosts" / "vps.md").exists() and (DEPLOY / "hosts" / "paas-with-volume.md").exists()
    assert "0.0.0.0/0" in (DEPLOY / "hosts" / "paas-with-volume.md").read_text()  # warned against, not used


def test_ci_workflow_runs_tests_builds_and_boots_the_image():
    if not WORKFLOW.exists():
        pytest.skip("workflow lives at the repository root")
    w = yaml.safe_load(WORKFLOW.read_text())
    jobs = w["jobs"]
    assert {"server-tests", "docker"} <= set(jobs)
    steps = " ".join(str(s.get("run", "")) + str(s.get("uses", "")) for s in jobs["server-tests"]["steps"])
    assert "pytest -q tests/server" in steps and "requirements.lock" in steps
    dsteps = " ".join(str(s.get("run", "")) + str(s.get("with", "")) for s in jobs["docker"]["steps"])
    assert "deploy/Dockerfile" in dsteps and "setup.sh --local" in dsteps and "/healthz" in dsteps
    assert jobs["docker"]["needs"] == "server-tests"
