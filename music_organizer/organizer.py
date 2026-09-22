from __future__ import annotations

import os
import shutil
from dataclasses import dataclass

from .config import Config
from .musicbrainz_client import MusicBrainzClient
from .planner import PlanEntry
from .tags import embed_cover, write_tags


@dataclass
class ApplySummary:
    moved: int = 0
    tagged: int = 0
    skipped_unmatched: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def apply_plan(entries: list[PlanEntry], config: Config) -> ApplySummary:
    summary = ApplySummary()
    mb = MusicBrainzClient(config) if config.embed_cover_art and config.use_musicbrainz else None
    cover_cache: dict[str, tuple[bytes, str] | None] = {}

    for entry in entries:
        if entry.status == "unmatched":
            summary.skipped_unmatched += 1
            continue

        try:
            if entry.old_path != entry.new_path:
                os.makedirs(os.path.dirname(entry.new_path), exist_ok=True)
                shutil.move(entry.old_path, entry.new_path)
                summary.moved += 1

            write_tags(entry.new_path, entry.tags)
            summary.tagged += 1

            if mb is not None and entry.release_group_mbid:
                if entry.release_group_mbid not in cover_cache:
                    cover_cache[entry.release_group_mbid] = mb.get_cover_art(
                        entry.release_group_mbid
                    )
                cover = cover_cache[entry.release_group_mbid]
                if cover is not None:
                    image_bytes, mime = cover
                    embed_cover(entry.new_path, image_bytes, mime)
                    if config.write_folder_jpg:
                        _write_folder_jpg(os.path.dirname(entry.new_path), image_bytes)

        except Exception as exc:  # noqa: BLE001 - report and keep going
            summary.errors.append(f"{entry.old_path}: {exc}")

    return summary


def _write_folder_jpg(album_dir: str, image_bytes: bytes) -> None:
    folder_jpg = os.path.join(album_dir, "folder.jpg")
    if os.path.exists(folder_jpg):
        return
    try:
        with open(folder_jpg, "wb") as f:
            f.write(image_bytes)
    except OSError:
        pass
