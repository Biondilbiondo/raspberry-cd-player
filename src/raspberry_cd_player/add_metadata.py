import musicbrainzngs as mb
from mutagen.id3 import (APIC, ID3, TALB, TCMP, TDRC, TIT2, TPE1, TPE2, TPOS,
                         TPUB, TRCK, TXXX, UFID)
from mutagen.flac import FLAC, Picture as FlacPicture
import logging
import os, shutil

from .config import config
from .database import db_update_track

log = logging.getLogger("metadata tagger")

mb.set_useragent(config.mb_agent, config.mb_version, config.mb_mail)


def _sanitize_name(name: str) -> str:
    """Replace characters illegal in filenames with a safe fallback."""
    return name.replace("/", "–").replace("\\", "_").replace(":", "-").strip()

INCLUDES = ["artists", "recordings", "artist-credits", "labels",
            "release-groups"]


def get_release(mbid):
    """Fetch a release with everything tag_file() needs."""
    return mb.get_release_by_id(mbid, includes=INCLUDES)["release"]


def write_tags(path: str, artist: str, album_artist: str, album: str, title: str,
               year: str, track: int, total_tracks: int, mbid: str = "",
               cover_bytes: bytes = None) -> bool:
    """Write tags from editor-provided metadata onto an audio file (web editor).

    Format-aware: FLAC (Vorbis comments + picture), MP3 (ID3 + APIC).
    Used when fixing metadata of existing (possibly untagged) archives.
    """
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC, Picture as FlacPicture
    from mutagen.id3 import APIC, ID3, TDRC, TIT2, TPE1, TPE2, TALB, TRCK, TXXX

    audio = MutagenFile(path)
    if audio is None:
        log.error("write_tags: cannot read audio file %s", path)
        return False

    mime = "image/png" if (cover_bytes and cover_bytes[:8] == b"\x89PNG\r\n\x1a\n") else "image/jpeg"

    if isinstance(audio, FLAC):
        tags = FLAC(path)
        tags["TITLE"] = title
        tags["ARTIST"] = artist
        tags["ALBUMARTIST"] = album_artist
        tags["ALBUM"] = album
        tags["TRACKNUMBER"] = f"{track}/{total_tracks}"
        if year:
            tags["DATE"] = year
        if mbid:
            tags["MUSICBRAINZ_ALBUMID"] = mbid
        if cover_bytes is not None:
            pic = FlacPicture()
            pic.data = cover_bytes
            pic.type = 3  # cover front
            pic.mime = mime
            tags.clear_pictures()
            tags.add_picture(pic)
        tags.save()

    elif isinstance(audio, ID3):
        tags = ID3(path)
        tags.add(TIT2(encoding=3, text=title))
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TPE2(encoding=3, text=album_artist))
        tags.add(TALB(encoding=3, text=album))
        tags.add(TRCK(encoding=3, text=f"{track}/{total_tracks}"))
        if year:
            tags.add(TDRC(encoding=3, text=year))
        if mbid:
            tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=mbid))
        if cover_bytes is not None:
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover_bytes))
        tags.save(path, v2_version=4)

    else:
        log.error("write_tags: unsupported audio format for %s", path)
        return False

    log.info("write_tags: wrote tags on %s (track %d/%d)", path, track, total_tracks)
    return True


def tag_file(path, track, vault_key, mbid, disc=1, cover_path=None, move_path=True,
             local_meta: dict = None):
    """Tag `path` with MusicBrainz metadata, using the correct format for the file type.

    If `mbid` is empty the archive is treated as untagged: no MusicBrainz lookup is
    performed, the file is moved into the untagged_CD/<DISCID>/ vault folder without
    tagging, and the track is flagged as untagged in the database.

    If `local_meta` is provided (from CD-TEXT), the file IS tagged with the local
    metadata even when `mbid` is empty. The file goes into the normal vault layout
    (not untagged_CD/) with CD-TEXT tags written directly to the file.
    """
    if not mbid:
        if local_meta:
            # ── Local metadata (CD-TEXT): tag with local data ───────────────────
            _tag_with_local(path, track, local_meta, cover_path)
            safe_title = _sanitize_name(local_meta.get("track_title", f"Track {track}"))
            safe_artist = _sanitize_name(local_meta.get("artist", "Unknown Artist"))
            safe_album = _sanitize_name(local_meta.get("album", "Unknown Album"))
            year = local_meta.get("year", "") or ""
            dest = config.vault_format.format(
                track_title=safe_title,
                track_num=track,
                artist=safe_artist,
                album=f"{safe_album} ({year})" if year else safe_album,
                year=year,
            )
            dest = os.path.join(config.vault_dir, dest)
            dest += f".{config.ffmpeg_format}"
            dirname, _ = os.path.split(dest)
            os.makedirs(dirname, exist_ok=True)
            shutil.move(path, dest)
            db_update_track(vault_key, track - 1, path=dest, state="done", untagged=1)
            log.info("Tagged with CD-TEXT → %s", dest)
            return
        
        # ── Untagged archive: move without tagging ──────────────────────────────
        safe_title = _sanitize_name(f"Track {track}")
        dest = os.path.join(config.vault_dir, "untagged_CD", str(vault_key),
                            f"{track:02d} - {safe_title}{'.' + config.ffmpeg_format}")
        dirname, _ = os.path.split(dest)
        os.makedirs(dirname, exist_ok=True)
        shutil.move(path, dest)
        db_update_track(vault_key, track - 1, path=dest, state="done", untagged=1)
        return

    release = get_release(mbid)
    media = release["medium-list"]
    medium = next(m for m in media if int(m["position"]) == disc)
    t = next(t for t in medium["track-list"] if int(t["position"]) == track)
    rec = t["recording"]

    artist = (t.get("artist-credit-phrase")
              or rec.get("artist-credit-phrase")
              or release.get("artist-credit-phrase", ""))
    credit = t.get("artist-credit") or rec.get("artist-credit") or []
    album_artist = release.get("artist-credit-phrase", "")
    track_title = t.get("title") or rec["title"]
    total_tracks = len(medium['track-list'])

    # Detect file type and tag accordingly
    if path.endswith(('.flac',)):
        _tag_flac(path, track, total_tracks, disc, len(media),
                  track_title, artist, album_artist, release, credit,
                  rec["id"], cover_path)
    elif path.endswith(('.mp3',)):
        _tag_mp3(path, track, total_tracks, disc, len(media),
                 track_title, artist, album_artist, release, credit,
                 rec["id"], cover_path)
    else:
        # Fallback: use ID3 anyway
        _tag_mp3(path, track, total_tracks, disc, len(media),
                 track_title, artist, album_artist, release, credit,
                 rec["id"], cover_path)

    if move_path:
        safe_title = _sanitize_name(rec['title'])
        safe_artist = _sanitize_name(album_artist)
        safe_album = _sanitize_name(release['title'])
        year = release.get('date', '')[:4]
        dest = config.vault_format.format(track_title=safe_title,
                                          track_num=track,
                                          artist=safe_artist,
                                          album=safe_album,
                                          year=year)
        dest = os.path.join(config.vault_dir, dest)
        dest += f".{config.ffmpeg_format}"

        log.debug(f"Moving file from {path} to {dest}")
        dirname, _ = os.path.split(dest)
        os.makedirs(dirname, exist_ok=True)
        shutil.move(path, dest)
        ntracks = len(medium['track-list'])
        db_update_track(vault_key, track-1, path=dest, state="done")


# ── Tag writers per format ──────────────────────────────────────────────────

def _set_common_vorbis(tags, track, total_tracks, disc, num_media,
                       track_title, artist, album_artist, release, credit,
                       track_id, cover_path):
    """Set shared VorbisComment fields."""
    tags["TITLE"] = track_title
    tags["ARTIST"] = artist
    tags["ALBUMARTIST"] = album_artist
    tags["ALBUM"] = release["title"]
    tags["TRACKNUMBER"] = f"{track}/{total_tracks}"
    if num_media > 1:
        tags["DISCNUMBER"] = f"{disc}/{num_media}"
    if release.get("date"):
        tags["DATE"] = release["date"]
    if album_artist.lower() == "various artists":
        tags["TCMP"] = "1"

    artist_ids = [c["artist"]["id"] for c in credit
                  if isinstance(c, dict) and "artist" in c]

    tags["MUSICBRAINZ_ALBUMID"] = release["id"]
    tags["MUSICBRAINZ_RELEASEGROUPID"] = release["release-group"]["id"]
    tags["MUSICBRAINZ_TRACKID"] = track_id
    if artist_ids:
        tags["MUSICBRAINZ_ARTISTID"] = ";".join(artist_ids)

    if cover_path:
        with open(cover_path, "rb") as f:
            cover = f.read()
        pic = FlacPicture()
        pic.data = cover
        pic.type = 3  # cover front
        pic.mime = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        tags.clear_pictures()
        tags.add_picture(pic)


def _tag_with_local(path: str, track: int, local_meta: dict, cover_path: str = None):
    """Tag an audio file with local CD-TEXT metadata (no MusicBrainz release needed)."""
    log.info("_tag_with_local: local_meta keys=%s, artist=%s, album=%s, track_titles=%s",
             list(local_meta.keys()), local_meta.get("artist"), local_meta.get("album"),
             local_meta.get("track_titles", {}))
    """Tag an audio file with local CD-TEXT metadata (no MusicBrainz release needed).
    
    local_meta should contain:
        - artist (str): performing artist
        - album (str): album title  
        - year (str, optional): release year
        - track_titles (dict): {track_number: title} for all tracks
        - total_tracks (int): total number of tracks
        - cover_path (str, optional): path to cover art image
    """
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC, Picture as FlacPicture
    from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TRCK, TDRC
    
    audio = MutagenFile(path)
    if audio is None:
        log.error("_tag_with_local: cannot read %s", path)
        return False
    
    artist = local_meta.get("artist", "Unknown Artist")
    album = local_meta.get("album", "Unknown Album")
    year = local_meta.get("year", "")
    total_tracks = local_meta.get("total_tracks", track)
    track_title = local_meta.get("track_titles", {}).get(track, f"Track {track}")
    
    if isinstance(audio, FLAC):
        tags = FLAC(path)
        tags["TITLE"] = track_title
        tags["ARTIST"] = artist
        tags["ALBUMARTIST"] = artist  # CD-TEXT usually doesn't distinguish
        tags["ALBUM"] = album
        tags["TRACKNUMBER"] = f"{track}/{total_tracks}"
        if year:
            tags["DATE"] = year
        if cover_path:
            with open(cover_path, "rb") as f:
                cover = f.read()
            mime = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            pic = FlacPicture()
            pic.data = cover
            pic.type = 3  # cover front
            pic.mime = mime
            tags.clear_pictures()
            tags.add_picture(pic)
        tags.save()
        
    elif isinstance(audio, ID3):
        tags = ID3(path)
        tags.add(TIT2(encoding=3, text=track_title))
        tags.add(TPE1(encoding=3, text=artist))
        tags.add(TPE2(encoding=3, text=artist))  # album artist = artist for CD-TEXT
        tags.add(TALB(encoding=3, text=album))
        tags.add(TRCK(encoding=3, text=f"{track}/{total_tracks}"))
        if year:
            tags.add(TDRC(encoding=3, text=year))
        if cover_path:
            with open(cover_path, "rb") as f:
                cover = f.read()
            mime = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
            tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
        tags.save(path, v2_version=4)
        
    else:
        log.error("_tag_with_local: unsupported format for %s", path)
        return False
    
    log.info("_tag_with_local: tagged %s → %s – %s (%d/%d)", path, artist, album, track, total_tracks)
    return True


def _tag_flac(path, track, total_tracks, disc, num_media,
              track_title, artist, album_artist, release, credit,
              track_id, cover_path):
    tags = FLAC(path) if os.path.exists(path) else FLAC()
    _set_common_vorbis(tags, track, total_tracks, disc, num_media,
                       track_title, artist, album_artist, release, credit,
                       track_id, cover_path)
    tags.save()



def _tag_mp3(path, track, total_tracks, disc, num_media,
             track_title, artist, album_artist, release, credit, 
             track_id, cover_path):
    tags = ID3() if not os.path.exists(path) else ID3(path)
    if tags:  # file existed with old ID3 tags — delete them to avoid duplicates
        tags.delete()
        tags = ID3()
    tags.add(TIT2(encoding=3, text=track_title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TPE2(encoding=3, text=album_artist))
    tags.add(TALB(encoding=3, text=release["title"]))
    tags.add(TRCK(encoding=3, text=f"{track}/{total_tracks}"))
    if num_media > 1:
        tags.add(TPOS(encoding=3, text=f"{disc}/{num_media}"))
    if release.get("date"):
        tags.add(TDRC(encoding=3, text=release["date"]))
    if release.get("label-info-list"):
        label = release["label-info-list"][0].get("label", {}).get("name")
        if label:
            tags.add(TPUB(encoding=3, text=label))
    if album_artist.lower() == "various artists":
        tags.add(TCMP(encoding=3, text="1"))

    for desc, val in [
        ("MusicBrainz Album Id", release["id"]),
        ("MusicBrainz Release Group Id", release["release-group"]["id"]),
        ("MusicBrainz Release Track Id", track_id),
        ("MusicBrainz Artist Id",
         [c["artist"]["id"] for c in credit
          if isinstance(c, dict) and "artist" in c]),
    ]:
        if val:
            tags.add(TXXX(encoding=3, desc=desc, text=val))
    tags.add(UFID(owner="http://musicbrainz.org",
                  data=track_id.encode("ascii")))

    if cover_path:
        with open(cover_path, "rb") as f:
            cover = f.read()
        mime = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        tags.add(APIC(encoding=3, mime=mime, type=3,
                      desc="Cover", data=cover))

    tags.save(path, v2_version=4)
