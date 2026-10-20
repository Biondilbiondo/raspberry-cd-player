#!/usr/bin/env python3
"""
metadata_manager.py — Fetch album metadata and artwork.

Lookup order for a physical CD:
  0. disc_overrides.json  (manual corrections — highest priority)
  1. SQLite cache         (instant on re-insert)
  2. MusicBrainz disc-ID lookup  → artist / album / year / tracks
  3. iTunes Search API           → art + second opinion on artist/album
  4. Deezer API                  → art fallback
  5. Unknown fallback

Consensus rule: if MusicBrainz returns a result and iTunes independently
finds the same artist (fuzzy match), the result is treated as confirmed.
If only one source returns a result it is used as-is.

To correct a wrong album, edit /opt/musicplayer/disc_overrides.json:
  {
    "<cddb_id>": {
      "artist": "Artist Name",
      "album":  "Album Title",
      "year":   "2001",
      "tracks": [{"num": 1, "title": "Track Name"}, ...]
    }
  }
"""

import hashlib
import json
import logging
import os
import sqlite3
import time
from datetime import datetime
from typing import Dict, List, Optional
import re
import subprocess
import discid
import musicbrainzngs
import configparser

import requests
from mutagen.id3 import APIC, TIT2, TALB, TPE1, TDRC, TXXX, UFID

log = logging.getLogger("metadata")
logging.getLogger("musicbrainzngs").setLevel(logging.ERROR)

config = configparser.ConfigParser()
config.read('config.ini')

CD_VAULT = config.get('storage', 'vault_dir')
# ── Paths ─────────────────────────────────────────────────────────────────────
CACHE_DIR      = config.get('storage', 'art_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

# ── API endpoints ─────────────────────────────────────────────────────────────
MB_BASE      = "https://musicbrainz.org/ws/2"
CAA_BASE     = "https://coverartarchive.org"
ITUNES_URL   = "https://itunes.apple.com/search"
DEEZER_URL   = "https://api.deezer.com/search/album"
DISCOGS_BASE = "https://api.discogs.com"

MB_AGENT = config.get('musicbrainz', 'agent') 
MB_VERSION = config.get('musicbrainz', 'version')
MB_MAIL = config.get('musicbrainz', 'mail')
musicbrainzngs.set_useragent(MB_AGENT, MB_VERSION, MB_MAIL)

HEADERS = {
    "User-Agent": f"{MB_AGENT}/{MB_VERSION} ({MB_MAIL})",
    "Accept":      "application/json",
}
DISCOGS_TOKEN = os.environ.get("DISCOGS_TOKEN", "")

# ── HTTP helpers ──────────────────────────────────────────────────────────────
def _http_get_json(url: str, params: dict = None,
                   extra_headers: dict = None, retry: int = 2) -> Optional[dict]:
    h = {**HEADERS, **(extra_headers or {})}
    for attempt in range(retry):
        try:
            r = requests.get(url, params=params, headers=h, timeout=12)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.HTTPError as e:
            log.debug("HTTP %s for %s", e.response.status_code, url)
            return None
        except Exception as e:
            log.debug("GET %s attempt %d: %s", url, attempt + 1, e)
            if attempt < retry - 1:
                time.sleep(1)
    return None


def _download_image(url: str, cache_key: str) -> Optional[str]:
    ext  = ".jpg" if any(x in url.lower() for x in ("jpg", "jpeg")) else ".png"
    name = hashlib.md5(cache_key.encode()).hexdigest() + ext
    path = os.path.join(CACHE_DIR, name)
    if os.path.exists(path):
        return path
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, stream=True)
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)
        log.info("Art cached: %s", path)
        return path
    except Exception as e:
        log.debug("Image download failed: %s", e)
        return None


# ── iTunes Search API ─────────────────────────────────────────────────────────
def _fetch_from_itunes(artist: str, album: str,
                       cache_key: str) -> Optional[Dict]:
    """
    Search iTunes for artist+album.
    Returns dict with art_path (and optionally track list) or None.
    """
    q = f"{artist} {album}".strip()
    try:
        r = requests.get(
            ITUNES_URL,
            params={"term": q, "media": "music", "entity": "album", "limit": 5},
            headers=HEADERS, timeout=8,
        )
        if not r.ok:
            return None
        results = r.json().get("results", [])
    except Exception as e:
        log.debug("iTunes search failed: %s", e)
        return None

    for res in results:
        itunes_artist = res.get("artistName", "")
        itunes_album  = res.get("collectionName", "")
        art_url = res.get("artworkUrl100", "").replace("100x100bb", "600x600bb")

        if not art_url:
            continue

        # Light validation: artist name must overlap with what we expect
        if artist and not _names_overlap(artist, itunes_artist):
            log.debug("iTunes artist mismatch: '%s' vs '%s'", artist, itunes_artist)
            continue

        art_path = _download_image(art_url, f"itunes_{cache_key}")
        if not art_path:
            continue

        log.info("iTunes confirmed: %s – %s", itunes_artist, itunes_album)
        return {
            "itunes_artist": itunes_artist,
            "itunes_album":  itunes_album,
            "art_path":      art_path,
        }

    # Nothing matched — still try to grab art from the first result if present
    for res in results:
        art_url = res.get("artworkUrl100", "").replace("100x100bb", "600x600bb")
        if art_url:
            art_path = _download_image(art_url, f"itunes_loose_{cache_key}")
            if art_path:
                log.info("iTunes art (loose match)")
                return {"art_path": art_path}

    return None


# ── Deezer ────────────────────────────────────────────────────────────────────
def _fetch_art_deezer(artist: str, album: str, cache_key: str) -> Optional[str]:
    try:
        r = requests.get(
            DEEZER_URL,
            params={"q": f"{artist} {album}".strip(), "limit": 5},
            headers=HEADERS, timeout=8,
        )
        if r.ok:
            for res in r.json().get("data", []):
                url = res.get("cover_xl") or res.get("cover_big")
                if url:
                    path = _download_image(url, f"deezer_{cache_key}")
                    if path:
                        log.info("Art from Deezer")
                        return path
    except Exception as e:
        log.debug("Deezer art: %s", e)
    return None


# ── MusicBrainz ───────────────────────────────────────────────────────────────
def _mb_extract_artist(release: dict) -> str:
    credits = release.get("artist-credit", [])
    if credits and isinstance(credits[0], dict):
        return credits[0].get("artist", {}).get("name", "")
    return ""


def _mb_extract_tracks(release: dict) -> List[Dict]:
    tracks = []
    for medium in release.get("medium-list", []):
        for t in medium.get("track-list", []):
            rec    = t.get("recording", {})
            dur_ms = rec.get("length") or t.get("length") or 0
            dur_s  = int(dur_ms) // 1000
            tracks.append({
                "num":      int(t.get("position", len(tracks) + 1)),
                "title":    rec.get("title") or t.get("title", f"Track {len(tracks)+1}"),
                "duration": f"{dur_s // 60}:{dur_s % 60:02d}" if dur_s else "",
            })
    return tracks


def _mb_pick_release(releases: list) -> dict:
    """Pick the earliest release (original, not a remaster/deluxe box)."""
    def key(r):
        date  = r.get("date", "") or ""
        year  = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else 9999
        first = len((r.get("media") or [{}])[0].get("tracks", []))
        return (year, -first)
    return min(releases, key=key)


def _fetch_from_mb(mb_disc_id: str, num_tracks: int,
                   offsets: List[int], total_secs: int,
                   raw_tracks: List[Dict]) -> Optional[Dict]:
    releases: List[Dict] = []
    _INCLUDES = ["recordings", "artists", "artist-credits", "release-groups"]

    # 1. Exact disc-ID lookup
    log.info("MB exact lookup: %s", mb_disc_id)
    try:
        data = musicbrainzngs.get_releases_by_discid(mb_disc_id, includes=_INCLUDES)
        releases = data.get("disc", {}).get("release-list", [])
    except musicbrainzngs.ResponseError:
        pass  # 404: disc ID not found, fall through to the TOC lookup
    except musicbrainzngs.WebServiceError as exc:
        log.warning("MB disc-ID lookup failed: %s", exc)

    # 2. Fuzzy TOC lookup
    if not releases:
        toc_str = f"1 {num_tracks} {total_secs * 75} " + " ".join(str(o) for o in offsets)
        log.info("MB TOC lookup: %s…", toc_str[:50])
        try:
            data = musicbrainzngs.get_releases_by_discid("-", toc=toc_str, includes=_INCLUDES)
            releases = data.get("release-list", [])
        except musicbrainzngs.WebServiceError as exc:
            log.warning("MB TOC lookup failed: %s", exc)
            return None

    if not releases:
        return None

    best = _mb_pick_release(releases)
    mbid = best.get("id", "")

    # Fetch full details if we only got a stub
    if mbid and not best.get("medium-list"):
        try:
            best = musicbrainzngs.get_release_by_id(mbid, includes=_INCLUDES)["release"]
        except musicbrainzngs.WebServiceError as exc:
            log.warning("MB release fetch failed: %s", exc)

    artist = _mb_extract_artist(best)
    album = best.get("title", "")
    year = (best.get("date") or "")[:4]
    tracks = _mb_extract_tracks(best) or raw_tracks

    if not artist or not album:
        return None

    log.info("MB found: %s – %s (%s)", artist, album, year)
    return {"artist": artist, "album": album, "year": year,
            "tracks": tracks, "mbid": mbid}


def _fetch_from_mb_by_metadata(artist: str, album: str,
                               raw_tracks: List[Dict],
                               min_score: float = 80.0) -> Optional[Dict]:
    def _norm(s: str) -> str:
        """Lowercase, strip punctuation/whitespace noise for comparison."""
        return re.sub(r"[^\w]+", " ", (s or "").lower()).strip()


    def _sim(a: str, b: str) -> float:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


    def _score_release(rel: Dict, artist: str, album: str, num_tracks: int) -> float:
        score = 0.0
        score += _sim(rel.get("title", ""), album) * 50
        score += _sim(rel.get("artist-credit-phrase", ""), artist) * 30
        score += float(rel.get("ext:score", 0)) / 100 * 10   # MB's own relevance score

        if rel.get("status") == "Official":
            score += 5
        if rel.get("date"):
            score += 2

        # Prefer releases whose track count matches what we already have
        try:
            if int(rel.get("medium-track-count", -1)) == num_tracks:
                score += 15
        except (TypeError, ValueError):
            pass
        return score


    def _flatten_tracks(release: Dict, num_tracks: int) -> List[Dict]:
        """Extract tracks, preferring a single medium that matches num_tracks."""
        media = release.get("medium-list", [])

        def _medium_tracks(medium: Dict) -> List[Dict]:
            out = []
            for t in medium.get("track-list", []):
                rec = t.get("recording", {})
                out.append({
                    "disc": int(medium.get("position", 1)),
                    "number": t.get("number") or t.get("position"),
                    "title": rec.get("title") or t.get("title", ""),
                    "artist": t.get("artist-credit-phrase")
                            or rec.get("artist-credit-phrase", ""),
                    "length_ms": int(t["length"]) if t.get("length") else None,
                    "recording_id": rec.get("id"),
                })
            return out

        for medium in media:
            if len(medium.get("track-list", [])) == num_tracks:
                return _medium_tracks(medium)

        # No single medium matches: return everything across all discs
        return [t for m in media for t in _medium_tracks(m)]
    
    _INCLUDES = ["recordings", "artists", "artist-credits", "release-groups"]
    num_tracks = len(raw_tracks)

    # 1. Search by artist + album
    log.info("MB search: %s – %s", artist, album)
    try:
        data = musicbrainzngs.search_releases(artist=artist, release=album, limit=15)
    except musicbrainzngs.WebServiceError as exc:
        log.warning("MB search failed: %s", exc)
        return None

    releases = data.get("release-list", [])
    if not releases:
        return None

    # 2. Score candidates and pick the best one
    scored = sorted(
        ((_score_release(r, artist, album, num_tracks), r) for r in releases),
        key=lambda x: x[0], reverse=True,
    )
    best_score, best = scored[0]
    log.info("MB best candidate: %s – %s (score %.1f)",
             best.get("artist-credit-phrase"), best.get("title"), best_score)

    if best_score < min_score:
        log.info("MB best score below threshold (%.1f), rejecting", min_score)
        return None

    mbid = best.get("id", "")

    # 3. Fetch full release (search results don't include track lists)
    try:
        full = musicbrainzngs.get_release_by_id(mbid, includes=_INCLUDES)["release"]
    except musicbrainzngs.WebServiceError as exc:
        log.warning("MB release fetch failed: %s", exc)
        return None

    # 4. Build cleaned result
    mb_tracks = _flatten_tracks(full, num_tracks)
    tracks_matched = len(mb_tracks) == num_tracks

    clean_artist = full.get("artist-credit-phrase", "")
    clean_album = full.get("title", "")
    year = (full.get("date") or "")[:4]

    if not clean_artist or not clean_album:
        return None

    log.info("MB found: %s – %s (%s)", clean_artist, clean_album, year)
    return {
        "artist": clean_artist,
        "album": clean_album,
        "year": year,
        "tracks": mb_tracks if tracks_matched else raw_tracks,
        "tracks_matched": tracks_matched,
        "mbid": mbid,
        "score": round(best_score, 1),
    }


def _fetch_art_caa(mbid: str, cache_key: str) -> Optional[str]:
    """Try MusicBrainz Cover Art Archive."""
    if not mbid:
        return None
    try:
        data = _http_get_json(f"{CAA_BASE}/release/{mbid}")
        if data:
            for img in data.get("images", []):
                if img.get("front"):
                    return _download_image(img["image"], f"caa_{cache_key}")
    except Exception as e:
        log.debug("CAA art: %s", e)
    return None

def _names_overlap(a: str, b: str, threshold: float = 0.5) -> bool:
    """True if the two names share enough words to be considered the same."""
    a, b = a.lower().strip(), b.lower().strip()
    if a == b:
        return True
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return False
    shorter = wa if len(wa) <= len(wb) else wb
    return len(shorter & (wa | wb)) / len(shorter) >= threshold

def _fetch_from_discogs(artist: str, album: str) -> Optional[Dict]:
    if not DISCOGS_TOKEN:
        return None
    try:
        data = requests.get(f"{DISCOGS_BASE}/database/search", params={
            "artist": artist, "release_title": album,
            "type": "master", "token": DISCOGS_TOKEN,
        }, headers=HEADERS, timeout=12).json()
    except Exception:
        return None

    results = data.get("results", [])
    if not results:
        return None
    master_id = results[0].get("master_id") or results[0].get("id")
    if not master_id:
        return None
    try:
        det = requests.get(
            f"{DISCOGS_BASE}/masters/{master_id}",
            headers={**HEADERS, "Authorization": f"Discogs token={DISCOGS_TOKEN}"},
            timeout=12,
        ).json()
    except Exception:
        return None

    tracks = [{"num": i+1, "title": t.get("title", f"Track {i+1}"),
               "duration": t.get("duration", "")}
              for i, t in enumerate(det.get("tracklist", []))]
    art_path = None
    for img in det.get("images", []):
        if img.get("type") == "primary":
            art_path = _download_image(img["uri"], f"discogs_{master_id}")
            break
    if not art_path and det.get("images"):
        art_path = _download_image(det["images"][0]["uri"], f"discogs_{master_id}_0")

    return {"artist": artist, "album": det.get("title", album),
            "year": str(det.get("year", "")), "tracks": tracks, "art_path": art_path}



def fetch_cd_metadata(disc_info: dict = None, 
                      device: str = discid.get_default_device()) -> Dict:

    if disc_info is None:
        disc_info = CDHandler(device=device).get_disc_info()
        if disc_info is None:
            return self._unknown([])

    cddb_id    = disc_info["freedb_id"]
    mb_disc_id = disc_info["mb_id"]
    offsets    = disc_info["offsets"]
    total_secs = disc_info["total_seconds"]
    raw_tracks = disc_info["tracks"]
    num_tracks = len(raw_tracks)

    # 2. From disc text
    out = subprocess.run(
        ["cd-info", "--no-device-info", "--no-cddb", device],
        capture_output=True, text=True, check=True,
    ).stdout

    disc, tracks, current = {}, {}, None
    for line in out.splitlines():
        if re.match(r"\s*CD-TEXT for Disc", line):
            current = disc
        elif (m := re.match(r"\s*CD-TEXT for Track\s+(\d+)", line)):
            current = tracks.setdefault(int(m.group(1)), {})
        elif current is not None and (m := re.match(r"\s+([A-Z_]+):\s*(.*)", line)):
            current[m.group(1).lower()] = m.group(2).strip()

    mb = _fetch_from_mb_by_metadata(artist=disc.get("performer"),
                                    album=disc.get("title"),
                                    raw_tracks=[tracks[n].get("title", "") for n in sorted(tracks)])
    if mb:
        log.info("MB Found from CD-TEXT + MusicBrainz")

    # 3. MusicBrainz — primary disc-ID lookup
    if not mb:
        mb = _fetch_from_mb(mb_disc_id, num_tracks, offsets, total_secs, raw_tracks)
        if mb:
            log.info("MB Found from DiscID + MusicBrainz")

    if mb:
        log.info(f"Identified MB release: https://musicbrainz.org/release/{mb['mbid']}")
        artist = mb["artist"]
        album  = mb["album"]
    else:
        artist = album = ""

    # 4. iTunes — second opinion + art
    itunes = _fetch_from_itunes(artist, album, cddb_id) if artist else None

    # Consensus: if iTunes confirms the artist, mark as high-confidence
    if mb and itunes and itunes.get("itunes_artist"):
        if _names_overlap(artist, itunes["itunes_artist"]):
            log.info("MB + iTunes agree on '%s' — high confidence", artist)
        else:
            log.info("MB says '%s', iTunes says '%s' — using MB", artist,
                        itunes.get("itunes_artist", "?"))

    if not mb:
        log.info("No metadata found for disc %s", cddb_id)
        return _unknown(raw_tracks)

    # 5. Art: iTunes → Deezer → MusicBrainz CAA
    art_path = (
        (itunes or {}).get("art_path")
        or _fetch_art_deezer(artist, album, cddb_id)
        or _fetch_art_caa(mb.get("mbid", ""), cddb_id)
    )

    meta = {
        "artist":   mb["artist"],
        "album":    mb["album"],
        "year":     mb["year"],
        "tracks":   mb["tracks"],
        "mbid":     mb["mbid"],
        "art_path": art_path,
    }

    return meta

def fetch_album_metadata(self, artist: str, album: str,
                            year: Optional[str] = None) -> Dict:
    """Fetch metadata for a library album."""
    result = _fetch_from_discogs(artist, album)
    if result:
        return result
    itunes   = _fetch_from_itunes(artist, album, hashlib.md5(f"{artist}{album}".encode()).hexdigest())
    art_path = (itunes or {}).get("art_path") or \
                _fetch_art_deezer(artist, album, hashlib.md5(f"{artist}{album}".encode()).hexdigest())
    return {"artist": artist, "album": album, "year": year or "",
            "tracks": [], "art_path": art_path}

def _unknown(raw_tracks: List[Dict]) -> Dict:
    return {"artist": "Unknown Artist", "album": "Unknown CD",
            "year": "", "tracks": raw_tracks, "art_path": None}


# ── File-based metadata extraction (vault playback) ───────────────────────
def read_file_metadata(path: str) -> Optional[Dict]:
    """Extract all useful metadata from a single audio file on disk.
    
    Works with both ID3-tagged files (MP3, MP4) and VorbisComment files (FLAC, OGG).
    """
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis
    from mutagen.mp3 import MP3

    audio = MutagenFile(path)
    if audio is None or audio.tags is None:
        return None

    def _v(key: str) -> str:
        """Get a VorbisComment key value (case-insensitive)."""
        vals = audio.tags.get(key)
        if vals:
            return str(vals[0])
        return ""

    # Title
    title = ""
    if isinstance(audio, (MP3, MP4)):
        for tag in audio.tags.values():
            if isinstance(tag, TIT2):
                title = str(tag.text[0]) if tag.text else ""
                break
    title = title or _v("TITLE") or _v("TiT2")
    if not title:
        title = os.path.basename(path)

    # Artist
    artist = ""
    if isinstance(audio, (MP3, MP4)):
        for tag in audio.tags.values():
            if isinstance(tag, TPE1):
                artist = str(tag.text[0]) if tag.text else ""
                break
    artist = artist or _v("ARTIST") or _v("TiAr")

    # Album
    album = ""
    if isinstance(audio, (MP3, MP4)):
        for tag in audio.tags.values():
            if isinstance(tag, TALB):
                album = str(tag.text[0]) if tag.text else ""
                break
    album = album or _v("ALBUM") or _v("TiAl")

    # Year
    year = ""
    if isinstance(audio, (MP3, MP4)):
        for tag in audio.tags.values():
            if isinstance(tag, TDRC):
                year = str(tag.text[0])[:4] if tag.text else ""
                break
    year = year or _v("DATE") or _v("TDRC") or _v("TiD")

    # MusicBrainz ID
    mbid = ""
    mbid = (_v("MUSICBRAINZ_ALBUMID") or _v("MUSICBRAINZ_TRACKID") or
            _v("MusicBrainz Album Id"))

    # Duration
    duration = audio.info.length if audio.info else 0.0
    duration_fmt = f"{int(duration // 60):02d}:{int(duration % 60):02d}"

    # Cover art — check both APIC (ID3) and PICTURE (FLAC VorbisComment)
    art_bytes = None
    if isinstance(audio, FLAC):
        for pic in audio.pictures:
            art_bytes = pic.data
            break
    else:
        for tag in audio.tags.values():
            if isinstance(tag, APIC):
                art_bytes = tag.data
                break

    return {
        "title":        title,
        "artist":       artist,
        "album":        album,
        "year":         year,
        "duration":     duration,
        "duration_fmt": duration_fmt,
        "mbid":         mbid,
        "art_bytes":    art_bytes,
    }


def extract_cover_from_tracks(track_paths: List[str], cache_dir: Optional[str] = None) -> Optional[str]:
    """Scan track files for embedded cover art; return a path to a cached copy or None."""
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.mp3 import MP3
    from mutagen.mp4 import MP4
    from mutagen.oggvorbis import OggVorbis

    if not track_paths:
        return None

    for path in track_paths:
        audio = MutagenFile(path)
        if audio is None or audio.tags is None:
            continue

        art_bytes = None
        if isinstance(audio, FLAC):
            for pic in audio.pictures:
                art_bytes = pic.data
                break
        elif isinstance(audio, (MP3, MP4, OggVorbis)):
            for tag in audio.tags.values():
                if isinstance(tag, APIC):
                    art_bytes = tag.data
                    break

        if art_bytes:
            if cache_dir:
                name = hashlib.md5(path.encode()).hexdigest() + ".jpg"
                cache_path = os.path.join(cache_dir, name)
                if not os.path.exists(cache_path):
                    with open(cache_path, "wb") as f:
                        f.write(art_bytes)
                return cache_path
            return None

    return None
