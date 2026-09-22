from __future__ import annotations

import os
import re
from dataclasses import dataclass

from .matcher import TrackMatch
from .tags import TrackTags

ILLEGAL_CHARS_RE = re.compile(r'[\\/:*?"<>|]')


@dataclass
class PlanEntry:
    old_path: str
    new_path: str
    tags: TrackTags
    status: str  # "matched" | "ai_matched" | "unmatched"
    confidence: int
    notes: list[str]
    release_group_mbid: str | None = None


def sanitize(name: str) -> str:
    name = ILLEGAL_CHARS_RE.sub("_", name)
    name = name.strip(" .")
    return name or "Unknown"


def build_plan(tracks: list[TrackMatch], library_root: str) -> list[PlanEntry]:
    disc_counts: dict[str, set[str]] = {}
    for tm in tracks:
        if tm.status == "unmatched":
            continue
        key = f"{tm.resolved_tags.albumartist}|{tm.resolved_tags.album}"
        disc_counts.setdefault(key, set()).add(tm.resolved_tags.discnumber or "1")

    entries: list[PlanEntry] = []
    used_paths: set[str] = set()

    for tm in tracks:
        ext = os.path.splitext(tm.path)[1].lower()
        tags = tm.resolved_tags

        if tm.status == "unmatched":
            entries.append(
                PlanEntry(
                    old_path=tm.path,
                    new_path=tm.path,
                    tags=tags,
                    status=tm.status,
                    confidence=tm.confidence,
                    notes=tm.notes or ["Needs manual review"],
                    release_group_mbid=tm.release_group_mbid,
                )
            )
            continue

        artist = sanitize(tags.albumartist or tags.artist or "Unknown Artist")
        album = sanitize(tags.album or "Unknown Album")
        if tags.date:
            album_folder = f"{album} ({tags.date})"
        else:
            album_folder = album

        key = f"{tags.albumartist}|{tags.album}"
        multi_disc = len(disc_counts.get(key, set())) > 1

        track_no = tags.tracknumber or "00"
        try:
            track_no = f"{int(track_no):02d}"
        except ValueError:
            pass
        prefix = f"{tags.discnumber}-{track_no}" if multi_disc and tags.discnumber else track_no

        title = sanitize(tags.title or os.path.splitext(os.path.basename(tm.path))[0])
        filename = f"{prefix} - {title}{ext}"

        new_dir = os.path.join(library_root, artist, album_folder)
        new_path = os.path.join(new_dir, filename)
        new_path = _dedupe(new_path, tm.path, used_paths)
        used_paths.add(new_path)

        entries.append(
            PlanEntry(
                old_path=tm.path,
                new_path=new_path,
                tags=tags,
                status=tm.status,
                confidence=tm.confidence,
                notes=tm.notes,
                release_group_mbid=tm.release_group_mbid,
            )
        )

    return entries


def _dedupe(new_path: str, old_path: str, used_paths: set[str]) -> str:
    if new_path == old_path:
        return new_path
    if new_path not in used_paths and not (
        os.path.exists(new_path) and os.path.abspath(new_path) != os.path.abspath(old_path)
    ):
        return new_path

    base, ext = os.path.splitext(new_path)
    n = 2
    candidate = f"{base} ({n}){ext}"
    while candidate in used_paths or (
        os.path.exists(candidate) and os.path.abspath(candidate) != os.path.abspath(old_path)
    ):
        n += 1
        candidate = f"{base} ({n}){ext}"
    return candidate
