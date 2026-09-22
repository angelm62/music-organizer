from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

import requests

from .cache import JsonCache
from .config import Config

PROMPT_TEMPLATE = """You extract music metadata from a messy filename and its folder path.
Respond with ONLY a compact JSON object, no prose, no markdown fences, using exactly these keys:
{{"artist": "...", "title": "...", "album": "..."}}
Use "" for any field you cannot confidently determine. Do not guess wildly; strip track numbers,
file extensions, bracketed junk like "[320kbps]" or "(Official Video)", and underscores/dashes
used as separators.

Folder path: {folder}
Filename: {filename}
"""


@dataclass
class FilenameGuess:
    artist: str = ""
    title: str = ""
    album: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.artist or self.title)


class OllamaClient:
    def __init__(self, config: Config, cache: Optional[JsonCache] = None):
        self.config = config
        self.cache = cache
        self.session = requests.Session()

    def guess_from_filename(self, folder: str, filename: str) -> FilenameGuess:
        cache_key = f"{folder}|{filename}"
        if self.cache:
            cached = self.cache.get("ollama_guess", cache_key)
            if cached is not None:
                return FilenameGuess(**cached)

        prompt = PROMPT_TEMPLATE.format(folder=folder, filename=filename)
        guess = self._call(prompt)

        if self.cache:
            self.cache.set("ollama_guess", cache_key, guess.__dict__)
        return guess

    def _call(self, prompt: str) -> FilenameGuess:
        try:
            resp = self.session.post(
                f"{self.config.ollama_host}/api/generate",
                json={
                    "model": self.config.ollama_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0},
                },
                timeout=self.config.ollama_timeout,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "")
        except requests.RequestException:
            return FilenameGuess()

        parsed = _extract_json(raw)
        if not parsed:
            return FilenameGuess()
        return FilenameGuess(
            artist=str(parsed.get("artist", "")).strip(),
            title=str(parsed.get("title", "")).strip(),
            album=str(parsed.get("album", "")).strip(),
        )


def _extract_json(text: str) -> Optional[dict]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
