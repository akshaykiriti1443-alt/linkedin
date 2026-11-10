"""Media jobs: fetch_video (server CDN download) and process_reel (local signals). DESIGN-SAAS.md 5.8, 6.0-6.5.1. WP3.

Both run in the worker's `media` pool. Neither holds a DB transaction across the download, ffmpeg or whisper:
each reads its inputs in one short transaction, works, then writes in another. Both end with
`pipeline.advance(user_id, reel_id)` (WP5), the only code that chains reel steps.

fetch_video  {"reel_id"} -> {"ok"}
    Downloads the signed Instagram CDN URL from the server: Referer https://www.instagram.com/, no cookies, only
    *.cdninstagram.com / *.fbcdn.net, 2-5 s between downloads and at most 2 at a time per member (per worker
    process), <= 50 MB. On 403/404/410, an expired `oe`, no URL, or SERVER_CDN_FETCH=0 the reel becomes
    `media_state=need_extension` and WP2's scheduler (`jobs_ig.schedule_need_extension`) batches it into an
    `ig_videos` job for the member's browser.
process_reel {"reel_id", "words": bool} -> {"signals_version"}
    Runs `signals.process` (probe, cuts, frames, sound map, transcript when there is speech, visual stats, edit
    stats, sheets) and writes `reel_signals` + `reels.signals_state`. Whisper model: WHISPER_MODEL (small) with word
    times when `words`, else WHISPER_SAMPLE_MODEL (base) without. A video over 5 min / 50 MB, or unreadable, sets
    `signals_state=failed` with the plain reason in `reel_signals.error`; the job itself finishes (nothing to retry).
    Other errors retry with backoff; the last attempt marks the reel failed.

Helpers other packages may call:
    queue_fetch(user_id, reel_id, *, priority=50, conn=None) -> job_id     # dedupe key fetch_video:<reel_id>
    queue_process(user_id, reel_id, *, words, priority=50, conn=None) -> job_id   # process_reel:<reel_id>:v<N>
    signals_for(user_id, reel_id) -> dict | None                          # the reel_signals row as JSON
    reel_dir(user_id, reel_id) -> Path
"""
from __future__ import annotations

import logging
import os
import random
import threading
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select

from . import db, jobs, storage
from .jobs import Fail, JobCtx, Retry, handler
from .settings import get_settings

log = logging.getLogger("reelvault.jobs_media")

CDN_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")
REFERER = "https://www.instagram.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 "
      "Safari/537.36")
VIDEO_MAX = 50 * 1024 * 1024
COVER_MAX = 2 * 1024 * 1024
GAP_S = (2.0, 5.0)              # pause between two downloads of one member (5.8)
PER_MEMBER = 2                  # downloads at a time per member (per worker process)
EXPIRY_MARGIN = timedelta(seconds=60)
GONE = (403, 404, 410)
TRANSPORT = None                # tests: an httpx transport (MockTransport) for the CDN download

MSG_TOO_LONG = "Video too long. ReelVault reads reels up to 5 minutes."
MSG_FAILED = "ReelVault could not read this video. It will be tagged from its caption and numbers."

_slots: dict[str, threading.BoundedSemaphore] = {}
_last: dict[str, float] = {}
_pace_lock = threading.Lock()


# ============================================================================================== helpers

def signals_version() -> int:
    from ..signals import SIGNALS_VERSION

    return SIGNALS_VERSION


def reel_dir(user_id: str, reel_id: str) -> Path:
    return storage.member_path(user_id, f"reels/{reel_id}")


def queue_fetch(user_id: str, reel_id: str, *, priority: int = 50, conn=None) -> str:
    return jobs.enqueue(user_id, "fetch_video", {"reel_id": reel_id}, priority=priority,
                        dedupe_key=f"fetch_video:{reel_id}", conn=conn)


def queue_process(user_id: str, reel_id: str, *, words: bool, priority: int = 50, conn=None) -> str:
    return jobs.enqueue(user_id, "process_reel", {"reel_id": reel_id, "words": bool(words)}, priority=priority,
                        dedupe_key=f"process_reel:{reel_id}:v{signals_version()}", conn=conn)


def signals_for(user_id: str, reel_id: str) -> dict | None:
    """The member's reel_signals row as a dict (None when missing or not theirs)."""
    with db.read() as conn:
        row = db.get_owned(conn, db.reel_signals, user_id, reel_id, id_col="reel_id")
    return dict(row._mapping) if row is not None else None


def _advance(user_id: str, reel_id: str) -> None:
    try:
        from . import pipeline

        pipeline.advance(user_id, reel_id)
    except NotImplementedError:
        pass
    except Exception:  # noqa: BLE001
        log.exception("pipeline.advance failed for %s", reel_id)


def _get_reel(user_id: str, reel_id: str):
    with db.read() as conn:
        return db.get_owned(conn, db.reels, user_id, reel_id)


def _set_reel(user_id: str, reel_id: str, *, storage_delta: int = 0, **vals) -> None:
    with db.tx() as conn:
        conn.execute(db.reels.update().where(db.reels.c.id == reel_id, db.reels.c.user_id == user_id).values(**vals))
        if storage_delta:
            conn.execute(db.users.update().where(db.users.c.id == user_id)
                         .values(storage_bytes=db.users.c.storage_bytes + storage_delta))


def _need_extension(user_id: str, reel_id: str, why: str) -> dict:
    log.info("reel %s needs the extension for its video (%s)", reel_id, why)
    _set_reel(user_id, reel_id, media_state="need_extension")
    try:
        from . import jobs_ig

        jobs_ig.schedule_need_extension(user_id)
    except Exception:  # noqa: BLE001
        log.exception("could not schedule ig_videos for %s", user_id)
    _advance(user_id, reel_id)
    return {"ok": False, "need_extension": True, "reason": why}


def cdn_url_ok(url: str | None) -> bool:
    if not url:
        return False
    p = urlparse(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host.endswith(s) for s in CDN_SUFFIXES) and not p.username


def _pace(user_id: str) -> threading.BoundedSemaphore:
    """Wait for this member's turn: <= PER_MEMBER downloads at once, GAP_S between two starts."""
    with _pace_lock:
        sem = _slots.setdefault(user_id, threading.BoundedSemaphore(PER_MEMBER))
    sem.acquire()
    while True:
        with _pace_lock:
            now = time.monotonic()
            due = _last.get(user_id, 0.0) + random.uniform(*GAP_S)
            if now >= due or GAP_S[1] <= 0:
                _last[user_id] = now
                return sem
            wait = due - now
        time.sleep(min(wait, 1.0))


def _download(url: str, max_bytes: int, timeout: float):
    from .security import safe_fetch

    return safe_fetch(url, max_bytes=max_bytes, timeout=timeout, allow_http=False, transport=TRANSPORT,
                      headers={"Referer": REFERER, "User-Agent": UA, "Accept": "*/*"})


def looks_like_mp4(data: bytes) -> bool:
    return len(data) > 12 and data[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"wide", b"skip")


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.part")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _dir_bytes(p: Path, *, skip: tuple[str, ...] = ("video.mp4", "cover.jpg")) -> int:
    total = 0
    if p.exists():
        for f in p.rglob("*"):
            if f.is_file() and f.name not in skip:
                total += f.stat().st_size
    return total


# ============================================================================================== fetch_video

@handler("fetch_video", job_class="media")
def fetch_video(job, ctx: JobCtx) -> dict:
    uid, rid = job.user_id, (job.payload or {}).get("reel_id")
    reel = _get_reel(uid, rid) if rid else None
    if reel is None:
        return {"ok": False, "skipped": "gone"}
    if reel.media_state == "have" and reel.video_path:
        try:
            if storage.member_path(uid, reel.video_path).is_file():
                _advance(uid, rid)
                return {"ok": True, "already": True}
        except storage.UnsafePath:
            pass
    if reel.media_state == "deleted" and not reel.starred and reel.tier not in ("priority", "sample") \
            and reel.deep_state != "queued":
        return {"ok": False, "skipped": "deleted"}
    s = get_settings()
    if not s.server_cdn_fetch:
        return _need_extension(uid, rid, "server fetch is off")
    if not cdn_url_ok(reel.video_url):
        return _need_extension(uid, rid, "no usable video address")
    if reel.video_url_expires_at is not None:
        exp = reel.video_url_expires_at
        if exp.tzinfo is None:
            from datetime import timezone

            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= db.utcnow() + EXPIRY_MARGIN:
            return _need_extension(uid, rid, "the video address expired")
    _set_reel(uid, rid, media_state="fetching")
    ctx.progress(0, 1, "Downloading the video")
    sem = _pace(uid)
    try:
        from .security import FetchError

        try:
            res = _download(reel.video_url, VIDEO_MAX, 120)
        except FetchError as e:
            msg = str(e)
            if "too big" in msg:
                _set_reel(uid, rid, media_state="failed", signals_state="failed")
                _advance(uid, rid)
                return {"ok": False, "reason": "too_big"}
            if job.attempts >= job.max_attempts:
                return _need_extension(uid, rid, f"download failed: {msg}")
            _set_reel(uid, rid, media_state="queued")
            raise Retry(60 * job.attempts, "Instagram didn't send the video. We'll try again soon.") from None
        if res.status in GONE:
            return _need_extension(uid, rid, f"CDN answered {res.status}")
        if res.status >= 400 or not looks_like_mp4(res.content):
            if job.attempts >= job.max_attempts or res.status < 500:
                return _need_extension(uid, rid, f"CDN answered {res.status}")
            _set_reel(uid, rid, media_state="queued")
            raise Retry(60 * job.attempts, "Instagram didn't send the video. We'll try again soon.")
        rel_v = f"reels/{rid}/video.mp4"
        dest = storage.member_path(uid, rel_v)
        old = dest.stat().st_size if dest.exists() else 0
        _write_atomic(dest, res.content)
        added = len(res.content) - old
        vals = {"media_state": "have", "video_path": rel_v}
        if reel.cover_url and not reel.cover_path and cdn_url_ok(reel.cover_url):
            try:
                cov = _download(reel.cover_url, COVER_MAX, 30)
                if cov.status == 200 and cov.content[:3] == b"\xff\xd8\xff":
                    rel_c = f"reels/{rid}/cover.jpg"
                    _write_atomic(storage.member_path(uid, rel_c), cov.content)
                    vals["cover_path"] = rel_c
                    added += len(cov.content)
            except Exception:  # noqa: BLE001  (a cover is nice to have)
                log.info("cover download failed for %s", rid)
        _set_reel(uid, rid, storage_delta=added, **vals)
    finally:
        sem.release()
    ctx.progress(1, 1, "Video saved")
    _advance(uid, rid)
    return {"ok": True, "bytes": added}


# ============================================================================================== process_reel

def _write_signals(uid: str, rid: str, values: dict, reel_vals: dict, storage_delta: int = 0) -> None:
    rs = db.reel_signals
    now = db.utcnow()
    row = {"reel_id": rid, "user_id": uid, "created_at": now, **values}
    with db.tx() as conn:
        exists = conn.execute(select(rs.c.reel_id).where(rs.c.reel_id == rid, rs.c.user_id == uid)).first()
        if exists:
            conn.execute(rs.update().where(rs.c.reel_id == rid, rs.c.user_id == uid).values(**row))
        else:
            conn.execute(rs.insert().values(**row))
        conn.execute(db.reels.update().where(db.reels.c.id == rid, db.reels.c.user_id == uid).values(**reel_vals))
        if storage_delta:
            conn.execute(db.users.update().where(db.users.c.id == uid)
                         .values(storage_bytes=db.users.c.storage_bytes + storage_delta))


def _failed(uid: str, rid: str, message: str) -> None:
    _write_signals(uid, rid, {"version": signals_version(), "error": message, "transcript": None, "cuts": None,
                              "frames": None, "sound": None, "edit": None, "visual": None},
                   {"signals_state": "failed"})


@handler("process_reel", job_class="media")
def process_reel(job, ctx: JobCtx) -> dict:
    from .. import signals

    uid = job.user_id
    p = job.payload or {}
    rid = p.get("reel_id")
    words = bool(p.get("words"))
    version = signals_version()
    reel = _get_reel(uid, rid) if rid else None
    if reel is None:
        return {"signals_version": version, "skipped": "gone"}
    video = None
    if reel.video_path:
        try:
            video = storage.member_path(uid, reel.video_path)
        except storage.UnsafePath:
            video = None
    if video is None or not video.is_file():
        # the file is gone (retention sweep, restore): fetch it again first
        if reel.media_state in ("have", "deleted"):
            _set_reel(uid, rid, media_state="none")
        _advance(uid, rid)
        return {"signals_version": version, "skipped": "no_video"}
    s = get_settings()
    model = s.whisper_model if words else s.whisper_sample_model
    out = reel_dir(uid, rid)
    before = _dir_bytes(out)
    steps = {"cuts": 1, "frames": 2, "sound": 3, "transcript": 4, "visual": 5}

    def progress(step: str, message: str) -> None:
        ctx.progress(steps.get(step, 0), 6, message)
        ctx.renew()

    t0 = time.monotonic()
    try:
        sig = signals.process(video, out, ig_audio=reel.audio or None, words=words, whisper_model=model,
                              has_audio=reel.has_audio, whisper_threads=2,
                              whisper_workers=max(1, s.whisper_concurrency), progress=progress)
    except signals.VideoRejected as e:
        _failed(uid, rid, e.message)
        _advance(uid, rid)
        return {"signals_version": version, "error": e.message}
    except Exception:
        log.exception("process_reel failed for %s (attempt %s of %s)", rid, job.attempts, job.max_attempts)
        if job.attempts >= job.max_attempts:
            _failed(uid, rid, MSG_FAILED)
            _advance(uid, rid)
            raise Fail(MSG_FAILED) from None
        raise
    row = sig.to_row()
    reel_vals = {"signals_state": "done"}
    if reel.duration is None and sig.duration:
        reel_vals["duration"] = sig.duration
    if reel.has_audio is None:
        reel_vals["has_audio"] = sig.has_audio
    _write_signals(uid, rid, row, reel_vals, storage_delta=_dir_bytes(out) - before)
    ctx.progress(6, 6, "Done")
    _advance(uid, rid)
    log.info("process_reel %s in %.1fs (%s)", rid, time.monotonic() - t0, sig.timings)
    return {"signals_version": version, "seconds": round(time.monotonic() - t0, 1), "asr": not sig.skipped_asr}
