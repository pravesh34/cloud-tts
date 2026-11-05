"""Chatterbox engine (Resemble AI, MIT).

Clones a voice from a short reference clip. We render sentence-by-sentence
because the model can drop or repeat words on long inputs.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .base import Engine, chunk_text, concat


class ChatterboxEngine(Engine):
    name = "chatterbox"

    def __init__(self, voices_dir: Path, device: str | None = None) -> None:
        self.voices_dir = Path(voices_dir)
        self.device = device
        self.model = None
        self.sample_rate = 24000

    def load(self) -> None:
        import torch
        from chatterbox.tts import ChatterboxTTS

        device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        print(f"[chatterbox] loading on {device} (first run downloads ~3 GB)")
        self.model = ChatterboxTTS.from_pretrained(device=device)
        self.device = device
        self.sample_rate = int(getattr(self.model, "sr", 24000))
        print(f"[chatterbox] ready at {self.sample_rate} Hz")

    def _reference(self, voice: dict) -> str | None:
        ref = voice.get("ref")
        if not ref:
            return None
        path = (self.voices_dir / ref).resolve()
        if path.exists():
            return str(path)
        print(f"[chatterbox] reference clip missing: {path} (using built-in voice)")
        return None

    def synth(self, text: str, voice: dict, *, exaggeration: float = 0.5) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("Chatterbox is not loaded")
        reference = self._reference(voice)
        chunks: list[np.ndarray] = []
        for piece in chunk_text(text):
            audio = self.model.generate(
                piece,
                audio_prompt_path=reference,
                exaggeration=float(exaggeration),
                cfg_weight=0.5,
            )
            array = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            chunks.append(array.reshape(-1).astype(np.float32))
        return concat(chunks, self.sample_rate, gap=0.18)
