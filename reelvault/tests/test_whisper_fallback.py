"""If the NVIDIA speech libraries are missing, transcription switches to the CPU."""

from __future__ import annotations

import types

import numpy as np

from reelvault import media


class Seg:
    def __init__(self, t):
        self.start, self.end, self.text = 0.0, 1.0, t


def test_falls_back_to_cpu(monkeypatch, tmp_path):
    made = []

    class FakeModel:
        def __init__(self, size, device, compute_type):
            self.device = device
            made.append(device)

        def transcribe(self, audio, **kw):
            info = types.SimpleNamespace(language="en", language_probability=0.99)
            if self.device != "cpu":
                def broken():
                    raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
                    yield  # pragma: no cover
                return broken(), info
            return iter([Seg(" Stop posting daily.")]), info

    import faster_whisper
    monkeypatch.setattr(faster_whisper, "WhisperModel", FakeModel)
    monkeypatch.setattr(media, "load_audio", lambda v: np.ones(16000, dtype=np.float32))
    monkeypatch.setattr(media, "_whisper", None)

    out = media.transcribe(tmp_path / "v.mp4", "small")
    assert out["text"] == "Stop posting daily."
    assert made == ["auto", "cpu"]
    # the next reel goes straight to the CPU model
    media.transcribe(tmp_path / "v.mp4", "small")
    assert made == ["auto", "cpu"]
