from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional


class JsonCache:
    """A tiny disk-backed cache so re-running the tool doesn't re-hit
    MusicBrainz/Ollama for tracks it already resolved (important given
    MusicBrainz's 1 req/sec rate limit on large libraries).

    Also auto-saves periodically (every `autosave_every` writes, or
    `autosave_seconds` since the last save) so an interrupted run on a large
    library doesn't lose everything it already resolved.
    """

    def __init__(self, path: str, autosave_every: int = 10, autosave_seconds: float = 15.0):
        self.path = path
        self.autosave_every = autosave_every
        self.autosave_seconds = autosave_seconds
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._dirty_count = 0
        self._last_save = time.monotonic()
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, namespace: str, key: str) -> Optional[Any]:
        return self._data.get(namespace, {}).get(key)

    def set(self, namespace: str, key: str, value: Any) -> None:
        with self._lock:
            self._data.setdefault(namespace, {})[key] = value
            self._dirty_count += 1
            due = (
                self._dirty_count >= self.autosave_every
                or (time.monotonic() - self._last_save) >= self.autosave_seconds
            )
        if due:
            self.save()

    def save(self) -> None:
        with self._lock:
            tmp_path = f"{self.path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
            self._dirty_count = 0
            self._last_save = time.monotonic()
