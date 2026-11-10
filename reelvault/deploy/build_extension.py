#!/usr/bin/env python3
"""Pack the ReelVault Members Chrome extension (DESIGN-SAAS.md 5, 5.2, 12.5 step 4). WP1 with WP2.

    python deploy/build_extension.py --server https://app.example.com                 # Chrome Web Store ZIP
    python deploy/build_extension.py --server https://app.example.com --build zip      # ZIP for "Load unpacked"
    python deploy/build_extension.py --server https://app.example.com --server http://localhost:8000 --build zip

The source folder is `extension-members/` next to `deploy/` (or $RV_EXTENSION_DIR, or --src). WP2 owns every file
in it. This script only:
  * copies the files (no dotfiles, tests, source maps, node_modules, *.zip),
  * writes `config.js` with the allowed servers:  export const SERVERS = [...]; export const BUILD = "store"|"zip";
    (other lines of WP2's config.js template are kept as they are),
  * refuses manifests that break the hard rules of 5.1 (no `cookies`, no `tabs`, no `<all_urls>`, no server hosts),
  * writes a byte-for-byte reproducible ZIP (fixed file times, sorted names).

The server's download route (`/download/reelvault-members-extension.zip`) uses build_zip() from this file at request
time with PUBLIC_URL, so the ZIP and the store build never drift apart. Standard library only.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
FIXED_TIME = (2026, 1, 1, 0, 0, 0)
SKIP_DIRS = {"node_modules", "tests", "test", "__pycache__", ".git", "dist"}
SKIP_SUFFIXES = (".map", ".zip", ".pyc", ".log")
FORBIDDEN_PERMISSIONS = {"cookies", "tabs", "webRequest", "webRequestBlocking", "history", "debugger"}
FORBIDDEN_HOSTS = {"<all_urls>", "*://*/*", "http://*/*", "https://*/*"}
SERVERS_RE = re.compile(r"^\s*export\s+const\s+SERVERS\s*=.*?;\s*$", re.M | re.S)
BUILD_RE = re.compile(r"^\s*export\s+const\s+BUILD\s*=.*?;\s*$", re.M)


class BuildError(Exception):
    """Plain-words reason the extension could not be built."""


def find_source(src: str | Path | None = None) -> Path:
    for cand in (src, os.environ.get("RV_EXTENSION_DIR"), HERE.parent / "extension-members",
                 "/opt/reelvault/extension-members"):
        if cand and (Path(cand) / "manifest.json").is_file():
            return Path(cand).resolve()
    raise BuildError("The extension folder (extension-members/manifest.json) was not found.")


def norm_server(url: str, *, build: str) -> str:
    """'https://app.example.com/' -> 'https://app.example.com'. Only bare origins; http only for localhost dev."""
    u = urlparse((url or "").strip())
    if u.scheme not in ("https", "http") or not u.hostname:
        raise BuildError(f"Server must look like https://app.example.com (got {url!r}).")
    if u.path not in ("", "/") or u.query or u.fragment or u.username or u.password:
        raise BuildError(f"Server must be just the address, with no path (got {url!r}).")
    local = u.hostname in ("localhost", "127.0.0.1")
    if u.scheme == "http" and not local:
        raise BuildError(f"Server must use https:// (got {url!r}).")
    if build == "store" and local:
        raise BuildError("A Chrome Web Store build can't include a localhost server.")
    host = u.hostname.lower()
    default = (u.scheme == "https" and u.port in (None, 443)) or (u.scheme == "http" and u.port in (None, 80))
    return f"{u.scheme}://{host}" + ("" if default else f":{u.port}")


def check_manifest(manifest: dict) -> None:
    perms = set(manifest.get("permissions") or [])
    bad = sorted(perms & FORBIDDEN_PERMISSIONS)
    if bad:
        raise BuildError(f"manifest.json asks for permissions the design forbids: {', '.join(bad)}.")
    hosts = list(manifest.get("host_permissions") or []) + list(manifest.get("optional_host_permissions") or [])
    broad = sorted(set(hosts) & FORBIDDEN_HOSTS)
    if broad:
        raise BuildError(f"manifest.json asks for every website: {', '.join(broad)}.")
    if manifest.get("manifest_version") != 3:
        raise BuildError("manifest.json must be Manifest V3.")
    if not re.fullmatch(r"\d+(\.\d+){0,3}", str(manifest.get("version", ""))):
        raise BuildError("manifest.json has no valid version.")


def config_js(servers: list[str], build: str, template: str | None = None) -> str:
    servers_line = f"export const SERVERS = {json.dumps(servers)};"
    build_line = f"export const BUILD = {json.dumps(build)};"
    if not template:
        return f"// Written by deploy/build_extension.py. Do not edit.\n{servers_line}\n{build_line}\n"
    out = template
    out = SERVERS_RE.sub(servers_line, out, count=1) if SERVERS_RE.search(out) else out.rstrip() + "\n" + servers_line
    out = BUILD_RE.sub(build_line, out, count=1) if BUILD_RE.search(out) else out.rstrip() + "\n" + build_line
    return out.rstrip() + "\n"


def iter_files(src: Path):
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or name.endswith(SKIP_SUFFIXES):
                continue
            p = Path(root) / name
            if p.is_symlink():
                continue
            yield p, p.relative_to(src).as_posix()


def build_zip(servers: list[str], *, build: str = "zip", src: str | Path | None = None) -> bytes:
    """The packed extension as ZIP bytes, with config.js for `servers` (list of origins)."""
    if build not in ("zip", "store"):
        raise BuildError("Build must be 'zip' or 'store'.")
    clean = []
    for s in servers:
        n = norm_server(s, build=build)
        if n not in clean:
            clean.append(n)
    if not clean:
        raise BuildError("Give at least one server address.")
    root = find_source(src)
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise BuildError(f"manifest.json can't be read: {e}") from None
    check_manifest(manifest)
    tmpl_path = root / "config.js"
    template = tmpl_path.read_text(encoding="utf-8") if tmpl_path.is_file() else None
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        def put(name: str, data: bytes) -> None:
            info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)

        names = []
        for p, rel in iter_files(root):
            if rel == "config.js":
                continue
            names.append(rel)
            put(rel, p.read_bytes())
        put("config.js", config_js(clean, build, template).encode("utf-8"))
        if "sw.js" not in names:
            raise BuildError("The extension folder has no sw.js.")
    return buf.getvalue()


def version_of(src: str | Path | None = None) -> str:
    root = find_source(src)
    return str(json.loads((root / "manifest.json").read_text(encoding="utf-8")).get("version", "0"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Pack the ReelVault Members Chrome extension.")
    ap.add_argument("--server", action="append", required=True,
                    help="Allowed ReelVault server, e.g. https://app.example.com (repeat for more)")
    ap.add_argument("--build", choices=("store", "zip"), default="store")
    ap.add_argument("--src", help="extension folder (default: extension-members/)")
    ap.add_argument("--out", default=str(HERE.parent / "dist"), help="output folder (default: dist/)")
    a = ap.parse_args(argv)
    try:
        data = build_zip(a.server, build=a.build, src=a.src)
        ver = version_of(a.src)
    except BuildError as e:
        print(f"Could not build the extension: {e}", file=sys.stderr)
        return 1
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"reelvault-members-extension-{ver}-{a.build}.zip"
    path.write_bytes(data)
    print(f"Built {path} ({len(data) // 1024} KB) for {', '.join(a.server)}.")
    if a.build == "store":
        print("Upload this ZIP in the Chrome Web Store developer dashboard (see deploy/DEPLOY.md, step 6).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
