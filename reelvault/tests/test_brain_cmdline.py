"""The Claude Code command must fit Windows' 8191-character limit (cmd.exe shims)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from reelvault import brain
from reelvault.analyze import SYSTEM
from reelvault.schema import ReelAnalysis


def test_command_is_short_and_instructions_go_in_a_file(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        seen["system"] = Path(cmd[cmd.index("--system-prompt-file") + 1]).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps({"is_error": True, "result": "stop"}), stderr="")

    monkeypatch.setattr(brain, "find_claude", lambda: "claude")
    monkeypatch.setattr(subprocess, "run", fake_run)
    long_profile = "Niche: paid ads. " * 300  # a long "About you" must not lengthen the command
    try:
        brain.structured("prompt", "Break this reel down.", SYSTEM + long_profile, tmp_path, ReelAnalysis,
                         read_files=True, model="claude-opus-5-5", effort="medium")
    except brain.BrainError:
        pass
    length = len(subprocess.list2cmdline(seen["cmd"]))
    assert length < 6000, length
    assert "skeleton.hook.hook_type" in seen["system"] and "Niche: paid ads." in seen["system"]
    assert not Path(seen["cmd"][seen["cmd"].index("--system-prompt-file") + 1]).exists()  # temp file removed


def test_compact_schema_keeps_every_field():
    full = brain.strict_schema(ReelAnalysis.model_json_schema())
    small = brain.compact_schema(ReelAnalysis.model_json_schema())

    def fields(node, defs):
        out = set()
        if isinstance(node, dict):
            for name in node.get("properties", {}):
                out.add(name)
            for v in node.values():
                out |= fields(v, defs)
        elif isinstance(node, list):
            for v in node:
                out |= fields(v, defs)
        return out

    assert fields(small, None) == fields(full, None)
    assert "title" in small["properties"] and "title" in small["required"]
    assert "description" not in json.dumps(small)
