"""Kokoro-82M engine (hexgrad, Apache-2.0).

Fast preset-voice fallback. Runs comfortably on CPU, which keeps the worker
usable on a Colab runtime without a GPU.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .base import Engine, concat

LANG_FOR_PREFIX = {"a": "a", "b": "b"}


class KokoroEngine(Engine):
    name = "kokoro"
    sample_rate = 24000

    def __init__(self, voices_dir: Path | None = None, device: str | None = None) -> None:
        self.device = device
        self.pipelines: dict[str, object] = {}

    def load(self) -> None:
        import torch
        from kokoro import KPipeline  # noqa: F401  (validates the install)

        self._torch = torch
        self.device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        print(f"[kokoro] ready ({self.device}); models load lazily per language")

    def _pipeline(self, lang_code: str):
        if lang_code not in self.pipelines:
            from kokoro import KPipeline

            device = self.device
            if device and str(device).startswith("cuda"):
                self.pipelines[lang_code] = KPipeline(lang_code=lang_code, device=device)
            else:
                self.pipelines[lang_code] = KPipeline(lang_code=lang_code)
        return self.pipelines[lang_code]

    def synth(self, text: str, voice: dict, *, exaggeration: float = 0.5) -> np.ndarray:
        voice_name = voice.get("voice") or "af_heart"
        lang_code = LANG_FOR_PREFIX.get(voice_name[0], "a")
        pipeline = self._pipeline(lang_code)
        chunks: list[np.ndarray] = []
        for _gs, _ps, audio in pipeline(text, voice=voice_name):
            array = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            chunks.append(array.reshape(-1).astype(np.float32))
        return concat(chunks, self.sample_rate, gap=0.12)
