"""Voice catalog shared by the gateway and the worker.

The canonical file lives at the repository root (``voices.json``). A fallback
catalog is embedded so the gateway still boots if the file is missing (for
example when only the ``gateway`` directory is deployed).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = Path(os.environ.get("VOICES_PATH", _ROOT / "voices.json"))

_FALLBACK = {
    "default_voice": "male_natural",
    "voices": [
        {
            "id": "male_natural",
            "label": "Male — Natural",
            "gender": "male",
            "engine": "chatterbox",
            "description": "Chatterbox, cloned from your reference clip",
            "ref": "male.wav",
        },
        {
            "id": "female_natural",
            "label": "Female — Natural",
            "gender": "female",
            "engine": "chatterbox",
            "description": "Chatterbox, cloned from your reference clip",
            "ref": "female.wav",
        },
        {
            "id": "male_fast",
            "label": "Male — Fast",
            "gender": "male",
            "engine": "kokoro",
            "description": "Kokoro preset, instant",
            "voice": "am_fenrir",
        },
        {
            "id": "female_fast",
            "label": "Female — Fast",
            "gender": "female",
            "engine": "kokoro",
            "description": "Kokoro preset, instant",
            "voice": "af_heart",
        },
    ],
}


def _load() -> dict:
    try:
        with CATALOG_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not data.get("voices"):
            raise ValueError("empty voice list")
        return data
    except Exception:
        return json.loads(json.dumps(_FALLBACK))


CATALOG = _load()
VOICES: dict[str, dict] = {v["id"]: v for v in CATALOG["voices"]}
DEFAULT_VOICE: str = CATALOG.get("default_voice") or next(iter(VOICES))
ENGINES = sorted({v["engine"] for v in VOICES.values()})


def get_voice(voice_id: Optional[str]) -> Optional[dict]:
    if not voice_id:
        return VOICES.get(DEFAULT_VOICE)
    return VOICES.get(voice_id)


def public_voices() -> list[dict]:
    """Catalog without internal reference paths."""
    return [
        {
            "id": v["id"],
            "label": v["label"],
            "gender": v.get("gender", "unknown"),
            "engine": v["engine"],
            "description": v.get("description", ""),
        }
        for v in VOICES.values()
    ]
