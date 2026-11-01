import musicbrainzngs as mb
from mutagen.id3 import (APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TPOS,
                         TRCK)
from mutagen.flac import FLAC, Picture as FlacPicture
import logging
import os, shutil

from .config import config
from .database import db_update_track, db_update_path

log = logging.getLogger("metadata tagger")

mb.set_useragent(config.mb_agent, config.mb_version, config.mb_mail)


def _sanitize_name(name: str) -> str:
    """Replace characters illegal in filenames with a safe fallback."""
    return name.replace("/", "–").replace("\\", "_").replace(":", "-").strip()

INCLUDES = ["artists", "recordings", "artist-credits", "labels",
            "release-groups"]


def _generate_dest_name(artist: str, album_artist: str, album: str, title: str,
                        year: str, track: int, total_tracks: int):
    return config.vault_format.format(track_title=_sanitize_name(title),
                                      track_artist=_sanitize_name(artist),
                                      track_num=track,
                                      artist=_sanitize_name(album_artist),
                                      album=_sanitize_name(album),
                                      year=year,
                                      total_tracks=total_tracks)

def _move_to_dest(old_path: str, new_path: str):
    dest = os.path.join(config.vault_dir, new_path)
    dest += f".{config.ffmpeg_format}"

    log.debug(f"Moving file from {old_path} to {dest}")
    dirname, _ = os.path.split(dest)
    os.makedirs(dirname, exist_ok=True)

    if os.path.abspath(dest) == os.path.abspath(old_path):
        log.debug(f"No need to move, destination and origin are the same")
        return
    
    if not os.path.exists(old_path):
        log.critical(f"File to move {old_path} does not exist!")
        return
    
    if os.path.exists(dest):
        log.error(f"Destination file {dest} is not empty!")
        return

    shutil.move(old_path, dest)
    log.info(f"Moved {old_path} to {dest}")
    
    # Remove empty parent directories of the old file
    old_dir = os.path.dirname(old_path)
    try:
        os.removedirs(old_dir)
        log.debug(f"Removed empty directory tree: {old_dir}")
    except OSError:
        pass  # not empty or not removable
    
    db_update_path(old_path, dest)

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

    if config.ffmpeg_format == 'flac':
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

    elif config.ffmpeg_format == 'mp3':
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
    _move_to_dest(path, 
                  _generate_dest_name(artist, album_artist, 
                                      album, title,
                                      year, track, total_tracks))
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
            log.info("Tagged with CD-TEXT")
        
        dest = os.path.join("untagged_CD", str(vault_key),
                    f"{track:02d} - Track {track}")
        _move_to_dest(path, dest)
        log.debug(f"Track {vault_key}/{track-1} marked as done.")
        db_update_track(vault_key, track-1, state="done", untagged=1)
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
    album_title = release.get("title", "Unknown Album")
    album_date = release.get("date", "")
    track_title = t.get("title") or rec["title"]
    total_tracks = len(medium['track-list'])

    # Detect file type and tag accordingly
    if config.ffmpeg_format == 'flac':
        _tag_flac(path, track, total_tracks, disc, len(media),
                  track_title, artist, album_artist, album_title, 
                  album_date, cover_path)
    elif config.ffmpeg_format == 'mp3':
        _tag_mp3(path, track, total_tracks, disc, len(media),
                 track_title, artist, album_artist, album_title,
                 album_date, cover_path)
    else:
        # Fallback: use ID3 anyway
        _tag_mp3(path, track, total_tracks, disc, len(media),
                 track_title, artist, album_artist, album_title,
                  album_date, cover_path)

    _move_to_dest(path, 
                  _generate_dest_name(artist, album_artist, 
                                      album_title, track_title,
                                      album_date, track, total_tracks))
    log.debug(f"Track {vault_key}/{track-1} marked as done.")
    db_update_track(vault_key, track-1, state="done", untagged=0)


# ── Tag writers per format ──────────────────────────────────────────────────

def _set_common_vorbis(tags, track, total_tracks, disc, num_media,
                       track_title, artist, album_artist, album_title, 
                       album_date, cover_path):
    """Set shared VorbisComment fields."""
    tags["TITLE"] = track_title
    tags["ARTIST"] = artist
    tags["ALBUMARTIST"] = album_artist
    tags["ALBUM"] = album_title
    tags["TRACKNUMBER"] = f"{track}/{total_tracks}"
    if num_media > 1:
        tags["DISCNUMBER"] = f"{disc}/{num_media}"
    tags["DATE"] = album_date
    if album_artist.lower() == "various artists":
        tags["TCMP"] = "1"

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
    log.debug("_tag_with_local: local_meta keys=%s, artist=%s, album=%s, track_titles=%s",
              list(local_meta.keys()), local_meta.get("artist"), local_meta.get("album"),
              local_meta.get("track_titles", {}))
    
    artist = local_meta.get("artist", "Unknown Artist")
    album = local_meta.get("album", "Unknown Album")
    year = local_meta.get("year", "")
    total_tracks = local_meta.get("total_tracks", track)
    track_title = local_meta.get("track_titles", {}).get(track, f"Track {track}")
    
    if config.ffmpeg_format == 'flac':
        _tag_flac(path, track, total_tracks, 1, 1,
                  track_title, artist, artist, album, 
                  year, cover_path)
    elif config.ffmpeg_format == 'mp3':
        _tag_mp3(path, track, total_tracks, 1, 1,
                  track_title, artist, artist, album, 
                  year, cover_path)
    else:
        _tag_mp3(path, track, total_tracks, 1, 1,
                  track_title, artist, artist, album, 
                  year, cover_path)
    
    log.info("_tag_with_local: tagged %s → %s – %s (%d/%d)", path, artist, album, track, total_tracks)
    return True


def _tag_flac(path, track, total_tracks, disc, num_media,
              track_title, artist, album_artist, album_title, 
              album_date, cover_path):
    tags = FLAC(path) if os.path.exists(path) else FLAC()
    _set_common_vorbis(tags, track, total_tracks, disc, num_media,
                       track_title, artist, album_artist, album_title, 
                       album_date, cover_path)
    tags.save()



def _tag_mp3(path, track, total_tracks, disc, num_media,
             track_title, artist, album_artist, album_title, album_date, 
             cover_path):
    tags = ID3() if not os.path.exists(path) else ID3(path)
    if tags:  # file existed with old ID3 tags — delete them to avoid duplicates
        tags.delete()
        tags = ID3()
    tags.add(TIT2(encoding=3, text=track_title))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TPE2(encoding=3, text=album_artist))
    tags.add(TALB(encoding=3, text=album_title))
    tags.add(TRCK(encoding=3, text=f"{track}/{total_tracks}"))
    if num_media > 1:
        tags.add(TPOS(encoding=3, text=f"{disc}/{num_media}"))
    tags.add(TDRC(encoding=3, text=album_date))

    if cover_path:
        with open(cover_path, "rb") as f:
            cover = f.read()
        mime = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        tags.add(APIC(encoding=3, mime=mime, type=3,
                      desc="Cover", data=cover))

    tags.save(path, v2_version=4)
