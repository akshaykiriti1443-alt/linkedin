"""Shared helpers for the fetch-engine server tests (DESIGN-FETCH.md). Not a test module.

`mk` = tests/fixtures/ig/make_fixtures.py, `lo` = tests/fixtures/ig/logged_out/make_lo_fixtures.py (the architect's
makers). Items are built the way the extension uploads them: raw Instagram JSON -> parse_ig_json (big ids become
strings) -> trim_media / trim_gql -> + {"shape", "strategy"}.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from sqlalchemy import func, select

from reelvault import igparse
from reelvault.server import db, jobs_ig

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ig"
LO_DIR = FIX / "logged_out"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lo = _load("rv_mk_lo_fx", LO_DIR / "make_lo_fixtures.py")
mk = lo.mk
PK = str(mk.TARGET["pk"])
TIMELINE = mk.account_timeline(250)          # the first 40 are the reels of the logged-out fixtures
ALL_LO = list(igparse.LO_STRATEGIES)


def raw(obj):
    """What the extension's parseIg gives for a raw Instagram answer: 16+ digit ids as strings."""
    return igparse.parse_ig_json(mk.dumps(obj))


def li_items(medias) -> list[dict]:
    """Logged-in IgMedia items (the existing upload shape)."""
    return [igparse.trim_media(m) for m in raw({"items": list(medias)})["items"]]


def v1_detail(m, *, views="both", strategy="lo_post_gql") -> dict:
    """A logged-out post detail (lo_post_gql / lo_media_gql) as LoItem shape v1. views: both | view_count | none."""
    return dict(igparse.trim_media(raw(lo.lo_media(m, views=views))), shape="v1", strategy=strategy)


def v1_reel(m, *, strategy="lo_reels_gql") -> dict:
    """A sparse reels-tab node (lo_reels_page / lo_reels_gql) as LoItem shape v1: play count, no caption or date."""
    return dict(igparse.trim_media(raw(lo.reels_tab_media(m))), shape="v1", strategy=strategy)


def gql_item(m, *, views=True, strategy="lo_web_profile") -> dict:
    """A legacy GraphQL node (lo_web_profile / lo_embed) as LoItem shape gql."""
    return igparse.trim_gql(raw(lo.gql_node(m, views=views)), strategy)


def lo_user(followers=182_000, private=False) -> dict:
    """IgUser from the logged-out profile page (xig_user_by_username)."""
    return igparse.trim_user(raw(lo.xig_user(mk.TARGET, followers=followers, private=private)))


def li_user(followers=182_000) -> dict:
    return igparse.trim_user(raw(mk.web_profile_info(mk.TARGET, followers=followers)["data"]["user"]))


def log_row(strategy, status="ok", code=None, http=200, items=0) -> dict:
    return {"strategy": strategy, "status": status, "code": code, "http": http, "ms": 300, "items": items,
            "at": 1793534400}


def add_account(user, handle="scaleit.demo", ig_pk=None, status="new"):
    aid = db.new_id("acc")
    with db.tx() as conn:
        conn.execute(db.accounts.insert().values(id=aid, user_id=user.id, handle=handle, ig_pk=ig_pk, status=status))
    return aid


def confirm(device_id, ig_user_id="61000777", username="member.reader"):
    with db.tx() as conn:
        conn.execute(db.devices.update().where(db.devices.c.id == device_id).values(
            ig_user_id=ig_user_id, ig_username=username, ig_confirmed_user_id=ig_user_id))


def set_version(device_id, version="2.1.0"):
    with db.tx() as conn:
        conn.execute(db.devices.update().where(db.devices.c.id == device_id).values(version=version))


def set_read_mode(user, mode):
    with db.tx() as conn:
        cur = conn.execute(select(db.users.c.settings).where(db.users.c.id == user.id)).scalar() or {}
        conn.execute(db.users.update().where(db.users.c.id == user.id).values(settings={**cur, "read_mode": mode}))


def device_row(device_id):
    with db.tx(immediate=False) as conn:
        return conn.execute(select(db.devices).where(db.devices.c.id == device_id)).first()


def job_row(job_id):
    with db.tx(immediate=False) as conn:
        return conn.execute(select(db.jobs).where(db.jobs.c.id == job_id)).first()


def pull_row(job_id):
    with db.tx(immediate=False) as conn:
        return conn.execute(select(db.pulls).where(db.pulls.c.job_id == job_id)).first()


def account_row(aid):
    with db.tx(immediate=False) as conn:
        return conn.execute(select(db.accounts).where(db.accounts.c.id == aid)).first()


def reel_rows(user_id) -> dict:
    """{ig_pk: row} of a member's reels."""
    with db.tx(immediate=False) as conn:
        return {r.ig_pk: r for r in conn.execute(select(db.reels).where(db.reels.c.user_id == user_id)).all()}


def count_reels(user_id) -> int:
    with db.tx(immediate=False) as conn:
        return conn.execute(select(func.count()).select_from(db.reels).where(db.reels.c.user_id == user_id)).scalar()


def run_now(job_id):
    """Make a waiting job due now (as if its wait ended)."""
    with db.tx() as conn:
        conn.execute(db.jobs.update().where(db.jobs.c.id == job_id).values(run_after=None))


class Ext:
    """A tiny stand-in for the extension's service worker (version 2.1.0 unless told otherwise)."""

    def __init__(self, client, headers, device_id):
        self.c, self.h, self.device_id = client, headers, device_id

    def post(self, path, body=None):
        return self.c.post("/api/ext/v2" + path, json=body if body is not None else {}, headers=self.h)

    def ok(self, path, body=None) -> dict:
        r = self.post(path, body)
        assert r.status_code == 200, r.text
        return r.json()

    def lease(self, kinds=jobs_ig.EXT_KINDS, budget=None, lo_state=None) -> dict:
        body = {"kinds": list(kinds), "budget": budget or {"hour_used": 0, "day_used": 0, "cdn_day_used": 0,
                                                           "lo_hour_used": 0, "lo_day_used": 0}}
        if lo_state is not None:
            body["lo"] = lo_state
        return self.ok("/jobs/lease", body)

    def lo_items(self, job_id, items, *, account=None, strategy="lo_post_gql", stage_done=False, lo_stop=None,
                 log=None, more=True, extra_paging=None):
        paging = {"source": "lo", "strategy": strategy, "more_available": more, "stage_done": stage_done,
                  "lo_stop": lo_stop}
        paging.update(extra_paging or {})
        body = {"layer": "logged_out", "items": items, "paging": paging,
                "fetch_log": log if log is not None else [log_row(strategy, items=len(items))]}
        if account is not None:
            body["account"] = account
        return self.post(f"/jobs/{job_id}/items", body)

    def stage_done(self, job_id, lo_stop="budget", log=None) -> dict:
        r = self.lo_items(job_id, [], strategy=None, stage_done=True, lo_stop=lo_stop, log=log or [])
        assert r.status_code == 200, r.text
        return r.json()

    def li_page(self, job_id, medias, *, max_id, more=True, account=None, source="clips"):
        body = {"page": 1, "items": li_items(medias),
                "paging": {"max_id": max_id, "more_available": more, "source": source}}
        if account is not None:
            body["account"] = account
        return self.post(f"/jobs/{job_id}/items", body)


def load_json(name: str):
    return json.loads((LO_DIR / name).read_text(encoding="utf-8"))
