from .base import Engine, chunk_text, concat, encode_audio
from .chatterbox import ChatterboxEngine
from .kokoro import KokoroEngine

ENGINES = {
    "chatterbox": ChatterboxEngine,
    "kokoro": KokoroEngine,
}

__all__ = ["ENGINES", "Engine", "chunk_text", "concat", "encode_audio"]
