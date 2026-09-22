from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".ogg", ".oga", ".opus", ".wma"}


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val else default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    library_path: str = field(default_factory=lambda: os.environ.get("LIBRARY_PATH", ""))

    ollama_host: str = field(
        default_factory=lambda: os.environ.get("OLLAMA_HOST", "http://10.27.27.190:11434")
    )
    ollama_model: str = field(default_factory=lambda: os.environ.get("OLLAMA_MODEL", "granite4.1:8b"))
    ollama_timeout: float = field(default_factory=lambda: _env_float("OLLAMA_TIMEOUT", 30.0))
    
    musicbrainz_app: str = "music-organizer"
    musicbrainz_version: str = "0.1.0"
    musicbrainz_contact: str = field(
        default_factory=lambda: os.environ.get("MUSICBRAINZ_CONTACT", "")
    )
    musicbrainz_rate_limit: float = field(
        default_factory=lambda: _env_float("MUSICBRAINZ_RATE_LIMIT", 1.1)
    )

    cache_path: str = field(
        default_factory=lambda: os.environ.get("CACHE_PATH", ".music_organizer_cache.json")
    )

    embed_cover_art: bool = field(default_factory=lambda: _env_bool("EMBED_COVER_ART", True))
    write_folder_jpg: bool = field(default_factory=lambda: _env_bool("WRITE_FOLDER_JPG", True))

    use_ai_fallback: bool = field(default_factory=lambda: _env_bool("USE_AI_FALLBACK", True))
    use_musicbrainz: bool = field(default_factory=lambda: _env_bool("USE_MUSICBRAINZ", True))

    min_match_score: int = field(default_factory=lambda: int(os.environ.get("MIN_MATCH_SCORE", "60")))

    @property
    def musicbrainz_user_agent(self) -> str:
        contact = self.musicbrainz_contact or "no-contact-set"
        return f"{self.musicbrainz_app}/{self.musicbrainz_version} ({contact})"
