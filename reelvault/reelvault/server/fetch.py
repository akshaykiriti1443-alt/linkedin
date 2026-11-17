"""Fetch engine policy: read layers, strategies, health, cooldowns, probes (DESIGN-FETCH.md 2, 4, 5). SERVER package.

Layers, tried in this order: `logged_out` (public data from the member's own Chrome, no cookies), `logged_in` (the
paced pull with the member's own Instagram), later `rented` (seam only, section 7; nothing here sends a request).

This module holds the rules and the texts. jobs_ig.py calls it while leasing and ingesting; api_ext.py serves it.
    read_mode(user) / member_settings(user) / save_member_settings(user, body)
    lease_ctx(...) + make_plan(...)          # which layers a job may use on this device, right now
    apply_log(conn, device, user_id, entries, ...) -> fetch state   # health counters + cooldowns from a fetch_log
    lo_view(fetch_state)                     # the `lo` object the extension mirrors
    start_probe / save_probe_rows / finish_probe / probe_json / latest_probe     # "Test my connection"
    status(user) / pulls(user, ...) / admin_health() / admin_set_strategy() / admin_save_settings()

Storage (4.3): devices.state["fetch"] per device, fetch_health per device + strategy + UTC day, fetch_probes, and
app settings under `internal.fetch.*` (see docs/wp-notes/FETCH-SERVER.md for why they carry the `internal.` prefix).
Member-facing text: plain words, short sentences, active voice.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from .. import igparse
from . import db, jobs, ratelimit
from .errors import ApiError, bad_input, not_found
from .settings import get_settings

# The job kind of "Test my connection". jobs.py (WP1) keeps the registry; this package adds its kind to it here.
jobs.KINDS.setdefault("ig_probe", ("external", "extension"))

# ============================================================================================== constants

LAYERS = ("logged_out", "logged_in")                       # `rented` joins later (section 7)
LO_STRATEGIES = igparse.LO_STRATEGIES                      # ladder order (section 2)
LI_STRATEGIES = ("li_session", "li_clips", "li_feed", "li_profile", "li_media")
ALL_STRATEGIES = LO_STRATEGIES + LI_STRATEGIES
USER_STRATEGIES = ("lo_profile_page", "lo_web_profile")    # step A: one of them must find the user
LIST_STRATEGIES = ("lo_reels_page", "lo_reels_gql", "lo_web_profile", "lo_posts_gql")
DETAIL_STRATEGIES = ("lo_post_gql", "lo_media_gql", "lo_embed")
READ_MODES = ("auto", "logged_out_only", "logged_in_only")
MIN_LO_VERSION = "2.1.0"
LI_BLOCK_REASONS = ("rate_limited", "warning", "challenge", "suspended")   # these also stop logged-out reads
LO_STOPS = ("target", "no_more", "budget", "blocked", "no_views", "no_strategy", "refresh_page")
LOG_STATUS = ("ok", "partial", "blocked", "error", "skipped")
PROBE_FIELDS = ("user", "followers", "reel_list", "lsd", "views", "likes", "comments", "taken_at", "caption",
                "video_url", "duration", "signed_in")
DOC_IDS = {"lo_posts_gql": ["28904005159232689"], "lo_reels_gql": ["29285972954340351", "7845543455542541"],
           "lo_post_gql": ["27128499623469141"], "lo_media_gql": ["27130156389949648"]}
DOC_ID_RE = re.compile(r"^\d{10,24}$")
CODE_RE = re.compile(r"^[a-z0-9_]{1,40}$")
HANDLE_RE = re.compile(r"^[a-z0-9._]{1,30}$")
PROBE_HANDLE = "instagram"
PROBE_ROWS_MAX = 20
LOG_MAX = 60

# Pacing for logged-out reads (2.4). Stricter than or equal to the logged-in numbers. Sent in hello.pacing.
LO_PACING = {"lo_gap_ms": [5000, 11000], "lo_page_gap_ms": [8000, 15000], "lo_hour_max": 60, "lo_day_max": 200,
             "lo_detail_max": 36, "layer_gap_ms": [20000, 40000], "probe_day_max": 3}
LO_TEST_PACING = dict(LO_PACING, lo_gap_ms=[50, 100], lo_page_gap_ms=[50, 100], layer_gap_ms=[100, 200],
                      probe_day_max=50)
LO_BUDGET = {"first": 40, "next": 40, "refresh": 16}       # logged-out calls per pull
LO_BUDGET_ONLY = 80                                         # ... when the member reads logged-out only
LO_BUDGET_VIDEOS = 30
LO_BUDGET_PROFILE = 2
PROBE_CALLS = 12

# Cooldowns of table 2.3, in hours: (first, repeat).
H_RATE_FIRST, H_RATE_MAX = 6, 48
H_CHALLENGE = (24, 48)
H_WALL = (6, 24)              # one strategy
H_WALL_ALL = (2, 6)           # login wall on 2+ strategies in one job: all logged-out reads
H_CHANGED = 24
H_EMPTY = 6
H_NO_VIEWS = 24
EMPTY_STREAK = 3
NO_VIEWS_STREAK = 3
REPEAT_WINDOW_H = 48          # a second block inside this window is a "repeat"
SAME_BLOCK_S = 600            # the same block reported twice (fetch_log + fail) counts once
LI_WAIT_MIN = {"rate": 15, "challenge": 60}   # logged-in fallback not before this many minutes
GLOBAL_OFF_ATTEMPTS, GLOBAL_OFF_DEVICES, GLOBAL_OFF_RATE, GLOBAL_OFF_H = 20, 3, 0.2, 24
HEALTH_DAYS = 2               # health windows count this many UTC days (today and yesterday)

KEY_DEFAULT_READ = "internal.fetch.default_read_mode"
KEY_STRATEGIES = "internal.fetch.strategies"
KEY_STRATEGY_OFF = "internal.fetch.strategy_off"
KEY_DOC_IDS = "internal.fetch.doc_ids"
KEY_PROBE_HANDLE = "internal.fetch.probe_handle"
KEY_LAYERS = "internal.fetch.layers"

# ============================================================================================== texts

READ_OPTIONS = [
    {"id": "auto", "label": "Safest first", "recommended": True,
     "text": "ReelVault reads without your login first. It uses your login only for what is missing."},
    {"id": "logged_in_only", "label": "Logged-in only",
     "text": "ReelVault always reads with your Instagram login. It stays slow and careful."},
    {"id": "logged_out_only", "label": "Logged-out only",
     "text": "ReelVault never uses your login. You may get fewer reels and no view counts."},
]
LAYER_LABELS = {"logged_out": "Without your login", "logged_in": "With your Instagram login"}
# id -> (label, what it gives, verified: source | guess | built)
STRATEGY_INFO = {
    "lo_profile_page": ("Profile page", "Followers and the newest posts", "source"),
    "lo_reels_page": ("Reels page", "The newest reels with view counts", "guess"),
    "lo_reels_gql": ("Reels list", "More reels with view counts", "guess"),
    "lo_web_profile": ("Profile data", "Followers and 12 posts with view counts", "source"),
    "lo_posts_gql": ("Posts list", "Older posts", "source"),
    "lo_post_gql": ("Reel detail", "One reel with likes, date, caption and video", "source"),
    "lo_media_gql": ("Reel detail, second way", "One reel with likes, date, caption and video", "source"),
    "lo_embed": ("Embed page", "One reel with views and video", "guess"),
    "li_session": ("Sign-in check", "Your Instagram login works in Chrome", "built"),
    "li_clips": ("Reels list", "Reels with view counts", "built"),
    "li_feed": ("Posts list", "Older reels", "built"),
    "li_profile": ("Profile", "Followers and the account id", "built"),
    "li_media": ("Reel detail", "One reel with its video", "built"),
}
STATUS_LABELS = {"ok": "Works", "partial": "Works in part", "blocked": "Blocked", "error": "Error",
                 "skipped": "Skipped"}
CODE_NOTES = {
    "login_wall": "Instagram asks for a login here.",
    "rate": "Instagram asks ReelVault to wait.",
    "challenge": "Instagram wants you to confirm it's you.",
    "changed": "Instagram changed this. We've been told.",
    "empty": "Instagram gave nothing back.",
    "no_data": "The page has no data inside.",
    "no_views": "This way shows no view counts.",
    "refused": "Instagram refused this one.",
    "no_lsd": "An earlier step did not work.",
    "no_user": "An earlier step did not work.",
    "no_reel": "ReelVault found no reel to test with.",
    "cooling": "ReelVault waits right now.",
    "signed_out": "You're signed out of Instagram in Chrome.",
    "not_found": "ReelVault can't find this account.",
    "private": "This account is private.",
    "error": "Instagram didn't answer.",
}
VERDICTS = {
    "best": {"title": "The safest way works.",
             "text": "ReelVault reads reels without your Instagram login on this connection.",
             "next": "You don't need to do anything."},
    "partial": {"title": "The safest way works in part.",
                "text": "ReelVault reads profiles and videos without your login. It uses your login for view counts.",
                "next": "Stay signed in to Instagram in Chrome."},
    "wait": {"title": "Instagram asks ReelVault to wait.",
             "text": "Instagram slows down reads from this connection for now.",
             "next": "ReelVault goes on by itself. Run the test again in a few hours."},
    "login_needed": {"title": "Instagram asks for a login here.",
                     "text": "ReelVault reads with your Instagram login. It stays slow and careful.",
                     "next": "Stay signed in to Instagram in Chrome."},
    "lo_only_blocked": {"title": "Instagram asks for a login here. You chose to read without it.",
                        "text": "ReelVault reads no reels this way right now.",
                        "next": "Try again later, or pick Safest first in Settings."},
    "signed_out": {"title": "You're signed out of Instagram in Chrome.",
                   "text": "Instagram asks for a login here. Chrome has none right now.",
                   "next": "Sign in at instagram.com in Chrome. Then run the test again."},
    "nothing": {"title": "Instagram blocks reads right now.",
                "text": "Instagram blocks reads with and without your login on this connection.",
                "next": "ReelVault waits by itself. Run the test again in a few hours."},
}
# The same verdict, said for a member who reads without the login only (never mention the login there).
VERDICTS_LO_ONLY = {
    "partial": {"title": "The safest way works in part.",
                "text": "ReelVault reads some reels without your login. Instagram hides the rest right now.",
                "next": "You don't need to do anything. You may get fewer reels and no view counts."},
}
NEXT_SIGN_IN = "Sign in to Instagram in Chrome. ReelVault uses your login to fill in what is missing."
MSG_WAIT = "ReelVault waits until Instagram allows reads again."
MSG_UPDATE = "Update the Chrome extension to read without your login."
MSG_COOLING = "Instagram asks ReelVault to wait. It goes on by itself."
MSG_LO_WALL = "Instagram asks for a login right now. ReelVault tries again later by itself."
MSG_NEEDS_LOGIN = "This step needs your Instagram login. You chose to read without it. Change that in Settings."
MSG_PROBE_WAIT = "Waiting for Chrome. The test starts within 30 seconds."
MSG_PROBE_LIMIT = "You ran {n} tests today. Try again tomorrow."
MSG_PROBE_NO_EXT = "Connect the Chrome extension first."
MSG_PROBE_UPDATE = "Update the Chrome extension first. Then run the test again."
MSG_LI_SIGNED_OUT = "You're signed out of Instagram in Chrome. Sign in at instagram.com. ReelVault checks again by itself."
MSG_LO_SKIPPED = "ReelVault waits a little. It goes on by itself."
PULL_STOP_LO_WALL = "Instagram asked for a login"
PULL_STOP_LO_BUDGET = "Read the safe amount for now"
PULL_MSG = {
    "logged_out": "Read without your Instagram login.",
    "logged_in": "Read with your Instagram login.",
    "mixed": "Read {lo} reels without your login. Your login filled in the other {li}.",
    "mixed_counts": "Read {lo} reels without your login. Your login filled in the view counts.",
    "after_block": "Instagram asks for a login right now. ReelVault used yours.",
    "lo_only": "Read {lo} reels without your login. Instagram shows no more without a login.",
    "lo_only_none": "Instagram asks for a login right now. You chose to read without it, so ReelVault read no reels. "
                    "Try again later or pick Safest first in Settings.",
    "cooldown": MSG_COOLING,
    "waiting": "Waiting for Chrome.",
    "running_lo": "Reading without your login.",
    "running_li": "Reading with your Instagram login.",
}


# ============================================================================================== small helpers

def _now() -> datetime:
    return db.utcnow()


def _dt(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=_now().tzinfo)


def _active(v, now: datetime | None = None) -> bool:
    d = _dt(v)
    return bool(d and d > (now or _now()))


def version_tuple(v) -> tuple:
    try:
        return tuple(int(x) for x in str(v or "0").split("."))
    except ValueError:
        return (0,)


def supports_lo(dev) -> bool:
    """Only an extension 2.1.0 or newer can read without the login."""
    return dev is not None and version_tuple(dev.version) >= version_tuple(MIN_LO_VERSION)


def ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "never"
    s = max(0, int(((now or _now()) - dt).total_seconds()))
    if s < 90:
        return "just now"
    if s < 3600:
        n = s // 60
        return f"{n} minutes ago"
    if s < 2 * 86400:
        n = s // 3600
        return f"{n} hour{'s' if n != 1 else ''} ago"
    return f"{s // 86400} days ago"


def pacing() -> dict:
    return dict(LO_TEST_PACING if get_settings().test_pacing else LO_PACING)


def _days(now: datetime | None = None) -> list[str]:
    now = now or _now()
    return [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(HEALTH_DAYS)]


# ============================================================================================== app settings

def default_read_mode(conn=None) -> str:
    v = db.get_setting(KEY_DEFAULT_READ, conn=conn)
    return v if v in READ_MODES else "auto"


def strategy_order(conn=None) -> list[str]:
    """The ladder order of the logged-out strategies (admin setting, else section 2)."""
    v = db.get_setting(KEY_STRATEGIES, conn=conn)
    out = [s for s in v if s in LO_STRATEGIES] if isinstance(v, list) else []
    out = list(dict.fromkeys(out))
    return out + [s for s in LO_STRATEGIES if s not in out]


def global_off(conn=None, now: datetime | None = None) -> dict:
    """{strategy: {"until": iso | None, "reason": "auto" | "admin"}} for strategies switched off for everyone."""
    v = db.get_setting(KEY_STRATEGY_OFF, conn=conn)
    out = {}
    for s, info in (v.items() if isinstance(v, dict) else []):
        if s not in LO_STRATEGIES or not isinstance(info, dict):
            continue
        until = info.get("until")
        if until is None or _active(until, now):
            out[s] = {"until": until, "reason": "admin" if info.get("reason") == "admin" else "auto"}
    return out


def doc_ids(conn=None) -> dict:
    v = db.get_setting(KEY_DOC_IDS, conn=conn)
    out = {k: list(ids) for k, ids in DOC_IDS.items()}
    for k, ids in (v.items() if isinstance(v, dict) else []):
        if k in DOC_IDS and isinstance(ids, list):
            good = [x for x in ids if isinstance(x, str) and DOC_ID_RE.match(x)][:4]
            if good:
                out[k] = good
    return out


def probe_handle(conn=None) -> str:
    v = db.get_setting(KEY_PROBE_HANDLE, conn=conn)
    return v if isinstance(v, str) and HANDLE_RE.match(v) else PROBE_HANDLE


def layer_order(conn=None) -> list[str]:
    v = db.get_setting(KEY_LAYERS, conn=conn)
    out = [x for x in v if x in LAYERS] if isinstance(v, list) else []
    return out or list(LAYERS)


# ============================================================================================== member settings

def read_mode(user_or_settings, conn=None) -> str:
    """The member's read mode: their own choice, else the admin default, else auto."""
    s = user_or_settings if isinstance(user_or_settings, dict) else (getattr(user_or_settings, "settings", None) or {})
    v = s.get("read_mode") if isinstance(s, dict) else None
    return v if v in READ_MODES else default_read_mode(conn)


def read_mode_for(conn, user_id: str) -> str:
    row = conn.execute(select(db.users.c.settings).where(db.users.c.id == user_id)).first()
    return read_mode(row[0] if row and isinstance(row[0], dict) else {}, conn)


def member_settings(user) -> dict:
    with db.tx(immediate=False) as conn:
        return {"read_mode": read_mode_for(conn, user.id), "default": default_read_mode(conn),
                "options": [dict(o) for o in READ_OPTIONS]}


def save_member_settings(user, body: dict) -> dict:
    mode = body.get("read_mode") if isinstance(body, dict) else None
    if mode not in READ_MODES:
        raise bad_input("Pick one of the three ways to read.")
    with db.tx() as conn:
        cur = conn.execute(select(db.users.c.settings).where(db.users.c.id == user.id)).scalar() or {}
        conn.execute(db.users.update().where(db.users.c.id == user.id).values(settings={**cur, "read_mode": mode}))
        db.audit("read_mode_changed", user_id=user.id, actor=user.id, detail={"read_mode": mode}, conn=conn)
    return member_settings(user)


# ============================================================================================== device fetch state

FETCH_STATE_DEFAULT = {"lo_cooldown_until": None, "lo_cooldown_reason": None, "lo_cooldown_set_at": None,
                       "lo_blocks": 0, "strategy_off": {}, "detail_off_until": None, "strikes": {}, "streaks": {},
                       "walls": {}, "budget": {"lo_hour_used": 0, "lo_day_used": 0}}


def fetch_state(dev_state: dict | None) -> dict:
    """devices.state["fetch"] with every key present (a copy; write it back with save_fetch_state)."""
    raw = (dev_state or {}).get("fetch") if isinstance(dev_state, dict) else None
    fs = {k: (dict(v) if isinstance(v, dict) else v) for k, v in FETCH_STATE_DEFAULT.items()}
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in fs:
                fs[k] = dict(fs[k], **v) if isinstance(fs[k], dict) and isinstance(v, dict) else v
    for k in ("strategy_off", "strikes", "streaks", "walls", "budget"):
        if not isinstance(fs[k], dict):
            fs[k] = dict(FETCH_STATE_DEFAULT[k])
    return fs


def device_fetch_state(dev) -> dict:
    return fetch_state(dev.state if dev is not None else None)


def save_fetch_state(conn, dev_id: str, fs: dict) -> None:
    row = conn.execute(select(db.devices.c.state).where(db.devices.c.id == dev_id)).first()
    if row is None:
        return
    st = dict(row[0] or {})
    st["fetch"] = fs
    conn.execute(db.devices.update().where(db.devices.c.id == dev_id).values(state=st))


def lo_cooling(fs: dict, now: datetime | None = None) -> bool:
    return _active(fs.get("lo_cooldown_until"), now)


def device_off(fs: dict, now: datetime | None = None) -> dict:
    """{strategy: iso} of this device's active per-strategy switch-offs."""
    return {s: u for s, u in (fs.get("strategy_off") or {}).items() if s in LO_STRATEGIES and _active(u, now)}


def lo_view(fs: dict, now: datetime | None = None) -> dict:
    """The `lo` object of hello, lease and upload replies. The extension keeps the later of its time and ours."""
    cooling = lo_cooling(fs, now)
    return {"cooldown_until": fs.get("lo_cooldown_until") if cooling else None,
            "cooldown_reason": fs.get("lo_cooldown_reason") if cooling else None,
            "strategy_off": device_off(fs, now)}


def clean_lo_budget(b: Any) -> dict:
    out = {}
    if isinstance(b, dict):
        for k in ("lo_hour_used", "lo_day_used"):
            v = b.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100000:
                out[k] = v
    return out


def merge_ext_lo(fs: dict, lo: Any, now: datetime | None = None) -> bool:
    """Lease body `lo: {cooldown_until, strategy_off}`: the extension applied table 2.3 at once. The later time
    wins, capped at the longest cooldown the table knows. Returns True when the state changed."""
    if not isinstance(lo, dict):
        return False
    now = now or _now()
    changed = False
    cap = now + timedelta(hours=H_RATE_MAX)
    cd = _dt(lo.get("cooldown_until")) if isinstance(lo.get("cooldown_until"), (str, int, float)) else None
    if isinstance(lo.get("cooldown_until"), (int, float)) and not isinstance(lo.get("cooldown_until"), bool):
        try:
            cd = datetime.fromtimestamp(lo["cooldown_until"] / 1000, tz=now.tzinfo)
        except (OverflowError, OSError, ValueError):
            cd = None
    if cd and cd > now:
        cd = min(cd, cap)
        cur = _dt(fs.get("lo_cooldown_until"))
        if cur is None or cd > cur:
            fs["lo_cooldown_until"] = db.iso(cd)
            fs["lo_cooldown_reason"] = fs.get("lo_cooldown_reason") or "rate"
            changed = True
    off = lo.get("strategy_off")
    if isinstance(off, dict):
        for s, u in list(off.items())[:20]:
            if s not in LO_STRATEGIES:
                continue
            d = None
            if isinstance(u, (int, float)) and not isinstance(u, bool):
                try:
                    d = datetime.fromtimestamp(u / 1000, tz=now.tzinfo)
                except (OverflowError, OSError, ValueError):
                    d = None
            elif isinstance(u, str):
                d = _dt(u)
            if d and d > now:
                d = min(d, cap)
                cur = _dt(fs["strategy_off"].get(s))
                if cur is None or d > cur:
                    fs["strategy_off"][s] = db.iso(d)
                    changed = True
    return changed


# ============================================================================================== fetch_log -> health + cooldowns

def clean_log(entries: Any, limit: int = LOG_MAX) -> list[dict]:
    """FetchLog entries (4.2) with only known values. Bad entries are dropped: a log never fails an upload."""
    out = []
    for e in (entries if isinstance(entries, list) else [])[:limit]:
        if not isinstance(e, dict) or e.get("strategy") not in ALL_STRATEGIES or e.get("status") not in LOG_STATUS:
            continue
        code = e.get("code")

        def num(k, hi):
            v = e.get(k)
            return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= hi else None

        out.append({"strategy": e["strategy"], "status": e["status"],
                    "code": code if isinstance(code, str) and CODE_RE.match(code) else None,
                    "http": num("http", 999), "ms": num("ms", 10**7), "items": num("items", 1000) or 0})
    return out


def lo_calls(entries: list[dict]) -> int:
    """How many logged-out Instagram calls a cleaned log holds (skipped rows made no call)."""
    return sum(1 for e in entries if e["strategy"] in LO_STRATEGIES and e["status"] != "skipped")


def _bump_health(conn, device_id: str, user_id: str | None, entries: list[dict], now: datetime) -> None:
    day = now.strftime("%Y-%m-%d")
    per: dict[str, dict] = {}
    for e in entries:
        if e["status"] == "skipped":
            continue
        a = per.setdefault(e["strategy"], {"ok": 0, "partial": 0, "blocked": 0, "error": 0, "set": {}})
        a[e["status"]] += 1
        if e["status"] in ("ok", "partial"):
            a["set"]["last_ok_at"] = now
        if e["status"] == "blocked":
            a["set"]["last_blocked_at"] = now
        if e["status"] != "ok" and e.get("code"):
            a["set"]["last_code"] = e["code"]
    for s, a in per.items():
        extra = dict(a.pop("set"), user_id=user_id)
        db.bump(db.fetch_health, {"device_id": device_id, "strategy": s, "day": day}, conn=conn, _set=extra, **a)


def _strike(fs: dict, key: str, now: datetime) -> bool:
    """Count a block for `key`. True when it repeats an earlier one inside the repeat window."""
    prev = _dt((fs["strikes"].get(key) or {}).get("at"))
    repeat = bool(prev and (now - prev) < timedelta(hours=REPEAT_WINDOW_H))
    fs["strikes"][key] = {"at": db.iso(now)}
    return repeat


def _cool_all(fs: dict, hours: float, reason: str, now: datetime) -> None:
    until = now + timedelta(hours=hours)
    cur = _dt(fs.get("lo_cooldown_until"))
    if cur is None or until > cur:
        fs["lo_cooldown_until"] = db.iso(until)
        fs["lo_cooldown_reason"] = reason
    fs["lo_cooldown_set_at"] = db.iso(now)


def _same_block(fs: dict, reason: str, now: datetime) -> bool:
    at = _dt(fs.get("lo_cooldown_set_at"))
    return bool(fs.get("lo_cooldown_reason") == reason and at and (now - at).total_seconds() < SAME_BLOCK_S
                and lo_cooling(fs, now))


def _off(fs: dict, strategy: str, hours: float, now: datetime) -> None:
    until = now + timedelta(hours=hours)
    cur = _dt(fs["strategy_off"].get(strategy))
    if cur is None or until > cur:
        fs["strategy_off"][strategy] = db.iso(until)


def apply_block(fs: dict, cls: str, strategy: str | None, now: datetime, *, job_id: str | None = None) -> bool:
    """One row of table 2.3 on a device's fetch state. Returns True when the class is a known block."""
    if cls == "rate":
        if not _same_block(fs, "rate", now):
            n = int(fs.get("lo_blocks") or 0)
            _cool_all(fs, min(H_RATE_FIRST * (2 ** min(n, 6)), H_RATE_MAX), "rate", now)
            fs["lo_blocks"] = n + 1
    elif cls == "challenge":
        if not _same_block(fs, "challenge", now):
            _cool_all(fs, H_CHALLENGE[1] if _strike(fs, "challenge", now) else H_CHALLENGE[0], "challenge", now)
    elif cls == "login_wall":
        if strategy in LO_STRATEGIES:
            _off(fs, strategy, H_WALL[1] if _strike(fs, strategy, now) else H_WALL[0], now)
            walls = fs.get("walls") or {}
            seen = list(walls.get("s") or []) if walls.get("job") == job_id and job_id else []
            if strategy not in seen:
                seen.append(strategy)
            fs["walls"] = {"job": job_id, "s": seen}
            if job_id and len(seen) >= 2 and not _same_block(fs, "login_wall", now):
                _cool_all(fs, H_WALL_ALL[1] if _strike(fs, "walls", now) else H_WALL_ALL[0], "login_wall", now)
    elif cls == "changed":
        if strategy in LO_STRATEGIES:
            _off(fs, strategy, H_CHANGED, now)
    elif cls == "empty":
        if strategy in LO_STRATEGIES:
            _off(fs, strategy, H_EMPTY, now)
    elif cls == "no_views":
        fs["detail_off_until"] = db.iso(now + timedelta(hours=H_NO_VIEWS))
    else:
        return False
    return True


def _apply_rules(fs: dict, entries: list[dict], now: datetime, *, job_id: str | None, probe: bool) -> list[dict]:
    """Table 2.3 over a cleaned log. Returns the blocked entries (for the admin's "recent blocks")."""
    blocks = []
    for e in entries:
        s, status, code = e["strategy"], e["status"], e.get("code")
        if s not in LO_STRATEGIES:
            if status == "blocked":
                blocks.append(e)
            continue
        if status == "ok":
            fs["lo_blocks"] = 0                       # the doubling of `rate` starts over after the first ok
            fs["streaks"].pop(s, None)
            fs["strikes"].pop(s, None)
            if probe:                                 # a probe is the re-test: it switches the strategy back on
                fs["strategy_off"].pop(s, None)
                if s in DETAIL_STRATEGIES and e.get("views"):
                    fs["detail_off_until"] = None
        elif status == "blocked":
            blocks.append(e)
            apply_block(fs, code or "", s, now, job_id=job_id)
        elif status in ("partial", "error") and code in ("empty", "no_views"):
            st = fs["streaks"].setdefault(s, {})
            st[code] = int(st.get(code) or 0) + 1
            if code == "empty" and st[code] >= EMPTY_STREAK:
                apply_block(fs, "empty", s, now)
                st.pop(code, None)
            if code == "no_views" and s in DETAIL_STRATEGIES and st[code] >= NO_VIEWS_STREAK:
                apply_block(fs, "no_views", s, now)
                st.pop(code, None)
    return blocks


def _maybe_global_off(conn, strategies: set[str], now: datetime) -> None:
    """Global rule (2.3): 20+ attempts on 3+ devices and under 20% that work, then off for everyone for 24 h."""
    fh = db.fetch_health
    off = global_off(conn, now)
    changed = False
    for s in strategies:
        if s not in LO_STRATEGIES or s in off:
            continue
        row = conn.execute(select(func.sum(fh.c.ok + fh.c.partial), func.sum(fh.c.blocked + fh.c.error),
                                  func.count(func.distinct(fh.c.device_id)))
                           .where(fh.c.strategy == s, fh.c.day.in_(_days(now)))).first()
        good, bad, devs = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        total = good + bad
        if total >= GLOBAL_OFF_ATTEMPTS and devs >= GLOBAL_OFF_DEVICES and good / total < GLOBAL_OFF_RATE:
            off[s] = {"until": db.iso(now + timedelta(hours=GLOBAL_OFF_H)), "reason": "auto"}
            db.audit("fetch_strategy_off", detail={"strategy": s, "reason": "auto", "attempts": total,
                                                   "devices": devs}, conn=conn)
            changed = True
    if changed:
        db.set_setting(KEY_STRATEGY_OFF, off, conn=conn)


def _global_on(conn, strategies: set[str], now: datetime) -> None:
    """A probe found these strategies ok: an automatic switch-off ends (an admin's stays)."""
    off = global_off(conn, now)
    hit = [s for s in strategies if (off.get(s) or {}).get("reason") == "auto"]
    if hit:
        for s in hit:
            off.pop(s, None)
        db.set_setting(KEY_STRATEGY_OFF, off, conn=conn)


def apply_log(conn, dev, user_id: str | None, entries: list[dict], *, job_id: str | None = None,
              probe: bool = False) -> dict:
    """Count a cleaned fetch_log (or probe rows) in fetch_health and apply table 2.3 to the device.
    dev None = a server-side fetcher (layer 3): health rows get device_id "server" and no device state changes.
    Returns the device's fetch state after the log."""
    now = _now()
    device_id = dev.id if dev is not None else "server"
    if entries:
        _bump_health(conn, device_id, user_id, entries, now)
    if dev is None:
        return fetch_state(None)
    row = conn.execute(select(db.devices).where(db.devices.c.id == dev.id)).first()
    fs = device_fetch_state(row)
    if not entries:
        return fs
    blocks = _apply_rules(fs, entries, now, job_id=job_id, probe=probe)
    save_fetch_state(conn, dev.id, fs)
    for e in blocks[:10]:
        db.audit("fetch_block", user_id=user_id, conn=conn,
                 detail={"strategy": e["strategy"], "code": e.get("code"), "http": e.get("http"),
                         "device": device_id, "job": job_id})
    if blocks:
        _maybe_global_off(conn, {e["strategy"] for e in blocks}, now)
    if probe:
        ok = {e["strategy"] for e in entries if e["status"] == "ok"}
        if ok:
            _global_on(conn, ok, now)
    return fs


def block_device(conn, dev, user_id: str, cls: str, strategy: str | None, http: int | None,
                 job_id: str | None) -> dict:
    """fail {code: "lo_blocked", lo: {class, strategy, http}}: set the cooldown of table 2.3."""
    now = _now()
    row = conn.execute(select(db.devices).where(db.devices.c.id == dev.id)).first()
    fs = device_fetch_state(row)
    apply_block(fs, cls, strategy, now, job_id=job_id)
    save_fetch_state(conn, dev.id, fs)
    return fs


# ============================================================================================== plan (4.1)

def lease_ctx(conn, dev, st: dict, bv: dict, now: datetime, *, li_ok: bool, li_cooling: bool,
              lo_used: tuple[int, int] = (0, 0)) -> dict:
    """What this device may do right now. `li_ok` = today's rule for logged-in work (confirmed, no cooldown,
    budget left). `lo_used` = the server's own (hour, day) count of logged-out calls for this member."""
    fs = fetch_state(st)
    p = pacing()
    li_block = li_cooling and st.get("cooldown_reason") in LI_BLOCK_REASONS
    hour_used = max(int(fs["budget"].get("lo_hour_used") or 0), lo_used[0])
    day_used = max(int(fs["budget"].get("lo_day_used") or 0), lo_used[1])
    lo_left = min(p["lo_hour_max"] - hour_used, p["lo_day_max"] - day_used, bv["hour_max"] - bv["hour_used"],
                  bv["day_max"] - bv["day_used"])
    g_off = global_off(conn, now)
    d_off = device_off(fs, now)
    strategies = [s for s in strategy_order(conn) if s not in g_off and s not in d_off]
    reason = None
    if not supports_lo(dev):
        reason = "update"
    elif li_block:
        reason = "li_block"
    elif lo_cooling(fs, now):
        reason = "cooling"
    elif lo_left <= 0:
        reason = "budget"
    elif not any(s in strategies for s in USER_STRATEGIES) or not any(s in strategies for s in LIST_STRATEGIES):
        reason = "no_strategy"
    return {"fs": fs, "lo_ok": reason is None, "lo_reason": reason, "li_ok": li_ok, "li_block": li_block,
            "lo_left": max(0, lo_left), "strategies": strategies,
            "detail_off": _active(fs.get("detail_off_until"), now), "version_ok": supports_lo(dev)}


def make_plan(kind: str, payload: dict, read: str, ctx: dict) -> dict:
    """`plan` of a leased job (4.1): {lo, li, lo_budget, strategies}. Both false = the job has to wait."""
    strategies = list(ctx["strategies"])
    if kind == "ig_pull":
        mode = payload.get("mode")
        skip = bool(payload.get("_skip_lo")) and read != "logged_out_only"
        lo = (read != "logged_in_only" and ctx["lo_ok"] and not (mode == "next" and read == "auto") and not skip)
        li = read != "logged_out_only" and ctx["li_ok"]
        budget = LO_BUDGET_ONLY if read == "logged_out_only" else LO_BUDGET.get(mode, LO_BUDGET["first"])
        if ctx["detail_off"]:
            strategies = [s for s in strategies if s not in DETAIL_STRATEGIES]
            if not any(s in strategies for s in LIST_STRATEGIES):
                lo = False
    elif kind == "ig_videos":
        skip = bool(payload.get("_skip_lo")) and read != "logged_out_only"
        lo = ctx["lo_ok"] and read != "logged_in_only" and not skip and "lo_post_gql" in strategies
        li = read != "logged_out_only" and ctx["li_ok"]
        budget = LO_BUDGET_VIDEOS
    elif kind == "ig_profile":
        skip = bool(payload.get("_skip_lo")) and read != "logged_out_only"
        lo = (ctx["lo_ok"] and read != "logged_in_only" and not skip
              and any(s in strategies for s in USER_STRATEGIES))
        li = read != "logged_out_only" and ctx["li_ok"]
        budget = LO_BUDGET_PROFILE
    else:
        return {"lo": False, "li": bool(ctx["li_ok"]), "lo_budget": 0, "strategies": []}
    budget = min(budget, ctx["lo_left"]) if lo else 0
    if lo and budget <= 0:
        lo, budget = False, 0
    return {"lo": bool(lo), "li": bool(li), "lo_budget": int(budget), "strategies": strategies if lo else []}


def plan_problem(plan: Any) -> bool:
    """Server twin of validate.js for `plan` (3.3). True = bad."""
    if not isinstance(plan, dict):
        return True
    if not isinstance(plan.get("lo"), bool) or not isinstance(plan.get("li"), bool):
        return True
    if not plan["lo"] and not plan["li"]:
        return True
    b = plan.get("lo_budget", 0)
    if not (isinstance(b, int) and not isinstance(b, bool) and 0 <= b <= 80):
        return True
    ss = plan.get("strategies", [])
    return not (isinstance(ss, list) and all(s in LO_STRATEGIES for s in ss))


def hold_message(read: str, ctx: dict, *, confirmed: bool, li_msg: str | None) -> str:
    """Why a pull waits when neither layer can run (4.1)."""
    if read == "logged_out_only":
        return MSG_UPDATE if not ctx["version_ok"] else MSG_WAIT
    if li_msg:
        return li_msg
    return MSG_WAIT


def hello_fetch(conn, fs: dict, now: datetime | None = None) -> dict:
    """hello.fetch (4.1): doc_ids are server config, so a rotation needs no extension release."""
    now = now or _now()
    off = set(global_off(conn, now)) | set(device_off(fs, now))
    order = strategy_order(conn)
    return {"layers": layer_order(conn), "strategies": order, "off": [s for s in order if s in off],
            "doc_ids": doc_ids(conn), "probe_handle": probe_handle(conn)}


# ============================================================================================== probe (4.4)

def _probe_total(handles: list, want: list | None = None) -> int:
    li = 2 if want is None else sum(1 for w in want if w in LI_STRATEGIES)
    return len(LO_STRATEGIES) + (1 if len(handles) > 1 else 0) + li


def _active_device(conn, user_id: str):
    d = db.devices
    return conn.execute(select(d).where(d.c.user_id == user_id, d.c.kind == "extension", d.c.revoked_at.is_(None))
                        .order_by(d.c.created_at.desc())).first()


def start_probe(user) -> tuple[dict, bool]:
    """POST /api/fetch/probe. Returns (answer, created). An active probe is returned again."""
    fp = db.fetch_probes
    limit = pacing()["probe_day_max"]
    with db.tx() as conn:
        dev = _active_device(conn, user.id)
        if dev is None:
            raise ApiError(409, "busy", MSG_PROBE_NO_EXT)
        live = conn.execute(db.scoped(conn, fp, user.id).where(fp.c.state.in_(("waiting", "running")))
                            .order_by(fp.c.created_at.desc())).first()
        if live is not None:
            job = conn.execute(select(db.jobs).where(db.jobs.c.id == live.job_id)).first()
            if job is not None and job.state in db.ACTIVE_JOB_STATES:
                return {"probe_id": live.id, "job_id": live.job_id, "state": live.state,
                        "message": MSG_PROBE_WAIT if live.state == "waiting" else "The test runs now."}, False
            conn.execute(fp.update().where(fp.c.id == live.id).values(state="failed", finished_at=_now()))
        if not supports_lo(dev):
            raise ApiError(409, "busy", MSG_PROBE_UPDATE)
        wk = ratelimit.window_key("day")
        t = db.usage_counters
        used = conn.execute(select(t.c.count).where(t.c.scope == "probe", t.c.key == dev.id,
                                                    t.c.window == wk)).scalar() or 0
        if used >= limit:
            raise ApiError(429, "limit_reached", MSG_PROBE_LIMIT.format(n=limit),
                           headers={"Retry-After": str(ratelimit.seconds_left("day"))})
        handles = [probe_handle(conn)]
        a = db.accounts
        acc = conn.execute(select(a.c.handle).where(a.c.user_id == user.id, a.c.status == "active",
                                                    a.c.is_private.is_(False))
                           .order_by(a.c.last_pull_at.desc().nulls_last(), a.c.created_at.desc()).limit(1)).first()
        if acc and HANDLE_RE.match(acc[0] or "") and acc[0] != handles[0]:
            handles.append(acc[0])
        pid = db.new_id("prb")
        want = list(LO_STRATEGIES)
        mode = read_mode_for(conn, user.id)
        if mode != "logged_out_only":     # "never uses your login" holds for the test too
            want += ["li_session", "li_clips"]
        payload = {"probe_id": pid, "handles": handles, "want": want, "read": mode}   # read: for the verdict
        jid = jobs.enqueue(user.id, "ig_probe", payload, executor="extension", priority=5,
                           dedupe_key=f"ig_probe:{user.id}", max_attempts=3, conn=conn,
                           progress={"done": 0, "total": _probe_total(handles, want), "message": MSG_PROBE_WAIT})
        conn.execute(fp.insert().values(id=pid, user_id=user.id, device_id=dev.id, job_id=jid, state="waiting",
                                        handles=handles, rows=[], created_at=_now()))
        db.bump(t, {"scope": "probe", "key": dev.id, "window": wk}, conn=conn, count=1)
    return {"probe_id": pid, "job_id": jid, "state": "waiting", "message": MSG_PROBE_WAIT}, True


def clean_probe_rows(rows: Any) -> list[dict]:
    """ProbeRow (3.4) with only known values; raises 400 when a row is not a probe row at all."""
    if not isinstance(rows, list) or not rows:
        raise bad_input("rows must be a list.")
    if len(rows) > PROBE_ROWS_MAX:
        raise bad_input("Too many rows.")
    out = []
    for r in rows:
        if not isinstance(r, dict) or r.get("strategy") not in ALL_STRATEGIES or r.get("status") not in LOG_STATUS:
            raise bad_input("A row is not in the expected shape.")
        s = r["strategy"]
        layer = "logged_out" if s in LO_STRATEGIES else "logged_in"
        handle = r.get("handle")
        code = r.get("code")

        def num(k, hi):
            v = r.get(k)
            return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= hi else None

        fields = [f for f in (r.get("fields") if isinstance(r.get("fields"), list) else []) if f in PROBE_FIELDS]
        out.append({"strategy": s, "layer": layer,
                    "handle": handle if isinstance(handle, str) and HANDLE_RE.match(handle) else None,
                    "status": r["status"], "code": code if isinstance(code, str) and CODE_RE.match(code) else None,
                    "http": num("http", 999), "ms": num("ms", 10**7), "items": num("items", 1000) or 0,
                    "fields": list(dict.fromkeys(fields))})
    return out


def save_probe_rows(conn, dev, job, rows: list[dict]) -> dict:
    """POST /jobs/<id>/probe: append the rows, count them in fetch_health, apply table 2.3 (an ok row switches
    the strategy back on). Returns {"ok", "rows", "lo"}."""
    fp = db.fetch_probes
    pid = (job.payload or {}).get("probe_id")
    probe = conn.execute(db.scoped(conn, fp, dev.user_id).where(fp.c.id == pid)).first()
    if probe is None:
        raise not_found()
    have = list(probe.rows or [])
    if len(have) + len(rows) > PROBE_ROWS_MAX:
        raise bad_input("Too many rows.")
    have.extend(rows)
    conn.execute(fp.update().where(fp.c.id == pid).values(rows=have, state="running"))
    entries = [dict(r, views="views" in r["fields"]) for r in rows]
    fs = apply_log(conn, dev, dev.user_id, entries, job_id=job.id, probe=True)
    total = _probe_total(probe.handles or [], (job.payload or {}).get("want"))
    conn.execute(db.jobs.update().where(db.jobs.c.id == job.id).values(
        progress={"done": min(len(have), total), "total": total, "message": f"Tested {len(have)} of {total} ways"}))
    return {"ok": True, "rows": len(have), "lo": lo_view(fs)}


def probe_mode(job) -> str | None:
    """The read mode a probe ran in: stored on the job at start, else read from its `want` list."""
    p = (job.payload or {}) if job is not None else {}
    if p.get("read") in READ_MODES:
        return p["read"]
    want = p.get("want")
    if isinstance(want, list) and want:
        return "logged_out_only" if not any(w in LI_STRATEGIES for w in want) else None
    return None


def verdict_of(rows: list[dict], mode: str | None = None) -> dict:
    """The probe verdict (4.4): best | partial | wait | login_needed | lo_only_blocked | signed_out | nothing.
    `mode` is the member's read mode when the test ran (a logged-out-only member never hears "sign in")."""
    rows = [r for r in rows if isinstance(r, dict)]

    def rows_of(names, status=("ok",)):
        return [r for r in rows if r.get("strategy") in names and r.get("status") in status]

    def fields(r):
        return r.get("fields") or []

    def gave(r):           # a row that read something: ok, or partial with items in hand
        return r.get("status") == "ok" or (r.get("status") == "partial" and int(r.get("items") or 0) > 0)

    reels_ok = [r for r in rows_of(("lo_reels_page", "lo_reels_gql")) if "views" in fields(r) or not fields(r)]
    list_ok = (rows_of(LIST_STRATEGIES)
               or [r for r in rows_of(("lo_profile_page",)) if "reel_list" in fields(r)])
    detail_views = [r for r in rows_of(DETAIL_STRATEGIES) if "views" in fields(r)]
    # "Works in part" needs a list row or a video row that read something; the profile page alone is not enough.
    lo_work = [r for r in rows if r.get("strategy") in LIST_STRATEGIES + DETAIL_STRATEGIES and gave(r)]
    rate = [r for r in rows if r.get("code") in ("rate", "rate_limited")]
    cooling = [r for r in rows if r.get("code") == "cooling"]
    li_ok = rows_of(("li_clips",))
    li_signed_out = [r for r in rows if r.get("layer", "logged_in" if r.get("strategy") in LI_STRATEGIES else None)
                     == "logged_in" and r.get("code") == "signed_out"]
    lo_only = mode == "logged_out_only"
    if reels_ok or (list_ok and detail_views):
        code = "best"
    elif rate and not lo_work:            # Instagram said "wait": that comes first, nothing else helps now
        code = "wait"
    elif lo_work:
        code = "partial"
    elif lo_only:
        code = "wait" if cooling else "lo_only_blocked"
    elif li_ok:
        code = "login_needed"
    elif li_signed_out:
        code = "signed_out"
    elif cooling:                         # an earlier "wait" still runs
        code = "wait"
    else:
        code = "nothing"
    v = dict(VERDICTS_LO_ONLY.get(code, VERDICTS[code]) if lo_only else VERDICTS[code], code=code)
    if code in ("best", "partial") and li_signed_out and not lo_only:
        v["next"] = NEXT_SIGN_IN        # the login fallback is broken: say how to fix it
    return v


def _probe_complete(rows: list) -> bool:
    """The rows are enough for a verdict: the logged-out part ran to its end (a logged-in row follows it)."""
    lo_n = sum(1 for r in rows if isinstance(r, dict) and r.get("layer") == "logged_out")
    return lo_n >= len(LO_STRATEGIES) or any(isinstance(r, dict) and r.get("layer") == "logged_in" for r in rows)


def finish_probe(conn, job, state: str) -> None:
    """The ig_probe job ended (done, failed, expired or canceled): close the probe. A verdict needs the whole
    logged-out part: a test that stopped later (the login was blocked) still ends as done with a verdict."""
    fp = db.fetch_probes
    pid = (job.payload or {}).get("probe_id")
    probe = conn.execute(select(fp).where(fp.c.id == pid, fp.c.user_id == job.user_id)).first()
    if probe is None or probe.state in ("done", "failed"):
        return
    rows = list(probe.rows or [])
    if state != "done" and _probe_complete(rows):
        state = "done"
    verdict = verdict_of(rows, probe_mode(job)) if state == "done" else None
    conn.execute(fp.update().where(fp.c.id == pid).values(state=state, verdict=verdict, finished_at=_now()))


def _row_json(r: dict) -> dict:
    label, gives, _ = STRATEGY_INFO.get(r.get("strategy"), (r.get("strategy"), "", "built"))
    status = r.get("status")
    note = None
    if status != "ok":
        note = CODE_NOTES.get(r.get("code") or "", None)
        if note is None and status == "blocked":
            note = CODE_NOTES["login_wall"]
        if note is None and status == "error":
            note = CODE_NOTES["error"]
    return {"strategy": r.get("strategy"), "layer": r.get("layer"), "label": label, "handle": r.get("handle"),
            "status": status, "status_label": STATUS_LABELS.get(status, "Error"), "gives": gives, "note": note,
            "ms": r.get("ms"), "code": r.get("code"), "http": r.get("http"), "items": r.get("items"),
            "fields": r.get("fields") or []}


def probe_json(conn, probe) -> dict:
    """GET /api/fetch/probe/<id> (5.3). A probe whose job ended without `done` shows as failed."""
    job = conn.execute(select(db.jobs).where(db.jobs.c.id == probe.job_id)).first() if probe.job_id else None
    state = probe.state
    if state in ("waiting", "running") and (job is None or job.state in ("failed", "canceled", "done")):
        state = "failed" if (job is None or job.state != "done") else "done"
    rows = [_row_json(r) for r in (probe.rows or []) if isinstance(r, dict)]
    handles = list(probe.handles or [])
    verdict = probe.verdict if isinstance(probe.verdict, dict) else None
    if verdict is None and state == "done":
        verdict = verdict_of(list(probe.rows or []), probe_mode(job))
    total = _probe_total(handles, (job.payload or {}).get("want") if job is not None else None)
    out = {"probe_id": probe.id, "job_id": probe.job_id, "state": state,
           "started_at": db.iso(job.started_at) if job is not None and job.started_at else None,
           "created_at": db.iso(probe.created_at), "finished_at": db.iso(probe.finished_at), "handles": handles,
           "progress": {"done": min(len(rows), total), "total": total},
           "rows": rows, "verdict": verdict}
    if state == "waiting":
        out["message"] = MSG_PROBE_WAIT
    elif state == "failed":
        out["message"] = (job.error_message if job is not None and job.error_message
                          else "The test did not finish. Open Chrome, then run it again.")
    return out


def get_probe(user, probe_id: str) -> dict:
    if not (isinstance(probe_id, str) and re.match(r"^prb_[a-z2-7]{16}$", probe_id)):
        raise not_found()
    with db.tx(immediate=False) as conn:
        probe = db.get_owned(conn, db.fetch_probes, user.id, probe_id)
        if probe is None:
            raise not_found()
        return probe_json(conn, probe)


def latest_probe(user) -> dict:
    fp = db.fetch_probes
    with db.tx(immediate=False) as conn:
        probe = conn.execute(db.scoped(conn, fp, user.id).order_by(fp.c.created_at.desc()).limit(1)).first()
        if probe is None:
            raise not_found("You have not run the test yet.")
        return probe_json(conn, probe)


# ============================================================================================== status (5.1)

def _health_times(conn, device_ids: list[str], strategies: tuple, now: datetime) -> tuple[datetime | None, datetime | None]:
    if not device_ids:
        return None, None
    fh = db.fetch_health
    row = conn.execute(select(func.max(fh.c.last_ok_at), func.max(fh.c.last_blocked_at))
                       .where(fh.c.device_id.in_(device_ids), fh.c.strategy.in_(strategies))).first()

    def aware(v):
        return _dt(v) if v is not None else None

    return aware(row[0]), aware(row[1])


def status(user) -> dict:
    """GET /api/fetch/status: which layer works for this member, in plain words."""
    from . import jobs_ig

    now = _now()
    fp = db.fetch_probes
    with db.tx(immediate=False) as conn:
        mode = read_mode_for(conn, user.id)
        dev = _active_device(conn, user.id)
        probe = conn.execute(db.scoped(conn, fp, user.id).where(fp.c.state == "done")
                             .order_by(fp.c.created_at.desc()).limit(1)).first()
        ids = [dev.id] if dev is not None else []
        lo_ok_at, lo_bad_at = _health_times(conn, ids, LO_STRATEGIES, now)
        li_ok_at, li_bad_at = _health_times(conn, ids, LI_STRATEGIES, now)
        strategies = strategy_order(conn)
        g_off = global_off(conn, now)
    device = {"connected": dev is not None, "version": dev.version if dev is not None else None,
              "needs_update": dev is not None and not supports_lo(dev)}

    def layer(lid, state, detail, cd=None, ok_at=None, bad_at=None):
        return {"id": lid, "label": LAYER_LABELS[lid], "state": state, "detail": detail, "cooldown_until": cd,
                "last_ok_at": db.iso(ok_at), "last_blocked_at": db.iso(bad_at)}

    if dev is None:
        lo = layer("logged_out", "unknown", MSG_PROBE_NO_EXT)
        li = layer("logged_in", "unknown", MSG_PROBE_NO_EXT)
    else:
        st = jobs_ig.device_state(dev)
        fs = fetch_state(st)
        li_cd = jobs_ig.cooldown_active(st, now)
        reason = st.get("cooldown_reason") if li_cd else None
        d_off = device_off(fs, now)
        usable = [s for s in strategies if s not in g_off and s not in d_off]
        # ---- without the login
        if mode == "logged_in_only":
            lo = layer("logged_out", "off", "You turned this off in your settings.", None, lo_ok_at, lo_bad_at)
        elif not supports_lo(dev):
            lo = layer("logged_out", "off", MSG_UPDATE, None, lo_ok_at, lo_bad_at)
        elif li_cd and reason in LI_BLOCK_REASONS:
            lo = layer("logged_out", "cooling", MSG_COOLING, st.get("cooldown_until"), lo_ok_at, lo_bad_at)
        elif lo_cooling(fs, now):
            lo = layer("logged_out", "cooling", MSG_LO_WALL if fs.get("lo_cooldown_reason") == "login_wall"
                       else MSG_COOLING, fs.get("lo_cooldown_until"), lo_ok_at, lo_bad_at)
        elif not any(s in usable for s in USER_STRATEGIES) or not any(s in usable for s in LIST_STRATEGIES):
            lo = layer("logged_out", "blocked", "Instagram asks for a login right now.", None, lo_ok_at, lo_bad_at)
        elif lo_ok_at is not None and (lo_bad_at is None or lo_ok_at >= lo_bad_at):
            lo = layer("logged_out", "ok", f"Works. Last read {ago(lo_ok_at, now)}.", None, lo_ok_at, lo_bad_at)
        elif lo_ok_at is not None:
            lo = layer("logged_out", "partial", "Instagram blocks some ways right now. The others work.", None,
                       lo_ok_at, lo_bad_at)
        elif lo_bad_at is not None:
            lo = layer("logged_out", "blocked", "Instagram asks for a login right now.", None, lo_ok_at, lo_bad_at)
        else:
            lo = layer("logged_out", "unknown", "Not tried yet. Run the test to check.", None, None, None)
        # ---- with the login
        if mode == "logged_out_only":
            li = layer("logged_in", "off", "You turned this off in your settings.", None, li_ok_at, li_bad_at)
        elif li_cd and reason == "signed_out":
            li = layer("logged_in", "blocked", jobs_ig.MSG["signed_out"], st.get("cooldown_until"), li_ok_at, li_bad_at)
        elif li_cd:
            li = layer("logged_in", "cooling", jobs_ig.MSG.get(reason or "", MSG_COOLING), st.get("cooldown_until"),
                       li_ok_at, li_bad_at)
        elif not jobs_ig.is_confirmed(dev):
            li = layer("logged_in", "blocked", jobs_ig.MSG["confirm_account"], None, li_ok_at, li_bad_at)
        elif st.get("signed_in") is False:     # no Try again button here (that one needs the signed_out cooldown)
            li = layer("logged_in", "blocked", MSG_LI_SIGNED_OUT, None, li_ok_at, li_bad_at)
        elif li_ok_at is not None:
            li = layer("logged_in", "ok", f"Works. Last read {ago(li_ok_at, now)}.", None, li_ok_at, li_bad_at)
        else:
            li = layer("logged_in", "ok", "Works.", None, li_ok_at, li_bad_at)
    last = None
    if probe is not None:
        v = probe.verdict if isinstance(probe.verdict, dict) else verdict_of(list(probe.rows or []), mode)
        last = {"id": probe.id, "at": db.iso(probe.finished_at or probe.created_at),
                "verdict": {"code": v.get("code"), "title": v.get("title")}}
    return {"read_mode": mode, "device": device, "layers": [lo, li], "last_probe": last}


# ============================================================================================== pulls (5.2)

def layer_of(layers: dict | None) -> str | None:
    """pulls.layer from pulls.layers: logged_out | logged_in | mixed | None (nothing read yet)."""
    layers = layers if isinstance(layers, dict) else {}
    lo, li = layers.get("logged_out") or {}, layers.get("logged_in") or {}
    lo_n = int(lo.get("reels") or 0) + int(lo.get("updated") or 0)
    li_n = int(li.get("reels") or 0) + int(li.get("updated") or 0)
    if lo_n and li_n:
        return "mixed"
    if lo_n:
        return "logged_out"
    if li_n or int(li.get("pages") or 0):
        return "logged_in"
    if int(lo.get("calls") or 0):
        return "logged_out"
    return None


def read_message(*, state: str, mode: str | None, layer: str | None, layers: dict, stage: str | None = None,
                 cooling: bool = False) -> str:
    lo, li = layers.get("logged_out") or {}, layers.get("logged_in") or {}
    lo_reels, li_reels = int(lo.get("reels") or 0), int(li.get("reels") or 0)
    if state == "waiting":
        return PULL_MSG["cooldown"] if cooling else PULL_MSG["waiting"]
    if state == "running":
        return PULL_MSG["running_li"] if stage == "li" or layer in ("logged_in", "mixed") else PULL_MSG["running_lo"]
    if mode == "logged_out_only" and layer in (None, "logged_out"):
        if lo.get("stop") in ("target", "no_more", "refresh_page"):
            return PULL_MSG["logged_out"]
        return PULL_MSG["lo_only"].format(lo=lo_reels) if lo_reels else PULL_MSG["lo_only_none"]
    if layer == "mixed":
        if li_reels:
            return PULL_MSG["mixed"].format(lo=lo_reels, li=li_reels)
        if lo_reels:
            return PULL_MSG["mixed_counts"].format(lo=lo_reels)
        return PULL_MSG["logged_in"]
    if layer == "logged_out":
        return PULL_MSG["logged_out"]
    if layer == "logged_in" and int(lo.get("calls") or 0):
        return PULL_MSG["after_block"]
    return PULL_MSG["logged_in"]


def stop_label(*, state: str, mode: str | None, layer: str | None, layers: dict) -> str | None:
    """A plain "why it stopped" for a pull that only read without the login and ended on a block (the stored
    stop_reason is the generic page_cap then). None: the stop_reason label says it well."""
    lo = layers.get("logged_out") or {}
    if state != "done" or mode != "logged_out_only" or layer not in (None, "logged_out"):
        return None
    if lo.get("stop") in ("blocked", "no_strategy", "no_views"):
        return PULL_STOP_LO_WALL
    if lo.get("stop") == "budget":
        return PULL_STOP_LO_BUDGET
    return None


def pulls(user, account_id: str | None = None, limit: int = 20, cursor: str | None = None) -> dict:
    """GET /api/fetch/pulls: how each pull was read, newest first."""
    limit = max(1, min(int(limit or 20), 100))
    try:
        off = max(0, int(cursor)) if cursor else 0
    except ValueError:
        raise bad_input("That page link doesn't look right.") from None
    p, a, j = db.pulls, db.accounts, db.jobs
    now = _now()
    with db.tx(immediate=False) as conn:
        if account_id is not None and db.get_owned(conn, a, user.id, account_id) is None:
            raise not_found()
        q = (select(p, a.c.handle, j.c.state.label("job_state"), j.c.run_after.label("job_run_after"),
                    j.c.result.label("job_result"), j.c.created_at.label("job_created_at"))
             .select_from(p.join(a, a.c.id == p.c.account_id).outerjoin(j, j.c.id == p.c.job_id))
             .where(p.c.user_id == user.id, a.c.user_id == user.id))
        if account_id is not None:
            q = q.where(p.c.account_id == account_id)
        q = q.order_by(func.coalesce(p.c.finished_at, p.c.started_at, j.c.created_at).desc().nulls_first(),
                       p.c.id.desc()).limit(limit + 1).offset(off)
        rows = conn.execute(q).all()
        default_mode = default_read_mode(conn)
    out = []
    for r in rows[:limit]:
        if r.finished_at is not None or r.job_state in (None, "done", "failed", "canceled"):
            state = "done"
        elif r.job_state == "running":
            state = "running"
        else:
            state = "waiting"
        layers = r.layers if isinstance(r.layers, dict) else {}
        layer = r.layer or layer_of(layers)
        if state == "done" and layer is None and r.read_mode != "logged_out_only":
            layer = "logged_in"                      # pulls from before the fetch engine used the login
        lo, li = layers.get("logged_out") or {}, layers.get("logged_in") or {}
        stage = ((r.job_result or {}).get("ingest") or {}).get("stage") if isinstance(r.job_result, dict) else None
        cooling = state == "waiting" and r.started_at is not None and _active(r.job_run_after, now)
        mode = r.read_mode or (default_mode if state != "done" else "logged_in_only")
        out.append({
            "id": r.id, "account_id": r.account_id, "handle": r.handle, "mode": r.mode, "state": state,
            "started_at": db.iso(r.started_at), "finished_at": db.iso(r.finished_at),
            "reels_new": int(r.reels_new or 0),
            "read": {"mode": mode, "layer": layer,
                     "logged_out": {"reels": int(lo.get("reels") or 0), "calls": int(lo.get("calls") or 0),
                                    "stop": lo.get("stop")},
                     "logged_in": {"reels": int(li.get("reels") or 0), "pages": int(li.get("pages") or 0)},
                     "message": read_message(state=state, mode=mode, layer=layer, layers=layers, stage=stage,
                                             cooling=cooling),
                     "stop_label": stop_label(state=state, mode=mode, layer=layer, layers=layers)}})
    return {"pulls": out, "next_cursor": str(off + limit) if len(rows) > limit else None}


# ============================================================================================== admin (5.4)

def admin_health() -> dict:
    """GET /api/admin/fetch-health. Numbers per strategy, layer and device. No member content."""
    from . import jobs_ig

    now = _now()
    days = _days(now)
    since = now - timedelta(hours=24 * HEALTH_DAYS)
    fh, p, d, u, fp = db.fetch_health, db.pulls, db.devices, db.users, db.fetch_probes
    with db.tx(immediate=False) as conn:
        order = strategy_order(conn)
        g_off = global_off(conn, now)
        agg = {r[0]: r for r in conn.execute(
            select(fh.c.strategy, func.sum(fh.c.ok), func.sum(fh.c.partial), func.sum(fh.c.blocked),
                   func.sum(fh.c.error), func.count(func.distinct(fh.c.device_id)), func.max(fh.c.last_ok_at),
                   func.max(fh.c.last_blocked_at)).where(fh.c.day.in_(days)).group_by(fh.c.strategy)).all()}
        codes = {}
        for r in conn.execute(select(fh.c.strategy, fh.c.last_code, fh.c.day).where(
                fh.c.day.in_(days), fh.c.last_code.is_not(None)).order_by(fh.c.day.asc())).all():
            codes[r[0]] = r[1]
        strategies = []
        for s in list(order) + list(LI_STRATEGIES):
            a = agg.get(s)
            ok, part, blocked, err = (int(a[i] or 0) for i in (1, 2, 3, 4)) if a else (0, 0, 0, 0)
            total = ok + part + blocked + err
            label, _, verified = STRATEGY_INFO[s]
            strategies.append({
                "id": s, "layer": "logged_out" if s in LO_STRATEGIES else "logged_in", "label": label,
                "verified": verified, "enabled": s not in g_off, "off": g_off.get(s),
                "can_switch": s in LO_STRATEGIES, "ok": ok, "partial": part, "blocked": blocked, "error": err,
                "rate": round(ok / total, 2) if total else None, "devices": int(a[5] or 0) if a else 0,
                "last_ok_at": db.iso(_dt(a[6])) if a and a[6] else None,
                "last_blocked_at": db.iso(_dt(a[7])) if a and a[7] else None, "last_code": codes.get(s)})
        per_layer = {x: {"id": x, "pulls": 0, "reels": 0} for x in ("logged_out", "logged_in", "mixed")}
        for r in conn.execute(select(p.c.layer, func.count(), func.sum(p.c.reels_new))
                              .where(p.c.finished_at >= since).group_by(p.c.layer)).all():
            key = r[0] if r[0] in per_layer else "logged_in"
            per_layer[key]["pulls"] += int(r[1] or 0)
            per_layer[key]["reels"] += int(r[2] or 0)
        verdicts = {}
        for r in conn.execute(select(fp.c.device_id, fp.c.verdict).where(fp.c.state == "done")
                              .order_by(fp.c.created_at.asc())).all():
            if isinstance(r[1], dict):
                verdicts[r[0]] = r[1].get("code")
        devices = []
        for r in conn.execute(select(d, u.c.email).select_from(d.join(u, u.c.id == d.c.user_id))
                              .where(d.c.kind == "extension", d.c.revoked_at.is_(None))
                              .order_by(d.c.last_seen_at.desc().nulls_last()).limit(200)).all():
            st = jobs_ig.device_state(r)
            fs = fetch_state(st)
            li_cd = st.get("cooldown_until") if jobs_ig.cooldown_active(st, now) else None
            if not supports_lo(r):
                lo_state = "off"
            elif lo_cooling(fs, now) or (li_cd and st.get("cooldown_reason") in LI_BLOCK_REASONS):
                lo_state = "cooling"
            else:
                lo_state = "ok"
            devices.append({"device_id": r.id, "user_email": r.email, "version": r.version, "lo_state": lo_state,
                            "lo_cooldown_until": fs.get("lo_cooldown_until") if lo_cooling(fs, now) else None,
                            "lo_cooldown_reason": fs.get("lo_cooldown_reason") if lo_cooling(fs, now) else None,
                            "li_cooldown_until": li_cd, "strategy_off": sorted(device_off(fs, now)),
                            "last_probe_verdict": verdicts.get(r.id), "last_seen_at": db.iso(r.last_seen_at)})
        blocks = conn.execute(select(db.audit_log).where(db.audit_log.c.action == "fetch_block")
                              .order_by(db.audit_log.c.at.desc()).limit(20)).all()
        settings = {"default_read_mode": default_read_mode(conn), "doc_ids": doc_ids(conn),
                    "probe_handle": probe_handle(conn)}
    return {"window_h": 24 * HEALTH_DAYS, "strategies": strategies, "layers": list(per_layer.values()),
            "devices": devices,
            "recent_blocks": [{"at": db.iso(b.at), "strategy": (b.detail or {}).get("strategy"),
                               "code": (b.detail or {}).get("code"), "http": (b.detail or {}).get("http"),
                               "device_id": (b.detail or {}).get("device")} for b in blocks],
            "settings": settings}


def admin_set_strategy(admin, body: dict) -> dict:
    """POST /api/admin/fetch-health/strategy {"strategy", "enabled"}: switch one logged-out strategy for everyone."""
    s = body.get("strategy") if isinstance(body, dict) else None
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if s not in ALL_STRATEGIES or not isinstance(enabled, bool):
        raise bad_input("Pick a way to read and say on or off.")
    if s not in LO_STRATEGIES:
        raise bad_input("You can only switch the ways that read without a login.")
    with db.tx() as conn:
        off = global_off(conn)
        if enabled:
            off.pop(s, None)
        else:
            off[s] = {"until": None, "reason": "admin"}
        db.set_setting(KEY_STRATEGY_OFF, off, conn=conn)
        db.audit("fetch_strategy_" + ("on" if enabled else "off"), actor=admin.id,
                 detail={"strategy": s, "reason": "admin"}, conn=conn)
    return {"ok": True}


def admin_save_settings(admin, body: dict) -> dict:
    """PUT /api/admin/fetch-health/settings {"default_read_mode"?, "doc_ids"?, "probe_handle"?}."""
    if not isinstance(body, dict):
        raise bad_input("The body must be a JSON object.")
    unknown = [k for k in body if k not in ("default_read_mode", "doc_ids", "probe_handle")]
    if unknown:
        raise bad_input(f"Unknown setting: {', '.join(sorted(unknown))}.")
    vals: dict = {}
    if "default_read_mode" in body:
        if body["default_read_mode"] not in READ_MODES:
            raise bad_input("default_read_mode must be auto, logged_out_only or logged_in_only.")
        vals[KEY_DEFAULT_READ] = body["default_read_mode"]
    if "probe_handle" in body:
        h = body["probe_handle"]
        h = h.strip().lstrip("@").lower() if isinstance(h, str) else ""
        if not HANDLE_RE.match(h):
            raise bad_input("That Instagram name doesn't look right.")
        vals[KEY_PROBE_HANDLE] = h
    if "doc_ids" in body:
        d = body["doc_ids"]
        if not isinstance(d, dict):
            raise bad_input("doc_ids must be an object.")
        clean = {}
        for k, ids in d.items():
            if k not in DOC_IDS:
                raise bad_input(f"{k} uses no doc_id.")
            if not isinstance(ids, list) or len(ids) > 4:
                raise bad_input("Give at most 4 ids for each way.")
            if any(not (isinstance(x, str) and DOC_ID_RE.match(x)) for x in ids):
                raise bad_input("A doc_id has 10 to 24 digits.")
            if ids:                              # an empty list goes back to the built-in ids
                clean[k] = list(dict.fromkeys(ids))
        vals[KEY_DOC_IDS] = clean
    with db.tx() as conn:
        for k, v in vals.items():
            db.set_setting(k, v, conn=conn)
        db.audit("fetch_settings_changed", actor=admin.id,
                 detail={"keys": sorted(k.rsplit(".", 1)[-1] for k in vals)}, conn=conn)
    return {"ok": True}


__all__ = ["LO_STRATEGIES", "LI_STRATEGIES", "ALL_STRATEGIES", "READ_MODES", "MIN_LO_VERSION", "pacing", "read_mode",
           "read_mode_for", "member_settings", "save_member_settings", "fetch_state", "lo_view", "apply_log",
           "block_device", "lease_ctx", "make_plan", "hello_fetch", "start_probe", "save_probe_rows", "finish_probe",
           "get_probe", "latest_probe", "status", "pulls", "admin_health", "admin_set_strategy",
           "admin_save_settings", "verdict_of", "layer_of"]
