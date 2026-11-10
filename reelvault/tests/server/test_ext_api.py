"""Extension pairing + device API + ingest (DESIGN-SAAS.md 4.6, 5, 5.8, 11.4, 12.2 test_ext_api). WP2."""
import importlib.util
import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from reelvault import igparse
from reelvault.server import db, jobs, jobs_ig
from reelvault.server.testing import BASE_URL

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ig"
_spec = importlib.util.spec_from_file_location("rv_mkfx", FIX / "make_fixtures.py")
mk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mk)

PK = str(mk.TARGET["pk"])
TIMELINE = mk.account_timeline(250)


def trimmed(raw_list):
    """Raw media dicts -> IgMedia exactly as the extension uploads them (bigints quoted, then trimmed)."""
    text = mk.dumps({"items": raw_list})
    return [igparse.trim_media(m) for m in igparse.parse_ig_json(text)["items"]]


def ig_user(followers=182_000, u=None):
    raw = mk.web_profile_info(u or mk.TARGET, followers=followers)["data"]["user"]
    return igparse.trim_user(igparse.parse_ig_json(mk.dumps(raw)))


# ============================================================================================== helpers

@pytest.fixture
def anon(app):
    with TestClient(app, base_url=BASE_URL) as c:
        yield c


def add_account(user, handle="scaleit.demo", ig_pk=None, role="competitor"):
    aid = db.new_id("acc")
    with db.tx() as conn:
        conn.execute(db.accounts.insert().values(id=aid, user_id=user.id, handle=handle, ig_pk=ig_pk, role=role))
    return aid


def confirm(device_id, ig_user_id="61000777", username="member.reader"):
    with db.tx() as conn:
        conn.execute(db.devices.update().where(db.devices.c.id == device_id).values(
            ig_user_id=ig_user_id, ig_username=username, ig_confirmed_user_id=ig_user_id))


class Ext:
    """A tiny stand-in for the extension's service worker."""

    def __init__(self, client, headers):
        self.c, self.h = client, headers

    def post(self, path, body=None, **kw):
        return self.c.post("/api/ext/v2" + path, json=body if body is not None else {}, headers=self.h, **kw)

    def lease(self, kinds=jobs_ig.EXT_KINDS, budget=None):
        r = self.post("/jobs/lease", {"kinds": list(kinds), "budget": budget or {"hour_used": 0, "day_used": 0,
                                                                                  "cdn_day_used": 0}})
        assert r.status_code == 200, r.text
        return r.json()

    def items(self, job_id, raw, *, max_id, more, source="clips", account=None, request_max_id="__omit__"):
        paging = {"max_id": max_id, "more_available": more, "source": source}
        if request_max_id != "__omit__":
            paging["request_max_id"] = request_max_id
        body = {"page": 1, "paging": paging, "items": trimmed(raw)}
        if account is not None:
            body["account"] = account
        return self.post(f"/jobs/{job_id}/items", body)


@pytest.fixture
def ext(anon, member, make_device):
    did, h = make_device(member)
    e = Ext(anon, h)
    e.device_id = did
    return e


def pull_pages(e, job_id, pages, *, size=50, start=0, account=True, source="clips"):
    """Upload `pages` pages of TIMELINE starting at offset `start`; returns the last response json."""
    out = None
    off = start
    for i in range(pages):
        chunk = TIMELINE[off:off + size]
        nxt = off + size
        more = nxt < len(TIMELINE)
        req = mk.cursor_for(off) if off else None
        r = e.items(job_id, chunk, max_id=mk.cursor_for(nxt) if more else None, more=more, source=source,
                    account=ig_user() if (account and i == 0) else None, request_max_id=req)
        assert r.status_code == 200, r.text
        out = r.json()
        off = nxt
        if out["stop"] or out.get("resume"):
            break
    return out


def account_row(aid):
    with db.tx(immediate=False) as conn:
        return conn.execute(select(db.accounts).where(db.accounts.c.id == aid)).first()


def count_reels(uid):
    with db.tx(immediate=False) as conn:
        return conn.execute(select(func.count()).select_from(db.reels).where(db.reels.c.user_id == uid)).scalar()


# ============================================================================================== pairing

def test_pair_claim_status_and_revoke(member_client, anon):
    r = member_client.post("/api/ext/pair", json={})
    assert r.status_code == 200
    body = r.json()
    code = body["code"]
    assert body["pair_link"] == f"http://localhost/pair#{code}" and len(code) == 9 and code[4] == "-"
    st = member_client.get("/api/ext/status").json()
    assert st["paired"] is False and st["latest_version"] == "2.0.0"

    c = anon.post("/api/ext/v2/pair/claim", json={"code": code.lower(), "name": "Chrome on Windows",
                                                  "version": "2.0.0", "kind": "extension"})
    assert c.status_code == 200, c.text
    tok = c.json()["token"]
    assert tok.startswith("rvd_") and c.json()["user"]["name"] == "Mia Member"
    # single use
    again = anon.post("/api/ext/v2/pair/claim", json={"code": code, "name": "x", "version": "2.0.0",
                                                      "kind": "extension"})
    assert again.status_code == 403 and "code" in again.json()["error"]
    h = {"Authorization": f"Bearer {tok}"}
    assert anon.post("/api/ext/v2/hello", json={"version": "2.0.0"}, headers=h).status_code == 200
    st = member_client.get("/api/ext/status").json()
    assert st["paired"] and st["device"]["name"] == "Chrome on Windows" and st["device"]["online"]
    assert st["device"]["ig_confirmed"] is False

    # pairing a second extension revokes the first token
    code2 = member_client.post("/api/ext/pair", json={}).json()["code"]
    tok2 = anon.post("/api/ext/v2/pair/claim", json={"code": code2, "name": "Brave", "version": "2.0.0",
                                                     "kind": "extension"}).json()["token"]
    assert anon.post("/api/ext/v2/hello", json={}, headers=h).status_code == 401
    h2 = {"Authorization": f"Bearer {tok2}"}
    assert anon.post("/api/ext/v2/hello", json={}, headers=h2).status_code == 200
    # extension tokens can't use the desktop worker API, session paths refuse bearer tokens
    assert anon.post("/api/worker/hello", json={}, headers=h2).status_code in (401, 404)
    # web UI disconnect revokes server-side
    assert member_client.delete("/api/ext/device").json() == {"ok": True}
    assert anon.post("/api/ext/v2/hello", json={}, headers=h2).status_code == 401


def test_pair_code_kind_and_attempt_limits(member_client, anon, member):
    from reelvault.server import api_ext

    code, _ = api_ext.new_pairing_code(member.id, "desktop")
    r = anon.post("/api/ext/v2/pair/claim", json={"code": code, "kind": "extension"})
    assert r.status_code == 403  # a desktop code can't pair an extension
    ok = anon.post("/api/ext/v2/pair/claim", json={"code": code, "kind": "desktop", "name": "PC"})
    assert ok.status_code == 200
    # 5 tries per code hash, independent of IP
    code3 = member_client.post("/api/ext/pair", json={}).json()["code"]
    with db.tx() as conn:
        conn.execute(db.pairing_codes.update().values(expires_at=db.utcnow() - timedelta(seconds=1)))
    statuses = [anon.post("/api/ext/v2/pair/claim", json={"code": code3, "kind": "extension"}).status_code
                for _ in range(6)]
    assert statuses[:5] == [403] * 5 and statuses[5] == 429


def test_expired_code_refused(member_client, anon):
    code = member_client.post("/api/ext/pair", json={}).json()["code"]
    with db.tx() as conn:
        conn.execute(db.pairing_codes.update().values(expires_at=db.utcnow() - timedelta(minutes=1)))
    assert anon.post("/api/ext/v2/pair/claim", json={"code": code, "kind": "extension"}).status_code == 403


def test_cors_preflight_for_extension(anon):
    r = anon.options("/api/ext/v2/hello", headers={"Origin": "chrome-extension://abcdefgh",
                                                   "Access-Control-Request-Method": "POST"})
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "chrome-extension://abcdefgh"
    bad = anon.options("/api/ext/v2/hello", headers={"Origin": "https://evil.example",
                                                     "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


# ============================================================================================== hello, confirm gate

def test_hello_and_confirm_gate(ext, member, member_client):
    aid = add_account(member)
    jid = jobs_ig.start_pull(member.id, aid, "first")
    h = ext.post("/hello", {"version": "2.0.0", "ig": {"signed_in": True, "user_id": "61000777",
                                                       "username": "member.reader", "checked_at": 1}})
    b = h.json()
    assert b["ok"] and b["has_work"] and b["ig_confirmed"] is False and b["pacing"]["hour_max"] == 120
    assert b["pacing"]["gap_ms"] == [4000, 9000] and b["min_version"] == "2.0.0"
    # Not confirmed: nothing Instagram is leased.
    lz = ext.lease()
    assert lz["job"] is None and lz["reason"] == "confirm_account" and lz["poll_after_s"] == 30
    st = member_client.get("/api/ext/status").json()
    assert st["device"]["ig_username"] == "member.reader" and not st["device"]["ig_confirmed"]
    assert member_client.post("/api/ext/confirm-account", json={"ig_user_id": "999"}).status_code == 400
    assert member_client.post("/api/ext/confirm-account", json={"ig_user_id": "61000777"}).json() == {"ok": True}
    lz = ext.lease()
    assert lz["job"]["id"] == jid and lz["job"]["kind"] == "ig_pull"
    p = lz["job"]["payload"]
    assert p == {"account_id": aid, "handle": "scaleit.demo", "mode": "first", "cursor_source": "clips",
                 "target": 100}
    assert jobs_ig.payload_problem("ig_pull", p) is None
    # A different Instagram account reported mid-job: the job goes back to waiting and the extension stops.
    pr = ext.post(f"/jobs/{jid}/progress", {"done": 0, "total": 100, "message": "x",
                                            "ig": {"signed_in": True, "user_id": "123", "username": "someone"}})
    assert pr.json()["cancel"] is True and pr.json()["reason"] == "confirm_account"
    assert jobs.get(jid).state == "waiting"
    assert ext.lease()["reason"] == "confirm_account"


# ============================================================================================== pulls

def leased_pull(ext, member, mode="first", aid=None):
    aid = aid or add_account(member)
    confirm(ext.device_id)
    jid = jobs_ig.start_pull(member.id, aid, mode)
    lz = ext.lease(["ig_pull"])
    assert lz["job"] and lz["job"]["id"] == jid, lz
    return aid, jid, lz["job"]


def test_first_pull_exactly_100_then_next_continues(ext, member, run_jobs):
    aid, jid, job = leased_pull(ext, member)
    out = pull_pages(ext, jid, 3)
    assert out["stop"] is True
    assert count_reels(member.id) == 100
    assert ext.post(f"/jobs/{jid}/done", {"stop_reason": "target_reached"}).json() == {"ok": True}
    acc = account_row(aid)
    assert acc.ig_pk == PK and acc.followers == 182000 and acc.reels_count == 100 and acc.status == "active"
    assert acc.cursor_max_id == mk.cursor_for(100) and acc.cursor_source == "clips" and not acc.cursor_done
    assert acc.oldest_pk == str(TIMELINE[99]["pk"]) and acc.newest_pk == str(TIMELINE[0]["pk"])
    with db.tx(immediate=False) as conn:
        pull = conn.execute(select(db.pulls).where(db.pulls.c.job_id == jid)).first()
        score = conn.execute(select(db.jobs).where(db.jobs.c.kind == "score_account")).first()
        snaps = conn.execute(select(func.count()).select_from(db.reel_snapshots)).scalar()
    assert (pull.reels_new, pull.pages, pull.stop_reason, pull.to_max_id) == (100, 2, "target_reached",
                                                                             mk.cursor_for(100))
    assert score.payload == {"account_id": aid} and snaps == 100
    assert jobs.get(jid).state == "done" and jobs.get(jid).result["reels_new"] == 100

    # next 100 from the saved cursor
    jid2 = jobs_ig.start_pull(member.id, aid, "next")
    lz = ext.lease(["ig_pull"])
    assert lz["job"]["payload"]["max_id"] == mk.cursor_for(100)
    assert lz["job"]["payload"]["oldest_pk"] == str(TIMELINE[99]["pk"])
    out = pull_pages(ext, jid2, 3, start=100, account=False)
    assert out["stop"] and count_reels(member.id) == 200
    ext.post(f"/jobs/{jid2}/done", {"stop_reason": "target_reached"})
    assert account_row(aid).cursor_max_id == mk.cursor_for(200)


def test_pages_of_12_cut_at_100_and_next_pull_loses_nothing(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    out = pull_pages(ext, jid, 12, size=12)
    assert out["stop"] and count_reels(member.id) == 100  # 9 pages = 108, cut at 100
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})
    acc = account_row(aid)
    assert acc.cursor_max_id == mk.cursor_for(96)  # the cursor that produced the cut page
    jid2 = jobs_ig.start_pull(member.id, aid, "next")
    assert ext.lease(["ig_pull"])["job"]["payload"]["max_id"] == mk.cursor_for(96)
    out = pull_pages(ext, jid2, 12, size=12, start=96, account=False)
    assert count_reels(member.id) == 200
    with db.tx(immediate=False) as conn:
        pk_list = [r[0] for r in conn.execute(select(db.reels.c.ig_pk))]
    assert sorted(pk_list) == sorted(str(m["pk"]) for m in TIMELINE[:200])


def test_page_cap_stops_at_12_pages(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    for i in range(12):
        raw = TIMELINE[i * 2:i * 2 + 2]  # tiny pages: target not reached
        r = ext.items(jid, raw, max_id=mk.cursor_for(i * 2 + 2), more=True, account=ig_user() if i == 0 else None)
        assert r.json()["stop"] is (i == 11)


def test_stale_cursor_resumes_through_feed_then_top(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    pull_pages(ext, jid, 2)
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})
    jid2 = jobs_ig.start_pull(member.id, aid, "next")
    ext.lease(["ig_pull"])
    # The old cursor came back with the FIRST page again: the server says resume from the oldest pk on feed/user.
    r = ext.items(jid2, TIMELINE[:50], max_id=mk.cursor_for(50), more=True)
    b = r.json()
    oldest = str(TIMELINE[99]["pk"])
    assert b["resume"] == {"source": "feed", "max_id": f"{oldest}_{PK}"} and b["accepted"] == 0 and not b["stop"]
    # Feed pages (12 each) from right after the oldest stored reel.
    off = 100
    while True:
        chunk = TIMELINE[off:off + 12]
        r = ext.items(jid2, chunk, max_id=f"{chunk[-1]['pk']}_{PK}", more=True, source="feed")
        off += 12
        if r.json()["stop"]:
            break
    assert count_reels(member.id) == 200  # never double-counted
    ext.post(f"/jobs/{jid2}/done", {"stop_reason": "target_reached", "result": {"recovery": "feed_oldest_pk"}})
    acc = account_row(aid)
    assert acc.cursor_source == "feed" and acc.cursor_max_id.endswith(f"_{PK}")
    with db.tx(immediate=False) as conn:
        p = conn.execute(select(db.pulls).where(db.pulls.c.job_id == jid2)).first()
    assert p.recovery == "feed_oldest_pk" and p.reels_new == 100


def test_stale_feed_falls_back_to_top_skip_known(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    pull_pages(ext, jid, 2)
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})
    with db.tx() as conn:  # the account's cursor is a feed cursor now (as after a recovery)
        conn.execute(db.accounts.update().where(db.accounts.c.id == aid).values(cursor_source="feed",
                                                                                cursor_max_id="1_2"))
    jid2 = jobs_ig.start_pull(member.id, aid, "next")
    ext.lease(["ig_pull"])
    r = ext.items(jid2, TIMELINE[:12], max_id="x_1", more=True, source="feed").json()
    assert r["resume"] == {"source": "clips", "max_id": None, "skip_known": True}
    # From the top: known reels are updated, only new ones count.
    pull_pages(ext, jid2, 5, account=False)
    assert count_reels(member.id) == 200


def test_refresh_stops_at_known_reel_and_keeps_cursor(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    pull_pages(ext, jid, 2, start=3)  # reels 3..102 stored; 0..2 are "new since"
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})
    before = account_row(aid)
    jid2 = jobs_ig.start_pull(member.id, aid, "refresh")
    assert ext.lease(["ig_pull"])["job"]["payload"]["mode"] == "refresh"
    b = ext.items(jid2, TIMELINE[:50], max_id=mk.cursor_for(50), more=True).json()
    assert b["known_hit"] is True and b["stop"] is True and b["accepted"] == 3
    ext.post(f"/jobs/{jid2}/done", {"stop_reason": "known_reel"})
    after = account_row(aid)
    assert after.cursor_max_id == before.cursor_max_id and after.last_refresh_at is not None
    assert count_reels(member.id) == 103


def test_refresh_ignores_pinned_known_reels(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    raw = mk.build_all()  # noqa: F841  (fixtures build is deterministic)
    pinned = igparse.page_items(igparse.parse_ig_json((FIX / "pinned.json").read_text()))
    first = ext.post(f"/jobs/{jid}/items", {"page": 1, "account": ig_user(), "items": [igparse.trim_media(m) for m in pinned[:3]],
                                            "paging": {"max_id": "c1", "more_available": True, "source": "clips"}})
    assert first.status_code == 200
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})
    jid2 = jobs_ig.start_pull(member.id, aid, "refresh")
    ext.lease(["ig_pull"])
    b = ext.post(f"/jobs/{jid2}/items", {"page": 1, "items": [igparse.trim_media(m) for m in pinned],
                                          "paging": {"max_id": "c2", "more_available": True,
                                                     "source": "clips"}}).json()
    assert b["known_hit"] is False and b["accepted"] == 5  # 3 pinned known reels at the top don't stop it


def test_suggested_dropped_collab_rows_and_owner_account(ext, member):
    partner = add_account(member, handle="partner.studio", ig_pk=str(mk.PARTNER["pk"]))
    aid, jid, _ = leased_pull(ext, member)
    sugg = igparse.page_items(igparse.parse_ig_json((FIX / "with_suggested.json").read_text()))
    b = ext.post(f"/jobs/{jid}/items", {"page": 1, "account": ig_user(), "items": [igparse.trim_media(m) for m in sugg],
                                         "paging": {"max_id": "c1", "more_available": True, "source": "clips"}}).json()
    assert b["accepted"] == 6
    collab = igparse.page_items(igparse.parse_ig_json((FIX / "collab.json").read_text()))
    b = ext.post(f"/jobs/{jid}/items", {"page": 2, "items": [igparse.trim_media(m) for m in collab],
                                         "paging": {"max_id": "c2", "more_available": True, "source": "clips"}}).json()
    assert b["accepted"] == 3  # both collabs and tl[10] (not on the suggested page) are new
    with db.tx(immediate=False) as conn:
        r = db.reels
        co = conn.execute(select(r).where(r.c.ig_pk == str(collab[0]["pk"]))).first()
        own = conn.execute(select(r).where(r.c.ig_pk == str(collab[2]["pk"]))).first()
        ra = conn.execute(select(db.reel_accounts).where(db.reel_accounts.c.reel_id == co.id)).all()
        pull = conn.execute(select(db.pulls).where(db.pulls.c.job_id == jid)).first()
    assert co.is_collab and co.account_id == partner  # owner tracked -> owner's account
    assert {(x.account_id, x.role) for x in ra} == {(aid, "coauthor"), (partner, "owner")}
    assert own.is_collab and own.account_id == aid and own.coauthors == ["partner.studio"]
    assert pull.collabs_seen == 1


def test_bigint_pk_stored_exactly_and_number_pk_rejected(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    raw = igparse.page_items(igparse.parse_ig_json((FIX / "bigint_pk.json").read_text()))
    body = {"page": 1, "account": ig_user(), "items": [igparse.trim_media(raw[0])],
            "paging": {"max_id": "c1", "more_available": True, "source": "clips"}}
    assert ext.post(f"/jobs/{jid}/items", body).status_code == 200
    with db.tx(immediate=False) as conn:
        row = conn.execute(select(db.reels.c.ig_pk, db.reels.c.code)).first()
    assert row.ig_pk == "3456789012345678901" and igparse.shortcode_to_pk(row.code) == row.ig_pk
    # A JSON number pk (a broken extension) is refused: build the JSON text by hand.
    text = json.dumps(body).replace('"pk": "3456789012345678901"', '"pk": 3456789012345678901')
    r = ext.c.post(f"/api/ext/v2/jobs/{jid}/items", content=text, headers={**ext.h, "Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "bad_input"


def test_cookie_fields_are_refused_and_logged(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    m = trimmed(TIMELINE[:1])[0]
    m["sessionid"] = "abc"
    r = ext.post(f"/jobs/{jid}/items", {"page": 1, "items": [m], "paging": {"max_id": None, "more_available": False,
                                                                            "source": "clips"}})
    assert r.status_code == 400
    r = ext.post("/jobs/lease", {"kinds": ["ig_pull"], "budget": {"csrftoken": "x"}})
    assert r.status_code == 400
    r = ext.post("/hello", {"Cookie": "sessionid=1"})
    assert r.status_code == 400
    with db.tx(immediate=False) as conn:
        n = conn.execute(select(func.count()).select_from(db.audit_log)
                         .where(db.audit_log.c.action == "ext_cookie_field")).scalar()
    assert n == 3


# ============================================================================================== stops, cooldown, budget

def test_rate_limit_cooldown_keeps_cursor_and_resumes(ext, member, make_user):
    aid, jid, _ = leased_pull(ext, member)
    pull_pages(ext, jid, 1)  # 50 stored
    r = ext.post(f"/jobs/{jid}/fail", {"code": "rate_limited", "http_status": 429})
    cd = r.json()["cooldown_until"]
    assert cd
    row = jobs.get(jid)
    assert row.state == "waiting" and row.run_after > db.utcnow() + timedelta(hours=23)
    assert "slow down" in row.error_message
    assert row.payload["mode"] == "next" and row.payload["max_id"] == mk.cursor_for(50) and row.payload["target"] == 50
    acc = account_row(aid)
    assert acc.cursor_max_id == mk.cursor_for(50)  # cursor kept
    with db.tx(immediate=False) as conn:
        pull = conn.execute(select(db.pulls).where(db.pulls.c.job_id == jid)).first()
    assert pull.stop_reason == "rate_limited" and pull.finished_at is None
    lz = ext.lease()
    assert lz["job"] is None and lz["reason"] == "cooldown"
    # page_read still runs during an Instagram cooldown (it never touches Instagram)
    src = db.new_id("src")
    with db.tx() as conn:
        conn.execute(db.profile_sources.insert().values(id=src, user_id=member.id, kind="skool_profile",
                                                        via="extension"))
    pjid = jobs_ig.request_page_read(member.id, src, "skool_profile", "https://www.skool.com/@mia-member")
    lz = ext.lease()
    assert lz["job"]["id"] == pjid and lz["job"]["kind"] == "page_read"
    # After the cooldown: the same job resumes from the cursor, needing 50 more.
    with db.tx() as conn:
        dev = conn.execute(select(db.devices).where(db.devices.c.id == ext.device_id)).first()
        st = dict(dev.state, cooldown_until=None, cooldown_reason=None)
        conn.execute(db.devices.update().where(db.devices.c.id == ext.device_id).values(state=st))
        conn.execute(db.jobs.update().where(db.jobs.c.id == jid).values(run_after=None))
    lz = ext.lease(["ig_pull"])
    assert lz["job"]["id"] == jid and lz["job"]["payload"]["max_id"] == mk.cursor_for(50)
    out = pull_pages(ext, jid, 2, start=50, account=False)
    assert out["stop"] and count_reels(member.id) == 100
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "target_reached"})
    with db.tx(immediate=False) as conn:
        pull = conn.execute(select(db.pulls).where(db.pulls.c.job_id == jid)).first()
    assert pull.reels_new == 100 and pull.stop_reason == "target_reached"


def test_signed_out_try_again(ext, member, member_client):
    aid, jid, _ = leased_pull(ext, member)
    r = ext.post(f"/jobs/{jid}/fail", {"code": "signed_out"})
    assert r.json()["cooldown_until"]
    st = member_client.get("/api/ext/status").json()
    assert st["device"]["cooldown_reason"] == "signed_out" and st["device"]["signed_in"] is False
    assert "signed out" in st["device"]["cooldown_text"]
    me = member_client.get("/api/me").json()
    assert me["setup"]["extension"] == "cooldown"
    assert member_client.post("/api/ext/try-again", json={}).json()["ok"]
    assert ext.lease(["ig_pull"])["job"]["id"] == jid


def test_challenge_and_suspended(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    jid_other = jobs_ig.request_profile(member.id, "other.creator", "competitor", account_id=add_account(member, "other.creator"))
    ext.post(f"/jobs/{jid}/fail", {"code": "challenge", "message": "redirect to /accounts/suspended/"})
    assert jobs.get(jid).state == "paused" and jobs.get(jid_other).state == "paused"
    assert "limited your account" in jobs.get(jid).error_message


def test_final_failures(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    ext.post(f"/jobs/{jid}/fail", {"code": "private"})
    assert account_row(aid).status == "private" and jobs.get(jid).state == "failed"
    assert "is private" in jobs.get(jid).error_message
    with pytest.raises(Exception):
        jobs_ig.start_pull(member.id, aid, "first")
    aid2, jid2, _ = leased_pull(ext, member, aid=add_account(member, "gone.account"))
    ext.post(f"/jobs/{jid2}/fail", {"code": "parse_error", "raw_keys": ["items", "paging_info"]})
    assert jobs.get(jid2).state == "failed" and "changed something" in jobs.get(jid2).error_message
    h = jobs_ig.ig_health()
    assert [f["code"] for f in h["fails"]][:2] == ["parse_error", "private"]
    assert h["fails"][0]["raw_keys"] == ["items", "paging_info"]
    assert ext.post(f"/jobs/{jid2}/fail", {"code": "made_up"}).status_code in (400, 409)


def test_budget_refusal(ext, member):
    add = add_account(member)
    confirm(ext.device_id)
    jobs_ig.start_pull(member.id, add, "first")
    lz = ext.lease(["ig_pull"], budget={"hour_used": 120, "day_used": 130, "cdn_day_used": 0})
    assert lz["job"] is None and lz["reason"] == "budget"
    # Server-side counts also refuse, whatever the extension reports.
    db.bump(db.usage_counters, {"scope": ext.device_id, "key": "ig_calls",
                                "window": jobs_ig.ratelimit.window_key("day")}, count=400)
    lz = ext.lease(["ig_pull"], budget={"hour_used": 0, "day_used": 0, "cdn_day_used": 0})
    assert lz["reason"] == "budget"


def test_extension_budget_fail_waits_without_cooldown(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    r = ext.post(f"/jobs/{jid}/fail", {"code": "budget", "cooldown_s": 600})
    assert r.json()["cooldown_until"] is None
    row = jobs.get(jid)
    assert row.state == "waiting" and row.run_after > db.utcnow() + timedelta(minutes=9)


def test_lease_expiry_and_409(ext, member):
    aid, jid, _ = leased_pull(ext, member)
    with db.tx() as conn:
        conn.execute(db.jobs.update().where(db.jobs.c.id == jid).values(lease_until=db.utcnow() - timedelta(seconds=1)))
    jobs.requeue_expired()
    r = ext.post(f"/jobs/{jid}/progress", {"done": 1, "total": 100, "message": "x"})
    assert r.status_code == 409  # the SW drops its local state
    lz = ext.lease(["ig_pull"])
    assert lz["job"]["id"] == jid
    pr = ext.post(f"/jobs/{jid}/progress", {"done": 1, "total": 100, "message": "Reading page 1",
                                            "cursor": {"max_id": "QVFabc"}}).json()
    assert pr["cancel"] is False and pr["lease_until"]
    jobs.cancel(jid, member.id)
    assert ext.post(f"/jobs/{jid}/progress", {}).json()["cancel"] is True


def test_another_members_device_gets_404(ext, member, other_member, make_device, anon):
    aid, jid, _ = leased_pull(ext, member)
    _, h2 = make_device(other_member)
    other = Ext(anon, h2)
    for path, body in ((f"/jobs/{jid}/progress", {}), (f"/jobs/{jid}/items", {"items": []}),
                       (f"/jobs/{jid}/done", {"stop_reason": "no_more"}), (f"/jobs/{jid}/fail", {"code": "private"}),
                       (f"/jobs/{jid}/page", {"final_url": "https://www.skool.com/x"})):
        assert other.post(path, body).status_code == 404, path
    assert other.post("/ig-user", {"job_id": jid, "user": {}}).status_code == 404
    assert other.lease()["job"] is None


def test_scheduled_pull_waits_for_its_day(ext, member):
    aid = add_account(member)
    confirm(ext.device_id)
    jid = jobs_ig.start_pull(member.id, aid, "first", run_after=db.utcnow() + timedelta(days=1))
    assert account_row(aid).status == "scheduled"
    assert ext.lease()["job"] is None
    assert jobs.to_api(jobs.get(jid))["run_after"]
    # one active pull per account
    assert jobs_ig.start_pull(member.id, aid, "first") == jid


def test_bad_payload_is_never_handed_out(ext, member):
    confirm(ext.device_id)
    jid = jobs.enqueue(member.id, "ig_profile", {"handle": "../../evil", "purpose": "competitor"})
    assert ext.lease()["job"] is None
    assert jobs.get(jid).state == "failed" and jobs.get(jid).error_code == "bad_job"


# ============================================================================================== profiles, discovery

def test_competitor_profile_lookup(ext, member):
    aid = add_account(member, "scaleit.demo")
    confirm(ext.device_id)
    jid = jobs_ig.request_profile(member.id, "@ScaleIt.Demo", "competitor", account_id=aid)
    lz = ext.lease(["ig_profile"])
    assert lz["job"]["payload"] == {"handle": "scaleit.demo", "purpose": "competitor", "account_id": aid}
    r = ext.post(f"/jobs/{jid}/items", {"account": ig_user(followers=200_000), "items": []})
    assert r.json()["stop"] is True
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "no_more", "result": {"found": True}})
    acc = account_row(aid)
    assert acc.ig_pk == PK and acc.followers == 200000 and acc.category == "Entrepreneur"
    # wrong account uploaded
    jid2 = jobs_ig.request_profile(member.id, "scaleit.demo", "competitor", account_id=aid)
    ext.lease(["ig_profile"])
    bad = ext.post(f"/jobs/{jid2}/items", {"account": ig_user(u=mk.OTHER), "items": []})
    assert bad.status_code == 400


def test_member_bio_goes_to_profile_source(ext, member):
    confirm(ext.device_id)
    src = db.new_id("src")
    with db.tx() as conn:
        conn.execute(db.profile_sources.insert().values(id=src, user_id=member.id, kind="instagram_bio", via="extension"))
    jid = jobs_ig.request_profile(member.id, "scaleit.demo", "member_bio", source_id=src)
    ext.lease(["ig_profile"])
    assert ext.post("/ig-user", {"job_id": jid, "user": ig_user()}).json() == {"ok": True}
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "no_more", "result": {"found": True}})
    with db.tx(immediate=False) as conn:
        s = conn.execute(select(db.profile_sources).where(db.profile_sources.c.id == src)).first()
    assert s.status == "ok" and "Helping agency owners" in s.text
    assert jobs.get(jid).state == "done"


def test_discover_then_check(ext, member):
    confirm(ext.device_id)
    add_account(member, "scaleit.demo")  # already tracked: hidden from candidates
    run = db.new_id("drun")
    with db.tx() as conn:
        conn.execute(db.discover_runs.insert().values(id=run, user_id=member.id, mode="search",
                                                      queries=["facebook ads"]))
    jid = jobs_ig.request_discover(member.id, run, "niche", queries=["Facebook ads <agency>!", "facebook ads agency"])
    lz = ext.lease(["ig_discover"])
    assert lz["job"]["payload"]["queries"] == ["Facebook ads agency", "facebook ads agency"]
    users = [dict(ig_user(u=u), source="search") for u in (mk.TARGET, mk.PARTNER, mk.OTHER)]
    users.append(dict(ig_user(u=mk.VIEWER), is_private=True, source="search"))
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "no_more", "result": {"users": users}})
    with db.tx(immediate=False) as conn:
        cands = conn.execute(select(db.discover_candidates).where(db.discover_candidates.c.run_id == run)).all()
    assert {c.handle for c in cands} == {"partner.studio", "other.creator"}
    lz = ext.lease(["ig_check"])
    assert lz["job"]["kind"] == "ig_check" and len(lz["job"]["payload"]["candidates"]) == 2
    cj = lz["job"]["id"]
    for c in lz["job"]["payload"]["candidates"]:
        u = mk.PARTNER if c["handle"] == "partner.studio" else mk.OTHER
        raw = [mk.media(u, mk.FIXTURE_NOW - k * 2 * mk.DAY, 1000 + k, views=100_000 + k, likes=3000, comments=40)
               for k in range(12)]
        r = ext.post(f"/jobs/{cj}/items", {"candidate": {"cand_id": c["cand_id"]}, "account": ig_user(u=u),
                                            "items": trimmed(raw), "paging": {"source": "clips", "max_id": None,
                                                                              "more_available": True}})
        assert r.status_code == 200 and r.json()["accepted"] == 12
    ext.post(f"/jobs/{cj}/done", {"stop_reason": "no_more"})
    with db.tx(immediate=False) as conn:
        assert conn.execute(select(db.discover_runs.c.state).where(db.discover_runs.c.id == run)).scalar() == "done"
        n_reels = conn.execute(select(func.count()).select_from(db.reels)).scalar()
    assert n_reels == 0  # ig_check reels are never stored as tracked reels


# ============================================================================================== videos, pages

MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 2000
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 500


def stored_reels(ext, member, n=3):
    aid, jid, _ = leased_pull(ext, member)
    ext.items(jid, TIMELINE[:n], max_id=None, more=False, account=ig_user())
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "no_more"})
    with db.tx(immediate=False) as conn:
        return [r[0] for r in conn.execute(select(db.reels.c.id).order_by(db.reels.c.taken_at.desc()))]


def test_video_upload_flow(ext, member, rv_env, monkeypatch):
    ids = stored_reels(ext, member)
    with db.tx() as conn:
        conn.execute(db.reels.update().where(db.reels.c.id.in_(ids[:2])).values(media_state="need_extension"))
    lz = ext.lease(["ig_videos"])  # the scheduler batched the need_extension reels
    job = lz["job"]
    assert job["kind"] == "ig_videos" and {i["reel_id"] for i in job["payload"]["items"]} == set(ids[:2])
    assert job["payload"]["refresh_urls"] is True and job["payload"]["want_cover"] is True
    url = f"/api/ext/v2/jobs/{job['id']}/video"
    r = ext.c.post(url, headers=ext.h, data={"reel_id": ids[0]},
                   files={"file": ("v.mp4", MP4, "video/mp4"), "cover": ("c.jpg", JPG, "image/jpeg")})
    assert r.status_code == 200, r.text
    p = rv_env / "users" / member.id / "reels" / ids[0] / "video.mp4"
    assert p.read_bytes() == MP4 and (p.parent / "cover.jpg").is_file()
    # not an mp4
    r = ext.c.post(url, headers=ext.h, data={"reel_id": ids[1]}, files={"file": ("v.mp4", b"<html>", "video/mp4")})
    assert r.status_code == 400
    # a reel that is not in this job
    r = ext.c.post(url, headers=ext.h, data={"reel_id": ids[2]}, files={"file": ("v.mp4", MP4, "video/mp4")})
    assert r.status_code == 400
    # bigger than the cap
    monkeypatch.setattr(jobs_ig, "VIDEO_MAX", 1000)
    r = ext.c.post(url, headers=ext.h, data={"reel_id": ids[1]}, files={"file": ("v.mp4", MP4, "video/mp4")})
    assert r.status_code == 413
    ext.post(f"/jobs/{job['id']}/done", {"stop_reason": "no_more", "result": {"failed": [
        {"reel_id": ids[1], "reason": "too_big"}]}})
    with db.tx(immediate=False) as conn:
        states = dict(conn.execute(select(db.reels.c.id, db.reels.c.media_state)).all())
    assert states[ids[0]] == "have" and states[ids[1]] == "failed" and states[ids[2]] == "none"
    assert jobs.get(job["id"]).result == {"uploaded": 1, "failed": [{"reel_id": ids[1], "reason": "too_big"}]}


def test_request_videos_batches_of_30(member, rv_env):
    aid = add_account(member)
    ids = []
    with db.tx() as conn:
        for m in TIMELINE[:65]:
            rid = db.new_id("reel")
            ids.append(rid)
            conn.execute(db.reels.insert().values(id=rid, user_id=member.id, account_id=aid, ig_pk=str(m["pk"]),
                                                  code=m["code"], url="u"))
    first = jobs_ig.request_videos(member.id, ids)
    assert first
    with db.tx(immediate=False) as conn:
        rows = conn.execute(select(db.jobs).where(db.jobs.c.kind == "ig_videos")).all()
    assert sorted(len(r.payload["items"]) for r in rows) == [5, 30, 30]
    assert jobs_ig.request_videos(member.id, ids) is None  # already queued


def test_page_read_skool_and_linkedin(ext, member):
    src = db.new_id("src")
    src2 = db.new_id("src")
    with db.tx() as conn:
        conn.execute(db.profile_sources.insert().values(id=src, user_id=member.id, kind="skool_profile", via="extension"))
        conn.execute(db.profile_sources.insert().values(id=src2, user_id=member.id, kind="linkedin_page",
                                                        via="extension"))
    with pytest.raises(Exception):
        jobs_ig.request_page_read(member.id, src, "skool_profile", "https://www.skool.com/@x/../evil")
    jid = jobs_ig.request_page_read(member.id, src, "skool_profile", "https://www.skool.com/@mia-member")
    lz = ext.lease(["page_read"])  # no Instagram confirm needed
    assert lz["job"]["payload"] == {"source_id": src, "kind": "skool_profile", "url": "https://www.skool.com/@mia-member"}
    nd = {"props": {"pageProps": {"currentUser": {"firstName": "Mia", "lastName": "Member",
                                                  "metadata": {"bio": "I run a Meta ads agency for dentists."}}}}}
    assert ext.post(f"/jobs/{jid}/page", {"final_url": "https://evil.example/", "next_data": nd}).status_code == 400
    assert ext.post(f"/jobs/{jid}/page", {"final_url": "https://www.skool.com/@mia-member", "title": "Mia",
                                          "next_data": nd}).json() == {"ok": True}
    ext.post(f"/jobs/{jid}/done", {"stop_reason": "no_more"})
    with db.tx(immediate=False) as conn:
        s = conn.execute(select(db.profile_sources).where(db.profile_sources.c.id == src)).first()
    assert s.status == "ok" and "dentists" in s.text and s.via == "extension"
    # LinkedIn: only /in/me/ is ever read; the final URL must be a profile page
    jid2 = jobs_ig.request_page_read(member.id, src2, "linkedin_self", "https://ignored.example")
    lz = ext.lease(["page_read"])
    assert lz["job"]["payload"]["url"] == "https://www.linkedin.com/in/me/"
    assert ext.post(f"/jobs/{jid2}/page", {"final_url": "https://www.linkedin.com/feed/", "text": "x"}).status_code == 400
    assert ext.post(f"/jobs/{jid2}/page", {"final_url": "https://www.linkedin.com/in/mia-m/",
                                           "text": "Mia   Member\n\n Founder"}).status_code == 200
    # permission denied on another page read -> needs_paste
    src3 = db.new_id("src")
    with db.tx() as conn:
        conn.execute(db.profile_sources.insert().values(id=src3, user_id=member.id, kind="skool_group", via="extension"))
    jid3 = jobs_ig.request_page_read(member.id, src3, "skool_group", "https://www.skool.com/ads-lab/about")
    ext.lease(["page_read"])
    ext.post(f"/jobs/{jid3}/fail", {"code": "permission_denied"})
    with db.tx(immediate=False) as conn:
        s3 = conn.execute(select(db.profile_sources).where(db.profile_sources.c.id == src3)).first()
    assert s3.status == "needs_paste" and "Paste" in s3.error


def test_page_read_daily_cap(member):
    src = db.new_id("src")
    with db.tx() as conn:
        conn.execute(db.profile_sources.insert().values(id=src, user_id=member.id, kind="skool_profile", via="extension"))
    for _ in range(5):
        jid = jobs_ig.request_page_read(member.id, src, "skool_profile", "https://www.skool.com/@mia")
        jobs.cancel(jid, member.id)
    with pytest.raises(Exception) as e:
        jobs_ig.request_page_read(member.id, src, "skool_profile", "https://www.skool.com/@mia")
    assert getattr(e.value, "status", None) == 429


def test_debug_sample_stored_once(ext, member):
    tree = {"items": [{"media": {"pk": "int", "caption": {"text": "str"}, "reshare_count": "int"}}],
            "paging_info": {"max_id": "str", "more_available": "bool"}, "status": "str"}
    assert ext.post("/debug-sample", {"endpoint": "clipsUser", "keys_tree": tree}).json() == {"ok": True}
    assert ext.post("/debug-sample", {"endpoint": "clipsUser", "keys_tree": {"x": "str"}}).json() == {"ok": True}
    assert ext.post("/debug-sample", {"endpoint": "clipsUser", "keys_tree": {"x": "a real value"}}).status_code == 400
    assert ext.post("/debug-sample", {"endpoint": "nope", "keys_tree": {}}).status_code == 400
    h = jobs_ig.ig_health()
    assert h["samples"]["clipsUser"]["keys_tree"] == tree
    assert h["fields_seen"]["reshare_count"] is True


# ============================================================================================== download

def test_extension_zip_download(client):
    r = client.get("/download/reelvault-members-extension.zip")
    assert r.status_code == 200, r.text
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(z.namelist())
    for f in ("manifest.json", "sw.js", "config.js", "endpoints.js", "validate.js", "bigint.js", "pacing.js",
              "popup.html", "grant.html", "icons/128.png"):
        assert f in names, f
    cfg = z.read("config.js").decode()
    assert 'export const SERVERS = ["http://localhost"];' in cfg and 'export const BUILD = "zip";' in cfg
    man = json.loads(z.read("manifest.json"))
    assert "cookies" not in man["permissions"] and "tabs" not in man["permissions"]
    assert client.get("/pair").status_code == 200
