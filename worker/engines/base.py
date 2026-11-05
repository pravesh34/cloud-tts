"""Engine interface and audio helpers shared by all TTS backends."""

from __future__ import annotations

import io
import re
import shutil
import subprocess

import numpy as np

SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
CLAUSE_SPLIT = re.compile(r"(?<=[,;:])\s+")


def chunk_text(text: str, max_chars: int = 220) -> list[str]:
    """Split text into sentence-sized chunks.

    Chatterbox degrades (drops or repeats words) on long inputs, so we render
    one sentence at a time and merge only short neighbours.
    """
    text = re.sub(r"[ \t]+", " ", text.replace("\r", "\n")).strip()
    if not text:
        return []

    sentences: list[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        for sentence in SENTENCE_SPLIT.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= max_chars:
                sentences.append(sentence)
                continue
            # Very long sentence: split at clauses, then hard-wrap.
            for clause in CLAUSE_SPLIT.split(sentence):
                clause = clause.strip()
                while len(clause) > max_chars:
                    cut = clause.rfind(" ", 0, max_chars)
                    cut = cut if cut > 40 else max_chars
                    sentences.append(clause[:cut].strip())
                    clause = clause[cut:].strip()
                if clause:
                    sentences.append(clause)

    chunks: list[str] = []
    buffer = ""
    for sentence in sentences:
        candidate = f"{buffer} {sentence}".strip()
        if buffer and len(candidate) > max_chars:
            chunks.append(buffer)
            buffer = sentence
        else:
            buffer = candidate
    if buffer:
        chunks.append(buffer)
    return chunks


def silence(seconds: float, sample_rate: int) -> np.ndarray:
    return np.zeros(int(seconds * sample_rate), dtype=np.float32)


def concat(chunks: list[np.ndarray], sample_rate: int, gap: float = 0.18) -> np.ndarray:
    parts: list[np.ndarray] = []
    for index, chunk in enumerate(chunks):
        if index:
            parts.append(silence(gap, sample_rate))
        parts.append(np.asarray(chunk, dtype=np.float32).reshape(-1))
    if not parts:
        return silence(0.4, sample_rate)
    return np.concatenate(parts)


def _pcm16(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _ffmpeg(samples: np.ndarray, sample_rate: int, fmt: str, speed: float) -> bytes:
    filters = f"atempo={speed:.3f}" if abs(speed - 1.0) > 0.01 else None
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
    ]
    if filters:
        cmd += ["-filter:a", filters]
    if fmt == "mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", "64k", "-f", "mp3", "pipe:1"]
    else:
        cmd += ["-c:a", "pcm_s16le", "-f", "wav", "pipe:1"]
    proc = subprocess.run(cmd, input=_pcm16(samples), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode('utf-8', 'replace')[:400]}")
    return proc.stdout


def encode_audio(samples: np.ndarray, sample_rate: int, fmt: str = "mp3", speed: float = 1.0) -> tuple[bytes, str]:
    """Return (bytes, mime type). Falls back to WAV when ffmpeg is unavailable."""
    fmt = (fmt or "mp3").lower()
    needs_ffmpeg = fmt == "mp3" or abs(speed - 1.0) > 0.01
    if needs_ffmpeg and shutil.which("ffmpeg"):
        data = _ffmpeg(samples, sample_rate, fmt, speed)
        return data, ("audio/mpeg" if fmt == "mp3" else "audio/wav")

    import soundfile as sf

    buf = io.BytesIO()
    sf.write(buf, samples, sample_rate, format="WAV", subtype="PCM_16")
    return buf.getvalue(), "audio/wav"


class Engine:
    name = "base"
    sample_rate = 24000

    def load(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def synth(self, text: str, voice: dict, *, exaggeration: float = 0.5) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError
