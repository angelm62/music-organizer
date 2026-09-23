from __future__ import annotations

import argparse
import sys

from .cache import JsonCache
from .config import Config
from .matcher import LibraryUnavailableError, match_all
from .organizer import apply_plan
from .planner import build_plan
from .report import console, print_dry_run, write_report_file
from .scanner import find_audio_files


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="music-organizer",
        description="Reorganize a music library into Artist/Album folders and fix tags "
        "using MusicBrainz (with a local Ollama model as fallback for messy filenames).",
    )
    parser.add_argument("--library", help="Path to the mounted music library root (overrides LIBRARY_PATH env var)")
    parser.add_argument("--apply", action="store_true", help="Actually move files and write tags. Without this flag, only a dry-run report is produced.")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N files found (useful for testing on a sample before running on the whole library)")
    parser.add_argument("--report", default="music_organizer_report.json", help="Path to write the full JSON report (default: %(default)s)")
    parser.add_argument("--no-ai", action="store_true", help="Disable the Ollama fallback for parsing messy filenames")
    parser.add_argument("--no-musicbrainz", action="store_true", help="Disable MusicBrainz lookups (tags will only be normalized/grouped from existing tags)")
    parser.add_argument("--no-cover-art", action="store_true", help="Don't fetch/embed cover art")
    parser.add_argument("--cache", default=None, help="Path to the resolution cache file (overrides CACHE_PATH env var)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print extra diagnostic info")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    config = Config()

    if args.library:
        config.library_path = args.library
    if not config.library_path:
        console.print(
            "[bold red]No library path set.[/bold red] Pass --library /path/to/mounted/share "
            "or set LIBRARY_PATH in your .env file."
        )
        return 2

    if args.no_ai:
        config.use_ai_fallback = False
    if args.no_musicbrainz:
        config.use_musicbrainz = False
    if args.no_cover_art:
        config.embed_cover_art = False
    if args.cache:
        config.cache_path = args.cache

    if not config.musicbrainz_contact and config.use_musicbrainz:
        console.print(
            "[yellow]Warning:[/yellow] MUSICBRAINZ_CONTACT is not set. MusicBrainz asks for a "
            "contact (email or URL) in the User-Agent for API access; set it in your .env file."
        )

    try:
        paths = list(find_audio_files(config.library_path))
    except NotADirectoryError as exc:
        console.print(f"[bold red]{exc}[/bold red]")
        return 2

    if not paths:
        console.print(f"No audio files found under {config.library_path}")
        return 0

    if args.limit:
        paths = paths[: args.limit]

    console.print(f"Found {len(paths)} audio file(s) under {config.library_path}")
    console.print("Resolving metadata (this respects MusicBrainz's rate limit; large libraries take a while, and reruns reuse the cache)...")

    cache = JsonCache(config.cache_path)
    try:
        tracks = match_all(paths, config, cache=cache)
    except LibraryUnavailableError as exc:
        console.print(f"\n[bold red]Stopped early:[/bold red] {exc}")
        console.print(
            "[yellow]Progress up to this point was saved to the cache[/yellow] -- "
            "reconnect the share and rerun; already-resolved tracks won't be re-queried."
        )
        return 1
    entries = build_plan(tracks, config.library_path)

    if args.apply:
        console.print(f"\n[bold]Applying changes to {len(entries)} tracks...[/bold]")
        summary = apply_plan(entries, config)
        console.print(
            f"\n[green]Done.[/green] Moved: {summary.moved}, tagged: {summary.tagged}, "
            f"skipped (needs review): {summary.skipped_unmatched}"
        )
        if summary.errors:
            console.print(f"[bold red]{len(summary.errors)} error(s):[/bold red]")
            for err in summary.errors[:25]:
                console.print(f"  - {err}")
    else:
        print_dry_run(entries, config.library_path)

    write_report_file(entries, args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
