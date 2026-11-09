"""Old Claude Code: update it and retry; if that fails, fall back to its default model."""

from __future__ import annotations

import json
import subprocess

import pytest

from reelvault import brain

TOO_OLD = ("API Error: 400 Claude Code 2.1.159 does not support this model; version 2.1.280 or newer is "
           "required. Run 'claude update', or update the Claude desktop app, then try again.")


def _ok(cmd):
    return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"subtype": "success", "is_error": False,
                                                                   "result": "hi"}), stderr="")


def _old(cmd):
    return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps({"subtype": "success", "is_error": True,
                                                                   "result": TOO_OLD}), stderr="")


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(brain, "_update", {"tried": False})
    monkeypatch.setattr(brain, "find_claude", lambda: "claude")


def test_updates_then_retries(monkeypatch, tmp_path):
    calls, state = [], {"updated": False}

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "update":
            state["updated"] = True
            return subprocess.CompletedProcess(cmd, 0, stdout="Successfully updated to 2.1.319", stderr="")
        return _ok(cmd) if state["updated"] else _old(cmd)

    monkeypatch.setattr(subprocess, "run", run)
    assert brain.text("p", "i", "s", tmp_path, "claude-opus-5-5", "low") == "hi"
    assert [c[1] for c in calls] == ["-p", "update", "-p"]
    assert "--model" in calls[-1]


def test_falls_back_to_default_model(monkeypatch, tmp_path):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "update":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="update failed")
        return _old(cmd) if "--model" in cmd else _ok(cmd)

    monkeypatch.setattr(subprocess, "run", run)
    assert brain.text("p", "i", "s", tmp_path, "claude-opus-5-5", "low") == "hi"
    assert "--model" not in calls[-1]
    # the next reel doesn't try to update again
    calls.clear()
    brain.text("p", "i", "s", tmp_path, "claude-opus-5-5", "low")
    assert all(c[1] != "update" for c in calls)
