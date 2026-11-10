"""pytest fixtures for every package's server tests (WP1). Import them in a conftest.py:

    from reelvault.server.testing import *  # noqa: F401,F403

Fixtures (all function-scoped, each test gets a fresh DATA_DIR + SQLite file already at the migration head):
    rv_env          -> Path of the temp DATA_DIR (env set: PUBLIC_URL=http://localhost, RV_MASTER_KEYS, ...)
    app             -> FastAPI app from create_app("web")
    client          -> anonymous TestClient (lifespan entered), base_url http://localhost
    make_user       -> factory: make_user(email=None, role="member", status="active", name=None) -> TestUser
    member, other_member, admin  -> TestUser (active)
    member_client, other_client, admin_client  -> TestClient signed in as that user (CSRF header preset)
    login           -> factory: login(user) -> signed-in TestClient
    make_device     -> factory: make_device(user, kind="extension") -> (device_id, headers with Bearer token)
    community_code  -> a fresh, known community access code (str)
    run_jobs        -> jobs.drain: run every due server job now (all pools), returns finished rows

TestUser has .id .email .password .role .name. Seed rows with `with db.tx() as conn: conn.execute(...)`.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

PASSWORD = "test-password-123"
BASE_URL = "http://localhost"
_HASH: list[str] = []


@dataclass
class TestUser:
    __test__ = False  # not a test class
    id: str
    email: str
    password: str
    role: str
    name: str


def _hash() -> str:
    if not _HASH:
        from .security import hash_password

        _HASH.append(hash_password(PASSWORD))
    return _HASH[0]


@pytest.fixture(scope="session")
def _rv_template_db(tmp_path_factory) -> Path:
    """Migrate once per session; each test copies the file (fast)."""
    import os

    from . import db

    d = tmp_path_factory.mktemp("rv_template")
    path = d / "reelvault.sqlite"
    old = os.environ.get("DATA_DIR")
    eng = db.init(f"sqlite:///{path}")
    db.migrate(eng)
    eng.dispose()
    db._engine = None  # noqa: SLF001
    if old is not None:
        os.environ["DATA_DIR"] = old
    return path


@pytest.fixture
def rv_env(tmp_path, monkeypatch, _rv_template_db) -> Path:
    from . import db, ratelimit, settings
    from .security import genkey

    data = tmp_path / "data"
    (data / "db").mkdir(parents=True)
    shutil.copy(_rv_template_db, data / "db" / "reelvault.sqlite")
    for k in ("DATABASE_URL", "ADMIN_EMAIL", "ADMIN_PASSWORD", "SMTP_URL", "RESEND_API_KEY", "SKOOL_HOOK_SECRET",
              "RV_ROLE", "TRUSTED_PROXIES", "MAIL_FROM"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("PUBLIC_URL", BASE_URL)
    monkeypatch.setenv("RV_MASTER_KEYS", genkey("test"))
    monkeypatch.setenv("RV_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("WHISPER_CONCURRENCY", "1")
    import os

    pg = os.environ.get("RV_TEST_DATABASE_URL", "").strip()
    if pg:  # optional Postgres run (CI job "postgres"): a fresh schema per test, migrated to head
        monkeypatch.setenv("DATABASE_URL", pg)
        settings.reload()
        eng = db.init()
        with eng.begin() as c:
            c.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
            c.exec_driver_sql("CREATE SCHEMA public")
        db.migrate()
    else:
        settings.reload()
        db.init()
    ratelimit.reset_memory()
    yield data
    if db._engine is not None:  # noqa: SLF001
        db._engine.dispose()  # noqa: SLF001
        db._engine = None  # noqa: SLF001
    settings.reload()


@pytest.fixture
def app(rv_env):
    from .app import create_app

    return create_app("web")


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app, base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def make_user(rv_env):
    from . import db

    n = [0]

    def make(email: str | None = None, *, role: str = "member", status: str = "active", name: str | None = None,
             password: str = PASSWORD) -> TestUser:
        from .security import hash_password

        n[0] += 1
        email = (email or f"user{n[0]}@example.com").lower()
        uid = db.new_id("usr")
        now = db.utcnow()
        h = _hash() if password == PASSWORD else hash_password(password)
        with db.tx() as conn:
            conn.execute(db.users.insert().values(
                id=uid, email=email, name=name or f"User {n[0]}", password_hash=h, status=status, role=role,
                created_at=now, membership_checked_at=now, last_seen_at=now, settings=dict(db.USER_SETTINGS_DEFAULT)))
            db.upsert(conn, db.profiles, {"user_id": uid, "brand_id": "me", "status": "empty", "answers": {},
                                          "updated_at": now}, do_nothing=True)
        return TestUser(uid, email, password, role, name or f"User {n[0]}")

    return make


@pytest.fixture
def login(app):
    from fastapi.testclient import TestClient

    from . import db, security

    made = []

    def do(user: TestUser):
        with db.tx() as conn:
            value, csrf = security.create_session(conn, user.id, None)
        c = TestClient(app, base_url=BASE_URL)
        c.cookies.set(security.SESSION_COOKIE, value)
        c.headers["X-CSRF-Token"] = csrf
        c.csrf = csrf  # type: ignore[attr-defined]
        made.append(c)
        return c

    yield do
    for c in made:
        c.close()


@pytest.fixture
def member(make_user) -> TestUser:
    return make_user("member@example.com", name="Mia Member")


@pytest.fixture
def other_member(make_user) -> TestUser:
    return make_user("other@example.com", name="Otto Other")


@pytest.fixture
def admin(make_user) -> TestUser:
    return make_user("admin@example.com", role="admin", name="Ada Admin")


@pytest.fixture
def member_client(login, member):
    return login(member)


@pytest.fixture
def other_client(login, other_member):
    return login(other_member)


@pytest.fixture
def admin_client(login, admin):
    return login(admin)


@pytest.fixture
def make_device(rv_env):
    from . import db, security

    def make(user: TestUser, kind: str = "extension", name: str = "Chrome on Test"):
        tok, h = security.new_token("rvd")
        did = db.new_id("dev")
        with db.tx() as conn:
            conn.execute(db.devices.insert().values(id=did, user_id=user.id, kind=kind, name=name, token_hash=h,
                                                    version="2.0.0", state=dict(db.DEVICE_STATE_DEFAULT),
                                                    created_at=db.utcnow()))
        return did, {"Authorization": f"Bearer {tok}"}

    return make


@pytest.fixture
def community_code(app) -> str:
    from . import api_admin, db

    with db.tx() as conn:
        return api_admin.create_community_code(conn, banner=False)


@pytest.fixture
def run_jobs(rv_env):
    from . import jobs

    def run(classes=("light", "media", "ai"), max_jobs: int = 200):
        return jobs.drain(classes, max_jobs=max_jobs)

    return run


__all__ = ["PASSWORD", "BASE_URL", "TestUser", "_rv_template_db", "rv_env", "app", "client", "make_user", "login",
           "member", "other_member", "admin", "member_client", "other_client", "admin_client", "make_device",
           "community_code", "run_jobs"]
