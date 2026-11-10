"""Job handler `import_source` (DESIGN-SAAS.md 3.13.1, 9.2): server-side profile sources only. Owner: WP7.

payload {"source_id"}. Kinds read here: `website` (trafilatura), `skool_group`/`skool_profile` (fallback A, only
while `app_settings.skool_fetch` is true), `linkedin_pdf` (pypdf). Skool/LinkedIn pages read in the member's browser
are WP2's `page_read` jobs; the member's Instagram bio is WP2's `ig_profile` job.
No transaction is held across the fetch: read the row, fetch, then write the result.
"""
from __future__ import annotations

import logging

from . import db, jobs, storage
from .settings import get_settings

log = logging.getLogger(__name__)
SERVER_KINDS = ("website", "skool_group", "skool_profile", "linkedin_pdf")


def _write(user_id: str, source_id: str, *, status: str, text: str = "", meta: dict | None = None,
           error: str | None = None) -> None:
    with db.tx() as conn:
        src = db.get_owned(conn, db.profile_sources, user_id, source_id)
        if src is None:
            return
        conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == source_id,
                                                       db.profile_sources.c.user_id == user_id).values(
            status=status, text=(text or "")[:20_000], meta=dict(src.meta or {}, **(meta or {})),
            error=error, fetched_at=db.utcnow()))


def read_source(user_id: str, src) -> dict:
    """Run the importer for one source row. Returns {"status", "text", "meta", "error"}."""
    from ..importers import linkedin, skool, website

    if src.kind == "website":
        try:
            res = website.fetch_site(src.url or "")
        except ValueError as e:
            return {"status": "failed", "text": "", "meta": {}, "error": str(e)}
        return {"status": res.status, "text": res.text, "meta": res.meta, "error": res.error}
    if src.kind in ("skool_group", "skool_profile"):
        if not db.get_setting("skool_fetch"):
            return {"status": "needs_paste", "text": "", "meta": {},
                    "error": "Connect the Chrome extension to read Skool pages, or paste your text here."}
        try:
            out = skool.fetch_page(src.url or "", public_origin=get_settings().public_origin)
        except ValueError as e:
            return {"status": "failed", "text": "", "meta": {}, "error": str(e)}
        return {"status": out.get("status", "ok"), "text": out.get("text", ""), "meta": out.get("meta") or {},
                "error": out.get("error")}
    if src.kind == "linkedin_pdf":
        try:
            path = storage.member_path(user_id, src.file_path or "")
            data = path.read_bytes()
        except (OSError, storage.UnsafePath):
            return {"status": "failed", "text": "", "meta": {}, "error": "We lost that PDF. Upload it again."}
        try:
            text = linkedin.pdf_text(data)
        except linkedin.PdfProblem as e:
            return {"status": "needs_paste", "text": "", "meta": {}, "error": str(e)}
        return {"status": "ok", "text": text, "meta": {"pages_text_chars": len(text)}, "error": None}
    return {"status": "failed", "text": "", "meta": {}, "error": "This kind of source is not read on the server."}


@jobs.handler("import_source", job_class="light")
def import_source(job, ctx) -> dict:
    sid = (job.payload or {}).get("source_id")
    with db.read() as conn:
        src = db.get_owned(conn, db.profile_sources, job.user_id, sid) if sid else None
    if src is None:
        raise jobs.Fail("That source was deleted.", code="not_found")
    ctx.progress(0, 1, "Reading your page")
    try:
        out = read_source(job.user_id, src)
    except Exception:  # noqa: BLE001 - never leave a source stuck in "queued"
        log.exception("import_source %s crashed", src.id)
        out = {"status": "needs_paste", "text": "", "meta": {},
               "error": "Something went wrong while reading that page. Paste the text instead."}
    _write(job.user_id, src.id, status=out["status"], text=out["text"], meta=out["meta"], error=out["error"])
    ctx.progress(1, 1, "Done" if out["status"] == "ok" else (out["error"] or "Needs your help"))
    log.info("import_source %s kind=%s status=%s chars=%d", src.id, src.kind, out["status"], len(out["text"] or ""))
    return {"status": out["status"]}


@jobs.on_lease_expired("import_source")
def _expired(conn, job) -> None:
    sid = (job.payload or {}).get("source_id")
    if not sid or not job.user_id or job.attempts < job.max_attempts:
        return  # it runs again; only the last expiry gives up
    conn.execute(db.profile_sources.update().where(db.profile_sources.c.id == sid,
                                                   db.profile_sources.c.user_id == job.user_id,
                                                   db.profile_sources.c.status == "queued").values(
        status="needs_paste", error="Reading that page took too long. Paste the text instead."))
