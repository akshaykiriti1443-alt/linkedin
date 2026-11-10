"""Files on disk (DESIGN-SAAS.md 3.17, 11.7): DATA_DIR paths, safe member paths, retention sweep, backups. WP1.

    storage.member_dir(uid)                    -> DATA_DIR/users/<uid>            (created)
    storage.member_path(uid, "reels/x/v.mp4")  -> absolute path, refuses '..', absolute paths and symlinks
    storage.files_url(uid, "reels/x/cover.jpg") -> "/files/<uid>/reels/x/cover.jpg"
"""
from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import subprocess
import tarfile
from datetime import timedelta
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from sqlalchemy import func, select

from . import db
from .settings import get_settings

log = logging.getLogger("reelvault.storage")

USER_ID_RE = re.compile(r"^usr_[a-z2-7]{16}$")


class UnsafePath(ValueError):
    """A relative path tried to leave the member's folder."""


def data_dir() -> Path:
    return get_settings().data_dir


def db_dir() -> Path:
    return data_dir() / "db"


def users_dir() -> Path:
    return data_dir() / "users"


def backups_dir() -> Path:
    return data_dir() / "backups"


def tmp_dir() -> Path:
    p = data_dir() / "tmp"
    p.mkdir(parents=True, exist_ok=True)
    return p


def models_dir() -> Path:
    return get_settings().models_dir


def ensure_dirs(*, clean_tmp: bool = False) -> None:
    for p in (db_dir(), users_dir(), backups_dir(), data_dir() / "tmp"):
        p.mkdir(parents=True, exist_ok=True)
    if clean_tmp:
        for child in (data_dir() / "tmp").iterdir():
            try:
                shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
            except OSError:
                pass


def member_dir(user_id: str, *, create: bool = True) -> Path:
    if not USER_ID_RE.match(user_id or ""):
        raise UnsafePath("bad user id")
    p = users_dir() / user_id
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def _check_rel(rel: str) -> PurePosixPath:
    if not rel or "\x00" in rel or "\\" in rel:
        raise UnsafePath("bad path")
    pp = PurePosixPath(rel)
    if pp.is_absolute() or any(part in ("..", "") for part in pp.parts) or rel.startswith("~"):
        raise UnsafePath("path escapes the member folder")
    return pp


def member_path(user_id: str, rel: str) -> Path:
    """Resolve rel inside users/<user_id>/; refuse '..', absolute paths and any symlink on the way."""
    base = member_dir(user_id)
    pp = _check_rel(str(rel))
    cur = base
    for part in pp.parts:
        cur = cur / part
        if cur.is_symlink():
            raise UnsafePath("symlinks are not allowed")
    resolved = cur.resolve()
    if not resolved.is_relative_to(base.resolve()):
        raise UnsafePath("path escapes the member folder")
    return cur


def files_url(user_id: str, rel: str) -> str:
    _check_rel(rel)
    return f"/files/{user_id}/{quote(rel)}"


def dir_size(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    for f in p.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def refresh_storage_bytes(user_id: str) -> int:
    n = dir_size(member_dir(user_id))
    with db.tx() as conn:
        conn.execute(db.users.update().where(db.users.c.id == user_id).values(storage_bytes=n))
    return n


def delete_member_files(user_id: str) -> None:
    p = member_dir(user_id, create=False)
    if p.exists():
        shutil.rmtree(p, ignore_errors=True)


def disk_free_mb() -> int:
    try:
        return int(shutil.disk_usage(data_dir()).free / 1024 / 1024)
    except OSError:
        return 0


# ============================================================================================== sweep (daily)

def sweep_storage() -> dict:
    """Delete videos past video_keep_days unless outlier/top10/starred; at 90% of storage_cap_mb delete the oldest
    non-starred videos. Recompute users.storage_bytes. (Output-referenced videos are kept by WP5 via starred.)"""
    keep_days = int(db.get_setting("video_keep_days") or 7)
    cap = int(db.get_setting("storage_cap_mb") or 4000) * 1024 * 1024
    cutoff = db.utcnow() - timedelta(days=keep_days)
    r = db.reels
    deleted = 0
    with db.tx(immediate=False) as conn:
        rows = conn.execute(select(r.c.id, r.c.user_id, r.c.video_path).where(
            r.c.video_path.is_not(None), r.c.media_state == "have", r.c.first_seen_at < cutoff,
            r.c.starred.is_(False), r.c.is_top10.is_(False),
            (r.c.band.is_(None)) | (r.c.band.not_in(("outlier", "big", "breakout"))))).all()
    for row in rows:
        if _delete_video(row.user_id, row.id, row.video_path):
            deleted += 1
    with db.tx(immediate=False) as conn:
        uids = [u[0] for u in conn.execute(select(db.users.c.id))]
    for uid in uids:
        used = refresh_storage_bytes(uid)
        if used > cap * 0.9:
            with db.tx(immediate=False) as conn:
                vids = conn.execute(select(r.c.id, r.c.video_path).where(
                    r.c.user_id == uid, r.c.video_path.is_not(None), r.c.starred.is_(False))
                    .order_by(r.c.first_seen_at.asc())).all()
            for v in vids:
                if used <= cap * 0.8:
                    break
                try:
                    size = member_path(uid, v.video_path).stat().st_size
                except (OSError, UnsafePath):
                    size = 0
                if _delete_video(uid, v.id, v.video_path):
                    used -= size
                    deleted += 1
            refresh_storage_bytes(uid)
    return {"videos_deleted": deleted}


def _delete_video(user_id: str, reel_id: str, rel: str) -> bool:
    try:
        p = member_path(user_id, rel)
        if p.exists():
            p.unlink()
    except (OSError, UnsafePath):
        return False
    with db.tx() as conn:
        conn.execute(db.reels.update().where(db.reels.c.id == reel_id, db.reels.c.user_id == user_id)
                     .values(video_path=None, media_state="deleted"))
    return True


# ============================================================================================== backups (daily)

def backup() -> dict:
    """Daily SQLite online backup (keep 7) + weekly member-files tar without videos (keep 4); BACKUP_COPY_CMD."""
    ensure_dirs()
    bdir = backups_dir()
    now = db.utcnow()
    out: dict = {}
    s = get_settings()
    if s.is_sqlite:
        src_path = s.database_url.split("///", 1)[-1]
        dest = bdir / f"db-{now:%Y-%m-%d}.sqlite"
        src = sqlite3.connect(src_path)
        try:
            dst = sqlite3.connect(str(dest))
            with dst:
                src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")  # one self-contained file (no -wal/-shm next to it)
            dst.close()
        finally:
            src.close()
        out["db"] = dest.name
        out["db_bytes"] = dest.stat().st_size
        _keep_newest(bdir.glob("db-*.sqlite"), 7)
    week = f"{now:%G-%V}"
    have_zstd = shutil.which("zstd") is not None and shutil.which("tar") is not None
    tar_name = f"users-{week}.tar.zst" if have_zstd else f"users-{week}.tar.gz"
    tar_path = bdir / tar_name
    if not tar_path.exists():
        if have_zstd:
            subprocess.run(["tar", "--zstd", "--exclude=*.mp4", "-cf", str(tar_path), "-C", str(data_dir()), "users"],
                           check=False, capture_output=True, timeout=3600)
        else:
            with tarfile.open(tar_path, "w:gz") as tf:
                tf.add(users_dir(), arcname="users", filter=lambda ti: None if ti.name.endswith(".mp4") else ti)
        out["files"] = tar_name
        _keep_newest(list(bdir.glob("users-*.tar.zst")) + list(bdir.glob("users-*.tar.gz")), 4)
    if s.backup_copy_cmd:
        res = subprocess.run(s.backup_copy_cmd, shell=True, check=False, capture_output=True, timeout=3600)
        out["copy_ok"] = res.returncode == 0
        if not out["copy_ok"]:  # fix round 1: this failed silently; Admin > Health shows it too
            log.error("BACKUP_COPY_CMD failed (exit %s): the off-site copy did not happen", res.returncode)
    db.set_setting("internal.last_backup", {"at": db.iso(now), **out})
    return out


def _keep_newest(paths, n: int) -> None:
    files = sorted(paths, key=lambda p: p.name, reverse=True)
    for old in files[n:]:
        try:
            old.unlink()
        except OSError:
            pass


def member_usage(user_id: str) -> dict:
    with db.tx(immediate=False) as conn:
        b = conn.execute(select(db.users.c.storage_bytes).where(db.users.c.id == user_id)).scalar() or 0
        reels = conn.execute(select(func.count()).select_from(db.reels).where(db.reels.c.user_id == user_id)).scalar()
    return {"storage_mb": round(b / 1024 / 1024, 1), "reels": reels}


# ============================================================================================== restore + CLI

def sqlite_path() -> Path:
    s = get_settings()
    if not s.is_sqlite:
        raise SystemExit("This server uses Postgres. Restore it with pg_restore (see DEPLOY.md, Backups).")
    return Path(s.database_url.split("///", 1)[-1])


def restore_db(backup_file: str | Path) -> dict:
    """Put a daily DB backup back in place. Run ONLY while web and worker are stopped (backup.sh does that).
    The current DB is kept as backups/before-restore-<time>.sqlite first, so a restore can be undone."""
    src = Path(backup_file).expanduser().resolve()
    if not src.is_file():
        raise SystemExit(f"Backup file not found: {src}")
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        has_users = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
    except sqlite3.DatabaseError:
        raise SystemExit("That file is not a ReelVault database backup.") from None
    finally:
        con.close()
    if ok != "ok" or not has_users:
        raise SystemExit("That backup file is damaged or is not a ReelVault database.")
    dest = sqlite_path()
    ensure_dirs()
    kept = None
    if dest.exists():
        kept = backups_dir() / f"before-restore-{db.utcnow():%Y-%m-%dT%H%M%S}.sqlite"
        cur = sqlite3.connect(str(dest))
        try:
            out = sqlite3.connect(str(kept))
            with out:
                cur.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")
            out.close()
        finally:
            cur.close()
    for suffix in ("-wal", "-shm"):
        Path(str(dest) + suffix).unlink(missing_ok=True)
    tmp = dest.with_suffix(".restoring")
    shutil.copyfile(src, tmp)
    tmp.replace(dest)
    return {"restored": src.name, "previous_kept_as": kept.name if kept else None}


def restore_files(tar_file: str | Path) -> dict:
    """Unpack a weekly users-*.tar.zst/.tar.gz over DATA_DIR/users (videos are not in it). Safe members only."""
    src = Path(tar_file).expanduser().resolve()
    if not src.is_file():
        raise SystemExit(f"Backup file not found: {src}")
    target = data_dir()
    if src.name.endswith(".zst"):
        if shutil.which("zstd") is None:
            raise SystemExit("zstd is needed to unpack this file.")
        res = subprocess.run(["tar", "--zstd", "-xf", str(src), "-C", str(target), "--no-same-owner",
                              "--no-overwrite-dir", "users"], capture_output=True, text=True, timeout=7200)
        if res.returncode != 0:
            raise SystemExit("Could not unpack the files backup: " + res.stderr[-300:])
        return {"restored": src.name}
    with tarfile.open(src, "r:*") as tf:
        tf.extractall(target, members=[m for m in tf.getmembers() if m.name == "users" or
                                       m.name.startswith("users/")], filter="data")
    return {"restored": src.name}


def list_backups() -> list[dict]:
    bdir = backups_dir()
    if not bdir.exists():
        return []
    return [{"name": p.name, "mb": round(p.stat().st_size / 1024 / 1024, 2)}
            for p in sorted(bdir.iterdir(), key=lambda p: p.name, reverse=True)
            if p.is_file() and not p.name.endswith(("-wal", "-shm", ".restoring"))]


def _main(argv: list[str]) -> int:
    import json

    cmd = argv[0] if argv else ""
    if cmd == "backup":
        db.init()
        print(json.dumps(backup()))
        return 0
    if cmd == "list":
        for b in list_backups():
            print(f"{b['name']}  {b['mb']} MB")
        return 0
    if cmd == "restore" and len(argv) == 2:
        out = restore_db(argv[1])
        print(f"Restored {out['restored']}. The old database was kept as {out['previous_kept_as']}.")
        return 0
    if cmd == "restore-files" and len(argv) == 2:
        print(f"Restored {restore_files(argv[1])['restored']}.")
        return 0
    if cmd == "sweep":
        db.init()
        print(json.dumps(sweep_storage()))
        return 0
    print("usage: python -m reelvault.server.storage backup | list | restore <db-backup.sqlite> | "
          "restore-files <users-backup.tar.zst> | sweep")
    return 2


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
