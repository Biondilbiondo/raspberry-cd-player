import musicbrainzngs as mb
from mutagen.id3 import (APIC, ID3, TALB, TCMP, TDRC, TIT2, TPE1, TPE2, TPOS,
                         TPUB, TRCK, TXXX, UFID)
from mutagen.flac import FLAC, Picture as FlacPicture
from mutagen.oggvorbis import OggVorbis
from mutagen.mp4 import MP4
import configparser
import logging
import os, shutil
from .database import db_add_entry

log = logging.getLogger("metadata tagger")

config = configparser.ConfigParser()
config.read('config.ini')

MB_AGENT = config.get('musicbrainz', 'agent') 
MB_VERSION = config.get('musicbrainz', 'version')
MB_MAIL = config.get('musicbrainz', 'mail')
VAULT_FORMAT = config.get('storage', 'vault_format')
CD_VAULT = config.get('storage', 'vault_dir')
FFMPEG_OUT_FORMAT = config.get('ffmpeg', 'format')

mb.set_useragent(MB_AGENT, MB_VERSION, MB_MAIL)

INCLUDES = ["artists", "recordings", "artist-credits", "labels",
            "release-groups"]


def get_release(mbid):
    """Fetch a release with everything tag_file() needs."""
    return mb.get_release_by_id(mbid, includes=INCLUDES)["release"]


def tag_file(path, track, vault_key, mbid, disc=1, cover_path=None, move_path=True):
    """Tag `path` with MusicBrainz metadata, using the correct format for the file type."""
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
    elif path.endswith(('.ogg',)):
        _tag_ogg(path, track, total_tracks, disc, len(media),
                 track_title, artist, album_artist, release, credit,
                 rec["id"], cover_path)
    elif path.endswith(('.mp3',)):
        _tag_mp3(path, track, total_tracks, disc, len(media),
                 track_title, artist, album_artist, release, credit,
                 rec["id"], cover_path)
    elif path.endswith(('.m4a', '.mp4')):
        _tag_mp4(path, track, total_tracks, disc, len(media),
                 track_title, artist, album_artist, release, credit,
                 rec["id"], cover_path)
    else:
        # Fallback: use ID3 anyway
        _tag_mp3(path, track, total_tracks, disc, len(media),
                 track_title, artist, album_artist, release, credit,
                 rec["id"], cover_path)

    if move_path:
        dest = VAULT_FORMAT.format(track_title=rec['title'],
                                   track_num=track,
                                   artist=album_artist,
                                   album=release['title'],
                                   year=release['date'][:4])
        dest = os.path.join(CD_VAULT, dest)
        dest += f".{FFMPEG_OUT_FORMAT}"

        dirname, _ = os.path.split(dest)
        os.makedirs(dirname, exist_ok=True)
        shutil.move(path, dest)
        db_add_entry(vault_key, len(medium['track-list']), track-1, dest)


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


def _tag_flac(path, track, total_tracks, disc, num_media,
              track_title, artist, album_artist, release, credit,
              track_id, cover_path):
    tags = FLAC(path) if os.path.exists(path) else FLAC()
    _set_common_vorbis(tags, track, total_tracks, disc, num_media,
                       track_title, artist, album_artist, release, credit,
                       track_id, cover_path)
    tags.save()


def _tag_ogg(path, track, total_tracks, disc, num_media,
             track_title, artist, album_artist, release, credit,
             track_id, cover_path):
    tags = OggVorbis(path) if os.path.exists(path) else OggVorbis()
    _set_common_vorbis(tags, track, total_tracks, disc, num_media,
                       track_title, artist, album_artist, release, credit,
                       track_id, cover_path)
    tags.save()


def _tag_mp3(path, track, total_tracks, disc, num_media,
             track_title, artist, album_artist, release, credit, cover_path):
    tags = ID3() if not os.path.exists(path) else ID3(path)
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
        ("MusicBrainz Release Track Id", t["id"]),
        ("MusicBrainz Artist Id",
         [c["artist"]["id"] for c in credit
          if isinstance(c, dict) and "artist" in c]),
    ]:
        if val:
            tags.add(TXXX(encoding=3, desc=desc, text=val))
    tags.add(UFID(owner="http://musicbrainz.org",
                  data=rec["id"].encode("ascii")))

    if cover_path:
        with open(cover_path, "rb") as f:
            cover = f.read()
        mime = "image/png" if cover[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        tags.add(APIC(encoding=3, mime=mime, type=3,
                      desc="Cover", data=cover))

    tags.save(path, v2_version=4)


def _tag_mp4(path, track, total_tracks, disc, num_media,
             track_title, artist, album_artist, release, credit, cover_path):
    from mutagen.mp4 import MP4AlbumArt
    tags = MP4() if not os.path.exists(path) else MP4(path)
    tags["\xa9nam"] = track_title
    tags["\xa9ART"] = artist
    tags["aART"] = album_artist
    tags["\xa9alb"] = release["title"]
    tags["trkn"] = [(track, total_tracks)]
    if num_media > 1:
        tags["disk"] = [(disc, num_media)]
    if release.get("date"):
        tags["\xa9day"] = release["date"]
    if album_artist.lower() == "various artists":
        tags["\xa9cpg"] = 1

    for desc, val in [
        ("MUSICBRAINZ_ALBUMID", release["id"]),
        ("MUSICBRAINZ_RELEASEGROUPID", release["release-group"]["id"]),
        ("MUSICBRAINZ_TRACKID", t["id"]),
    ]:
        tags["----:com.example:" + desc] = val.encode("utf-8")

    artist_ids = [c["artist"]["id"] for c in credit
                  if isinstance(c, dict) and "artist" in c]
    if artist_ids:
        tags["----:com.example:MUSICBRAINZ_ARTISTID"] = ";".join(artist_ids).encode("utf-8")

    if cover_path:
        with open(cover_path, "rb") as f:
            tags["covr"] = [MP4AlbumArt(f.read())]

    tags.save()