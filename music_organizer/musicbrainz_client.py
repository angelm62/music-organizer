from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from .cache import JsonCache
from .config import Config

MB_BASE = "https://musicbrainz.org/ws/2"
COVER_ART_BASE = "https://coverartarchive.org"


@dataclass
class ReleaseCandidate:
    release_mbid: str
    release_group_mbid: str
    release_title: str
    artist_credit: str
    date: Optional[str]
    track_count: Optional[int]
    status: Optional[str]
    primary_type: Optional[str]
    score: int


@dataclass
class ReleaseTrack:
    position: int
    disc: int
    title: str
    length_ms: Optional[int] = None


@dataclass
class ReleaseDetail:
    release_mbid: str
    release_group_mbid: str
    title: str
    artist_credit: str
    date: Optional[str]
    genres: list[str] = field(default_factory=list)
    tracks: list[ReleaseTrack] = field(default_factory=list)


class MusicBrainzClient:
    def __init__(self, config: Config, cache: Optional[JsonCache] = None):
        self.config = config
        self.cache = cache
        self._last_request = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config.musicbrainz_user_agent

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        wait = self.config.musicbrainz_rate_limit - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _get(self, path: str, params: dict) -> Optional[dict]:
        self._throttle()
        try:
            resp = self.session.get(f"{MB_BASE}/{path}", params=params, timeout=15)
            if resp.status_code == 503:
                time.sleep(2)
                self._throttle()
                resp = self.session.get(f"{MB_BASE}/{path}", params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            return None

    def search_recording(
        self, artist: str, title: str, album_hint: Optional[str] = None
    ) -> list[ReleaseCandidate]:
        cache_key = f"{artist.lower()}|{title.lower()}|{(album_hint or '').lower()}"
        if self.cache:
            cached = self.cache.get("mb_search", cache_key)
            if cached is not None:
                return [ReleaseCandidate(**c) for c in cached]

        base_parts = [f'recording:"{_escape(title)}"', f'artist:"{_escape(artist)}"']
        strict_filters = [
            "status:official",
            "type:album",
            "NOT secondarytype:compilation",
            "NOT secondarytype:live",
            "NOT secondarytype:interview",
            "NOT secondarytype:spokenword",
            "NOT secondarytype:audiobook",
        ]

        # Unfiltered searches for prolific artists (e.g. The Beatles) return hundreds
        # of bootlegs/compilations tied at the top score, which drowns out the real
        # studio album. Try a tight, server-side-filtered query first -- official
        # studio albums only -- and only fall back to looser queries (singles, EPs,
        # unofficial-but-only-known releases, etc.) if that comes up empty.
        #
        # The release_hint clause only goes in leading tiers, never baked into every
        # tier: local album tags routinely carry ripper/label suffixes ("Sea Change
        # (MOFI)", "Abbey Road [FLAC]") that don't match MusicBrainz's canonical
        # release title, so a hint that's wrong would otherwise kill every fallback
        # tier along with it and return zero results.
        tiers = []
        if album_hint:
            release_clause = f'release:"{_escape(album_hint)}"'
            tiers.append(base_parts + [release_clause] + strict_filters)
            tiers.append(base_parts + [release_clause])
        tiers.append(base_parts + strict_filters)
        tiers.append(base_parts + ["status:official"])
        tiers.append(base_parts)

        candidates: list[ReleaseCandidate] = []
        for query_parts in tiers:
            data = self._get(
                "recording",
                {"query": " AND ".join(query_parts), "fmt": "json", "limit": 15, "inc": "releases"},
            )
            candidates = _parse_recordings(data, artist)
            if candidates:
                break

        if self.cache:
            self.cache.set(
                "mb_search",
                cache_key,
                [c.__dict__ for c in candidates],
            )
        return candidates

    def get_release_detail(self, release_mbid: str) -> Optional[ReleaseDetail]:
        if self.cache:
            cached = self.cache.get("mb_release", release_mbid)
            if cached is not None:
                cached = dict(cached)
                cached["tracks"] = [ReleaseTrack(**t) for t in cached["tracks"]]
                return ReleaseDetail(**cached)

        data = self._get(
            f"release/{release_mbid}",
            {"fmt": "json", "inc": "recordings+release-groups+genres+artist-credits"},
        )
        if not data:
            return None

        artist_credit = "".join(
            c.get("name", "") + c.get("joinphrase", "") for c in data.get("artist-credit", [])
        )
        genres = sorted(
            {g.get("name", "") for g in data.get("genres", []) if g.get("name")},
            key=lambda g: -next(
                (x.get("count", 0) for x in data.get("genres", []) if x.get("name") == g), 0
            ),
        )[:3]

        tracks: list[ReleaseTrack] = []
        for medium in data.get("media", []):
            disc_no = medium.get("position", 1)
            for track in medium.get("tracks", []):
                tracks.append(
                    ReleaseTrack(
                        position=track.get("position", 0),
                        disc=disc_no,
                        title=track.get("title", ""),
                        length_ms=track.get("length"),
                    )
                )

        detail = ReleaseDetail(
            release_mbid=release_mbid,
            release_group_mbid=data.get("release-group", {}).get("id", ""),
            title=data.get("title", ""),
            artist_credit=artist_credit,
            date=data.get("date"),
            genres=genres,
            tracks=tracks,
        )

        if self.cache:
            payload = {
                "release_mbid": detail.release_mbid,
                "release_group_mbid": detail.release_group_mbid,
                "title": detail.title,
                "artist_credit": detail.artist_credit,
                "date": detail.date,
                "genres": detail.genres,
                "tracks": [t.__dict__ for t in detail.tracks],
            }
            self.cache.set("mb_release", release_mbid, payload)
        return detail

    def get_cover_art(self, release_group_mbid: str) -> Optional[tuple[bytes, str]]:
        """Fetch front cover art bytes + mime type for a release group, if available."""
        if self.cache:
            cached = self.cache.get("mb_cover_missing", release_group_mbid)
            if cached:
                return None

        try:
            resp = self.session.get(
                f"{COVER_ART_BASE}/release-group/{release_group_mbid}/front",
                timeout=20,
                allow_redirects=True,
            )
            if resp.status_code == 200:
                mime = resp.headers.get("Content-Type", "image/jpeg")
                return resp.content, mime
        except requests.RequestException:
            pass

        if self.cache:
            self.cache.set("mb_cover_missing", release_group_mbid, True)
        return None


def _escape(value: str) -> str:
    for ch in '+-&&||!(){}[]^"~*?:\\/':
        value = value.replace(ch, " ")
    return value.strip()


def _parse_recordings(data: Optional[dict], artist_fallback: str) -> list[ReleaseCandidate]:
    candidates: list[ReleaseCandidate] = []
    if not data:
        return candidates
    for rec in data.get("recordings", []):
        score = int(rec.get("score", 0))
        artist_credit = "".join(
            c.get("name", "") + c.get("joinphrase", "") for c in rec.get("artist-credit", [])
        ) or artist_fallback
        for rel in rec.get("releases", []):
            rg = rel.get("release-group", {})
            media = rel.get("media", [])
            track_count = sum(m.get("track-count", 0) for m in media) or None
            candidates.append(
                ReleaseCandidate(
                    release_mbid=rel.get("id", ""),
                    release_group_mbid=rg.get("id", ""),
                    release_title=rel.get("title", ""),
                    artist_credit=artist_credit,
                    date=rel.get("date"),
                    track_count=track_count,
                    status=rel.get("status"),
                    primary_type=rg.get("primary-type"),
                    score=score,
                )
            )
    return candidates
