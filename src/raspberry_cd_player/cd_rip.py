import os
import shutil
import subprocess
import logging
from .config import config
from .add_metadata import tag_file
from .database import (
    db_add_entry, db_update_track,
    db_get_pending_tracks, db_get_track_state,
    db_is_cd_complete, db_get_track
)

log = logging.getLogger("cd rip")

def _rip_estimated_len_s(file: str) -> float:
    """Return the estimated length in seconds of a wav file that
    is being ripped."""
    HEADER = 44
    BYTES_PER_SEC = 176400
    try:
        return (os.path.getsize(file) - HEADER) / BYTES_PER_SEC
    except FileNotFoundError:
        return 0.0


def _rip_chain(start_track: int,
               ntracks: int,
               vault_key: str,
               mbid: str,
               art_path: str,
               cdparanoia_timeout: int = 6000):
    """Rip and archive a CD. Runs in a child process.

    State machine per track:
      pending → cdparanoia starts → ripping → cdparanoia done → ripped
                → ffmpeg + tag + move → done

    The DB tracks state so the main process can monitor progress at any time.
    WAV files stay in dest_dir until archiviation is fully complete.
    """
    os.setsid()
    log.info("_rip_chain: vault=%s start=%d ntracks=%d", vault_key, start_track, ntracks)

    # ── Phase 1: Rip tracks (skip already-ripped) ───────────────────────────
    order = list(range(start_track, ntracks)) + list(range(start_track))
    pending = db_get_pending_tracks(vault_key)

    # Only process tracks from start_track onward that are still pending
    _process_list = [i for i in order if i in pending]
    log.info("Tracks to rip: %s", _process_list)

    for track in _process_list:
        if not _rip_track(vault_key, track, cdparanoia_timeout):
            log.error("Rip failed for track %d — aborting rip chain", track)
            db_update_track(vault_key, track, state="pending")
            return 
        # Rip succeeded → mark as ripped (WAV fully formed)
        db_update_track(vault_key, track, state="ripped")
        log.info("Track %d: ripping done → state=ripped", track)

    # ── Phase 2: Encode + tag + move to vault ──────────────────────────────
    for track in range(ntracks):
        state = db_get_track_state(vault_key, track)
        if state == "done":
            log.info("Track %d already archived, skipping", track)
            continue
        if state not in ("ripped", "pending"):
            log.warning("Track %d state=%s, skipping", track, state)
            continue
        if state == "pending":
            log.warning("Track %d still pending (rip failed?), skipping encode", track)
            continue

        _encode_ripped_track(vault_key, track, mbid, art_path)
    
    # Finally remove the cache dir
    _cache_dir = os.dirname(db_get_track(vault_key, 0)['wav_path'])
    try:
        os.rmdir(_cache_dir)
    except OSError:
        log.error(f"Cannot remove directory f{_cache_dir}")
    
    log.info("CD archiviation completed!")


def _rip_track(vault_key: str, track: int, cdparanoia_timeout: int) -> bool:
    """Rip a single track with cdparanoia. Sets state=ripping→ripped.
    Returns True on success."""
    wav_path = db_get_track(vault_key, track)['wav_path']
    
    # Ensure dest_dir exists and seed DB entries
    os.makedirs(os.path.dirname(wav_path), exist_ok=True)
    
    log.info("Ripping track %d → %s", track, wav_path)

    # Mark as ripping now — main process can see this and kill if needed
    db_update_track(vault_key, track, state="ripping")

    if not shutil.which("cdparanoia"):
        log.error("cdparanoia not found — cannot rip")
        return False

    try:
        r = subprocess.run(
            ["cdparanoia", *config.cdparanoia_flags.split(),
                "-S", str(config.cd_speed),
                "-d", config.cd_device,
                str(track + 1), wav_path],
            timeout=cdparanoia_timeout,
            capture_output=True
        )
        ok = r.returncode == 0 and os.path.exists(wav_path) and os.path.getsize(wav_path) > 4096
        if not ok:
            log.error(r.stdout)
            log.error(r.stderr)
            log.error("cdparanoia exit=%d, file invalid", r.returncode)
        else:
            log.info("cdparanoia exit=%d, file valid", r.returncode)
        return ok
    except subprocess.TimeoutExpired:
        log.error("cdparanoia timed out for track %d", track)
        return False
    except Exception as exc:
        log.error("cdparanoia error for track %d: %s", track, exc)
        return False


def _encode_ripped_track(vault_key: str, track: int, mbid: str, art_path: str):
    """Encode WAV → FLAC/MP3, tag, move to vault, update DB."""
    wav_path = db_get_track(vault_key, track)['wav_path']
    if not os.path.exists(wav_path):
        log.error("WAV not found for track %d: %s", track, wav_path)
        return

    encoded_path = db_get_track(vault_key, track)['path']
    log.info("Encoding track %d", track)

    if not shutil.which("ffmpeg"):
        log.error("ffmpeg not found — cannot encode track %d", track)
        return

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", wav_path] + config.ffmpeg_options.split() + [encoded_path],
            timeout=120,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not os.path.exists(encoded_path):
            log.error("ffmpeg produced no output for track %d", track)
            return
    except subprocess.TimeoutExpired:
        log.error("ffmpeg timed out for track %d", track)
        return
    except Exception as exc:
        log.error("ffmpeg error for track %d: %s", track, exc)
        return

    log.info("Tagging and moving track %d → vault", track)
    try:
        tag_file(encoded_path, track + 1, vault_key, mbid, cover_path=art_path)
    except Exception as exc:
        log.error("tag_file failed for track %d: %s", track, exc)
        return

    # Clean up cache files
    for f in (wav_path, encoded_path):
        try:
            os.remove(f)
        except OSError:
            pass

    log.info("Track %d: encoded → archived, cache cleaned", track)
