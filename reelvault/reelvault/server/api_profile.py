"""/api/profile/*, /api/topics/* (DESIGN-SAAS.md 4.4) and the member-facing UI copy (7.3.4). Owner: WP7.

Bodies are read by hand (not pydantic route models) so another member's ids always get 404 before the body is
checked (11.5, tests/server/test_isolation.py).
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select

from . import db, jobs, ratelimit, security, storage
from .errors import ApiError, bad_input, not_found

log = logging.getLogger(__name__)
router = APIRouter()

SOURCE_KINDS = ("skool_group", "skool_profile", "website", "linkedin_pdf", "linkedin_text", "linkedin_page",
                "instagram_bio", "paste")
MAX_SOURCES = 20
TEXT_MAX = 20_000
PDF_MAX = 5 * 1024 * 1024
MAX_TOPICS = 20
SLUG_RE = re.compile(r"^[a-z0-9-]{2,40}$")
COPY_FILE = Path(__file__).resolve().parent.parent / "web" / "copy" / "connector.json"
APPROVE_MSG = "Answer questions 1, 3, 7 and 12 first."


# ============================================================================================== helpers

async def _body(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise bad_input("Send JSON.") from None
    if not isinstance(data, dict):
        raise bad_input("Send a JSON object.")
    return data


def _profile(conn, user_id: str):
    """The member's profile row; created (status empty) when missing. Needs a write transaction then."""
    p = db.profiles
    row = conn.execute(select(p).where(p.c.user_id == user_id, p.c.brand_id == "me")).first()
    if row is None:
        db.upsert(conn, p, {"user_id": user_id, "brand_id": "me", "brand_label": "Me", "status": "empty",
                            "answers": {}, "version": 0, "updated_at": db.utcnow()}, do_nothing=True)
        row = conn.execute(select(p).where(p.c.user_id == user_id, p.c.brand_id == "me")).first()
    return row


def _extension_paired(conn, user_id: str) -> bool:
    d = db.devices
    return conn.execute(select(d.c.id).where(d.c.user_id == user_id, d.c.kind == "extension",
                                             d.c.revoked_at.is_(None))).first() is not None


def _job_json(job_id: str | None) -> dict | None:
    if not job_id:
        return None
    row = jobs.get(job_id)
    return jobs.to_api(row) if row is not None else None


def source_json(s, *, with_job: bool = False) -> dict:
    text = s.text or ""
    meta = s.meta or {}
    out: dict[str, Any] = {"id": s.id, "kind": s.kind, "url": s.url, "via": s.via, "status": s.status,
                           "chars": len(text), "preview": text[:300], "error": s.error,
                           "fetched_at": db.iso(s.fetched_at), "created_at": db.iso(s.created_at)}
    if meta.get("label"):
        out["label"] = meta["label"]
    if meta.get("display_name"):
        out["title"] = meta["display_name"]
    elif meta.get("title"):
        out["title"] = meta["title"]
    found = meta.get("found_links") or []
    if found:
        out["found"] = found[:4]
    if with_job and s.job_id and s.status in ("queued", "waiting"):
        out["job"] = _job_json(s.job_id)
    return out


def _answers_out(answers: dict) -> dict:
    from ..importers import merge

    out = {}
    for qid in merge.QIDS:
        a = answers.get(qid)
        out[qid] = dict(a) if isinstance(a, dict) else merge.empty_answer()
    return out


def _questions() -> list[dict]:
    from ..importers import merge

    return [{k: v for k, v in q.items() if k != "prefill"} | {"prefill_from": q.get("prefill") or ""}
            for q in merge.QUESTIONS]


def profile_json(user_id: str) -> dict:
    from ..importers import merge

    with db.read() as conn:
        missing_row = conn.execute(select(db.profiles.c.user_id).where(
            db.profiles.c.user_id == user_id, db.profiles.c.brand_id == "me")).first() is None
    if missing_row:
        with db.tx() as conn:
            _profile(conn, user_id)
    with db.read() as conn:
        prof = _profile(conn, user_id)
        srcs = conn.execute(db.scoped(conn, db.profile_sources, user_id)
                            .order_by(db.profile_sources.c.created_at)).all()
        jrow = conn.execute(select(db.jobs).where(db.jobs.c.user_id == user_id, db.jobs.c.kind == "draft_profile",
                                                  db.jobs.c.state.in_(db.ACTIVE_JOB_STATES))
                            .order_by(db.jobs.c.created_at.desc())).first()
    answers = dict(prof.answers or {})
    draft = answers.get("_draft") if isinstance(answers.get("_draft"), dict) else {}
    missing = [q for q in merge.REQUIRED if merge.is_empty(answers.get(q))]
    out: dict[str, Any] = {
        "status": prof.status, "answers": _answers_out(answers), "questions": _questions(),
        "sources": [source_json(s, with_job=True) for s in srcs], "digest": prof.digest,
        "version": prof.version or 0, "approved_at": db.iso(prof.approved_at), "updated_at": db.iso(prof.updated_at),
        "required": list(merge.REQUIRED), "missing_required": missing,
        "draft": {k: draft.get(k) for k in ("niche_label", "search_terms", "competitor_handles", "at")
                  if draft.get(k)},
    }
    if jrow is not None:
        out["drafting_job"] = jobs.to_api(jrow)
    return out


# ============================================================================================== profile

@router.get("/api/profile")
def get_profile(user=Depends(security.current_user)):
    return profile_json(user.id)


@router.put("/api/profile/answers")
async def put_answers(request: Request, user=Depends(security.current_user)):
    from ..importers import merge

    body = await _body(request)
    incoming = body.get("answers")
    if not isinstance(incoming, dict) or not incoming:
        raise bad_input("Send the answers you changed.")
    unknown = [k for k in incoming if k not in merge.BY_ID]
    if unknown:
        raise bad_input(f"Unknown question: {str(unknown[0])[:20]}.")
    p = db.profiles
    with db.tx() as conn:
        prof = _profile(conn, user.id)
        answers = dict(prof.answers or {})
        changed = False
        for qid, val in incoming.items():
            try:
                new = merge.normalize_answer(qid, val, answers.get(qid))
            except merge.BadAnswer as e:
                raise bad_input(str(e)) from None
            if new != answers.get(qid):
                answers[qid] = new
                changed = True
        status = prof.status
        if changed and status in ("approved", "empty"):
            status = "draft"
        conn.execute(p.update().where(p.c.user_id == user.id, p.c.brand_id == "me").values(
            answers=answers, status=status, updated_at=db.utcnow()))
    return profile_json(user.id)


def _covered_and_samples(conn, user_id: str) -> tuple[list[str], list[str]]:
    """Topics of the member's own reels (last 180 days) and the first lines of their best own reels (9.4)."""
    from datetime import timedelta

    a, r, acc = db.analyses, db.reels, db.accounts
    own = [x[0] for x in conn.execute(select(acc.c.id).where(acc.c.user_id == user_id, acc.c.role == "self"))]
    if not own:
        return [], []
    since = db.utcnow() - timedelta(days=180)
    covered: list[str] = []
    for row in conn.execute(select(a.c.topics, a.c.overrides).join(r, r.c.id == a.c.reel_id).where(
            a.c.user_id == user_id, r.c.user_id == user_id, r.c.account_id.in_(own), a.c.level == "quick",
            r.c.taken_at >= since)):
        ov = row.overrides or {}
        for t in (ov.get("topics") if isinstance(ov.get("topics"), list) else row.topics) or []:
            if isinstance(t, str) and t not in covered:
                covered.append(t)
    samples: list[str] = []
    for row in conn.execute(select(r.c.id, r.c.caption).where(r.c.user_id == user_id, r.c.account_id.in_(own))
                            .order_by(r.c.score.desc().nullslast()).limit(10)):
        sig = conn.execute(select(db.reel_signals.c.transcript).where(db.reel_signals.c.reel_id == row.id)).first()
        line = ""
        tr = (sig[0] if sig else None) or {}
        if isinstance(tr, dict) and isinstance(tr.get("text"), str):
            line = re.split(r"(?<=[.!?])\s", tr["text"].strip(), maxsplit=1)[0]
        if not line and row.caption:
            line = row.caption.strip().split("\n", 1)[0]
        if line:
            samples.append(line[:200])
        if len(samples) >= 5:
            break
    return covered[:20], samples


@router.post("/api/profile/approve")
def approve(user=Depends(security.current_user)):
    from ..importers import merge

    p = db.profiles
    with db.tx() as conn:
        prof = _profile(conn, user.id)
        answers = dict(prof.answers or {})
        missing = [q for q in merge.REQUIRED if merge.is_empty(answers.get(q))]
        if missing:
            raise ApiError(400, "bad_input", APPROVE_MSG, detail={"missing": missing})
        covered, samples = _covered_and_samples(conn, user.id)
        digest = merge.build_digest(answers, covered_topics=covered, samples=samples)
        for qid in merge.QIDS:  # approving confirms what is on the page
            if isinstance(answers.get(qid), dict) and not merge.is_empty(answers[qid]):
                answers[qid] = dict(answers[qid], confirmed=True)
        version = int(prof.version or 0) + 1
        now = db.utcnow()
        conn.execute(p.update().where(p.c.user_id == user.id, p.c.brand_id == "me").values(
            status="approved", digest=digest, version=version, answers=answers, approved_at=now, updated_at=now))
        has_topics = conn.execute(select(func.count()).select_from(db.topics).where(
            db.topics.c.user_id == user.id)).scalar() or 0
        db.audit("profile_approved", user_id=user.id, actor=user.id, detail={"version": version}, conn=conn)
    if not has_topics and user.ai_mode:
        try:  # 9 step 4: topics are suggested from the profile now (0 is fine)
            from . import work

            work.queue_topics(user.id, "profile")
        except Exception:  # noqa: BLE001
            log.exception("queue_topics after approve failed")
    try:  # can_make depends on q12: re-sort the cards
        from . import jobs_score

        jobs_score.queue_insights(user.id)
    except Exception:  # noqa: BLE001
        log.exception("queue_insights after approve failed")
    return {"status": "approved", "version": version, "digest": digest}


@router.post("/api/profile/draft")
def draft(user=Depends(security.current_user)):
    from ..importers import merge
    from . import work

    with db.tx() as conn:
        prof = _profile(conn, user.id)
        ok_sources = conn.execute(select(func.count()).select_from(db.profile_sources).where(
            db.profile_sources.c.user_id == user.id, db.profile_sources.c.status == "ok")).scalar() or 0
    answers = dict(prof.answers or {})
    answered = sum(1 for q in merge.QIDS if not merge.is_empty(answers.get(q)))
    if not ok_sources and answered < 3:
        raise bad_input("Add a link we can read, or answer 3 questions first.")
    jid = work.queue_profile_draft(user.id)
    return {"job": _job_json(jid)}


# ============================================================================================== sources

def _clean_text(text: Any) -> str:
    from ..importers import linkedin

    if not isinstance(text, str) or not text.strip():
        raise bad_input("Paste some text first.")
    if len(text) > TEXT_MAX:
        raise bad_input("That text is too long. Keep it under 20,000 characters.")
    return linkedin.clean_paste(text)


def _count_sources(conn, user_id: str) -> int:
    return conn.execute(select(func.count()).select_from(db.profile_sources).where(
        db.profile_sources.c.user_id == user_id)).scalar() or 0


def _insert_source(conn, user_id: str, **vals) -> str:
    sid = db.new_id("src")
    now = db.utcnow()
    conn.execute(db.profile_sources.insert().values(id=sid, user_id=user_id, created_at=now, meta={}, **vals))
    return sid


def _read_source(conn, user_id: str, sid: str):
    return db.get_owned(conn, db.profile_sources, user_id, sid)


async def _pdf_upload(request: Request, user) -> tuple[dict, Any]:
    form = await request.form()
    kind = form.get("kind") or "linkedin_pdf"
    if kind != "linkedin_pdf":
        raise bad_input("Only a LinkedIn PDF can be uploaded.")
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        raise bad_input("Choose your LinkedIn PDF first.")
    data = await f.read(PDF_MAX + 1)
    if len(data) > PDF_MAX:
        raise ApiError(413, "too_large", "That PDF is bigger than 5 MB.")
    if not data.startswith(b"%PDF"):
        raise bad_input("That file is not a PDF. On LinkedIn, open your profile, click Resources, then Save to PDF.")
    return {"kind": "linkedin_pdf"}, data


@router.post("/api/profile/sources", status_code=201)
async def add_source(request: Request, user=Depends(security.current_user)):
    from ..importers import instagram, skool, website

    ctype = (request.headers.get("content-type") or "").lower()
    pdf = None
    if ctype.startswith("multipart/form-data"):
        body, pdf = await _pdf_upload(request, user)
    else:
        body = await _body(request)
    kind = body.get("kind")
    if kind == "skool":  # convenience: the UI may send one "Skool link" box
        try:
            kind, _ = skool.normalize_url(body.get("url") or "")
        except ValueError as e:
            raise bad_input(str(e)) from None
    if kind not in SOURCE_KINDS:
        raise bad_input("Pick what kind of link this is.")
    via_req = body.get("via")
    if via_req not in (None, "extension", "server"):
        raise bad_input("via must be extension or server.")
    label = body.get("label") if isinstance(body.get("label"), str) else ""
    ratelimit.hit("profile_import", user.id, 20, "day",
                  message="You added 20 links today. Paste text instead, or try again tomorrow.")

    with db.tx(immediate=False) as conn:
        if _count_sources(conn, user.id) >= MAX_SOURCES:
            raise bad_input("You have 20 sources. Delete one before adding another.")
        paired = _extension_paired(conn, user.id)
    skool_fetch = bool(db.get_setting("skool_fetch"))

    job_id = None
    if kind in ("paste", "linkedin_text"):
        text = _clean_text(body.get("text"))
        with db.tx() as conn:
            sid = _insert_source(conn, user.id, kind=kind, via="paste", status="ok", text=text, fetched_at=db.utcnow())
            if label:
                conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == sid).values(
                    meta={"label": label[:80]}))
    elif kind == "linkedin_pdf":
        if pdf is None:
            raise bad_input("Upload the PDF as a file.")
        with db.tx() as conn:
            sid = _insert_source(conn, user.id, kind=kind, via="upload", status="queued")
            rel = f"sources/{sid}.pdf"
            path = storage.member_path(user.id, rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(pdf)
            job_id = jobs.enqueue(user.id, "import_source", {"source_id": sid}, executor="server", priority=10,
                                  dedupe_key=f"import_source:{sid}", conn=conn,
                                  progress={"done": 0, "total": 1, "message": "Reading your PDF"})
            conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == sid).values(
                file_path=rel, job_id=job_id))
    elif kind == "website":
        try:
            url = website.normalize_url(body.get("url") or "")
        except ValueError as e:
            raise bad_input(str(e)) from None
        with db.tx() as conn:
            sid = _insert_source(conn, user.id, kind=kind, url=url, via="server", status="queued")
            job_id = jobs.enqueue(user.id, "import_source", {"source_id": sid}, executor="server", priority=10,
                                  dedupe_key=f"import_source:{sid}", conn=conn,
                                  progress={"done": 0, "total": 1, "message": "Reading your website"})
            conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == sid).values(job_id=job_id))
    elif kind in ("skool_group", "skool_profile"):
        try:
            real_kind, url = skool.normalize_url(body.get("url") or "")
        except ValueError as e:
            raise bad_input(str(e)) from None
        kind = real_kind
        via = via_req or ("extension" if paired else "server" if skool_fetch else None)
        if via == "extension" and not paired:
            raise bad_input("Connect the Chrome extension first, or paste the text.")
        if via == "server" and not skool_fetch:
            raise bad_input("This server doesn't read Skool pages. Connect the Chrome extension or paste the text.")
        if via == "server":
            ratelimit.hit("skool_fetch", user.id, 5, "day",
                          message="You can read 5 Skool pages a day. Paste the text instead.")
        with db.tx() as conn:
            sid = _insert_source(conn, user.id, kind=kind, url=url, via=via or "paste",
                                 status="queued" if via else "needs_paste",
                                 error=None if via else ("Connect the Chrome extension to read this page, or "
                                                         "paste your About text here."))
            if via == "server":
                job_id = jobs.enqueue(user.id, "import_source", {"source_id": sid}, executor="server",
                                      priority=10, dedupe_key=f"import_source:{sid}", conn=conn,
                                      progress={"done": 0, "total": 1, "message": "Reading your Skool page"})
                conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == sid).values(job_id=job_id))
        if via == "extension":
            job_id = _page_read(user.id, sid, kind, url)
    elif kind == "linkedin_page":
        with db.tx() as conn:
            sid = _insert_source(conn, user.id, kind=kind, url="https://www.linkedin.com/in/me/",
                                 via="extension" if paired else "paste",
                                 status="queued" if paired else "needs_paste",
                                 error=None if paired else ("Connect the Chrome extension to read your LinkedIn, "
                                                            "upload the PDF, or paste the text."))
        if paired:
            job_id = _page_read(user.id, sid, "linkedin_self", "https://www.linkedin.com/in/me/")
    elif kind == "instagram_bio":
        try:
            handle = instagram.normalize_handle(body.get("url") or body.get("handle") or body.get("text") or "")
        except ValueError as e:
            raise bad_input(str(e)) from None
        url = f"https://www.instagram.com/{handle}/"
        with db.tx() as conn:
            sid = _insert_source(conn, user.id, kind=kind, url=url, via="extension" if paired else "paste",
                                 status="queued" if paired else "needs_paste",
                                 error=None if paired else ("Connect the Chrome extension to read your bio, or "
                                                            "paste it here."))
        if paired:
            from . import jobs_ig

            try:
                job_id = jobs_ig.request_profile(user.id, handle, "member_bio", source_id=sid)
            except ApiError:
                _mark_paste(user.id, sid, "We couldn't ask your extension. Paste your bio here.")
                raise
    else:  # pragma: no cover - SOURCE_KINDS is closed
        raise bad_input("Unknown kind.")

    with db.read() as conn:
        src = _read_source(conn, user.id, sid)
    out: dict[str, Any] = {"source": source_json(src)}
    if job_id:
        out["job"] = _job_json(job_id)
    return out


def _mark_paste(user_id: str, sid: str, msg: str) -> None:
    with db.tx() as conn:
        conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == sid,
                                                       db.profile_sources.c.user_id == user_id).values(
            status="needs_paste", via="paste", error=msg))


def _page_read(user_id: str, sid: str, kind: str, url: str) -> str | None:
    from . import jobs_ig

    try:
        return jobs_ig.request_page_read(user_id, sid, kind, url)
    except ApiError as e:
        _mark_paste(user_id, sid, f"{e.message} You can paste the text here instead.")
        if e.status == 429:
            return None
        raise


@router.delete("/api/profile/sources/{source_id}")
def delete_source(source_id: str, user=Depends(security.current_user)):
    with db.tx() as conn:
        src = _read_source(conn, user.id, source_id)
        if src is None:
            raise not_found()
        job_id = src.job_id if src.status in ("queued", "waiting") else None
        db.delete_owned(conn, db.profile_sources, user.id, source_id)
    if job_id:
        try:
            jobs.cancel(job_id, user.id)
        except Exception:  # noqa: BLE001
            log.exception("cancel source job failed")
    if src.file_path:
        try:
            storage.member_path(user.id, src.file_path).unlink(missing_ok=True)
        except (OSError, storage.UnsafePath):
            pass
    return {"ok": True}


@router.post("/api/profile/sources/{source_id}/paste")
async def paste_source(source_id: str, request: Request, user=Depends(security.current_user)):
    with db.read() as conn:
        if _read_source(conn, user.id, source_id) is None:
            raise not_found()
    body = await _body(request)
    text = _clean_text(body.get("text"))
    with db.tx() as conn:
        src = _read_source(conn, user.id, source_id)
        if src is None:
            raise not_found()
        conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == source_id,
                                                       db.profile_sources.c.user_id == user.id).values(
            text=text, status="ok", via="paste", error=None, fetched_at=db.utcnow()))
        job_id = src.job_id if src.status in ("queued", "waiting") else None
        src = _read_source(conn, user.id, source_id)
    if job_id:
        try:
            jobs.cancel(job_id, user.id)
        except Exception:  # noqa: BLE001
            pass
    return {"source": source_json(src)}


# ============================================================================================== topics

def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40].strip("-")


def topic_json(t, seen_in: int | None = None) -> dict:
    out = {"id": t.id, "slug": t.slug, "label": t.label, "description": t.description or "",
           "examples": list(t.examples or []), "source": t.source, "status": t.status, "position": t.position}
    if seen_in is not None:
        out["seen_in"] = seen_in
    return out


def _topics(conn, user_id: str, status: str) -> list:
    t = db.topics
    return conn.execute(select(t).where(t.c.user_id == user_id, t.c.brand_id == "me", t.c.status == status)
                        .order_by(t.c.position, t.c.created_at)).all()


def _tag_stats(conn, user_id: str) -> tuple[int, dict]:
    """(reels in Unsorted, {slug: reels whose quick tags suggested that new topic})."""
    a = db.analyses
    unsorted = 0
    seen: dict[str, int] = {}
    for row in conn.execute(select(a.c.topics, a.c.overrides, a.c.data).where(a.c.user_id == user_id,
                                                                              a.c.level == "quick")):
        ov = row.overrides or {}
        tops = ov.get("topics") if isinstance(ov.get("topics"), list) else row.topics
        if not tops:
            unsorted += 1
        nt = (row.data or {}).get("new_topic") if isinstance(row.data, dict) else None
        if isinstance(nt, str) and nt.strip():
            key = _slugify(nt.removeprefix("new:"))
            seen[key] = seen.get(key, 0) + 1
    return unsorted, seen


def topics_json(user_id: str) -> dict:
    with db.read() as conn:
        active = _topics(conn, user_id, "active")
        sugg = _topics(conn, user_id, "suggested")
        unsorted, seen = _tag_stats(conn, user_id)
        jrow = conn.execute(select(db.jobs).where(db.jobs.c.user_id == user_id, db.jobs.c.kind.in_(
            ("suggest_topics", "retag_topics")), db.jobs.c.state.in_(db.ACTIVE_JOB_STATES))
            .order_by(db.jobs.c.created_at.desc())).first()
    out = {"topics": [topic_json(t) for t in active],
           "suggested": [topic_json(t, seen.get(t.slug, seen.get(_slugify(t.label), 0))) for t in sugg],
           "unsorted_reels": unsorted, "max": MAX_TOPICS}
    if jrow is not None:
        out["job"] = jobs.to_api(jrow)
    return out


def _retag(user_id: str) -> dict | None:
    from . import work

    try:
        jid = work.queue_retag_topics(user_id)
    except Exception:  # noqa: BLE001
        log.exception("queue_retag_topics failed")
        return None
    return _job_json(jid)


@router.get("/api/topics")
def get_topics(user=Depends(security.current_user)):
    return topics_json(user.id)


@router.post("/api/topics/suggest")
async def suggest_topics(request: Request, user=Depends(security.current_user)):
    from ..importers import merge
    from . import work

    body = await _body(request)
    src = body.get("from") or "profile"
    if src not in ("profile", "reels"):
        raise bad_input("from must be profile or reels.")
    with db.read() as conn:
        if src == "profile":
            ok_sources = conn.execute(select(func.count()).select_from(db.profile_sources).where(
                db.profile_sources.c.user_id == user.id, db.profile_sources.c.status == "ok")).scalar() or 0
            prof = conn.execute(select(db.profiles.c.answers).where(db.profiles.c.user_id == user.id)).first()
            q1 = ((prof[0] or {}) if prof else {}).get("q1")
            if not ok_sources and merge.is_empty(q1):
                raise bad_input("Add a link or answer question 1 first.")
        else:
            tagged = conn.execute(select(func.count()).select_from(db.reels).where(
                db.reels.c.user_id == user.id, db.reels.c.tag_state == "done")).scalar() or 0
            if tagged < 30:
                raise bad_input(f"We need 30 tagged reels to suggest topics from them. You have {tagged}.")
    jid = work.queue_topics(user.id, src)
    return {"job": _job_json(jid)}


@router.post("/api/topics/accept")
async def accept_topics(request: Request, user=Depends(security.current_user)):
    body = await _body(request)
    ids = body.get("ids") or []
    dismiss = body.get("dismiss") or []
    if not isinstance(ids, list) or not isinstance(dismiss, list) or not all(isinstance(x, str) for x in ids + dismiss):
        raise bad_input("Send lists of topic ids.")
    t = db.topics
    with db.tx() as conn:
        active = len(_topics(conn, user.id, "active"))
        pos = conn.execute(select(func.coalesce(func.max(t.c.position), 0)).where(t.c.user_id == user.id)).scalar()
        accepted = 0
        for tid in dict.fromkeys(ids):
            row = db.get_owned(conn, t, user.id, tid)
            if row is None or row.status != "suggested":
                continue
            if active + accepted >= MAX_TOPICS:
                raise bad_input("You can have up to 20 topics. Remove one first.")
            pos = int(pos or 0) + 1
            conn.execute(t.update().where(t.c.id == tid, t.c.user_id == user.id).values(status="active", position=pos))
            accepted += 1
        for tid in dict.fromkeys(dismiss):
            row = db.get_owned(conn, t, user.id, tid)
            if row is not None and row.status == "suggested":
                conn.execute(t.update().where(t.c.id == tid, t.c.user_id == user.id).values(status="archived"))
    out = topics_json(user.id)
    if accepted:
        job = _retag(user.id)
        if job:
            out["retag_job"] = job
    return out


def _clean_topic(item: Any, i: int) -> dict:
    if not isinstance(item, dict):
        raise bad_input("Each topic needs a label.")
    label = (item.get("label") or "").strip() if isinstance(item.get("label"), str) else ""
    if not label:
        raise bad_input(f"Topic {i + 1} needs a name.")
    if len(label) > 40:
        raise bad_input(f"Topic {i + 1}: keep the name under 40 characters.")
    desc = item.get("description") or ""
    if not isinstance(desc, str) or len(desc) > 200:
        raise bad_input(f"Topic {i + 1}: keep the description under 200 characters.")
    ex = item.get("examples") or []
    if isinstance(ex, str):
        ex = [x.strip() for x in ex.split("\n") if x.strip()]
    if not isinstance(ex, list) or len(ex) > 5 or not all(isinstance(x, str) and len(x) <= 160 for x in ex):
        raise bad_input(f"Topic {i + 1}: add up to 5 short examples.")
    slug = item.get("slug") or _slugify(label)
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        slug = _slugify(label)
    if not SLUG_RE.match(slug or ""):
        raise bad_input(f"Topic {i + 1}: use at least 2 letters or numbers in the name.")
    tid = item.get("id") if isinstance(item.get("id"), str) else None
    return {"id": tid, "slug": slug, "label": label, "description": desc.strip(), "examples": [x.strip() for x in ex]}


@router.put("/api/topics")
async def put_topics(request: Request, user=Depends(security.current_user)):
    """Full list, in order (0-20). Existing topics keep their slug (reels are tagged by slug), so a rename only
    changes the label. A new topic whose slug matches an archived or suggested one brings that one back."""
    body = await _body(request)
    items = body.get("topics")
    if not isinstance(items, list):
        raise bad_input("Send the full list of topics.")
    if len(items) > MAX_TOPICS:
        raise bad_input("You can have up to 20 topics.")
    clean = [_clean_topic(x, i) for i, x in enumerate(items)]
    t = db.topics
    with db.tx() as conn:
        rows = {r.id: r for r in conn.execute(select(t).where(t.c.user_id == user.id, t.c.brand_id == "me"))}
        by_slug = {r.slug: r for r in rows.values()}
        before = {(r.slug, r.description or "") for r in rows.values() if r.status == "active"}
        used: set[str] = set()
        plan: list[tuple[str | None, dict]] = []
        for c in clean:
            row = rows.get(c["id"]) if c["id"] else None
            if c["id"] and row is None:
                raise not_found()
            if row is None:
                cand = by_slug.get(c["slug"])
                if cand is not None and cand.status != "active" and cand.id not in used:
                    row = cand
            if row is not None:
                if row.id in used:
                    raise bad_input("The same topic is in the list twice.")
                used.add(row.id)
                plan.append((row.id, dict(c, slug=row.slug)))
            else:
                plan.append((None, c))
        taken = {r.slug for r in rows.values()}
        final = []
        for rid, c in plan:
            if rid is None:
                base, n = c["slug"], 2
                while c["slug"] in taken:
                    c = dict(c, slug=f"{base[:36]}-{n}")
                    n += 1
                taken.add(c["slug"])
            final.append((rid, c))
        slugs = [c["slug"] for _, c in final]
        if len(set(slugs)) != len(slugs):
            raise bad_input("Two topics have the same name. Give each one its own name.")
        keep: set[str] = set()
        for pos, (rid, c) in enumerate(final, start=1):
            vals = {"label": c["label"], "description": c["description"], "examples": c["examples"],
                    "status": "active", "position": pos}
            if rid is None:
                rid = db.new_id("top")
                conn.execute(t.insert().values(id=rid, user_id=user.id, brand_id="me", slug=c["slug"],
                                               source="member", created_at=db.utcnow(), **vals))
            else:
                conn.execute(t.update().where(t.c.id == rid, t.c.user_id == user.id).values(**vals))
            keep.add(rid)
        for r in rows.values():
            if r.status == "active" and r.id not in keep:
                conn.execute(t.update().where(t.c.id == r.id).values(status="archived"))
        after = {(c["slug"], c["description"]) for _, c in final}
        changed = before != after
    out = topics_json(user.id)
    if changed:
        job = _retag(user.id)
        if job:
            out["retag_job"] = job
    return out


# ============================================================================================== UI copy (7.3.4)

def copy_defaults() -> dict:
    try:
        return json.loads(COPY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("connector.json is missing or broken")
        return {}


def _fill(v: Any, host: str, mcp_url: str) -> Any:
    if isinstance(v, str):
        return v.replace("{MCP_URL}", mcp_url).replace("{HOST}", host)
    if isinstance(v, list):
        return [_fill(x, host, mcp_url) for x in v]
    return v


def ui_copy() -> dict:
    from .settings import get_settings

    s = get_settings()
    host = s.public_host or "localhost"
    mcp_url = f"{s.public_origin}/mcp" if s.public_origin else "/mcp"
    merged = dict(copy_defaults())
    for k, v in (db.get_setting("copy_overrides") or {}).items():
        if isinstance(k, str) and isinstance(v, (str, list)) and v not in ("", []):
            merged[k] = v
    return {k: _fill(v, host, mcp_url) for k, v in merged.items() if not k.startswith("_")}


@router.get("/api/copy")
def get_copy(user=Depends(security.current_user)):
    """The editable UI copy (Claude connector steps and notes), defaults merged with the admin's overrides."""
    out = {"copy": ui_copy()}
    if user.role == "admin":
        out["defaults"] = {k: v for k, v in copy_defaults().items() if not k.startswith("_")}
    return out
