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


def find_claude() -> str | None:
    """Locate the Claude Code executable, including the Windows native install spot."""
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


def _env() -> dict:
    env = dict(os.environ)
    # An API key in the environment would make Claude Code bill the API instead of the subscription.
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def claude_code(prompt: str, instruction: str, system: str, cwd: Path, schema: dict | None = None,
                read_files: bool = False, model: str | None = None, effort: str | None = None,
                timeout: int = 900) -> dict:
    """Run one headless Claude Code turn. Returns the parsed JSON result envelope."""
    exe = find_claude()
    if not exe:
        raise BrainError("Claude Code is not installed. Open ReelVault and click 'Install Claude Code'.")
    cmd = [
        exe, "-p", instruction,
        "--output-format", "json",
        "--system-prompt", system,
        "--tools", "Read" if read_files else "",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--disable-slash-commands",
        "--no-session-persistence",
    ]
    if read_files:
        cmd += ["--allowedTools", "Read"]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
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
        raise BrainError(f"Claude Code failed: {msg[:500]}")
    return data


def structured(prompt: str, instruction: str, system: str, cwd: Path, model_cls, read_files: bool,
               model: str | None, effort: str | None):
    """Ask Claude Code for output matching a pydantic model; one retry on a bad shape."""
    schema = strict_schema(model_cls.model_json_schema())
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
