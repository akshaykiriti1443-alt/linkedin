"""Environment config for the ReelVault Members server (DESIGN-SAAS.md 12.5). WP1.

Read once per process with get_settings(); tests call reload() after changing the environment.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

VERSION = "1.0.0"
EXTENSION_MIN = "2.0.0"


def _int(name: str, default: int) -> int:
    v = os.environ.get(name, "").strip()
    return int(v) if v else default


def _float(name: str, default: float) -> float:
    v = os.environ.get(name, "").strip()
    return float(v) if v else default


def _flag(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    public_url: str
    data_dir: Path
    database_url: str
    master_keys: tuple[tuple[str, bytes], ...]
    admin_email: str | None
    admin_password: str | None
    smtp_url: str | None
    resend_api_key: str | None
    skool_hook_secret: str | None
    trusted_proxies: tuple = field(default_factory=tuple)
    role: str = "web"
    server_cdn_fetch: bool = True
    whisper_model: str = "small"
    whisper_sample_model: str = "base"
    whisper_concurrency: int = 1
    ai_concurrency: int = 6
    light_concurrency: int = 2
    server_media_per_day: int = 1200
    backup_copy_cmd: str | None = None
    sfx_floor: float = 0.03
    sfx_rel: float = 6.0
    log_level: str = "info"
    models_dir: Path = Path("/opt/reelvault/models")
    test_pacing: bool = False
    test_allow_http_cimd: bool = False
    mail_from: str | None = None

    # ---- derived
    @property
    def public_host(self) -> str:
        """Host (and port when not default) of PUBLIC_URL, e.g. 'app.example.com' or 'localhost:9100'."""
        return urlparse(self.public_url).netloc

    @property
    def public_origin(self) -> str:
        u = urlparse(self.public_url)
        return f"{u.scheme}://{u.netloc}"

    @property
    def is_https(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def cookie_secure(self) -> bool:
        # 11.2: Secure is dropped only for http://localhost (and 127.0.0.1) dev/test URLs.
        return not (self.public_url.startswith("http://localhost") or self.public_url.startswith("http://127.0.0.1"))

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def problems(self) -> list[str]:
        """Plain-words reasons the server must not start. Empty = fine."""
        out = []
        if not self.public_url:
            out.append("PUBLIC_URL is not set. Set it to your https address, e.g. https://app.example.com.")
        if not self.master_keys:
            out.append("RV_MASTER_KEYS is not set. Make one with: python -m reelvault.server.security genkey")
        return out


def _parse_keys(raw: str) -> tuple[tuple[str, bytes], ...]:
    import base64

    keys = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        if ":" not in part:
            raise SystemExit("RV_MASTER_KEYS must look like kid:base64key (comma separated).")
        kid, b64 = part.split(":", 1)
        try:
            key = base64.b64decode(b64 + "=" * (-len(b64) % 4))
        except Exception:  # noqa: BLE001
            raise SystemExit(f"RV_MASTER_KEYS entry '{kid}' is not valid base64.") from None
        if len(key) != 32:
            raise SystemExit(f"RV_MASTER_KEYS entry '{kid}' must be 32 bytes.")
        keys.append((kid.strip(), key))
    return tuple(keys)


def _parse_proxies(raw: str) -> tuple:
    nets = []
    for part in [p.strip() for p in raw.split(",") if p.strip()]:
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            raise SystemExit(f"TRUSTED_PROXIES has a bad entry: {part}") from None
    return tuple(nets)


def _load() -> Settings:
    cpus = os.cpu_count() or 2
    data_dir = Path(os.environ.get("DATA_DIR", "/data")).expanduser()
    db_url = os.environ.get("DATABASE_URL", "").strip() or f"sqlite:///{data_dir / 'db' / 'reelvault.sqlite'}"
    return Settings(
        public_url=os.environ.get("PUBLIC_URL", "").strip().rstrip("/"),
        data_dir=data_dir,
        database_url=db_url,
        master_keys=_parse_keys(os.environ.get("RV_MASTER_KEYS", "")),
        admin_email=(os.environ.get("ADMIN_EMAIL") or "").strip().lower() or None,
        admin_password=os.environ.get("ADMIN_PASSWORD") or None,
        smtp_url=os.environ.get("SMTP_URL") or None,
        resend_api_key=os.environ.get("RESEND_API_KEY") or None,
        skool_hook_secret=os.environ.get("SKOOL_HOOK_SECRET") or None,
        trusted_proxies=_parse_proxies(os.environ.get("TRUSTED_PROXIES", "127.0.0.1,172.16.0.0/12")),
        role=os.environ.get("RV_ROLE", "web").strip() or "web",
        server_cdn_fetch=_flag("SERVER_CDN_FETCH", True),
        whisper_model=os.environ.get("WHISPER_MODEL", "small"),
        whisper_sample_model=os.environ.get("WHISPER_SAMPLE_MODEL", "base"),
        whisper_concurrency=_int("WHISPER_CONCURRENCY", max(1, cpus // 2)),
        ai_concurrency=_int("AI_CONCURRENCY", 6),
        light_concurrency=_int("LIGHT_CONCURRENCY", 2),
        server_media_per_day=_int("SERVER_MEDIA_PER_DAY", cpus * 1200),
        backup_copy_cmd=os.environ.get("BACKUP_COPY_CMD") or None,
        sfx_floor=_float("SFX_FLOOR", 0.03),
        sfx_rel=_float("SFX_REL", 6.0),
        log_level=os.environ.get("LOG_LEVEL", "info").lower(),
        models_dir=Path(os.environ.get("RV_MODELS_DIR", "/opt/reelvault/models")),
        test_pacing=_flag("RV_TEST_PACING"),
        test_allow_http_cimd=_flag("RV_TEST_ALLOW_HTTP_CIMD"),
        mail_from=(os.environ.get("MAIL_FROM") or "").strip() or None,
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = _load()
    return _settings


def reload() -> Settings:
    """Re-read the environment (tests, dev entry)."""
    global _settings
    _settings = _load()
    return _settings
