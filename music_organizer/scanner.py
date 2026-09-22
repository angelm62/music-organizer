from __future__ import annotations

import os
from collections.abc import Iterator

from .config import AUDIO_EXTENSIONS


def find_audio_files(root: str) -> Iterator[str]:
    """Recursively yield paths to every audio file under `root`."""
    if "://" in root:
        raise NotADirectoryError(
            f"Library path {root!r} looks like a URL, not a mounted filesystem path. "
            "Mount the SMB share first (Finder -> Go -> Connect to Server, or `mount_smbfs`) "
            "and point --library / LIBRARY_PATH at the local mount path, e.g. /Volumes/Media/Music."
        )
    if not os.path.isdir(root):
        raise NotADirectoryError(
            f"Library path {root!r} is not a directory. "
            "Is the SMB share mounted? (check --library / LIBRARY_PATH)"
        )
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip dot-directories (e.g. .git, .Trashes, ._sync) and our own cache/report dirs.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in AUDIO_EXTENSIONS:
                yield os.path.join(dirpath, name)
