"""Migration 0002_fetch is safe for a database that already holds data (DESIGN-FETCH.md 4.3)."""
from __future__ import annotations

import json
import sqlite3

import pytest
from alembic import command
from alembic.config import Config

from reelvault.server import db

T0 = "2026-11-01 00:00:00"


def _alembic(path, rev, *, down=False):
    eng = db.init(f"sqlite:///{path}")
    try:
        cfg = Config()
        cfg.set_main_option("script_location", str(db.migrations_dir()))
        cfg.set_main_option("sqlalchemy.url", str(eng.url))
        with eng.connect() as conn:
            cfg.attributes["connection"] = conn
            (command.downgrade if down else command.upgrade)(cfg, rev)
            if conn.in_transaction():
                conn.commit()
    finally:
        eng.dispose()
        db._engine = None  # noqa: SLF001


def _seed(path):
    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("INSERT INTO users (id,email,name,password_hash,status,role,settings,storage_bytes,created_at) "
              "VALUES ('usr_a','a@example.com','A','h','active','member',?,0,?)",
              (json.dumps({"timezone": "Europe/London"}), T0))
    c.execute("INSERT INTO accounts (id,user_id,role,platform,handle,is_private,status,cursor_source,cursor_done,"
              "reels_count,created_at) VALUES ('acc_a','usr_a','competitor','instagram','h',0,'active','clips',0,1,?)", (T0,))
    for i in range(40):
        c.execute("INSERT INTO jobs (id,user_id,kind,executor,state,priority,payload,progress,attempts,max_attempts,"
                  "job_class,dedupe_key,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (f"job_{i}", "usr_a", "ig_pull" if i % 2 else "score_account", "extension" if i % 2 else "server",
                   "waiting" if i % 2 else "done", 10, json.dumps({"n": i}), "{}", i % 3, 8,
                   "external" if i % 2 else "light", f"k{i}" if i < 30 else None, T0))
    c.execute("INSERT INTO pulls (id,user_id,account_id,job_id,mode,pages,reels_new,reels_updated,collabs_seen,"
              "stop_reason) VALUES ('pul_a','usr_a','acc_a','job_1','first',2,90,0,0,'page_cap')")
    c.execute("INSERT INTO reels (id,user_id,account_id,ig_pk,code,url,is_collab,is_pinned,is_paid_partnership,"
              "coauthors,audio,counts_hidden,media_state,first_seen_at,last_seen_at,is_top10,badges,tier,"
              "sample_weight,signals_state,tag_state,needs_topic_retag,deep_state,starred,views,views_field) "
              "VALUES ('reel_a','usr_a','acc_a','123','C1abc','u',0,0,0,'[]','{}',0,'none',?,?,0,'[]','none',0,"
              "'none','none',0,'none',0,777,'play_count')", (T0, T0))
    c.execute("INSERT INTO reel_snapshots (reel_id,seen_at,user_id,views) VALUES ('reel_a',?,'usr_a',5)", (T0,))
    c.execute("INSERT INTO reel_accounts (reel_id,account_id,user_id,role,first_seen_at) "
              "VALUES ('reel_a','acc_a','usr_a','owner',?)", (T0,))
    c.execute("INSERT INTO reel_signals (reel_id,user_id,version,created_at) VALUES ('reel_a','usr_a',1,?)", (T0,))
    c.commit()
    c.close()


def _sql(path, name):
    c = sqlite3.connect(path)
    try:
        return " ".join(c.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()[0].split())
    finally:
        c.close()


def test_upgrade_keeps_every_row_and_every_rule(tmp_path, monkeypatch):
    if not db.migrations_dir():
        pytest.skip("migrations folder not shipped")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    old = tmp_path / "old.sqlite"
    _alembic(old, "0001")
    _seed(old)
    reels_before, pulls_before = _sql(old, "reels"), _sql(old, "pulls")
    _alembic(old, "head")

    c = sqlite3.connect(old)
    c.execute("PRAGMA foreign_keys=ON")
    # `reels` and `pulls` were NOT copied (their children have ON DELETE CASCADE): only columns were added
    assert _sql(old, "reels").startswith(reels_before[:300]) and _sql(old, "pulls").startswith(pulls_before[:300])
    for t, n in (("jobs", 40), ("reels", 1), ("pulls", 1), ("reel_snapshots", 1), ("reel_accounts", 1),
                 ("reel_signals", 1), ("users", 1), ("accounts", 1), ("fetch_health", 0), ("fetch_probes", 0)):
        assert c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == n, t
    # old rows read as "read with the login, complete"
    assert c.execute("SELECT read_mode, layer, layers, stop_reason, reels_new FROM pulls").fetchone() == (
        None, None, "{}", "page_cap", 90)
    assert c.execute("SELECT read_layer, read_strategy, partial, views, views_field FROM reels").fetchone() == (
        None, None, 0, 777, "play_count")
    # every job row survived the table copy, value for value
    rows = c.execute("SELECT id, kind, executor, state, attempts, job_class, dedupe_key, payload FROM jobs ORDER BY id").fetchall()
    assert len(rows) == 40 and {json.loads(r[7])["n"] for r in rows} == set(range(40))
    assert sum(1 for r in rows if r[6] is None) == 10 and {r[1] for r in rows} == {"ig_pull", "score_account"}
    # the new job kind is allowed, every other CHECK and the unique key still hold
    c.execute("INSERT INTO jobs (id,user_id,kind,executor,state,priority,payload,progress,attempts,max_attempts,"
              "job_class,created_at) VALUES ('job_p','usr_a','ig_probe','extension','waiting',5,'{}','{}',0,3,'external',?)", (T0,))
    for bad in ("kind='nope'", "state='nope'", "executor='nope'", "job_class='nope'", "dedupe_key='k2'"):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(f"UPDATE jobs SET {bad} WHERE id='job_1'")
    assert {r[1] for r in c.execute("PRAGMA index_list(jobs)")} >= {"ix_jobs_lease", "ix_jobs_lease_until",
                                                                      "ix_jobs_user_id_state"}
    assert c.execute("PRAGMA foreign_key_check").fetchall() == [] and c.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    # deleting the member still cascades to the jobs (the foreign key was kept by the copy)
    c.execute("DELETE FROM users WHERE id='usr_a'")
    assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    c.rollback()
    c.close()

    # same schema as a database made new at head
    fresh = tmp_path / "fresh.sqlite"
    _alembic(fresh, "head")

    def schema(path):
        x = sqlite3.connect(path)
        try:
            out = {}
            for name, sql in x.execute("SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"):
                parts = " ".join(sql.split()).replace(", CONSTRAINT", "\nCONSTRAINT").rstrip(" )").split("\n")
                out[name] = (parts[0], sorted(p.rstrip(" )") for p in parts[1:]))
            return out
        finally:
            x.close()

    assert schema(old) == schema(fresh)

    # and back: the downgrade drops only what the upgrade added
    c = sqlite3.connect(old)
    c.execute("INSERT INTO jobs (id,user_id,kind,executor,state,priority,payload,progress,attempts,max_attempts,"
              "job_class,created_at) VALUES ('job_p','usr_a','ig_probe','extension','waiting',5,'{}','{}',0,3,'external',?)", (T0,))
    c.commit()
    c.close()
    _alembic(old, "0001", down=True)
    c = sqlite3.connect(old)
    assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 40 and c.execute("SELECT count(*) FROM reels").fetchone()[0] == 1
    assert c.execute("SELECT count(*) FROM reel_snapshots").fetchone()[0] == 1
    assert "read_layer" not in [r[1] for r in c.execute("PRAGMA table_info(reels)")]
    assert not c.execute("SELECT name FROM sqlite_master WHERE name IN ('fetch_health','fetch_probes')").fetchall()
    c.close()
    _alembic(old, "head")       # and up again


def test_new_columns_have_defaults_for_rows_written_by_old_code(rv_env, make_user):
    """Code that does not know the new columns (another package, an older worker) can still insert."""
    u = make_user()
    with db.tx() as conn:
        conn.exec_driver_sql("INSERT INTO accounts (id,user_id,role,platform,handle,is_private,status,cursor_source,"
                             "cursor_done,reels_count,created_at) VALUES ('acc_x',?,'competitor','instagram','h',0,"
                             "'active','clips',0,0,'2026-11-01 00:00:00')", (u.id,))
        conn.exec_driver_sql("INSERT INTO pulls (id,user_id,account_id,mode,pages,reels_new,reels_updated,collabs_seen) "
                             "VALUES ('pul_x',?,'acc_x','first',0,0,0,0)", (u.id,))
        conn.execute(db.reels.insert().values(id="reel_x", user_id=u.id, account_id="acc_x", ig_pk="5", code="Cx",
                                              url="u"))
    with db.tx(immediate=False) as conn:
        pull = conn.execute(db.pulls.select()).first()
        reel = conn.execute(db.reels.select()).first()
    assert pull.layers == {} and pull.layer is None and pull.read_mode is None
    assert reel.partial is False and reel.read_layer is None
    assert db.USER_SETTINGS_DEFAULT["read_mode"] is None and "ig_probe" in db.JOB_KINDS


def _version(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        c.close()


def test_unversioned_old_database_is_stamped_then_upgraded(tmp_path, monkeypatch):
    """The localhost app before 0.5 had no migrations folder: create_all made the 0001 tables, no alembic_version.
    migrate() stamps 0001 and upgrades, so the rows stay and the new columns and the ig_probe kind arrive."""
    if not db.migrations_dir():
        pytest.skip("migrations folder not shipped")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    old = tmp_path / "old.sqlite"
    _alembic(old, "0001")
    _seed(old)
    c = sqlite3.connect(old)
    c.execute("DROP TABLE alembic_version")
    c.commit()
    c.close()
    eng = db.init(f"sqlite:///{old}")
    try:
        db.migrate(eng)
    finally:
        eng.dispose()
        db._engine = None  # noqa: SLF001
    assert _version(old) == "0002"
    c = sqlite3.connect(old)
    try:
        assert c.execute("SELECT count(*) FROM jobs").fetchone()[0] == 40
        assert c.execute("SELECT read_layer, partial FROM reels").fetchone() == (None, 0)
        assert c.execute("SELECT layers FROM pulls").fetchone()[0] is not None
        c.execute("INSERT INTO jobs (id,user_id,kind,executor,state,priority,payload,progress,attempts,max_attempts,"
                  "job_class,created_at) VALUES ('job_p','usr_a','ig_probe','extension','waiting',5,'{}','{}',0,3,"
                  "'external',?)", (T0,))
    finally:
        c.close()


def test_unversioned_new_database_is_stamped_head(tmp_path, monkeypatch):
    """create_all with today's tables (no migrations folder at the time): stamped 0002, nothing re-added."""
    if not db.migrations_dir():
        pytest.skip("migrations folder not shipped")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    new = tmp_path / "new.sqlite"
    eng = db.init(f"sqlite:///{new}")
    try:
        db.metadata.create_all(eng)
        db.migrate(eng)
        db.migrate(eng)
    finally:
        eng.dispose()
        db._engine = None  # noqa: SLF001
    assert _version(new) == "0002"
