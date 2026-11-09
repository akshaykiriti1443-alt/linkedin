"""The AI that writes breakdowns and scripts.

Two choices (set `brain` in reelvault.toml):

- "claude-code" (default): runs the Claude Code app in the background with
  `claude -p`. It uses whatever account Claude Code is signed in to, so it runs
  on your Claude subscription. No API key.
- "api": calls the Claude API with ANTHROPIC_API_KEY (pay per use).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


class BrainError(RuntimeError):
    pass


class ClaudeTooOld(BrainError):
    """The installed Claude Code is older than the chosen model needs."""


_update = {"tried": False}


def update_claude() -> bool:
    """Run `claude update` once per app run. Returns True if it ran without error."""
    if _update["tried"]:
        return False
    _update["tried"] = True
    exe = find_claude()
    if not exe:
        return False
    print("  Claude Code is out of date; updating it (about a minute)...")
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        r = subprocess.run([exe, "update"], capture_output=True, text=True, encoding="utf-8", timeout=600,
                           env=_env(), creationflags=flags)
    except Exception as e:
        print(f"  Claude Code update didn't run: {e}")
        return False
    last = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    print(f"  {last[-1] if last else 'update finished'}")
    return r.returncode == 0


def find_claude() -> str | None:
    """Locate the Claude Code executable, including the Windows native install spot."""
    native = Path.home() / ".local" / "bin" / "claude.exe"
    if sys.platform == "win32" and native.exists():
        return str(native)  # the real .exe: no cmd.exe shim and its 8191-character limit
    for name in ("claude", "claude.exe", "claude.cmd"):
        p = shutil.which(name)
        if p:
            return p
    home = Path.home()
    for p in (home / ".local" / "bin" / "claude.exe", home / ".local" / "bin" / "claude",
              home / "AppData" / "Roaming" / "npm" / "claude.cmd"):
        if p.exists():
            return str(p)
    return None


def strict_schema(schema: dict) -> dict:
    """Make every object closed and every field required (what structured outputs expect)."""
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    schema = json.loads(json.dumps(schema))
    walk(schema)
    return schema


def compact_schema(schema: dict) -> dict:
    """Schema without titles/descriptions, so the command line stays short on Windows
    (cmd.exe allows only 8191 characters). The descriptions go into the prompt instead."""
    def strip(node, is_properties=False):
        if isinstance(node, dict):
            if is_properties:  # keys here are field names (one is literally "title"): keep them all
                return {k: strip(v) for k, v in node.items()}
            return {k: strip(v, k in ("properties", "$defs")) for k, v in node.items()
                    if k not in ("title", "description")}
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node
    return strip(strict_schema(schema))


def field_guide(schema: dict) -> str:
    """'skeleton.hook.hook_type: One of ...' lines from the schema's descriptions."""
    defs = schema.get("$defs", {})
    lines: list[str] = []

    def walk(node, path):
        if "$ref" in node:
            node = defs[node["$ref"].split("/")[-1]]
        if node.get("type") == "array" and "items" in node:
            walk(node["items"], path + "[]")
            return
        for name, sub in (node.get("properties") or {}).items():
            p = f"{path}.{name}" if path else name
            if sub.get("description"):
                lines.append(f"- {p}: {sub['description']}")
            walk(sub, p)

    walk(schema, "")
    return "\n".join(lines)


def _env() -> dict:
    env = dict(os.environ)
    # An API key in the environment would make Claude Code bill the API instead of the subscription.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def claude_code(prompt: str, instruction: str, system: str, cwd: Path, schema: dict | None = None,
                read_files: bool = False, model: str | None = None, effort: str | None = None,
                timeout: int = 900) -> dict:
    """Run one headless Claude Code turn. Returns the parsed JSON result envelope.

    If Claude Code is too old for the chosen model, update it and try again; if that
    doesn't help, use the model this Claude Code supports instead of failing."""
    args = (prompt, instruction, system, cwd, schema, read_files)
    if _update.get("no_model"):
        model = None  # already fell back this run
    try:
        return _claude_code_once(*args, model, effort, timeout)
    except ClaudeTooOld:
        if update_claude():
            try:
                return _claude_code_once(*args, model, effort, timeout)
            except ClaudeTooOld:
                pass
        if not _update.get("no_model"):
            _update["no_model"] = True
            print(f"  Using Claude Code's own default model for now (update Claude Code to use {model}).")
        return _claude_code_once(*args, None, effort, timeout)


def _claude_code_once(prompt: str, instruction: str, system: str, cwd: Path, schema: dict | None,
                      read_files: bool, model: str | None, effort: str | None, timeout: int) -> dict:
    exe = find_claude()
    if not exe:
        raise BrainError("Claude Code is not installed. Open ReelVault and click 'Install Claude Code'.")
    import tempfile

    sys_file = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    sys_file.write(system)
    sys_file.close()
    cmd = [
        exe, "-p", instruction,
        "--output-format", "json",
        "--system-prompt-file", sys_file.name,
        "--tools", "Read" if read_files else "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if read_files:
        cmd += ["--allowedTools", "Read"]
    if schema:
        cmd += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              cwd=str(cwd), env=_env(), timeout=timeout, creationflags=flags)
    except subprocess.TimeoutExpired:
        raise BrainError("Claude Code took too long and was stopped.")
    finally:
        Path(sys_file.name).unlink(missing_ok=True)
    out = proc.stdout.strip()
    try:
        data = json.loads(out.splitlines()[-1] if out else "{}")
    except json.JSONDecodeError:
        data = {}
    if proc.returncode != 0 or data.get("is_error") or data.get("subtype") not in (None, "success"):
        msg = (data.get("result") or proc.stderr or out or "unknown error").strip()
        low = msg.lower()
        if "login" in low or "log in" in low or "authenticat" in low or "/login" in low:
            raise BrainError("Claude Code is not signed in. Open ReelVault and click 'Sign in to Claude'.")
        if "limit" in low and ("usage" in low or "reached" in low):
            raise BrainError("Your Claude plan's usage limit is reached. ReelVault will continue after it resets.")
        if "does not support this model" in low or "or newer is required" in low:
            raise ClaudeTooOld(msg)
        raise BrainError(f"Claude Code failed: {msg[:500]}")
    return data


def structured(prompt: str, instruction: str, system: str, cwd: Path, model_cls, read_files: bool,
               model: str | None, effort: str | None):
    """Ask Claude Code for output matching a pydantic model; one retry on a bad shape."""
    full = model_cls.model_json_schema()
    schema = compact_schema(full)
    system = system + "\n\n<field_guide>\nWhat each field of your answer means:\n" + field_guide(full) + "\n</field_guide>"
    last = None
    for attempt in range(2):
        data = claude_code(prompt, instruction, system, cwd, schema, read_files, model, effort)
        payload = data.get("structured_output")
        if payload is None:
            text = (data.get("result") or "").strip().strip("`")
            start, end = text.find("{"), text.rfind("}")
            payload = json.loads(text[start:end + 1]) if start >= 0 and end > start else None
        try:
            return model_cls.model_validate(payload)
        except Exception as e:  # pydantic.ValidationError or a None payload
            last = e
            instruction += "\nYour last answer did not match the schema. Return every field."
    raise BrainError(f"Claude Code returned an incomplete breakdown: {last}")


def text(prompt: str, instruction: str, system: str, cwd: Path, model: str | None, effort: str | None) -> str:
    data = claude_code(prompt, instruction, system, cwd, None, False, model, effort)
    return data.get("result") or ""
