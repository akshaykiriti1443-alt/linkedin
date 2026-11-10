"""Desktop worker for ReelVault Members: runs a member's AI jobs with Claude Code on their own computer
(DESIGN-SAAS.md 7.2, 7.4). WP5.

The desktop app (reelvault/app.py, Settings > "Use my Claude for ReelVault Members") pairs once with a code from the
web UI and then, while the switch is on and the app is open, this loop:

    every 30 s: POST /api/worker/hello, POST /api/worker/lease
    a job -> download its images into a temp folder -> run the unmodified Claude Code (`claude -p`, the member's own
    plan, through brain.claude_code) -> POST /result (on 422 retry once with the server's message) or /fail

Command-line size (D23): the schema normally goes in --json-schema. When the full argv would pass 6,000 characters,
the schema is written to schema.json in the temp folder instead, and Claude is told to read it and answer with one
matching JSON object; pydantic validates the answer either way. This keeps every model under the 8,191-character
cmd.exe limit of `claude.cmd` shims.

The pairing token lives in HOME/members.json (outside the repo). Nothing here stores or sends Claude credentials:
Claude Code keeps its own login. Uses only `requests` and existing deps.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

try:
    from . import __version__
except ImportError:  # imported through a namespace path (e.g. pytest run from the repo's parent folder)
    try:
        from importlib.metadata import version as _dist_version

        __version__ = _dist_version("reelvault")
    except Exception:  # noqa: BLE001
        __version__ = "0.0.0"

log = logging.getLogger("reelvault.members")

CONFIG_NAME = "members.json"
ARGV_LIMIT = 6000
POLL_S = 30
REQUEST_TIMEOUT = 30
KINDS = ["tag_reels", "retag_topics", "analyze_reel", "name_formats", "make_output", "draft_profile", "suggest_topics"]
INSTRUCTION = "Do the task in the prompt. Your final answer is the JSON object only."
FILE_NOTE = (
    "Your final answer must be one JSON object that matches schema.json in this folder (read it first). "
    "Answer with the JSON only, no prose and no code fences."
)


class WorkerError(RuntimeError):
    """Plain message for the desktop app."""


class Unpaired(WorkerError):
    pass


def home() -> Path:
    return Path(os.environ.get("REELVAULT_HOME", Path.home() / "ReelVault")).resolve()


def config_path() -> Path:
    return home() / CONFIG_NAME


def load_config() -> dict:
    p = config_path()
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)


def normalize_server(url: str) -> str:
    from urllib.parse import urlparse

    u = (url or "").strip().rstrip("/")
    if u and "://" not in u:
        u = "https://" + u
    p = urlparse(u)
    local = p.hostname in ("localhost", "127.0.0.1")
    if p.scheme not in ("https", "http") or not p.hostname or (p.scheme == "http" and not local):
        raise WorkerError("The server address must start with https://.")
    return f"{p.scheme}://{p.netloc}"


# ============================================================================================== HTTP


class Client:
    def __init__(self, server: str, token: str | None = None, session=None):
        import requests

        self.server = server.rstrip("/")
        self.token = token
        self.s = session or requests.Session()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "User-Agent": f"ReelVault-Desktop/{__version__}"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def post(self, path: str, body: dict, *, ok_codes=(200, 201)) -> tuple[int, dict]:
        import requests

        try:
            r = self.s.post(self.server + path, data=json.dumps(body), headers=self._headers(), timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            raise WorkerError("Can't reach ReelVault Members. Check your internet connection.") from None
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 401:
            raise Unpaired("This computer is not linked any more. Link it again with a new code.")
        if r.status_code not in ok_codes and r.status_code != 422:
            raise WorkerError(data.get("error") or f"ReelVault Members answered {r.status_code}.")
        return r.status_code, data

    def get_bytes(self, path: str) -> bytes:
        import requests

        try:
            r = self.s.get(self.server + path, headers=self._headers(), timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            raise WorkerError("Can't reach ReelVault Members. Check your internet connection.") from None
        if r.status_code == 401:
            raise Unpaired("This computer is not linked any more. Link it again with a new code.")
        if r.status_code != 200:
            raise WorkerError(f"Couldn't download an image ({r.status_code}).")
        return r.content


def pair(server: str, code: str, name: str | None = None, *, session=None) -> dict:
    """Claim a desktop pairing code (POST /api/ext/v2/pair/claim, kind desktop) and save the token."""
    import platform

    server = normalize_server(server)
    code = (code or "").strip().upper()
    if not code:
        raise WorkerError("Paste the code from ReelVault Members (Settings > AI > Use the desktop app).")
    c = Client(server, None, session)
    status, data = c.post(
        "/api/ext/v2/pair/claim",
        {
            "code": code,
            "kind": "desktop",
            "version": __version__,
            "name": name or f"ReelVault on {platform.system() or 'PC'}",
        },
    )
    if status not in (200, 201) or not data.get("token"):
        raise WorkerError(data.get("error") or "That code didn't work. Make a new one and try again.")
    cfg = load_config()
    cfg.update(
        {
            "server": server,
            "token": data["token"],
            "device_id": data.get("device_id"),
            "member": (data.get("user") or {}).get("name"),
            "working": True,
            "linked_at": int(time.time()),
        }
    )
    save_config(cfg)
    return {"ok": True, "server": server, "member": cfg.get("member")}


def unlink() -> None:
    cfg = load_config()
    for k in ("token", "device_id", "member", "linked_at"):
        cfg.pop(k, None)
    cfg["working"] = False
    save_config(cfg)


# ============================================================================================== Claude Code


def schema_for(schema_name: str) -> tuple[type, dict]:
    from . import schema_members
    from .ai import portable_schema
    from .brain import compact_schema

    model = schema_members.model_for(schema_name)
    return model, compact_schema(portable_schema(model, "anthropic"))


def argv_length(instruction: str, schema: dict | None, exe: str = "claude") -> int:
    """Length of the claude command line brain.claude_code builds (prompt and system prompt go via stdin/file)."""
    parts = [
        exe,
        "-p",
        instruction,
        "--output-format",
        "json",
        "--system-prompt-file",
        "x" * 80,
        "--tools",
        "Read",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--allowedTools",
        "Read",
        "--effort",
        "medium",
    ]
    if schema is not None:
        parts += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
    return sum(len(p) + 3 for p in parts)


def _parse(data: dict):
    payload = data.get("structured_output")
    if payload is None:
        from .ai.base import extract_json

        payload = extract_json(data.get("result") or "")
    return payload


Runner = Callable[..., dict]


def model_of(data: dict) -> str | None:
    """The model Claude Code used, from its JSON result envelope (modelUsage keys), when it says."""
    mu = data.get("modelUsage") if isinstance(data, dict) else None
    if isinstance(mu, dict) and mu:
        return max(mu, key=lambda k: (mu[k] or {}).get("outputTokens", 0) if isinstance(mu[k], dict) else 0)
    m = data.get("model") if isinstance(data, dict) else None
    return m if isinstance(m, str) else None


def run_task(task: dict, folder: Path, *, runner: Runner | None = None) -> tuple[dict, bool, str | None]:
    """Run one AiTask with Claude Code in `folder` (images already there).
    Returns (answer JSON, used_schema_file, model)."""
    from pydantic import ValidationError

    from .brain import BrainError, claude_code, find_claude

    runner = runner or claude_code
    model, schema = schema_for(task["schema_name"])
    names = [im["name"] for im in task.get("images") or []]
    instruction = INSTRUCTION
    if names:
        instruction += " First open each image with the Read tool: " + ", ".join(names) + "."
    use_file = argv_length(instruction, schema, find_claude() or "claude") > ARGV_LIMIT
    if use_file:
        (folder / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
        instruction += " " + FILE_NOTE
    note = ""
    last = None
    for _ in range(2):
        data = runner(
            task["prompt"] + note,
            instruction,
            task["system"],
            folder,
            None if use_file else schema,
            bool(names) or use_file,
            None,
            task.get("effort"),
        )
        try:
            payload = _parse(data)
            model.model_validate(payload)
            return payload, use_file, model_of(data)
        except (ValueError, ValidationError) as e:
            last = e
            note = "\n\nYour last answer was not valid:\n" + str(e)[:1500] + "\nReturn the complete JSON object again."
    raise BrainError(f"Claude Code's answer didn't fit the format twice: {str(last)[:300]}")


# ============================================================================================== loop


def process_one(
    client: Client,
    *,
    runner: Runner | None = None,
    kinds: list[str] | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> dict | None:
    """Lease one job, run it, post the answer. Returns a small record of what happened (or None: no work)."""
    from .brain import BrainError

    _, got = client.post("/api/worker/lease", {"kinds": kinds or KINDS})
    job = got.get("job")
    if not job:
        return None
    task = job["task"]
    rec = {"id": job["id"], "kind": job["kind"], "at": int(time.time()), "ok": False, "message": ""}
    if on_event:
        on_event(dict(rec, message="Working"))
    folder = Path(tempfile.mkdtemp(prefix="rv-members-"))
    try:
        for im in task.get("images") or []:
            name = Path(im["name"]).name
            (folder / name).write_bytes(client.get_bytes(f"/api/worker/files/{job['id']}/{name}"))
        try:
            answer, used_file, model_id = run_task(task, folder, runner=runner)
        except BrainError as e:
            client.post(f"/api/worker/jobs/{job['id']}/fail", {"message": str(e)[:300]})
            rec["message"] = str(e)[:200]
            return rec
        rec["schema_file"] = used_file
        status, res = client.post(f"/api/worker/jobs/{job['id']}/result", {"output": answer, "model": model_id})
        if status == 422:
            msg = ((res.get("detail") or {}).get("validation") or res.get("error") or "")[:1500]
            task = dict(
                task,
                prompt=task["prompt"]
                + "\n\nReelVault could not use your last answer:\n"
                + msg
                + "\nFix it and return the complete JSON object again.",
            )
            try:
                answer, _, model_id = run_task(task, folder, runner=runner)
            except BrainError as e:
                client.post(f"/api/worker/jobs/{job['id']}/fail", {"message": str(e)[:300]})
                rec["message"] = str(e)[:200]
                return rec
            status, res = client.post(f"/api/worker/jobs/{job['id']}/result", {"output": answer, "model": model_id})
            if status == 422:
                client.post(
                    f"/api/worker/jobs/{job['id']}/fail", {"message": "The answer didn't fit the format twice."}
                )
                rec["message"] = "The answer didn't fit the format twice."
                return rec
        rec["ok"] = True
        rec["message"] = "Done" if res.get("next_chunk") is None else "Part done, more to come"
        return rec
    except WorkerError as e:
        rec["message"] = str(e)
        if not isinstance(e, Unpaired):
            try:
                client.post(f"/api/worker/jobs/{job['id']}/fail", {"message": str(e)[:300]})
            except WorkerError:
                pass
        raise
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def claude_state() -> dict:
    from .brain import find_claude

    found = find_claude() is not None
    return {"found": found, "signed_in": found}


class Worker:
    """The background loop the desktop app runs while 'Work on Members jobs while this app is open' is on."""

    def __init__(self, *, runner: Runner | None = None, poll_s: float = POLL_S, session=None):
        self.runner = runner
        self.poll_s = poll_s
        self.session = session
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_jobs: list[dict] = list(load_config().get("last_jobs") or [])[:5]
        self.status = "stopped"
        self.error = ""
        self.lock = threading.Lock()

    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> None:
        if self.running():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="rv-members-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.status = "stopped"

    def _remember(self, rec: dict) -> None:
        with self.lock:
            self.last_jobs = [r for r in self.last_jobs if r.get("id") != rec.get("id")]
            self.last_jobs.insert(0, rec)
            self.last_jobs = self.last_jobs[:5]
            cfg = load_config()
            cfg["last_jobs"] = self.last_jobs
            save_config(cfg)

    def tick(self) -> dict | None:
        """One hello + lease + job. Returns the job record or None."""
        cfg = load_config()
        if not cfg.get("token") or not cfg.get("server"):
            self.status = "not linked"
            return None
        client = Client(cfg["server"], cfg["token"], self.session)
        st = claude_state()
        _, hello = client.post("/api/worker/hello", {"version": __version__, "claude": st, "working": True})
        if not st["found"]:
            self.status = "Claude Code is not installed"
            return None
        if not hello.get("has_work"):
            self.status = "waiting for work"
            return None
        self.status = "working"
        rec = process_one(client, runner=self.runner, on_event=lambda r: setattr(self, "status", "working"))
        if rec:
            self._remember(rec)
        self.status = "waiting for work"
        return rec

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            wait = self.poll_s
            try:
                if load_config().get("working"):
                    rec = self.tick()
                    self.error = ""
                    if rec:
                        wait = 1  # more work may be waiting
                else:
                    self.status = "off"
            except Unpaired as e:
                self.error = str(e)
                self.status = "not linked"
                unlink()
            except WorkerError as e:
                self.error = str(e)
            except Exception as e:  # noqa: BLE001 - the loop must survive anything
                log.exception("members worker tick failed")
                self.error = f"Something went wrong: {str(e)[:200]}"
            self.stop_event.wait(wait)
        self.status = "stopped"

    def info(self) -> dict:
        cfg = load_config()
        return {
            "linked": bool(cfg.get("token")),
            "server": cfg.get("server"),
            "member": cfg.get("member"),
            "working": bool(cfg.get("working")),
            "running": self.running(),
            "status": self.status,
            "error": self.error,
            "last_jobs": self.last_jobs[:5],
        }
