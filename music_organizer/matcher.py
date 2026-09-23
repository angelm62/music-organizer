from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz, utils as fuzz_utils
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from .cache import JsonCache
from .config import Config
from .console import console
from .musicbrainz_client import MusicBrainzClient, ReleaseCandidate, ReleaseDetail, ReleaseTrack
from .ollama_client import OllamaClient
from .tags import TrackTags, read_tags

FILENAME_JUNK_RE = re.compile(
    r"\[(.*?)\]|\((?:official|lyrics?|audio|video|hq|hd|remaster\w*)\)|\d{2,4}\s?kbps",
    re.IGNORECASE,
)

# Bracketed/parenthesized qualifiers that describe a *version* of a recording rather
# than being part of its title -- MusicBrainz's exact-phrase search on `recording:`
# usually only has the plain song title indexed, so "Julia (2018 Mix)" or
# "Fire (Live)" fail to find the (very real) underlying recording even though the
# song title itself is spelled correctly. Stripped only for search/fuzzy-matching
# purposes -- matched tracks still get retitled from MusicBrainz's canonical title,
# and unmatched tracks are never rewritten, so nothing is lost by cleaning here.
TITLE_QUALIFIER_RE = re.compile(
    r"""
    \s*[\(\[]
        [^()\[\]]*?
        (?:
            \d{2,4}\s*(?:mix|remaster\w*|version|edit)
            | remaster\w*
            | remix\w*
            | \bmix\b
            | \blive\b
            | \bdemo\b
            | \bmono\b
            | \bstereo\b
            | acoustic
            | instrumental
            | \bversion\b
            | radio\s+edit
            | extended
            | bonus\s+track
            | alternate
            | alt\.?\s*(?:mix|take|version)
            | take\s*\d+
            | feat\.?\s
            | ft\.?\s
            | with\s
        )
        [^()\[\]]*?
    [\)\]]\s*
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Edition/format noise on ALBUM tags -- "Sea Change (MOFI)", "The Wall [FLAC] 88" --
# that doesn't match MusicBrainz's canonical release titles. Cleaning this lets the
# strict first search tier succeed directly instead of relying on the hint-free
# fallback tiers.
ALBUM_QUALIFIER_RE = re.compile(
    r"""
    \s*[\(\[]
        [^()\[\]]*?
        (?:
            deluxe | remaster\w* | anniversary | expanded | edition
            | mofi | flac | mp3 | hi-?res | \d{2,4}\s*kbps
            | \d*\s*cd\d*\b | vinyl | bonus | box\s+set
            | \d{2,4}\s*bit | \d{2,3}\s*khz | \bwav\b
        )
        [^()\[\]]*?
    [\)\]]\s*
    """,
    re.IGNORECASE | re.VERBOSE,
)


def clean_search_title(title: Optional[str]) -> str:
    if not title:
        return ""
    cleaned = TITLE_QUALIFIER_RE.sub(" ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned or title


def clean_search_album(album: Optional[str]) -> str:
    if not album:
        return ""
    cleaned = ALBUM_QUALIFIER_RE.sub(" ", album)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -")
    return cleaned or album


TRACK_PREFIX_RE = re.compile(r"^\s*\d{1,3}[\s.\-_)]+")


@dataclass
class TrackMatch:
    path: str
    original_tags: TrackTags
    resolved_tags: TrackTags
    query_source: str  # "tags" | "ai" | "filename"
    status: str = "unmatched"  # "matched" | "ai_matched" | "unmatched"
    confidence: int = 0
    release_group_mbid: Optional[str] = None
    group_key: tuple = ()
    notes: list[str] = field(default_factory=list)
    search_title: str = ""  # title with mix/live/remaster-style qualifiers stripped


def _clean_filename_guess(filename: str) -> str:
    stem = os.path.splitext(filename)[0]
    stem = TRACK_PREFIX_RE.sub("", stem)
    stem = FILENAME_JUNK_RE.sub("", stem)
    stem = re.sub(r"[_]+", " ", stem)
    return re.sub(r"\s+", " ", stem).strip(" -_.")


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def resolve_query(
    path: str, tags: TrackTags, config: Config, ollama: Optional[OllamaClient]
) -> tuple[str, str, str, str]:
    """Return (artist, title, album, source) best-effort query info for MB search."""
    if tags.artist and tags.title:
        return tags.artist, tags.title, tags.album or "", "tags"

    guess_artist, guess_title, guess_album = "", "", ""
    source = "filename"
    if config.use_ai_fallback and ollama is not None:
        folder = os.path.basename(os.path.dirname(path))
        guess = ollama.guess_from_filename(folder, os.path.basename(path))
        if guess.usable:
            guess_artist, guess_title, guess_album = guess.artist, guess.title, guess.album
            source = "ai"

    artist = tags.artist or guess_artist
    title = tags.title or guess_title
    album = tags.album or guess_album

    if not artist and not title:
        cleaned = _clean_filename_guess(os.path.basename(path))
        if " - " in cleaned:
            maybe_artist, maybe_title = cleaned.split(" - ", 1)
            artist, title = artist or maybe_artist.strip(), title or maybe_title.strip()
        else:
            title = title or cleaned
            artist = artist or os.path.basename(os.path.dirname(path))

    return artist, title, album, source


def _rank(candidates: list[ReleaseCandidate]) -> list[ReleaseCandidate]:
    return sorted(
        candidates,
        key=lambda c: (
            c.status == "Official",
            c.primary_type == "Album",
            c.score,
        ),
        reverse=True,
    )


def _progress() -> Progress:
    return Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    )


class LibraryUnavailableError(RuntimeError):
    """Raised when too many consecutive files fail with an OSError -- almost
    always a dropped network mount rather than a batch of coincidentally
    unreadable files."""


CONSECUTIVE_IO_ERROR_LIMIT = 8


def match_all(
    paths: list[str], config: Config, cache: Optional[JsonCache] = None
) -> list[TrackMatch]:
    cache = cache or JsonCache(config.cache_path)
    mb = MusicBrainzClient(config, cache) if config.use_musicbrainz else None
    ollama = OllamaClient(config, cache) if config.use_ai_fallback else None

    tracks: list[TrackMatch] = []
    io_error_tracks: list[TrackMatch] = []
    candidates_by_path: dict[str, list[ReleaseCandidate]] = {}
    consecutive_io_errors = 0

    with _progress() as progress:
        search_task = progress.add_task("Looking up tracks on MusicBrainz", total=len(paths))
        for path in paths:
            try:
                tags = read_tags(path)
            except OSError as exc:
                consecutive_io_errors += 1
                io_error_tracks.append(
                    TrackMatch(
                        path=path,
                        original_tags=TrackTags(),
                        resolved_tags=TrackTags(),
                        query_source="io_error",
                        notes=[f"File could not be read ({exc}) -- is the share still mounted?"],
                    )
                )
                progress.advance(search_task)
                if consecutive_io_errors >= CONSECUTIVE_IO_ERROR_LIMIT:
                    raise LibraryUnavailableError(
                        f"{consecutive_io_errors} files in a row could not be read "
                        f"(most recently: {exc}). This almost always means the network "
                        "share disconnected mid-run rather than that many files being "
                        "individually broken -- check the mount and try again."
                    ) from exc
                continue
            consecutive_io_errors = 0

            if tags.is_fully_tagged():
                # The file already carries everything needed to file it correctly --
                # artist, title, album, track number. Trust it as-is rather than
                # sending it through MusicBrainz's fuzzy title/artist search, which
                # can (and has) matched a well-tagged file to the wrong release when
                # a search returns a same-named track off a different album. Only
                # the filesystem name gets cleaned (planner.sanitize); the tag
                # content itself is left alone.
                # Edition/format noise in the file's own ALBUM tag ("Boy (Deluxe Edition
                # Remastered) [2CD]") would otherwise fork this track into its own album
                # folder apart from its siblings tagged plainly "Boy" -- the same
                # qualifier-stripping used for MusicBrainz search cleans it for real here.
                cleaned_album = clean_search_album(tags.album) or tags.album
                resolved = TrackTags(
                    **{
                        **tags.__dict__,
                        "album": cleaned_album,
                        "albumartist": tags.albumartist or tags.artist,
                    }
                )
                tracks.append(
                    TrackMatch(
                        path=path,
                        original_tags=tags,
                        resolved_tags=resolved,
                        query_source="tags",
                        status="matched",
                        confidence=100,
                        notes=["File already fully tagged -- used as-is, no MusicBrainz lookup"],
                    )
                )
                progress.advance(search_task)
                continue

            artist, title, album, source = resolve_query(path, tags, config, ollama)
            query_tags = TrackTags(
                artist=artist or None, title=title or None, album=album or tags.album
            )

            search_title = clean_search_title(title)
            search_album = clean_search_album(album) if album else None

            candidates: list[ReleaseCandidate] = []
            if mb is not None and artist and search_title:
                candidates = mb.search_recording(artist, search_title, search_album)
            candidates_by_path[path] = _rank(candidates)

            tm = TrackMatch(
                path=path,
                original_tags=tags,
                resolved_tags=tags.merged_with(query_tags),
                query_source=source,
                search_title=search_title,
            )
            tracks.append(tm)
            progress.advance(search_task)

        needs_resolution = [tm for tm in tracks if tm.status != "matched"]
        _group_and_resolve(needs_resolution, candidates_by_path, mb, progress)

    _unify_fast_path_album_dates(tracks)
    cache.save()
    return tracks + io_error_tracks


def _unify_fast_path_album_dates(tracks: list[TrackMatch]) -> None:
    """Fast-tracked (already-fully-tagged) tracks skip MusicBrainz, so nothing
    picks one canonical release date for the album the way the MB-resolved path
    does. Left alone, a compilation or reissue where each track kept its own
    original-recording year in its DATE tag forks into one folder per distinct
    year (e.g. a "Best Of" spanning 1980-2000 splitting into six folders). Vote
    on the most common date per (albumartist, album) group instead, so the whole
    album lands in one folder -- same consensus approach _resolve_group uses for
    MusicBrainz-matched groups, just without a MusicBrainz release to anchor to.
    """
    groups: dict[tuple, list[TrackMatch]] = {}
    for tm in tracks:
        if tm.query_source != "tags" or tm.status != "matched":
            continue
        key = (
            _normalize(tm.resolved_tags.albumartist or ""),
            _normalize(tm.resolved_tags.album or ""),
        )
        groups.setdefault(key, []).append(tm)

    for members in groups.values():
        raw_dates = [tm.resolved_tags.date for tm in members]
        if len(set(raw_dates)) <= 1:
            continue  # already unanimous, including "nobody has a date tag"
        non_empty = [d for d in raw_dates if d]
        if not non_empty:
            continue
        canonical = Counter(non_empty).most_common(1)[0][0]
        for tm in members:
            if tm.resolved_tags.date != canonical:
                tm.resolved_tags = TrackTags(**{**tm.resolved_tags.__dict__, "date": canonical})


TOP_N_CANDIDATES = 10


def _group_and_resolve(
    tracks: list[TrackMatch],
    candidates_by_path: dict[str, list[ReleaseCandidate]],
    mb: Optional[MusicBrainzClient],
    progress: Progress,
) -> None:
    """Group tracks into albums and resolve each group to a MusicBrainz release.

    Tracks with an existing ALBUM tag are grouped directly by (album artist, album)
    since that's trusted, user-supplied information. Tracks without one are first
    scoped to (artist, folder) so we never merge unrelated artists, then clustered
    by shared MusicBrainz release-group candidates via union-find -- this lets
    multiple tracks vote on the same album *before* any one of them commits to a
    specific release, which is what makes cross-track consensus possible.
    """
    tag_groups: dict[tuple, list[TrackMatch]] = {}
    remaining: list[TrackMatch] = []
    for tm in tracks:
        tags = tm.resolved_tags
        if tags.album:
            key = ("tag", _normalize(tags.albumartist or tags.artist or ""), _normalize(tags.album))
            tm.group_key = key
            tag_groups.setdefault(key, []).append(tm)
        else:
            remaining.append(tm)

    scope_groups: dict[tuple, list[TrackMatch]] = {}
    for tm in remaining:
        scope_key = (_normalize(tm.resolved_tags.artist or ""), os.path.dirname(tm.path))
        scope_groups.setdefault(scope_key, []).append(tm)

    mb_groups: dict[tuple, list[TrackMatch]] = {}
    for scope_key, members in scope_groups.items():
        for i, cluster in enumerate(_cluster_by_shared_candidates(members, candidates_by_path)):
            key = ("mb", scope_key, i)
            for tm in cluster:
                tm.group_key = key
            mb_groups[key] = cluster

    all_groups = list({**tag_groups, **mb_groups}.values())
    resolve_task = progress.add_task("Resolving albums", total=len(all_groups))
    for members in all_groups:
        _resolve_group(members, candidates_by_path, mb)
        progress.advance(resolve_task)


def _cluster_by_shared_candidates(
    members: list[TrackMatch], candidates_by_path: dict[str, list[ReleaseCandidate]]
) -> list[list[TrackMatch]]:
    parent = list(range(len(members)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    first_seen: dict[str, int] = {}
    for i, tm in enumerate(members):
        for cand in candidates_by_path[tm.path][:TOP_N_CANDIDATES]:
            if not cand.release_group_mbid:
                continue
            if cand.release_group_mbid in first_seen:
                union(i, first_seen[cand.release_group_mbid])
            else:
                first_seen[cand.release_group_mbid] = i

    clusters: dict[int, list[TrackMatch]] = {}
    for i, tm in enumerate(members):
        clusters.setdefault(find(i), []).append(tm)
    return list(clusters.values())


def _resolve_group(
    members: list[TrackMatch],
    candidates_by_path: dict[str, list[ReleaseCandidate]],
    mb: Optional[MusicBrainzClient],
) -> None:
    if mb is None:
        for tm in members:
            tm.status = "unmatched"
            tm.notes.append("MusicBrainz disabled")
        return

    vote = Counter()
    candidate_by_group: dict[str, ReleaseCandidate] = {}
    for tm in members:
        # A single track's candidate list can list the same release-group more than
        # once (different pressings/editions), so only count it once per track.
        seen_groups: set[str] = set()
        for cand in candidates_by_path[tm.path][:TOP_N_CANDIDATES]:
            if not cand.release_group_mbid:
                continue
            if cand.release_group_mbid not in seen_groups:
                seen_groups.add(cand.release_group_mbid)
                vote[cand.release_group_mbid] += 1
            best = candidate_by_group.get(cand.release_group_mbid)
            if best is None or _rank([best, cand])[0] is cand:
                candidate_by_group[cand.release_group_mbid] = cand

    if not vote:
        for tm in members:
            tm.status = "unmatched"
            tm.notes.append("No MusicBrainz match found")
        return

    winning_rg, votes = vote.most_common(1)[0]
    winning_candidate = candidate_by_group[winning_rg]
    detail = mb.get_release_detail(winning_candidate.release_mbid)
    if detail is None:
        for tm in members:
            tm.status = "unmatched"
            tm.notes.append("Failed to fetch release detail from MusicBrainz")
        return

    confidence = min(100, int(100 * votes / len(members)))
    _apply_release_to_members(members, detail, confidence, winning_candidate.artist_credit)


DUPLICATE_TITLE_SCORE = 90  # near-exact match; safe to let two local files share a slot
MIN_TITLE_MATCH_SCORE = 55


def _apply_release_to_members(
    members: list[TrackMatch],
    detail: ReleaseDetail,
    group_confidence: int,
    group_artist_credit: str = "",
) -> None:
    """Assign each local file to its best-matching track on the resolved release.

    Folders that got tag-grouped into one album routinely contain the same song
    more than once (a genuine duplicate rip, or the same title pulled in from a
    B-sides/bonus disc alongside the main album) -- matching members against the
    tracklist in arbitrary file order and removing each claimed track as it goes
    meant whichever file happened to be processed first "won" a shared title and
    every other copy fell through to unmatched, even when it was an equally good
    (or better) match. Instead, every member is scored against the *full*
    tracklist up front, then claims are resolved strongest-match-first: a
    near-exact match (>=90) is allowed to share its slot with another equally
    confident claim on the same track (almost always real duplicates -- the
    planner's existing filename dedupe handles the resulting path collision),
    while more ambiguous matches (55-89) claim exclusively so a weak/unrelated
    match can't steal the slot from the file that's actually the better fit.
    """
    year = detail.date[:4] if detail.date and detail.date[:4].isdigit() else detail.date
    genre = "; ".join(detail.genres) if detail.genres else None

    scored: list[tuple[TrackMatch, Optional[ReleaseTrack], float]] = []
    for tm in members:
        local_title = tm.search_title or tm.resolved_tags.title or ""
        best_track: Optional[ReleaseTrack] = None
        best_score = -1.0
        for candidate_track in detail.tracks:
            score = fuzz.token_sort_ratio(
                local_title, candidate_track.title, processor=fuzz_utils.default_process
            )
            if score > best_score:
                best_score, best_track = score, candidate_track
        scored.append((tm, best_track, best_score))

    claimed_scores: dict[tuple[int, int], float] = {}
    for tm, best_track, best_score in sorted(scored, key=lambda s: s[2], reverse=True):
        resolved = TrackTags(
            artist=tm.resolved_tags.artist,
            # `group_artist_credit` comes from the recording-level search hit that won
            # the vote for this release group; `detail.artist_credit` is the specific
            # release's own artist-credit, which can be a region-locked pressing (e.g. a
            # Japanese-market edition) with a translated/transliterated artist name.
            # Preferring the recording-level one keeps the same artist filed under one
            # consistent folder name regardless of which pressing MusicBrainz picked.
            albumartist=group_artist_credit or detail.artist_credit or tm.resolved_tags.artist,
            album=detail.title,
            date=year,
            genre=genre or tm.resolved_tags.genre,
        )

        slot_key = (best_track.disc, best_track.position) if best_track else None
        slot_taken = slot_key is not None and slot_key in claimed_scores
        slot_available = not slot_taken or best_score >= DUPLICATE_TITLE_SCORE

        if best_track is not None and best_score >= MIN_TITLE_MATCH_SCORE and slot_available:
            claimed_scores.setdefault(slot_key, best_score)
            resolved.title = best_track.title
            resolved.tracknumber = str(best_track.position)
            resolved.discnumber = str(best_track.disc)
            resolved.totaltracks = str(
                sum(1 for t in detail.tracks if t.disc == best_track.disc)
            )
            tm.status = "matched" if tm.query_source == "tags" else "ai_matched"
            tm.confidence = max(group_confidence, int(best_score))
        else:
            resolved.title = tm.resolved_tags.title
            resolved.tracknumber = tm.resolved_tags.tracknumber
            resolved.discnumber = tm.resolved_tags.discnumber
            tm.status = "unmatched"
            tm.confidence = int(best_score) if best_score >= 0 else 0
            if slot_taken and best_score < DUPLICATE_TITLE_SCORE:
                tm.notes.append("Another file was a stronger match for this same track")
            else:
                tm.notes.append(
                    "Could not confidently match this track's title within the release"
                )

        tm.resolved_tags = resolved
        tm.release_group_mbid = detail.release_group_mbid
