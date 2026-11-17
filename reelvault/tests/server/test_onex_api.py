"""OneX findings API (GET /api/onex/v1/findings): signed server-to-server calls, one member's own data only.

Secrets here are test-only values (the repo is public): the contract's test vector secret and "s" * 40.
"""
from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from _wp5_support import add_account, add_reel, breakdown

from reelvault.server import api_onex, db, ratelimit
from reelvault.server.errors import ApiError

PATH = "/api/onex/v1/findings"
SECRET = "s" * 40

VECTOR = {"secret": "test-secret-that-is-at-least-32-characters-long", "email": "jay@climbranked.test",
          "ts": "1763380800", "nonce": "0123456789abcdef0123456789abcdef",
          "sig": "v1=iuSXICirPXL1Sa8kel2VgzZQKDH52Yv2Lspnm-nvep8"}

HOOK_KEYS = {"id", "text", "on_screen_text", "hook_type", "hook_label", "formula", "why", "handle", "score", "level",
             "reel_url"}
FORMAT_KEYS = {"id", "name", "visual_format", "visual_format_label", "line", "lift", "evidence", "can_make", "reels",
               "competitors"}
TOP_KEYS = {"v", "status", "email", "computed_at", "competitors", "hooks", "formats"}


def signed(email, *, secret=SECRET, ts=None, nonce=None, method="GET", path=PATH, skew=0):
    ts = str(int(time.time()) + skew) if ts is None else ts
    nonce = nonce or secrets.token_hex(16)
    return {"X-OneX-Email": email, "X-OneX-Timestamp": ts, "X-OneX-Nonce": nonce, "Accept": "application/json",
            "X-OneX-Signature": api_onex.sign(secret, email, ts, nonce, method=method, path=path)}


@pytest.fixture
def onex(monkeypatch, rv_env):
    monkeypatch.setenv("ONEX_SSO_SECRET", SECRET)


def get(client, headers):
    return client.get(PATH, headers=headers)


def code_of(r):
    return r.json()["code"]


# ---------------------------------------------------------------------------------------------- seeding (no ffmpeg)

SNAP_AT = datetime(2026, 11, 17, 9, 12, 3, tzinfo=timezone.utc)


def _analysis(uid, rid, level, data, **cols):
    with db.tx() as c:
        c.execute(db.analyses.insert().values(id=db.new_id("ana"), reel_id=rid, user_id=uid, level=level, data=data,
                                              tag_quality="vision", **cols))


def seed(uid: str, word: str) -> dict:
    """One competitor (@<word>coach) with a deep hook, quick hooks (one duplicate, one weak, one off-Instagram URL),
    the member's own account (never listed), two formats (one under 1x), and an insights snapshot."""
    aid = add_account(uid, handle=f"{word}coach")
    own = add_account(uid, handle=f"{word}self", role="self")
    deep = add_reel(uid, aid, score=6.04, tag_state="done", deep_state="done")
    bd = breakdown(deep)
    bd["skeleton"]["hook"].update(spoken=f"{word.title()} deep hook got 41 patients",
                                  on_screen_text=f"{word.upper()} 41 PATIENTS", hook_type="bold_claim")
    _analysis(uid, deep, "deep", bd, visual_format="VF04", hook_type="bold_claim")
    q1 = add_reel(uid, aid, score=4.0, tag_state="done")
    _analysis(uid, q1, "quick", {"hook_line": f"Stop boosting {word} posts."}, hook_type="contrarian",
              visual_format="VF04")
    dup = add_reel(uid, aid, score=3.0, tag_state="done")
    _analysis(uid, dup, "quick", {"hook_line": f"STOP BOOSTING {word.upper()} POSTS."}, hook_type="contrarian")
    odd = add_reel(uid, aid, score=2.5, tag_state="done", url="https://evil.example/reel/x/")
    _analysis(uid, odd, "quick", {"hook_line": f"{word.title()} odd link hook."}, hook_type="made_up_type")
    weak = add_reel(uid, aid, score=1.2, tag_state="done")
    _analysis(uid, weak, "quick", {"hook_line": f"{word.title()} weak hook."}, hook_type="question")
    mine = add_reel(uid, own, score=9.0, tag_state="done")
    _analysis(uid, mine, "quick", {"hook_line": f"{word.title()} my own hook."}, hook_type="question")
    add_reel(uid, aid, score=0.4)  # pulled, not studied yet

    good, under = db.new_id("fmt"), db.new_id("fmt")
    with db.tx() as c:
        c.execute(db.formats.insert().values(id=good, user_id=uid, recipe_key=f"VF04|{word}", vf="VF04",
                                             recipe={}, name=f"{word.title()} green-screen myth bust",
                                             stats={"active": True, "lift": 3.14}))
        c.execute(db.formats.insert().values(id=under, user_id=uid, recipe_key=f"VF01|{word}", vf="VF01",
                                             recipe={}, name=f"{word.title()} slow desk talk",
                                             stats={"active": True, "lift": 0.5}))
        line = "3.1x more likely to be an outlier · 12 reels from 3 competitors"
        c.execute(db.insight_snapshots.insert().values(
            id=db.new_id("snap"), user_id=uid, computed_at=SNAP_AT, data={
                "cards": [{"title": "Green screen over image", "dimension": "visual_format", "value": "VF04",
                           "line": line + " · Strong evidence", "lift": 3.14, "evidence": "strong",
                           "can_make": {"ok": True, "reason": None}, "best_reels": []}],
                "formats": [
                    {"id": good, "name": "x", "vf": "VF04", "line": line, "lift": 3.14, "evidence": "strong",
                     "n": 12, "k": 3},
                    {"id": under, "name": "y", "vf": "VF01", "line": "Less likely", "lift": 0.5, "evidence": "thin",
                     "n": 4, "k": 1},
                    {"id": "fmt_" + "a" * 16, "name": "gone", "vf": "VF02", "lift": 9.0, "evidence": "strong",
                     "n": 3, "k": 1}]}))
    return {"aid": aid, "deep": deep, "q1": q1, "dup": dup, "odd": odd, "good": good, "under": under}


# ---------------------------------------------------------------------------------------------- signing (pure)

def test_contract_test_vector():
    v = VECTOR
    assert api_onex.sign(v["secret"], v["email"], v["ts"], v["nonce"]) == v["sig"]
    api_onex.verify_onex_request(v["email"], v["ts"], v["nonce"], v["sig"], v["secret"], now=1763380800)
    msg = api_onex.signing_message("GET", PATH, v["email"], v["ts"], v["nonce"])
    assert msg == b"onex-api-v1\nGET\n/api/onex/v1/findings\njay@climbranked.test\n1763380800\n" \
                  b"0123456789abcdef0123456789abcdef"


def _verify_code(**over):
    v = dict(VECTOR, now=1763380800)
    v.update(over)
    try:
        api_onex.verify_onex_request(v["email"], v["ts"], v["nonce"], v["sig"], v["secret"], v["now"])
    except ApiError as e:
        assert e.status == 401
        return e.code
    return None


def test_verify_rejects_tampering_and_bad_formats():
    v = VECTOR
    assert _verify_code(secret="x" * 47) == "bad_signature"
    assert _verify_code(email="amy@climbranked.test") == "bad_signature"
    assert _verify_code(nonce="1" * 32) == "bad_signature"
    assert _verify_code(ts="1763380801", now=1763380801) == "bad_signature"
    for method, path in (("POST", PATH), ("GET", "/api/onex/v1/other"), ("get", PATH)):
        assert _verify_code(sig=api_onex.sign(v["secret"], v["email"], v["ts"], v["nonce"], method=method,
                                              path=path)) == "bad_signature", (method, path)
    for bad in ("Jay@climbranked.test", " jay@climbranked.test", "jay@climbranked", "", "jay@climbranked.test\n",
                "jäy@climbranked.test", "a" * 250 + "@x.test"):
        assert _verify_code(email=bad) == "bad_signature", bad
    for bad in ("", "-1", "1.5", "0x10", "1" * 13, " 1763380800"):
        assert _verify_code(ts=bad) == "bad_signature", bad
    for bad in ("", "0123456789ABCDEF0123456789ABCDEF", "0123456789abcdef", "g" * 32, "0" * 33):
        assert _verify_code(nonce=bad) == "bad_signature", bad
    for bad in ("", v["sig"][3:], "v2=" + v["sig"][3:], v["sig"] + "=", v["sig"][:-1], v["sig"] + "A",
                "v1=" + "+" * 43, "v1=" + "/" * 43):
        assert _verify_code(sig=bad) == "bad_signature", bad
    assert _verify_code(sig=None) == "bad_signature"   # a missing header is not a crash


def test_verify_rejects_a_trailing_newline_even_when_signed_that_way():
    """Python's "$" also matches before a final "\\n": the formats must be a full match, so "nonce\\n" never passes
    (it would otherwise have its own replay key)."""
    v = VECTOR
    ts_nl, nonce_nl = v["ts"] + "\n", v["nonce"] + "\n"
    assert _verify_code(ts=ts_nl, sig=api_onex.sign(v["secret"], v["email"], ts_nl, v["nonce"])) == "bad_signature"
    assert _verify_code(nonce=nonce_nl,
                        sig=api_onex.sign(v["secret"], v["email"], v["ts"], nonce_nl)) == "bad_signature"
    assert _verify_code(sig=v["sig"] + "\n") == "bad_signature"
    email_nl = v["email"] + "\n"
    assert _verify_code(email=email_nl,
                        sig=api_onex.sign(v["secret"], email_nl, v["ts"], v["nonce"])) == "bad_signature"


def test_old_nonce_rows_are_swept(rv_env):
    from reelvault.server import api_auth

    now = 1763380800.0   # 2025-11-17
    rows = [("onexapi:" + "1" * 32, "2025-11-10"), ("onexapi:" + "2" * 32, "2025-11-14"),
            ("onexapi:" + "3" * 32, "2025-11-15"), ("onexapi:" + "4" * 32, "2025-11-17"),
            ("sso:oldnonce12345", "2025-11-01"), ("pulls", "2025-11-01")]
    for key, window in rows:
        db.bump(db.usage_counters, {"scope": "global", "key": key, "window": window}, count=1)
    assert api_auth.prune_old_nonces("onexapi:", now=now, force=True) == 2
    with db.read() as c:
        left = {(r.key, r.window) for r in c.execute(select(db.usage_counters)).all()}
    assert ("onexapi:" + "1" * 32, "2025-11-10") not in left and ("onexapi:" + "2" * 32, "2025-11-14") not in left
    # the last 2 days stay (a replay inside the clock window still lands on its row); other keys are untouched
    assert {("onexapi:" + "3" * 32, "2025-11-15"), ("onexapi:" + "4" * 32, "2025-11-17"),
            ("sso:oldnonce12345", "2025-11-01"), ("pulls", "2025-11-01")} <= left
    assert api_auth.prune_old_nonces("sso:", now=now, force=True) == 1
    # throttled: a second sweep within the hour does nothing
    db.bump(db.usage_counters, {"scope": "global", "key": "sso:another12345", "window": "2025-11-01"}, count=1)
    assert api_auth.prune_old_nonces("sso:", now=now + 60) == 0
    assert api_auth.prune_old_nonces("sso:", now=now + 3601) == 1


def test_verify_clock_window_is_300_seconds_either_way():
    t = 1763380800
    assert _verify_code(now=t + 300) is None and _verify_code(now=t - 300) is None
    assert _verify_code(now=t + 301) == "stale" and _verify_code(now=t - 301) == "stale"


# ---------------------------------------------------------------------------------------------- the route: auth

def test_good_request_is_200_no_store_no_cookie(onex, client, make_user):
    u = make_user("jay@climbranked.test")
    r = get(client, signed(u.email))
    assert r.status_code == 200, r.text
    assert r.headers["cache-control"] == "no-store" and r.headers["content-type"].startswith("application/json")
    assert "set-cookie" not in r.headers and "access-control-allow-origin" not in r.headers
    assert not client.cookies
    body = r.json()
    assert set(body) == TOP_KEYS and body["v"] == 1 and body["email"] == u.email   # exactly the contract's fields


def test_wrong_secret_changed_email_method_or_path_are_refused(onex, client, member, other_member):
    for h in (signed(member.email, secret="x" * 40),
              signed(member.email, method="POST"),
              signed(member.email, path="/api/onex/v1/other"),
              signed(member.email, path="/api/onex/v1/findings/")):
        r = get(client, h)
        assert r.status_code == 401 and code_of(r) == "bad_signature", h
        assert r.headers["cache-control"] == "no-store"
    h = signed(member.email)
    h["X-OneX-Email"] = other_member.email   # member A's signature, member B's email
    r = get(client, h)
    assert r.status_code == 401 and code_of(r) == "bad_signature"
    assert "hooks" not in r.json() and other_member.email not in r.text


def test_missing_or_malformed_headers(onex, client, member):
    assert code_of(get(client, {})) == "bad_signature"
    good = signed(member.email)
    for name in ("X-OneX-Email", "X-OneX-Timestamp", "X-OneX-Nonce", "X-OneX-Signature"):
        h = {k: v for k, v in good.items() if k != name}
        r = get(client, h)
        assert r.status_code == 401 and code_of(r) == "bad_signature", name
    for name, value in (("X-OneX-Nonce", "Z" * 32), ("X-OneX-Signature", good["X-OneX-Signature"] + "="),
                        ("X-OneX-Email", member.email.upper()), ("X-OneX-Timestamp", "soon")):
        r = get(client, {**good, name: value})
        assert r.status_code == 401 and code_of(r) == "bad_signature", name


def test_stale_timestamps(onex, client, member, monkeypatch):
    import types

    # A frozen server clock: without it, +301 s could land at 300.x s after int() rounding and the slow test run.
    now = int(time.time())
    monkeypatch.setattr(api_onex, "time", types.SimpleNamespace(time=lambda: float(now)))
    for skew in (301, -301, 3600, -86400):
        r = get(client, signed(member.email, ts=str(now + skew)))
        assert r.status_code == 401 and code_of(r) == "stale", skew
    for skew in (300, -300, -290):
        assert get(client, signed(member.email, ts=str(now + skew))).status_code == 200, skew


def test_nonce_works_once_and_bad_signatures_do_not_burn_it(onex, client, member):
    h = signed(member.email)
    assert get(client, h).status_code == 200
    r = get(client, h)
    assert r.status_code == 401 and code_of(r) == "replayed"
    # A stranger who sees a nonce cannot spend it: bad signatures are refused before the nonce is counted.
    nonce = secrets.token_hex(16)
    assert code_of(get(client, signed(member.email, nonce=nonce, secret="x" * 40))) == "bad_signature"
    assert code_of(get(client, signed(member.email, nonce=nonce, skew=900))) == "stale"
    h = signed(member.email, nonce=nonce)
    assert get(client, h).status_code == 200
    assert code_of(get(client, signed(member.email, nonce=nonce))) == "replayed"
    # the counter row is keyed by the SIGNED timestamp's date
    with db.read() as c:
        row = c.execute(select(db.usage_counters).where(db.usage_counters.c.key == "onexapi:" + nonce)).first()
    assert row.scope == "global" and row.count == 2
    assert row.window == datetime.fromtimestamp(int(h["X-OneX-Timestamp"]), timezone.utc).strftime("%Y-%m-%d")


def test_nonce_window_is_the_signed_date_not_today(rv_env):
    nonce = "f" * 32
    late = "1763423999"   # 2025-11-17T23:59:59Z (the test vector's day, last second)
    api_onex._spend_nonce(nonce, late)
    with pytest.raises(ApiError) as e:
        api_onex._spend_nonce(nonce, late)   # the same signed request after midnight still lands in the same row
    assert e.value.status == 401 and e.value.code == "replayed"
    with db.read() as c:
        row = c.execute(select(db.usage_counters).where(db.usage_counters.c.key == "onexapi:" + nonce)).first()
    assert row.window == "2025-11-17"


def test_missing_or_short_secret_is_503(client, member, monkeypatch):
    monkeypatch.delenv("ONEX_SSO_SECRET", raising=False)
    r = get(client, signed(member.email))
    assert r.status_code == 503 and r.json() == {"error": "Sign-in from OneX is not set up yet.", "code": "not_set_up"}
    monkeypatch.setenv("ONEX_SSO_SECRET", "  " + "s" * 31 + "  ")
    assert get(client, signed(member.email, secret="s" * 31)).status_code == 503
    monkeypatch.setenv("ONEX_SSO_SECRET", "  " + SECRET + "\n")   # surrounding whitespace is ignored
    assert get(client, signed(member.email)).status_code == 200


def test_a_session_cookie_is_not_a_signature(onex, member_client):
    r = member_client.get(PATH)
    assert r.status_code == 401 and code_of(r) == "bad_signature"


def test_121_calls_in_a_minute_get_429(onex, client, member, monkeypatch):
    monkeypatch.setattr(ratelimit, "window_key", lambda window, now=None: "fixed-minute")
    for i in range(120):
        assert get(client, {}).status_code == 401, i
    r = get(client, signed(member.email))
    assert r.status_code == 429 and code_of(r) == "too_many"
    assert int(r.headers["retry-after"]) >= 1 and r.headers["cache-control"] == "no-store"
    assert r.json()["error"] == "Too many requests. Wait a minute."


# ---------------------------------------------------------------------------------------------- the route: data

def test_status_no_account(onex, client):
    r = get(client, signed("nobody@example.com"))
    assert r.status_code == 200
    b = r.json()
    assert b["status"] == "no_account" and b["email"] == "nobody@example.com"
    assert b["hooks"] == [] and b["formats"] == [] and b["competitors"] == 0 and b["computed_at"] is None


def test_status_off_hides_the_data(onex, client, make_user):
    for status in ("disabled", "pending"):
        u = make_user(f"{status}@example.com", status=status)
        seed(u.id, status)
        b = get(client, signed(u.email)).json()
        assert b["status"] == "off", status
        assert b["hooks"] == [] and b["formats"] == [] and b["competitors"] == 0 and b["computed_at"] is None


def test_status_empty_new_member_and_member_with_competitors(onex, client, member):
    b = get(client, signed(member.email)).json()
    assert b["status"] == "empty" and b["competitors"] == 0 and b["hooks"] == [] and b["formats"] == []
    add_account(member.id, handle="firstcomp")
    add_account(member.id, handle="secondcomp")
    aid = add_account(member.id, handle="thirdcomp")
    add_reel(member.id, aid, score=0.5)   # pulled but not an outlier yet
    b = get(client, signed(member.email)).json()
    assert b["status"] == "empty" and b["competitors"] == 3 and b["computed_at"] is None


def test_ready_member_gets_hooks_and_formats_with_exact_fields(onex, client, member):
    s = seed(member.id, "alpha")
    with db.read() as c:
        jobs_before = c.execute(select(func.count()).select_from(db.jobs)).scalar()
    r = get(client, signed(member.email))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "ready" and b["v"] == 1 and b["email"] == member.email
    assert b["computed_at"] == "2026-11-17T09:12:03Z" and b["competitors"] == 1
    assert set(b) == TOP_KEYS

    hooks = b["hooks"]
    assert all(set(h) == HOOK_KEYS for h in hooks)
    assert [h["id"] for h in hooks] == [s["deep"], s["q1"], s["odd"]]   # deep first, dupes and weak ones dropped
    d, q, o = hooks
    assert d == {"id": s["deep"], "text": "Alpha deep hook got 41 patients", "on_screen_text": "ALPHA 41 PATIENTS",
                 "hook_type": "bold_claim", "hook_label": "Bold claim", "formula": "This [THING] got [RESULT]",
                 "why": "A number", "handle": "alphacoach", "score": 6.0, "level": "deep",
                 "reel_url": d["reel_url"]}
    assert d["reel_url"].startswith("https://www.instagram.com/reel/")
    assert q["text"] == "Stop boosting alpha posts." and q["on_screen_text"] == "" and q["level"] == "quick"
    assert q["hook_type"] == "contrarian" and q["hook_label"] == "Contrarian" and q["score"] == 4.0
    assert q["formula"] is None and q["why"] is None
    assert o["reel_url"] is None and o["hook_type"] == "made_up_type" and o["hook_label"] is None
    assert not any("my own hook" in h["text"] for h in hooks)

    assert b["formats"] == [{"id": s["good"], "name": "Alpha green-screen myth bust", "visual_format": "VF04",
                             "visual_format_label": "Green screen over image",
                             "line": "3.1x more likely to be an outlier · 12 reels from 3 competitors", "lift": 3.1,
                             "evidence": "strong", "can_make": True, "reels": 12, "competitors": 3}]
    with db.read() as c:
        assert c.execute(select(func.count()).select_from(db.jobs)).scalar() == jobs_before   # read-only


def test_hooks_are_capped_trimmed_and_deduped(onex, client, member):
    aid = add_account(member.id, handle="@bigcoach")
    for i in range(20):
        rid = add_reel(member.id, aid, score=2.0 + i / 10)
        _analysis(member.id, rid, "quick", {"hook_line": (f"Hook number {i} " + "x" * 400) if i else "   "},
                  hook_type="question")
    rid = add_reel(member.id, aid, score=50.0)
    bd = breakdown(rid)
    bd["skeleton"]["hook"].update(spoken="", on_screen_text="SCREEN ONLY " + "Y" * 300)
    bd["hook_formula"] = "F" * 500
    bd["skeleton"]["hook"]["why_it_works"] = "W" * 500
    _analysis(member.id, rid, "deep", bd)
    hooks = get(client, signed(member.email)).json()["hooks"]
    assert len(hooks) == 12
    top = hooks[0]
    assert top["level"] == "deep" and top["text"].startswith("SCREEN ONLY") and len(top["text"]) <= 200
    assert top["on_screen_text"] == "" and len(top["formula"]) == 200 and len(top["why"]) == 300
    assert all(len(h["text"]) <= 300 and h["text"].strip() and h["handle"] == "bigcoach" for h in hooks)
    scores = [h["score"] for h in hooks[1:]]
    assert scores == sorted(scores, reverse=True)
    assert len({h["text"].lower() for h in hooks}) == 12


def test_each_member_gets_only_their_own_data(onex, client, member, other_member):
    a = seed(member.id, "alpha")
    b = seed(other_member.id, "beta")
    got_a = get(client, signed(member.email)).json()
    got_b = get(client, signed(other_member.email)).json()
    assert got_a["email"] == member.email and got_b["email"] == other_member.email
    ids_a = {h["id"] for h in got_a["hooks"]} | {f["id"] for f in got_a["formats"]}
    ids_b = {h["id"] for h in got_b["hooks"]} | {f["id"] for f in got_b["formats"]}
    assert ids_a and ids_b and not ids_a & ids_b
    assert a["deep"] in ids_a and b["deep"] in ids_b
    texts_a = " ".join(h["text"] + h["on_screen_text"] + h["handle"] for h in got_a["hooks"]).lower()
    texts_b = " ".join(h["text"] + h["on_screen_text"] + h["handle"] for h in got_b["hooks"]).lower()
    assert "alpha" in texts_a and "beta" not in texts_a and "beta" in texts_b and "alpha" not in texts_b
    assert {h["handle"] for h in got_a["hooks"]} == {"alphacoach"}
    assert [f["name"] for f in got_b["formats"]] == ["Beta green-screen myth bust"]


def test_router_reads_no_cookie_and_sets_none_even_when_signed_in(onex, member_client, member):
    r = member_client.get(PATH, headers=signed(member.email))
    assert r.status_code == 200 and "set-cookie" not in r.headers
