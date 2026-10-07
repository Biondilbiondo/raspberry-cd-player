import musicbrainzngs as mb
from mutagen.id3 import (APIC, ID3, TALB, TCMP, TDRC, TIT2, TPE1, TPE2, TPOS,
                         TPUB, TRCK, TXXX, UFID)
import configparser
import logging
import os, shutil

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


def tag_file(path, track, mbid, disc=1, cover_path=None, move_path=True):
    """Blindly tag `path` with track number `track` (on medium `disc`).

    release: dict from get_release()
    cover:   optional JPEG bytes (e.g. mb.get_image_front(mbid))
    """
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

    tags = ID3()
    tags.add(TIT2(encoding=3, text=t.get("title") or rec["title"]))
    tags.add(TPE1(encoding=3, text=artist))
    tags.add(TPE2(encoding=3, text=album_artist))
    tags.add(TALB(encoding=3, text=release["title"]))
    tags.add(TRCK(encoding=3, text=f"{track}/{len(medium['track-list'])}"))
    if len(media) > 1:
        tags.add(TPOS(encoding=3, text=f"{disc}/{len(media)}"))
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

    tags.save(path, v2_version=4)  # replaces any existing ID3 tags
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