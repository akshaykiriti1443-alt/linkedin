"""SSRF-safe, DNS-pinned httpx transports (DESIGN-SAAS.md 11.8). Owner: WP1 (WP5 and WP6 import it).

PinnedTransport resolves the host ONCE, refuses private/loopback/link-local/CGNAT/metadata addresses, then connects
to that checked IP while keeping the hostname for SNI and the Host header. Pass the same transport to every
openai_compat SDK client and CIMD fetch so nothing re-resolves DNS after the check (no DNS rebinding):

    client = httpx.Client(transport=PinnedTransport())
    openai.OpenAI(base_url=..., api_key=..., http_client=httpx.Client(transport=PinnedTransport()))
    httpx.AsyncClient(transport=AsyncPinnedTransport())
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Callable

import httpx

BLOCKED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "64:ff9b::/96",
))


class UnsafeAddress(httpx.TransportError):
    """The URL resolves to an address we refuse to call (private, loopback, metadata, ...)."""


def is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not any(addr in net for net in BLOCKED_NETS if addr.version == net.version)


Resolver = Callable[[str, int], list[str]]


def system_resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


def resolve_checked(host: str, port: int, *, allow_private: bool = False, resolver: Resolver | None = None) -> str:
    """Return one IP for host after checking EVERY resolved address; raise UnsafeAddress otherwise."""
    host = host.strip("[]")
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        try:
            ips = (resolver or system_resolve)(host, port)
        except OSError as e:
            raise httpx.ConnectError(f"Could not find {host}.") from e
    if not ips:
        raise httpx.ConnectError(f"Could not find {host}.")
    if not allow_private:
        bad = [ip for ip in ips if not is_public_ip(ip)]
        if bad:
            raise UnsafeAddress(f"{host} points to a private or local address. We can't call it.")
    return ips[0]


def _pin(request: httpx.Request, allow_private: bool, resolver: Resolver | None) -> httpx.Request:
    url = request.url
    host = url.host
    port = url.port or (443 if url.scheme == "https" else 80)
    ip = resolve_checked(host, port, allow_private=allow_private, resolver=resolver)
    if ip == host.strip("[]"):
        return request
    request.url = url.copy_with(host=ip)
    # Host header already carries the original hostname (set when the request was built).
    if "host" not in request.headers:
        request.headers["Host"] = url.netloc.decode("ascii") if isinstance(url.netloc, bytes) else str(url.netloc)
    if url.scheme == "https":
        request.extensions = {**request.extensions, "sni_hostname": host}
    return request


class PinnedTransport(httpx.HTTPTransport):
    """Sync transport: resolve once, refuse private ranges, connect to the checked IP (11.8)."""

    def __init__(self, *, allow_private: bool = False, resolver: Resolver | None = None, **kw):
        kw.setdefault("retries", 0)
        super().__init__(**kw)
        self.allow_private = allow_private
        self.resolver = resolver

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return super().handle_request(_pin(request, self.allow_private, self.resolver))


class AsyncPinnedTransport(httpx.AsyncHTTPTransport):
    """Async twin of PinnedTransport."""

    def __init__(self, *, allow_private: bool = False, resolver: Resolver | None = None, **kw):
        kw.setdefault("retries", 0)
        super().__init__(**kw)
        self.allow_private = allow_private
        self.resolver = resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await super().handle_async_request(_pin(request, self.allow_private, self.resolver))
