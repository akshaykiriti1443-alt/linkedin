"""Rate limits and quotas (DESIGN-SAAS.md 11.6). WP1.

    ratelimit.hit("ip", ip, 20, "quarter")           # counts one hit; raises TooMany (HTTP 429) over the limit
    ratelimit.check("email", email, 5, "quarter")    # raises if already at the limit, counts nothing
    ratelimit.count("email", email, "quarter")       # current count

Windows: "minute" (in memory, per process), "quarter" (15 min), "hour", "day" (stored with the atomic
db.bump upsert in usage_counters; never read-modify-write).

Keys are about the CLIENT, not only its IP address (11.1, 11.6):
    ratelimit.client_key(request)            # "ip:203.0.113.7" or "ip6:2001:db8:1:2::/64" (one IPv6 home = one key)
    ratelimit.key("oauth_reg", ip, redirect_uri, client_name)   # composite key, hashed when long
    ratelimit.ip_limit(30, ip)               # x10 for Anthropic's cloud range (160.79.104.0/21), else the limit
    ratelimit.hit_all(request, [("login_email", email, 5), ("login_ip", None, 20)], "quarter")
A None key in hit_all/check_all means "this request's client_key". Per-member and per-device limits key on the
account or device id, global limits on a fixed key, so many members behind one office or phone network (one IP)
are not locked out together, and one client rotating IPs still hits the per-account and global limits.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Literal

import hashlib
import ipaddress
from typing import Iterable

from sqlalchemy import select

from . import db
from .errors import ApiError

Window = Literal["minute", "quarter", "hour", "day"]


class TooMany(ApiError):
    def __init__(self, message: str, retry_after: int, code: str = "too_many"):
        super().__init__(429, code, message, headers={"Retry-After": str(max(1, int(retry_after)))})
        self.retry_after = retry_after


def window_key(window: Window, now: datetime | None = None) -> str:
    now = now or db.utcnow()
    if window == "day":
        return now.strftime("%Y-%m-%d")
    if window == "hour":
        return now.strftime("%Y-%m-%dT%H")
    if window == "quarter":
        return now.strftime("%Y-%m-%dT%H:") + f"{(now.minute // 15) * 15:02d}"
    if window == "minute":
        return now.strftime("%Y-%m-%dT%H:%M")
    raise ValueError(window)


def seconds_left(window: Window, now: datetime | None = None) -> int:
    now = now or db.utcnow()
    if window == "day":
        end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif window == "hour":
        end = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    elif window == "quarter":
        end = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0) + timedelta(minutes=15)
    else:
        end = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return max(1, int((end - now).total_seconds()))


_mem: dict[tuple[str, str, str], int] = defaultdict(int)
_mem_lock = threading.Lock()
_mem_gc = [time.monotonic()]


def _mem_hit(scope: str, key: str, n: int) -> int:
    wk = window_key("minute")
    with _mem_lock:
        if time.monotonic() - _mem_gc[0] > 120:
            for k in [k for k in _mem if k[2] != wk]:
                del _mem[k]
            _mem_gc[0] = time.monotonic()
        _mem[(scope, key, wk)] += n
        return _mem[(scope, key, wk)]


def reset_memory() -> None:
    with _mem_lock:
        _mem.clear()


def _default_message(window: Window) -> str:
    return {"minute": "Too many tries. Wait a minute and try again.",
            "quarter": "Too many tries. Try again in 15 minutes.",
            "hour": "Too many tries. Try again in an hour.",
            "day": "You reached today's limit. Try again tomorrow."}[window]


def count(scope: str, key: str, window: Window) -> int:
    if window == "minute":
        with _mem_lock:
            return _mem.get((scope, key, window_key("minute")), 0)
    t = db.usage_counters
    with db.tx(immediate=False) as conn:
        row = conn.execute(select(t.c.count).where(t.c.scope == scope, t.c.key == key,
                                                   t.c.window == window_key(window))).first()
    return int(row[0]) if row else 0


def check(scope: str, key: str, limit: int, window: Window, *, message: str | None = None,
          code: str = "too_many") -> None:
    """Raise TooMany when the counter is already at/over the limit. Counts nothing."""
    if count(scope, key, window) >= limit:
        raise TooMany(message or _default_message(window), seconds_left(window), code)


def hit(scope: str, key: str, limit: int, window: Window, *, message: str | None = None, n: int = 1,
        code: str = "too_many") -> int:
    """Count one hit and raise TooMany when the new count is over the limit. Returns the new count."""
    if window == "minute":
        c = _mem_hit(scope, key, n)
    else:
        c = int(db.bump(db.usage_counters, {"scope": scope, "key": key, "window": window_key(window)},
                        count=n).get("count", 0))
    if c > limit:
        raise TooMany(message or _default_message(window), seconds_left(window), code)
    return c


# ============================================================================================== client keys

ANTHROPIC_NETS = (ipaddress.ip_network("160.79.104.0/21"),)
_MAX_KEY = 120


def norm_ip(ip: str) -> str:
    """'ip:<v4>' for IPv4 (and IPv4-mapped IPv6); 'ip6:<net>/64' for IPv6, because one home or phone gets a whole
    /64 and can rotate inside it for free. Unparseable values are kept (cut to 64 chars)."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return "raw:" + (ip or "")[:64]
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.version == 4:
        return f"ip:{addr}"
    net = ipaddress.ip_network(f"{addr}/64", strict=False)
    return f"ip6:{net}"


def client_key(request) -> str:
    """The rate-limit key of the client behind a request (honours X-Forwarded-For only from TRUSTED_PROXIES)."""
    from .security import client_ip

    return norm_ip(client_ip(request))


def key(*parts) -> str:
    """Composite key from several parts, e.g. key(ip, redirect_uri, client_name). Long keys are hashed so the
    usage_counters row stays small and no raw URL or email is stored when it would be long."""
    raw = "|".join("" if p is None else str(p) for p in parts)
    if len(raw) <= _MAX_KEY:
        return raw
    return "h:" + hashlib.sha256(raw.encode()).hexdigest()


def in_anthropic_range(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in ANTHROPIC_NETS)


def ip_limit(limit: int, ip: str, *, factor: int = 10) -> int:
    """Per-IP limit, raised `factor` times for Anthropic's cloud range: every member's Claude connector comes from
    there, so one shared IP must not lock the others out (11.6 OAuth register)."""
    return limit * factor if in_anthropic_range(ip) else limit


def check_all(request, limits: Iterable[tuple[str, str | None, int]], window: Window, *,
              message: str | None = None, code: str = "too_many") -> None:
    """check() for several (scope, key, limit) at once; key None = client_key(request). Counts nothing."""
    ck = client_key(request) if request is not None else "none"
    for scope, k, limit in limits:
        check(scope, ck if k is None else k, limit, window, message=message, code=code)


def hit_all(request, limits: Iterable[tuple[str, str | None, int]], window: Window, *,
            message: str | None = None, code: str = "too_many") -> None:
    """hit() for several (scope, key, limit): every counter is counted, then the first one over its limit
    raises. Counting all of them first keeps per-account and global counters exact even when one key is over."""
    ck = client_key(request) if request is not None else "none"
    over: TooMany | None = None
    for scope, k, limit in limits:
        try:
            hit(scope, ck if k is None else k, limit, window, message=message, code=code)
        except TooMany as e:
            over = over or e
    if over is not None:
        raise over
