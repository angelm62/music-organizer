from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from rapidfuzz import fuzz
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from .cache import JsonCache
from .config import Config
from .console import console
from .musicbrainz_client import MusicBrainzClient, ReleaseCandidate, ReleaseDetail
from .ollama_client import OllamaClient
from .tags import TrackTags, read_tags

FILENAME_JUNK_RE = re.compile(
    r"\[(.*?)\]|\((?:official|lyrics?|audio|video|hq|hd|remaster\w*)\)|\d{2,4}\s?kbps",
    re.IGNORECASE,
)
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


def match_all(
    paths: list[str], config: Config, cache: Optional[JsonCache] = None
) -> list[TrackMatch]:
    cache = cache or JsonCache(config.cache_path)
    mb = MusicBrainzClient(config, cache) if config.use_musicbrainz else None
    ollama = OllamaClient(config, cache) if config.use_ai_fallback else None

    tracks: list[TrackMatch] = []
    candidates_by_path: dict[str, list[ReleaseCandidate]] = {}

    with _progress() as progress:
        search_task = progress.add_task("Looking up tracks on MusicBrainz", total=len(paths))
        for path in paths:
            tags = read_tags(path)
            artist, title, album, source = resolve_query(path, tags, config, ollama)
            query_tags = TrackTags(
                artist=artist or None, title=title or None, album=album or tags.album
            )

            candidates: list[ReleaseCandidate] = []
            if mb is not None and artist and title:
                candidates = mb.search_recording(artist, title, album or None)
            candidates_by_path[path] = _rank(candidates)

            tm = TrackMatch(
                path=path,
                original_tags=tags,
                resolved_tags=tags.merged_with(query_tags),
                query_source=source,
            )
            tracks.append(tm)
            progress.advance(search_task)

        _group_and_resolve(tracks, candidates_by_path, mb, progress)

    cache.save()
    return tracks


TOP_N_CANDIDATES = 5


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
    _apply_release_to_members(members, detail, confidence)


def _apply_release_to_members(
    members: list[TrackMatch], detail: ReleaseDetail, group_confidence: int
) -> None:
    year = detail.date[:4] if detail.date and detail.date[:4].isdigit() else detail.date
    genre = "; ".join(detail.genres) if detail.genres else None
    unused_tracks = list(detail.tracks)

    for tm in members:
        local_title = tm.resolved_tags.title or ""
        best_track = None
        best_score = -1.0
        for candidate_track in unused_tracks:
            score = fuzz.token_sort_ratio(local_title, candidate_track.title)
            if score > best_score:
                best_score, best_track = score, candidate_track

        resolved = TrackTags(
            artist=tm.resolved_tags.artist,
            albumartist=detail.artist_credit or tm.resolved_tags.artist,
            album=detail.title,
            date=year,
            genre=genre or tm.resolved_tags.genre,
        )

        if best_track is not None and best_score >= 55:
            unused_tracks.remove(best_track)
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
            tm.notes.append("Could not confidently match this track's title within the release")

        tm.resolved_tags = resolved
        tm.release_group_mbid = detail.release_group_mbid
