from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Optional

from mutagen import File as MutagenFile
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, APIC, ID3NoHeaderError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis
from mutagen.asf import ASF
from mutagen.wave import WAVE

VORBIS_EXTS = {".ogg", ".oga", ".opus"}
MP4_EXTS = {".m4a", ".aac"}


@dataclass
class TrackTags:
    artist: Optional[str] = None
    albumartist: Optional[str] = None
    album: Optional[str] = None
    title: Optional[str] = None
    tracknumber: Optional[str] = None  # "n" or "n/total"
    totaltracks: Optional[str] = None
    discnumber: Optional[str] = None
    date: Optional[str] = None  # year, "YYYY"
    genre: Optional[str] = None

    def is_mostly_complete(self) -> bool:
        return bool(self.artist and self.title)

    def is_fully_tagged(self) -> bool:
        """True if the file's own tags are complete enough to organize by directly,
        with no MusicBrainz lookup needed (and no risk of MusicBrainz's fuzzy
        matching swapping in a wrong album/track for an otherwise-fine file)."""
        return bool(self.artist and self.title and self.album and self.tracknumber)

    def merged_with(self, other: "TrackTags") -> "TrackTags":
        """Return a copy of self with blanks filled in from `other`."""
        kwargs = {}
        for f in fields(self):
            mine = getattr(self, f.name)
            kwargs[f.name] = mine if mine else getattr(other, f.name)
        return TrackTags(**kwargs)


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def _is_io_error(exc: BaseException) -> bool:
    """True if `exc` is, or wraps, an OSError (missing file, permission denied,
    dropped network share, etc.) rather than a genuine "this file's tags/headers
    are corrupt" parsing failure. mutagen wraps the underlying OSError in its own
    MutagenError rather than subclassing OSError, so a plain isinstance check on
    the caught exception isn't enough -- its __cause__/__context__ has to be
    checked too."""
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, OSError):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


def read_tags(path: str) -> TrackTags:
    """Read tags from `path`.

    Deliberately lets OSError (file vanished, permission denied, network share
    dropped mid-read, etc.) propagate instead of swallowing it: a track that
    genuinely has no tags and a track that couldn't be *read* need different
    handling upstream -- treating a dropped NAS mount as "this file has no
    metadata" silently turns a connectivity problem into hundreds of bogus
    "no match found" results with no indication anything was wrong.
    """
    ext = _ext(path)
    try:
        if ext == ".wma":
            return _read_asf(path)
        if ext == ".wav":
            return _read_wave(path)
        audio = MutagenFile(path, easy=True)
        if audio is None:
            return TrackTags()
        tags = audio.tags or {}

        def first(key: str) -> Optional[str]:
            val = tags.get(key)
            if not val:
                return None
            return str(val[0]) if isinstance(val, list) else str(val)

        track_raw = first("tracknumber")
        track_no, total_tracks = _split_slash(track_raw)
        disc_raw = first("discnumber")
        disc_no, _ = _split_slash(disc_raw)

        return TrackTags(
            artist=first("artist"),
            albumartist=first("albumartist"),
            album=first("album"),
            title=first("title"),
            tracknumber=track_no,
            totaltracks=total_tracks,
            discnumber=disc_no,
            date=_year_only(first("date")),
            genre=first("genre"),
        )
    except Exception as exc:
        if _is_io_error(exc):
            raise OSError(str(exc)) from exc
        return TrackTags()


def _split_slash(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not value:
        return None, None
    parts = value.split("/", 1)
    num = parts[0].strip() or None
    total = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return num, total


def _year_only(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return value[:4] if len(value) >= 4 and value[:4].isdigit() else value


def _read_asf(path: str) -> TrackTags:
    audio = ASF(path)
    tags = audio.tags or {}

    def first(key: str) -> Optional[str]:
        val = tags.get(key)
        if not val:
            return None
        return str(val[0])

    track_no, total_tracks = _split_slash(first("WM/TrackNumber"))
    disc_no, _ = _split_slash(first("WM/PartOfSet"))

    return TrackTags(
        artist=first("Author"),
        albumartist=first("WM/AlbumArtist"),
        album=first("WM/AlbumTitle"),
        title=first("Title"),
        tracknumber=track_no,
        totaltracks=total_tracks,
        discnumber=disc_no,
        date=_year_only(first("WM/Year")),
        genre=first("WM/Genre"),
    )


def _read_wave(path: str) -> TrackTags:
    audio = WAVE(path)
    id3 = audio.tags
    if id3 is None:
        return TrackTags()

    def first(frame_id: str) -> Optional[str]:
        frame = id3.get(frame_id)
        if frame is None or not getattr(frame, "text", None):
            return None
        return str(frame.text[0])

    track_no, total_tracks = _split_slash(first("TRCK"))
    disc_no, _ = _split_slash(first("TPOS"))

    return TrackTags(
        artist=first("TPE1"),
        albumartist=first("TPE2"),
        album=first("TALB"),
        title=first("TIT2"),
        tracknumber=track_no,
        totaltracks=total_tracks,
        discnumber=disc_no,
        date=_year_only(first("TDRC") or first("TYER")),
        genre=first("TCON"),
    )


def write_tags(path: str, tags: TrackTags) -> None:
    ext = _ext(path)
    if ext == ".mp3":
        _write_easy(path, tags, MP3, EasyID3, ensure_id3=True)
    elif ext == ".flac":
        _write_vorbis_like(path, tags, FLAC(path))
    elif ext in MP4_EXTS:
        _write_mp4(path, tags)
    elif ext in VORBIS_EXTS:
        cls = OggOpus if ext == ".opus" else OggVorbis
        _write_vorbis_like(path, tags, cls(path))
    elif ext == ".wma":
        _write_asf(path, tags)
    elif ext == ".wav":
        _write_wave(path, tags)
    else:
        raise ValueError(f"Unsupported audio format: {ext}")


def _write_easy(path: str, tags: TrackTags, media_cls, easy_cls, ensure_id3: bool) -> None:
    try:
        audio = easy_cls(path)
    except ID3NoHeaderError:
        audio = media_cls(path)
        audio.add_tags()
        audio = easy_cls(path)

    _set_easy(audio, "artist", tags.artist)
    _set_easy(audio, "albumartist", tags.albumartist)
    _set_easy(audio, "album", tags.album)
    _set_easy(audio, "title", tags.title)
    _set_easy(audio, "genre", tags.genre)
    _set_easy(audio, "date", tags.date)
    _set_easy(audio, "tracknumber", _join_slash(tags.tracknumber, tags.totaltracks))
    _set_easy(audio, "discnumber", tags.discnumber)
    audio.save()


def _set_easy(audio, key: str, value: Optional[str]) -> None:
    if value:
        audio[key] = [value]


def _join_slash(num: Optional[str], total: Optional[str]) -> Optional[str]:
    if not num:
        return None
    return f"{num}/{total}" if total else num


def _write_vorbis_like(path: str, tags: TrackTags, audio) -> None:
    def setv(key: str, value: Optional[str]) -> None:
        if value:
            audio[key] = [value]
        elif key in audio:
            del audio[key]

    setv("artist", tags.artist)
    setv("albumartist", tags.albumartist)
    setv("album", tags.album)
    setv("title", tags.title)
    setv("genre", tags.genre)
    setv("date", tags.date)
    setv("tracknumber", _join_slash(tags.tracknumber, tags.totaltracks))
    setv("discnumber", tags.discnumber)
    audio.save()


def _write_mp4(path: str, tags: TrackTags) -> None:
    audio = MP4(path)
    if tags.artist:
        audio["\xa9ART"] = [tags.artist]
    if tags.albumartist:
        audio["aART"] = [tags.albumartist]
    if tags.album:
        audio["\xa9alb"] = [tags.album]
    if tags.title:
        audio["\xa9nam"] = [tags.title]
    if tags.genre:
        audio["\xa9gen"] = [tags.genre]
    if tags.date:
        audio["\xa9day"] = [tags.date]
    if tags.tracknumber:
        try:
            trkn = int(tags.tracknumber)
            total = int(tags.totaltracks) if tags.totaltracks else 0
            audio["trkn"] = [(trkn, total)]
        except ValueError:
            pass
    if tags.discnumber:
        try:
            audio["disk"] = [(int(tags.discnumber), 0)]
        except ValueError:
            pass
    audio.save()


def _write_asf(path: str, tags: TrackTags) -> None:
    audio = ASF(path)
    if tags.artist:
        audio["Author"] = [tags.artist]
    if tags.albumartist:
        audio["WM/AlbumArtist"] = [tags.albumartist]
    if tags.album:
        audio["WM/AlbumTitle"] = [tags.album]
    if tags.title:
        audio["Title"] = [tags.title]
    if tags.genre:
        audio["WM/Genre"] = [tags.genre]
    if tags.date:
        audio["WM/Year"] = [tags.date]
    if tags.tracknumber:
        audio["WM/TrackNumber"] = [tags.tracknumber]
    if tags.discnumber:
        audio["WM/PartOfSet"] = [tags.discnumber]
    audio.save()


def _write_wave(path: str, tags: TrackTags) -> None:
    audio = WAVE(path)
    if audio.tags is None:
        audio.add_tags()
    id3 = audio.tags
    from mutagen.id3 import TPE1, TPE2, TALB, TIT2, TCON, TDRC, TRCK, TPOS

    if tags.artist:
        id3.setall("TPE1", [TPE1(encoding=3, text=[tags.artist])])
    if tags.albumartist:
        id3.setall("TPE2", [TPE2(encoding=3, text=[tags.albumartist])])
    if tags.album:
        id3.setall("TALB", [TALB(encoding=3, text=[tags.album])])
    if tags.title:
        id3.setall("TIT2", [TIT2(encoding=3, text=[tags.title])])
    if tags.genre:
        id3.setall("TCON", [TCON(encoding=3, text=[tags.genre])])
    if tags.date:
        id3.setall("TDRC", [TDRC(encoding=3, text=[tags.date])])
    if tags.tracknumber:
        id3.setall("TRCK", [TRCK(encoding=3, text=[_join_slash(tags.tracknumber, tags.totaltracks)])])
    if tags.discnumber:
        id3.setall("TPOS", [TPOS(encoding=3, text=[tags.discnumber])])
    audio.save()


def embed_cover(path: str, image_bytes: bytes, mime: str = "image/jpeg") -> None:
    ext = _ext(path)
    if ext == ".mp3":
        _embed_cover_id3(path, image_bytes, mime)
    elif ext == ".flac":
        _embed_cover_flac(path, image_bytes, mime)
    elif ext in MP4_EXTS:
        _embed_cover_mp4(path, image_bytes, mime)
    elif ext in VORBIS_EXTS:
        _embed_cover_vorbis(path, image_bytes, mime, ext)
    elif ext == ".wma":
        _embed_cover_asf(path, image_bytes, mime)
    elif ext == ".wav":
        _embed_cover_id3(path, image_bytes, mime, wave=True)
    # No cover-art container for other/unknown formats; silently skip.


def _embed_cover_id3(path: str, image_bytes: bytes, mime: str, wave: bool = False) -> None:
    if wave:
        audio = WAVE(path)
        if audio.tags is None:
            audio.add_tags()
        id3 = audio.tags
    else:
        try:
            id3 = ID3(path)
        except ID3NoHeaderError:
            id3 = ID3()
    id3.delall("APIC")
    id3.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=image_bytes))
    if wave:
        audio.save()
    else:
        id3.save(path)


def _embed_cover_flac(path: str, image_bytes: bytes, mime: str) -> None:
    audio = FLAC(path)
    pic = Picture()
    pic.type = 3
    pic.mime = mime
    pic.data = image_bytes
    audio.clear_pictures()
    audio.add_picture(pic)
    audio.save()


def _embed_cover_mp4(path: str, image_bytes: bytes, mime: str) -> None:
    audio = MP4(path)
    fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
    audio["covr"] = [MP4Cover(image_bytes, imageformat=fmt)]
    audio.save()


def _embed_cover_vorbis(path: str, image_bytes: bytes, mime: str, ext: str) -> None:
    import base64

    cls = OggOpus if ext == ".opus" else OggVorbis
    audio = cls(path)
    pic = Picture()
    pic.type = 3
    pic.mime = mime
    pic.data = image_bytes
    encoded = base64.b64encode(pic.write()).decode("ascii")
    audio["metadata_block_picture"] = [encoded]
    audio.save()


def _embed_cover_asf(path: str, image_bytes: bytes, mime: str) -> None:
    import struct

    from mutagen.asf import ASFByteArrayAttribute

    # WM/Picture binary layout: type(1) + size(4 LE) + mime(utf-16le, NUL) +
    # description(utf-16le, NUL) + image data.
    mime_bytes = mime.encode("utf-16-le") + b"\x00\x00"
    desc_bytes = "Cover".encode("utf-16-le") + b"\x00\x00"
    payload = (
        struct.pack("<b", 3)
        + struct.pack("<I", len(image_bytes))
        + mime_bytes
        + desc_bytes
        + image_bytes
    )
    audio = ASF(path)
    audio["WM/Picture"] = [ASFByteArrayAttribute(payload)]
    audio.save()
