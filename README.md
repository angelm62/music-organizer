# music-organizer

Reorganizes a music library that's been dumped flat into `Artist/` folders into a proper
`Artist/Album (Year)/NN - Title.ext` layout, and fixes the tags (artist, album, title, track
number, year, genre, cover art) so media players and NAS media servers show the right info.

It looks up canonical metadata on [MusicBrainz](https://musicbrainz.org) (free, no API key) and,
when a file's existing tags and filename are too messy to search with, falls back to a local
Ollama model to guess the artist/title/album from the filename.

## How it works

1. Scans every audio file under your library path (MP3, FLAC, M4A/AAC, OGG/Opus, WAV, WMA).
2. Reads existing tags. If `artist`/`title` are missing, asks your local Ollama model to parse
   them out of the filename and folder name.
3. Groups tracks into albums — either by an existing `album` tag, or (for artist folders with no
   album info) by having tracks vote on which MusicBrainz release-group they share in common, so
   an artist's whole discography sitting in one flat folder gets split back into separate albums.
4. Fetches the canonical release from MusicBrainz (title, year, track listing, genre) and cover
   art from the Cover Art Archive.
5. Prints a dry-run plan. Nothing is touched until you pass `--apply`.
6. On `--apply`: moves each file to `Artist/Album (Year)/NN - Title.ext`, rewrites its tags, and
   embeds the cover art (plus a `folder.jpg` per album directory).

Tracks it can't confidently match are **left where they are** and listed separately for manual
review — nothing is guessed into place.

## Setup

1. Mount the NAS share over SMB (Finder → Go → Connect to Server → `smb://your-nas/share`, or
   `mount_smbfs`), and note the local mount path (e.g. `/Volumes/Music`).
2. Make sure your Ollama server has a model pulled (`ollama pull <model>` on that machine) — the
   default assumes `10.27.27.190`, adjust in `.env` if needed.
3. Install:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e .
   ```

4. Copy `.env.example` to `.env` and fill in `LIBRARY_PATH`, `MUSICBRAINZ_CONTACT` (MusicBrainz
   requires a contact email/URL in the API User-Agent), and `OLLAMA_MODEL`.

## Usage

```bash
# Dry run on everything -- just prints/reports the plan, touches nothing
music-organizer

# Try it on a handful of files first
music-organizer --limit 20

# Once the plan looks right, actually move files and write tags
music-organizer --apply
```

Every run writes a full JSON report (`--report`, default `music_organizer_report.json`) with the
old/new path, resolved tags, match confidence, and notes for every track — useful for reviewing
before `--apply`, or for auditing what happened after.

Useful flags:
- `--library PATH` — override `LIBRARY_PATH` from `.env`
- `--no-ai` — skip the Ollama fallback (only use existing tags)
- `--no-musicbrainz` — skip MusicBrainz lookups entirely (offline, tag-normalization only)
- `--no-cover-art` — don't fetch/embed cover art
- `--cache PATH` — where to keep the resolution cache (reused across reruns so interrupted runs
  and MusicBrainz's 1 req/sec rate limit don't mean starting over)

Large libraries take a while on first run because of MusicBrainz's rate limit — that's expected;
let it run, and reruns will reuse the cache for anything already resolved.

## Notes

- WAV and WMA support is included but is less common in practice than MP3/FLAC/M4A — verify on a
  `--limit` sample of your own files before running on the whole library.
- Files that already have accurate tags but no album folder still get moved and get their tags
  normalized/filled in (genre, cover art, canonical spelling) from MusicBrainz.
