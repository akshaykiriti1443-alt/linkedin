"""Database layer for ReelVault Members (DESIGN-SAAS.md 2.2, 3). WP1 owns this file.

SQLAlchemy 2.x Core only (no ORM sessions). SQLite by default (WAL, BEGIN IMMEDIATE on writes), Postgres-ready.

Use:
    from reelvault.server import db
    with db.tx() as conn:                       # write transaction (BEGIN IMMEDIATE on SQLite)
        conn.execute(db.reels.update().where(...))
    with db.tx(immediate=False) as conn:        # read-only / deferred transaction
        row = conn.execute(db.scoped(conn, db.reels, uid).where(db.reels.c.id == rid)).first()
    db.bump(db.usage_counters, {"scope": uid, "key": "pulls", "window": "2026-11-09"}, count=1)

Never hold a transaction across an AI call, ffmpeg, whisper or an HTTP request.
Access `db.engine` (or db.get_engine()) as an attribute at call time; db.init() can replace it (tests).
"""
from __future__ import annotations

import base64
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import (
    JSON, BigInteger, Boolean, CheckConstraint, Column, Engine, Float, ForeignKey, Index, Integer, LargeBinary,
    MetaData, PrimaryKeyConstraint, Select, Table, Text, UniqueConstraint, create_engine, event, select,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.types import DateTime, TypeDecorator

# --------------------------------------------------------------------------------------------- types and ids

ID_PREFIXES = ("usr", "inv", "ses", "aic", "dev", "acc", "pul", "reel", "job", "out", "src", "top", "snap", "aud",
               "fmt", "cand", "drun", "ana", "dcr")


def new_id(prefix: str) -> str:
    """prefix + '_' + 16 lowercase base32 chars from 10 random bytes, e.g. 'usr_7q2k...'."""
    return f"{prefix.rstrip('_')}_{base64.b32encode(os.urandom(10)).decode().lower()}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    """ISO 8601 UTC with 'Z' (API times, 4.1)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class UTCDateTime(TypeDecorator):
    """DateTime(timezone=True) that always hands back aware UTC datetimes (SQLite stores naive UTC text)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        value = value.astimezone(timezone.utc)
        if dialect.name == "sqlite":
            value = value.replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


JSONType = JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = UTCDateTime


def _enum(name: str, values: tuple[str, ...], *, nullable: bool = False, default: str | None = None) -> Column:
    """TEXT column whose allowed values become a named table-level CHECK (added by _t, so Alembic renders it)."""
    return Column(name, Text, nullable=nullable, default=default, info={"enum": tuple(values)})


def _uid(table: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> Column:
    return Column("user_id", Text, ForeignKey("users.id", ondelete=ondelete), nullable=nullable)


metadata = MetaData(naming_convention={
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
})


def _t(name: str, *cols, **kw) -> Table:
    checks = []
    for c in cols:
        if isinstance(c, Column) and c.info.get("enum"):
            vals = ", ".join(f"'{v}'" for v in c.info["enum"])
            checks.append(CheckConstraint(f"{c.name} IN ({vals})", name=f"ck_{name}_{c.name}"))
    return Table(name, metadata, *cols, *checks, **kw)


# --------------------------------------------------------------------------------------------- 3.1 users

users = _t(
    "users",
    Column("id", Text, primary_key=True),
    Column("email", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False, default=""),
    Column("password_hash", Text, nullable=False),
    Column("skool_url", Text),
    _enum("status", ("active", "pending", "disabled"), default="pending"),
    _enum("role", ("member", "admin"), default="member"),
    _enum("ai_mode", ("api", "claude_connector", "desktop"), nullable=True),
    Column("tagging_conn_id", Text),
    Column("settings", JSONType, nullable=False, default=lambda: dict(USER_SETTINGS_DEFAULT)),
    Column("storage_bytes", BigInteger, nullable=False, default=0),
    Column("created_at", TS, nullable=False, default=utcnow),
    Column("last_login_at", TS),
    Column("last_seen_at", TS),
    Column("invite_id", Text),
    Column("membership_checked_at", TS),
    _enum("disabled_reason", ("admin", "left_community", "inactive", "not_reconfirmed"), nullable=True),
    Index("ix_users_status", "status"),
)

USER_SETTINGS_DEFAULT = {"timezone": "Europe/London", "language": "en", "email_codes": False, "process_all": False,
                         "support_access_until": None}

# --------------------------------------------------------------------------------------------- 3.2 sessions

sessions = _t(
    "sessions",
    Column("id_hash", Text, primary_key=True),
    _uid("sessions"),
    Column("csrf_token", Text, nullable=False),
    Column("created_at", TS, nullable=False, default=utcnow),
    Column("last_seen_at", TS, nullable=False, default=utcnow),
    Column("expires_at", TS, nullable=False),
    Column("ip", Text),
    Column("user_agent", Text),
    Index("ix_sessions_user_id", "user_id"),
)

# --------------------------------------------------------------------------------------------- 3.3

invites = _t(
    "invites",
    Column("id", Text, primary_key=True),
    _enum("kind", ("community", "single")),
    Column("code_hash", Text, nullable=False, unique=True),
    Column("code_hint", Text),
    Column("label", Text),
    Column("uses_left", Integer),
    Column("used_count", Integer, nullable=False, default=0),
    Column("expires_at", TS),
    Column("created_by", Text),
    Column("created_at", TS, nullable=False, default=utcnow),
    Column("revoked_at", TS),
    Index("ix_invites_kind", "kind", "revoked_at"),
)

allowlist = _t(
    "allowlist",
    Column("email", Text, primary_key=True),
    _enum("source", ("zapier", "admin", "csv", "skool_sync")),
    Column("added_at", TS, nullable=False, default=utcnow),
)

reset_links = _t(
    "reset_links",
    Column("token_hash", Text, primary_key=True),
    _uid("reset_links"),
    Column("created_by", Text, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("used_at", TS),
    Index("ix_reset_links_user_id", "user_id"),
)

app_settings = _t(
    "app_settings",
    Column("key", Text, primary_key=True),
    Column("value", JSONType),
)

APP_SETTINGS_DEFAULTS: dict[str, Any] = {
    "signup_mode": "pending",
    "community_code_days": 30,
    "notify_admin_email": True,
    "expire_inactive_days": 120,
    "reconfirm_days": 0,
    "skool_fetch": False,
    "max_accounts_per_member": 10,
    "max_pulls_per_day": 5,
    "outputs_per_day": 40,
    "member_media_per_day": 150,
    "storage_cap_mb": 4000,
    "video_keep_days": 7,
    "allow_local_ai_urls": False,
    "baseline_sample_per_account": 25,
    "insight_window_days": 365,
    "recent_window_days": 90,
    "copy_overrides": {},
    "community_stats": False,
}

# --------------------------------------------------------------------------------------------- 3.4 - 3.6.1

profiles = _t(
    "profiles",
    _uid("profiles"),
    Column("brand_id", Text, nullable=False, default="me"),
    Column("brand_label", Text, nullable=False, default="Me"),
    _enum("status", ("empty", "drafting", "draft", "approved"), default="empty"),
    Column("answers", JSONType, nullable=False, default=dict),
    Column("digest", JSONType),
    Column("version", Integer, nullable=False, default=0),
    Column("approved_at", TS),
    Column("updated_at", TS, nullable=False, default=utcnow),
    PrimaryKeyConstraint("user_id", "brand_id"),
)

profile_sources = _t(
    "profile_sources",
    Column("id", Text, primary_key=True),
    _uid("profile_sources"),
    _enum("kind", ("skool_group", "skool_profile", "website", "linkedin_pdf", "linkedin_text", "linkedin_page",
                   "instagram_bio", "paste")),
    Column("url", Text),
    _enum("via", ("extension", "server", "upload", "paste")),
    _enum("status", ("queued", "waiting", "ok", "needs_paste", "failed"), default="queued"),
    Column("text", Text),
    Column("meta", JSONType, nullable=False, default=dict),
    Column("error", Text),
    Column("file_path", Text),
    Column("job_id", Text),
    Column("fetched_at", TS),
    Column("created_at", TS, nullable=False, default=utcnow),
    Index("ix_profile_sources_user_id", "user_id"),
)

topics = _t(
    "topics",
    Column("id", Text, primary_key=True),
    _uid("topics"),
    Column("brand_id", Text, nullable=False, default="me"),
    Column("slug", Text, nullable=False),
    Column("label", Text, nullable=False),
    Column("description", Text, nullable=False, default=""),
    Column("examples", JSONType, nullable=False, default=list),
    _enum("source", ("ai", "member", "ai_from_reels"), default="member"),
    _enum("status", ("active", "suggested", "archived"), default="active"),
    Column("position", Integer, nullable=False, default=0),
    Column("created_at", TS, nullable=False, default=utcnow),
    UniqueConstraint("user_id", "brand_id", "slug"),
)

formats = _t(
    "formats",
    Column("id", Text, primary_key=True),
    _uid("formats"),
    Column("brand_id", Text, nullable=False, default="me"),
    Column("recipe_key", Text, nullable=False),
    Column("vf", Text, nullable=False),
    Column("recipe", JSONType, nullable=False, default=dict),
    Column("name", Text),
    Column("guide", JSONType),
    Column("stats", JSONType, nullable=False, default=dict),
    Column("example_reels", JSONType, nullable=False, default=list),
    Column("needs", JSONType, nullable=False, default=list),
    Column("computed_at", TS, nullable=False, default=utcnow),
    Column("named_at", TS),
    UniqueConstraint("user_id", "brand_id", "recipe_key"),
)

# --------------------------------------------------------------------------------------------- 3.7 AI

ai_connections = _t(
    "ai_connections",
    Column("id", Text, primary_key=True),
    _uid("ai_connections"),
    _enum("provider", ("anthropic", "gemini", "openai", "openai_compat")),
    _enum("preset", ("openrouter", "groq", "ollama_cloud", "ollama_local", "lmstudio", "gemini_openai", "custom"),
          nullable=True),
    Column("base_url", Text),
    Column("model", Text, nullable=False),
    Column("cheap_model", Text),
    Column("key_ciphertext", LargeBinary),
    Column("key_nonce", LargeBinary),
    Column("key_kid", Text),
    Column("key_last4", Text),
    Column("options", JSONType, nullable=False, default=dict),
    Column("is_default", Boolean, nullable=False, default=False),
    _enum("status", ("untested", "ok", "bad_key", "quota", "error"), default="untested"),
    Column("last_tested_at", TS),
    Column("last_error", Text),
    Column("created_at", TS, nullable=False, default=utcnow),
    Index("ix_ai_connections_user_id", "user_id"),
)

ai_usage = _t(
    "ai_usage",
    Column("conn_id", Text, ForeignKey("ai_connections.id", ondelete="CASCADE"), nullable=False),
    Column("month", Text, nullable=False),
    _uid("ai_usage"),
    Column("calls", Integer, nullable=False, default=0),
    Column("tokens_in", BigInteger, nullable=False, default=0),
    Column("tokens_out", BigInteger, nullable=False, default=0),
    PrimaryKeyConstraint("conn_id", "month"),
    Index("ix_ai_usage_user_id", "user_id", "month"),
)

# --------------------------------------------------------------------------------------------- 3.8 devices

devices = _t(
    "devices",
    Column("id", Text, primary_key=True),
    _uid("devices"),
    _enum("kind", ("extension", "desktop")),
    Column("name", Text, nullable=False, default=""),
    Column("token_hash", Text, nullable=False, unique=True),
    Column("version", Text),
    Column("ig_user_id", Text),
    Column("ig_username", Text),
    Column("ig_confirmed_user_id", Text),
    Column("state", JSONType, nullable=False, default=lambda: dict(DEVICE_STATE_DEFAULT)),
    Column("created_at", TS, nullable=False, default=utcnow),
    Column("last_seen_at", TS),
    Column("revoked_at", TS),
    Index("ix_devices_user_id", "user_id", "kind"),
)

DEVICE_STATE_DEFAULT = {"cooldown_until": None, "cooldown_reason": None, "signed_in": True, "ig_checked_at": None,
                        "budget": {"hour_used": 0, "day_used": 0, "cdn_day_used": 0}}

pairing_codes = _t(
    "pairing_codes",
    Column("code_hash", Text, primary_key=True),
    _uid("pairing_codes"),
    _enum("kind", ("extension", "desktop")),
    Column("expires_at", TS, nullable=False),
    Column("attempts", Integer, nullable=False, default=0),
    Column("used_at", TS),
    Index("ix_pairing_codes_user_id", "user_id"),
)

# --------------------------------------------------------------------------------------------- 3.9 - 3.10

accounts = _t(
    "accounts",
    Column("id", Text, primary_key=True),
    _uid("accounts"),
    _enum("role", ("competitor", "self"), default="competitor"),
    _enum("platform", ("instagram",), default="instagram"),
    Column("handle", Text, nullable=False),
    Column("ig_pk", Text),
    Column("full_name", Text),
    Column("bio", Text),
    Column("category", Text),
    Column("followers", BigInteger),
    Column("following", BigInteger),
    Column("media_count", BigInteger),
    Column("is_private", Boolean, nullable=False, default=False),
    _enum("status", ("new", "scheduled", "active", "paused", "not_found", "private", "error"), default="new"),
    Column("cursor_max_id", Text),
    _enum("cursor_source", ("clips", "feed"), default="clips"),
    Column("cursor_done", Boolean, nullable=False, default=False),
    Column("oldest_pk", Text),
    Column("oldest_taken_at", TS),
    Column("newest_pk", Text),
    Column("newest_taken_at", TS),
    Column("reels_count", Integer, nullable=False, default=0),
    Column("baseline_views", BigInteger),
    Column("last_pull_at", TS),
    Column("last_refresh_at", TS),
    Column("note", Text),
    Column("created_at", TS, nullable=False, default=utcnow),
    UniqueConstraint("user_id", "platform", "handle"),
)

pulls = _t(
    "pulls",
    Column("id", Text, primary_key=True),
    _uid("pulls"),
    Column("account_id", Text, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", Text),
    _enum("mode", ("first", "next", "refresh")),
    Column("from_max_id", Text),
    Column("to_max_id", Text),
    Column("pages", Integer, nullable=False, default=0),
    Column("reels_new", Integer, nullable=False, default=0),
    Column("reels_updated", Integer, nullable=False, default=0),
    Column("collabs_seen", Integer, nullable=False, default=0),
    _enum("recovery", ("feed_oldest_pk", "top_skip_known", "page_size_12"), nullable=True),
    _enum("stop_reason", ("target_reached", "no_more", "known_reel", "page_cap", "rate_limited", "signed_out",
                          "challenge", "error", "canceled"), nullable=True),
    Column("run_after", TS),
    Column("started_at", TS),
    Column("finished_at", TS),
    Index("ix_pulls_user_id", "user_id", "account_id"),
)

# --------------------------------------------------------------------------------------------- 3.11 reels

reels = _t(
    "reels",
    Column("id", Text, primary_key=True),
    _uid("reels"),
    # No FK: DELETE /api/accounts/<id> may keep reels (4.8); WP4 decides what happens to them.
    Column("account_id", Text, nullable=False),
    Column("ig_pk", Text, nullable=False),
    Column("code", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("is_collab", Boolean, nullable=False, default=False),
    Column("taken_at", TS),
    Column("caption", Text),
    Column("duration", Float),
    Column("has_audio", Boolean),
    Column("is_pinned", Boolean, nullable=False, default=False),
    Column("is_paid_partnership", Boolean, nullable=False, default=False),
    Column("coauthors", JSONType, nullable=False, default=list),
    Column("audio", JSONType, nullable=False, default=dict),
    Column("views", BigInteger),
    Column("likes", BigInteger),
    Column("comments", BigInteger),
    _enum("views_field", ("play_count", "ig_play_count", "view_count"), nullable=True),
    Column("shares", BigInteger),
    Column("reposts", BigInteger),
    Column("counts_hidden", Boolean, nullable=False, default=False),
    Column("followers_at_pull", BigInteger),
    Column("video_url", Text),
    Column("cover_url", Text),
    Column("video_url_expires_at", TS),
    _enum("media_state", ("none", "queued", "fetching", "have", "need_extension", "failed", "deleted"), default="none"),
    Column("video_path", Text),
    Column("cover_path", Text),
    Column("first_seen_at", TS, nullable=False, default=utcnow),
    Column("last_seen_at", TS, nullable=False, default=utcnow),
    # scoring (WP4 writes)
    Column("age_days", Float),
    Column("adj_views", Float),
    Column("baseline", Float),
    Column("baseline_n", Integer),
    Column("score", Float),
    Column("log2_score", Float),
    Column("percentile", Float),
    _enum("band", ("below", "normal", "above", "outlier", "big", "breakout", "growing", "unscored"), nullable=True),
    _enum("confidence", ("high", "medium", "low"), nullable=True),
    _enum("basis", ("views", "likes"), nullable=True),
    Column("is_top10", Boolean, nullable=False, default=False),
    Column("rel_er", Float),
    Column("rel_comment_ratio", Float),
    Column("badges", JSONType, nullable=False, default=list),
    Column("scored_at", TS),
    # pipeline states
    _enum("tier", ("priority", "sample", "rest", "none"), default="none"),
    Column("sample_weight", Float, nullable=False, default=0.0),
    _enum("signals_state", ("none", "queued", "done", "failed", "skipped"), default="none"),
    _enum("tag_state", ("none", "queued", "leased", "done", "failed"), default="none"),
    Column("tag_batch", Text),
    _enum("tag_quality", ("vision", "measured", "member"), nullable=True),
    Column("needs_topic_retag", Boolean, nullable=False, default=False),
    _enum("deep_state", ("none", "queued", "waiting", "done", "failed"), default="none"),
    Column("starred", Boolean, nullable=False, default=False),
    UniqueConstraint("user_id", "ig_pk"),
    Index("ix_reels_user_id_account", "user_id", "account_id"),
    Index("ix_reels_user_id_tag", "user_id", "tag_state"),
    Index("ix_reels_user_id_score", "user_id", "score"),
)

reel_accounts = _t(
    "reel_accounts",
    Column("reel_id", Text, ForeignKey("reels.id", ondelete="CASCADE"), nullable=False),
    Column("account_id", Text, ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
    _uid("reel_accounts"),
    _enum("role", ("owner", "coauthor"), default="owner"),
    Column("first_seen_at", TS, nullable=False, default=utcnow),
    PrimaryKeyConstraint("reel_id", "account_id"),
    Index("ix_reel_accounts_user_id", "user_id", "account_id"),
)

# --------------------------------------------------------------------------------------------- 3.12

reel_snapshots = _t(
    "reel_snapshots",
    Column("reel_id", Text, ForeignKey("reels.id", ondelete="CASCADE"), nullable=False),
    Column("seen_at", TS, nullable=False),
    _uid("reel_snapshots"),
    Column("views", BigInteger),
    Column("likes", BigInteger),
    Column("comments", BigInteger),
    Column("shares", BigInteger),
    PrimaryKeyConstraint("reel_id", "seen_at"),
    Index("ix_reel_snapshots_user_id", "user_id"),
)

reel_signals = _t(
    "reel_signals",
    Column("reel_id", Text, ForeignKey("reels.id", ondelete="CASCADE"), primary_key=True),
    _uid("reel_signals"),
    Column("version", Integer, nullable=False, default=1),
    Column("duration", Float),
    Column("width", Integer),
    Column("height", Integer),
    Column("fps", Float),
    Column("transcript", JSONType),
    Column("cuts", JSONType),
    Column("frames", JSONType),
    Column("contact_sheet_path", Text),
    Column("sound", JSONType),
    Column("edit", JSONType),
    Column("visual", JSONType),
    Column("created_at", TS, nullable=False, default=utcnow),
    Column("error", Text),
    Index("ix_reel_signals_user_id", "user_id"),
)

analyses = _t(
    "analyses",
    Column("id", Text, primary_key=True),
    Column("reel_id", Text, ForeignKey("reels.id", ondelete="CASCADE"), nullable=False),
    _uid("analyses"),
    _enum("level", ("quick", "deep")),
    Column("schema_version", Integer, nullable=False, default=1),
    Column("data", JSONType, nullable=False, default=dict),
    Column("visual_format", Text),
    Column("visual_format_2", JSONType),
    Column("sound_primary", Text),
    Column("sound_secondary", JSONType),
    Column("topics", JSONType),
    Column("hook_type", Text),
    Column("cta_type", Text),
    Column("caption_style", Text),
    Column("has_text_hook", Boolean),
    Column("cut_pace", Text),
    Column("length_bucket", Text),
    Column("headline_box", Boolean),
    Column("face_bubble", Boolean),
    Column("broll_share", Text),
    Column("setting", Text),
    Column("framing", Text),
    Column("recipe_key", Text),
    Column("format_id", Text),
    Column("tag_quality", Text),
    Column("overrides", JSONType, nullable=False, default=dict),
    Column("made_by", JSONType, nullable=False, default=dict),
    Column("tokens_in", Integer),
    Column("tokens_out", Integer),
    Column("created_at", TS, nullable=False, default=utcnow),
    Column("updated_at", TS, nullable=False, default=utcnow),
    UniqueConstraint("reel_id", "level"),
    Index("ix_analyses_user_id", "user_id", "level"),
)

# --------------------------------------------------------------------------------------------- 3.13 jobs

JOB_KINDS = ("ig_pull", "ig_profile", "ig_videos", "ig_discover", "ig_check", "page_read", "fetch_video",
             "process_reel", "score_account", "pick_sample", "refresh_insights", "tag_reels", "retag_topics",
             "analyze_reel", "name_formats", "make_output", "import_source", "draft_profile", "suggest_topics",
             "sweep_storage", "backup", "expire_members", "rotate_code")
JOB_STATES = ("queued", "running", "waiting", "paused", "done", "failed", "canceled")
ACTIVE_JOB_STATES = ("queued", "running", "waiting", "paused")

jobs = _t(
    "jobs",
    Column("id", Text, primary_key=True),
    # Nullable only for system jobs (sweep_storage, backup, expire_members, rotate_code).
    _uid("jobs", nullable=True),
    _enum("kind", JOB_KINDS),
    _enum("executor", ("server", "extension", "mcp", "desktop"), default="server"),
    _enum("state", JOB_STATES, default="queued"),
    Column("priority", Integer, nullable=False, default=50),
    Column("payload", JSONType, nullable=False, default=dict),
    Column("progress", JSONType, nullable=False, default=lambda: {"done": 0, "total": 0, "message": ""}),
    Column("result", JSONType),
    Column("error_code", Text),
    Column("error_message", Text),
    Column("attempts", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False, default=3),
    _enum("job_class", ("media", "ai", "light", "external")),
    Column("lease_owner", Text),
    Column("lease_until", TS),
    Column("run_after", TS),
    Column("parent_id", Text),
    Column("dedupe_key", Text, unique=True),
    Column("dedupe_done", Text),
    Column("created_at", TS, nullable=False, default=utcnow),
    Column("started_at", TS),
    Column("finished_at", TS),
    Index("ix_jobs_user_id_state", "user_id", "state"),
    Index("ix_jobs_lease", "state", "job_class", "executor"),
    Index("ix_jobs_lease_until", "lease_until"),
)

job_turns = _t(
    "job_turns",
    _uid("job_turns"),
    Column("job_class", Text, nullable=False),
    Column("last_served_at", TS, nullable=False),
    PrimaryKeyConstraint("user_id", "job_class"),
)

member_budget = _t(
    "member_budget",
    _uid("member_budget"),
    Column("day", Text, nullable=False),
    Column("media_left", Integer, nullable=False, default=0),
    Column("media_used", Integer, nullable=False, default=0),
    PrimaryKeyConstraint("user_id", "day"),
)

# --------------------------------------------------------------------------------------------- 3.14

outputs = _t(
    "outputs",
    Column("id", Text, primary_key=True),
    _uid("outputs"),
    Column("brand_id", Text, nullable=False, default="me"),
    _enum("kind", ("reel_script", "meta_ads", "content_plan", "competitor_report")),
    _enum("purpose", ("remake", "format", "hook", "learn"), nullable=True),
    Column("title", Text, nullable=False, default=""),
    _enum("status", ("queued", "waiting", "working", "done", "failed"), default="queued"),
    Column("options", JSONType, nullable=False, default=dict),
    Column("inputs", JSONType, nullable=False, default=dict),
    Column("data", JSONType),
    Column("markdown", Text),
    Column("schema_version", Integer, nullable=False, default=1),
    Column("made_by", JSONType, nullable=False, default=dict),
    Column("starred", Boolean, nullable=False, default=False),
    Column("job_id", Text),
    Column("created_at", TS, nullable=False, default=utcnow),
    Column("updated_at", TS, nullable=False, default=utcnow),
    Index("ix_outputs_user_id", "user_id", "created_at"),
)

insight_snapshots = _t(
    "insight_snapshots",
    Column("id", Text, primary_key=True),
    _uid("insight_snapshots"),
    Column("computed_at", TS, nullable=False, default=utcnow),
    Column("reel_count", Integer, nullable=False, default=0),
    Column("account_count", Integer, nullable=False, default=0),
    Column("data", JSONType, nullable=False, default=dict),
    Index("ix_insight_snapshots_user_id", "user_id", "computed_at"),
)

# --------------------------------------------------------------------------------------------- 3.15 OAuth

oauth_clients = _t(
    "oauth_clients",
    Column("client_id", Text, primary_key=True),
    _enum("kind", ("cimd", "dcr")),
    Column("metadata", JSONType, nullable=False, default=dict),
    Column("redirect_uris", JSONType, nullable=False, default=list),
    Column("created_at", TS, nullable=False, default=utcnow),
    Column("last_used_at", TS),
)

oauth_codes = _t(
    "oauth_codes",
    Column("code_hash", Text, primary_key=True),
    Column("client_id", Text, nullable=False),
    _uid("oauth_codes"),
    Column("redirect_uri", Text, nullable=False),
    Column("code_challenge", Text, nullable=False),
    Column("scope", Text),
    Column("resource", Text),
    Column("expires_at", TS, nullable=False),
    Column("used_at", TS),
    Index("ix_oauth_codes_user_id", "user_id"),
)

oauth_tokens = _t(
    "oauth_tokens",
    Column("token_hash", Text, primary_key=True),
    _enum("kind", ("access", "refresh")),
    Column("client_id", Text, nullable=False),
    _uid("oauth_tokens"),
    Column("scope", Text),
    Column("resource", Text),
    Column("family_id", Text, nullable=False),
    Column("expires_at", TS, nullable=False),
    Column("revoked_at", TS),
    Column("replaced_by", Text),
    Column("created_at", TS, nullable=False, default=utcnow),
    Index("ix_oauth_tokens_user_id", "user_id"),
    Index("ix_oauth_tokens_family_id", "family_id"),
)

# --------------------------------------------------------------------------------------------- 3.16

audit_log = _t(
    "audit_log",
    Column("id", Text, primary_key=True),
    _uid("audit_log", nullable=True, ondelete="SET NULL"),
    Column("actor", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("detail", JSONType, nullable=False, default=dict),
    Column("ip", Text),
    Column("at", TS, nullable=False, default=utcnow),
    Index("ix_audit_log_user_id", "user_id", "at"),
    Index("ix_audit_log_action", "action", "at"),
)

usage_counters = _t(
    "usage_counters",
    Column("scope", Text, nullable=False),
    Column("key", Text, nullable=False),
    Column("window", Text, nullable=False),
    Column("count", Integer, nullable=False, default=0),
    Column("updated_at", TS, nullable=False, default=utcnow),
    PrimaryKeyConstraint("scope", "key", "window"),
)

discover_runs = _t(
    "discover_runs",
    Column("id", Text, primary_key=True),
    _uid("discover_runs"),
    _enum("mode", ("niche", "similar", "search")),
    Column("queries", JSONType, nullable=False, default=list),
    Column("seeds", JSONType, nullable=False, default=list),
    Column("state", Text, nullable=False, default="queued"),
    Column("created_at", TS, nullable=False, default=utcnow),
    Index("ix_discover_runs_user_id", "user_id"),
)

discover_candidates = _t(
    "discover_candidates",
    Column("id", Text, primary_key=True),
    _uid("discover_candidates"),
    Column("run_id", Text, ForeignKey("discover_runs.id", ondelete="CASCADE"), nullable=False),
    Column("handle", Text, nullable=False),
    Column("pk", Text),
    Column("full_name", Text),
    Column("followers", BigInteger),
    Column("is_private", Boolean, nullable=False, default=False),
    Column("is_verified", Boolean, nullable=False, default=False),
    _enum("source", ("search", "similar")),
    Column("seed_handle", Text),
    Column("median_views_12", BigInteger),
    Column("posts_per_week", Float),
    Column("last_post_at", TS),
    Column("rank_score", Float),
    Column("checked_at", TS),
    _enum("status", ("found", "checked", "tracked", "dismissed"), default="found"),
    Index("ix_discover_candidates_user_id", "user_id", "run_id"),
)

admin_access = _t(
    "admin_access",
    _uid("admin_access"),
    Column("granted_at", TS, nullable=False, default=utcnow),
    Column("expires_at", TS, nullable=False),
    Column("revoked_at", TS),
    PrimaryKeyConstraint("user_id", "granted_at"),
)

TABLES: dict[str, Table] = dict(metadata.tables)

# --------------------------------------------------------------------------------------------- engine

_engine: Engine | None = None
_url: str | None = None


def _sqlite_hooks(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.isolation_level = None  # we emit BEGIN ourselves (below)
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):  # noqa: ANN001
        if conn.get_execution_options().get("rv_deferred"):
            conn.exec_driver_sql("BEGIN")
        else:
            conn.exec_driver_sql("BEGIN IMMEDIATE")


def init(database_url: str | None = None) -> Engine:
    """Create (or replace) the process engine. Called lazily on first use; tests call it per temp DATA_DIR."""
    global _engine, _url
    from .settings import get_settings

    url = database_url or get_settings().database_url
    if _engine is not None:
        _engine.dispose()
    if url.startswith("sqlite"):
        import sqlite3

        if tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2]) < (3, 35):
            raise SystemExit(f"SQLite {sqlite3.sqlite_version} is too old. ReelVault needs 3.35 or newer.")
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})
        _sqlite_hooks(eng)
    else:
        eng = create_engine(url, pool_pre_ping=True)
    _engine, _url = eng, url
    return eng


def get_engine() -> Engine:
    return _engine if _engine is not None else init()


def __getattr__(name: str):  # PEP 562: `db.engine` is always the current engine
    if name == "engine":
        return get_engine()
    raise AttributeError(name)


def is_sqlite() -> bool:
    return get_engine().dialect.name == "sqlite"


@contextmanager
def tx(immediate: bool = True) -> Iterator[Connection]:
    """One transaction. immediate=True (default) = a write tx (BEGIN IMMEDIATE on SQLite); False = deferred/read."""
    eng = get_engine()
    with eng.connect() as conn:
        if not immediate:
            conn.execution_options(rv_deferred=True)
        with conn.begin():
            yield conn


def read() -> Any:
    """Shorthand for tx(immediate=False)."""
    return tx(immediate=False)


# --------------------------------------------------------------------------------------------- migrations

def migrations_dir() -> Path | None:
    cand = [os.environ.get("RV_MIGRATIONS_DIR"), Path(__file__).resolve().parents[2] / "migrations",
            Path.cwd() / "migrations"]
    for c in cand:
        if c and (Path(c) / "env.py").exists():
            return Path(c)
    return None


def migrate(engine: Engine | None = None) -> None:
    """Bring the schema to head (Alembic). Falls back to create_all when the migrations folder is not shipped."""
    eng = engine or get_engine()
    mdir = migrations_dir()
    if mdir is None:
        import logging

        logging.getLogger("reelvault.db").warning(
            "migrations folder not found (set RV_MIGRATIONS_DIR); creating tables without Alembic")
        metadata.create_all(eng)
        return
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", str(mdir))
    cfg.set_main_option("sqlalchemy.url", str(eng.url).replace("%", "%%"))
    with eng.connect() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        if conn.in_transaction():
            conn.commit()


# --------------------------------------------------------------------------------------------- helpers

def table_of(t: Table | str) -> Table:
    return TABLES[t] if isinstance(t, str) else t


def scoped(conn: Connection | None, table: Table | str, user_id: str) -> Select:
    """select(table).where(table.c.user_id == user_id). The ONLY way to read member-owned rows (11.5)."""
    t = table_of(table)
    if not user_id:
        raise ValueError("scoped() needs a user_id")
    return select(t).where(t.c.user_id == user_id)


def get_owned(conn: Connection, table: Table | str, user_id: str, row_id: str, *, id_col: str = "id"):
    """One member-owned row by id, or None (callers turn None into 404, also for another member's ids)."""
    t = table_of(table)
    return conn.execute(scoped(conn, t, user_id).where(t.c[id_col] == row_id)).first()


def update_owned(conn: Connection, table: Table | str, user_id: str, row_id: str, values: dict, *,
                 id_col: str = "id") -> int:
    t = table_of(table)
    return conn.execute(t.update().where(t.c.user_id == user_id, t.c[id_col] == row_id).values(**values)).rowcount


def delete_owned(conn: Connection, table: Table | str, user_id: str, row_id: str, *, id_col: str = "id") -> int:
    t = table_of(table)
    return conn.execute(t.delete().where(t.c.user_id == user_id, t.c[id_col] == row_id)).rowcount


def _insert_for(eng_or_conn) -> Any:
    name = eng_or_conn.dialect.name
    if name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as ins
    elif name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as ins
    else:  # pragma: no cover
        raise RuntimeError(f"bump() does not support {name}")
    return ins


def upsert(conn: Connection, table: Table | str, values: dict, *, update: dict | None = None,
           do_nothing: bool = False):
    """INSERT ... ON CONFLICT (pk) DO UPDATE SET <update> (or DO NOTHING). Returns the CursorResult."""
    t = table_of(table)
    ins = _insert_for(conn)
    stmt = ins(t).values(**values)
    pk = [c.name for c in t.primary_key.columns]
    if do_nothing or not update:
        stmt = stmt.on_conflict_do_nothing(index_elements=pk)
    else:
        stmt = stmt.on_conflict_do_update(index_elements=pk, set_=update)
    return conn.execute(stmt)


def bump(table: Table | str, key: dict, *, conn: Connection | None = None, _set: dict | None = None,
         **deltas: int) -> dict:
    """Atomic counter upsert: INSERT key+deltas ON CONFLICT (pk) DO UPDATE SET x = x + :dx (3.7, 3.16).

    `key` holds the primary-key columns; `_set` holds plain columns written on insert and update (e.g. user_id,
    updated_at). Returns the row's new counter values, e.g. {"count": 3}. Never read-modify-write a counter.
    """
    t = table_of(table)
    extra = dict(_set or {})
    if "updated_at" in t.c and "updated_at" not in extra:
        extra["updated_at"] = utcnow()

    def run(c: Connection) -> dict:
        ins = _insert_for(c)
        stmt = ins(t).values(**key, **extra, **deltas)
        upd = {k: t.c[k] + v for k, v in deltas.items()}
        upd.update(extra)
        stmt = stmt.on_conflict_do_update(index_elements=[col.name for col in t.primary_key.columns], set_=upd)
        stmt = stmt.returning(*[t.c[k] for k in deltas])
        row = c.execute(stmt).first()
        return dict(row._mapping) if row is not None else {}

    if conn is not None:
        return run(conn)
    with tx() as c:
        return run(c)


# --------------------------------------------------------------------------------------------- app settings

def get_setting(key: str, *, conn: Connection | None = None) -> Any:
    """app_settings value, or the 3.3 default when unset."""
    def run(c: Connection) -> Any:
        row = c.execute(select(app_settings.c.value).where(app_settings.c.key == key)).first()
        if row is None:
            d = APP_SETTINGS_DEFAULTS.get(key)
            return dict(d) if isinstance(d, dict) else d
        return row[0]

    if conn is not None:
        return run(conn)
    with tx(immediate=False) as c:
        return run(c)


def set_setting(key: str, value: Any, *, conn: Connection | None = None) -> None:
    def run(c: Connection) -> None:
        upsert(c, app_settings, {"key": key, "value": value}, update={"value": value})

    if conn is not None:
        run(conn)
    else:
        with tx() as c:
            run(c)


def all_settings(*, conn: Connection | None = None) -> dict:
    """Every PUBLIC app setting (defaults merged). Keys starting with 'internal.' are hidden."""
    def run(c: Connection) -> dict:
        out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in APP_SETTINGS_DEFAULTS.items()}
        for k, v in c.execute(select(app_settings.c.key, app_settings.c.value)).all():
            if not k.startswith("internal."):
                out[k] = v
        return out

    if conn is not None:
        return run(conn)
    with tx(immediate=False) as c:
        return run(c)


def audit(action: str, *, user_id: str | None = None, actor: str = "system", detail: dict | None = None,
          ip: str | None = None, conn: Connection | None = None) -> None:
    """Write one audit_log row. `detail` must never hold secrets."""
    vals = dict(id=new_id("aud"), user_id=user_id, actor=actor, action=action, detail=detail or {}, ip=ip,
                at=utcnow())
    if conn is not None:
        conn.execute(audit_log.insert().values(**vals))
    else:
        with tx() as c:
            c.execute(audit_log.insert().values(**vals))


# --------------------------------------------------------------------------------------------- local fs check

NETWORK_FS = ("nfs", "nfs4", "cifs", "smb3", "smbfs", "9p", "fuse", "ceph", "glusterfs", "afs", "lustre", "davfs")
NETWORK_FS_MESSAGE = ("Your data folder is on a network drive. SQLite needs a local disk. "
                      "See DEPLOY.md, 'Host must have'.")


def fs_type(path: str | Path) -> str | None:
    """Filesystem type of the mount holding `path` (Linux /proc/self/mountinfo); None when unknown."""
    try:
        target = os.path.realpath(str(path))
        best, best_type = "", None
        with open("/proc/self/mountinfo", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if "-" not in parts:
                    continue
                sep = parts.index("-")
                mnt = parts[4].replace("\\040", " ")
                ftype = parts[sep + 1]
                if (target == mnt or target.startswith(mnt.rstrip("/") + "/") or mnt == "/") and len(mnt) >= len(best):
                    best, best_type = mnt, ftype
        return best_type
    except OSError:
        return None


def check_local_fs(path: str | Path) -> None:
    """Refuse to run SQLite on NFS/SMB/CIFS/FUSE/9p (2.2). Raises SystemExit with the plain message."""
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    ftype = (fs_type(p) or "").lower()
    if ftype and (ftype in NETWORK_FS or ftype.startswith("fuse") or ftype.startswith("nfs")):
        raise SystemExit(NETWORK_FS_MESSAGE)


# --------------------------------------------------------------------------------------------- CLI

def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print("usage: python -m reelvault.server.db migrate | copy-from-sqlite <sqlite_path>")
        return 0
    if argv[0] == "migrate":
        migrate()
        print("Database is up to date.")
        return 0
    if argv[0] == "copy-from-sqlite":
        if len(argv) < 2:
            print("Give the path of the SQLite file to copy from.")
            return 2
        src = create_engine(f"sqlite:///{argv[1]}")
        dst = get_engine()
        migrate(dst)
        with src.connect() as s, dst.begin() as d:
            for t in metadata.sorted_tables:
                rows = [dict(r._mapping) for r in s.execute(select(t))]
                if rows:
                    d.execute(t.insert(), rows)
                print(f"{t.name}: {len(rows)} rows")
        return 0
    print(f"Unknown command: {argv[0]}")
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
