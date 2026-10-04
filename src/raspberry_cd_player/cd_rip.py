import os
import shutil
import subprocess
import configparser
import logging
from .add_metadata import tag_file
import configparser

# Target drive speed (1x = 150 KB/s; 4x is quiet enough for audio)
config = configparser.ConfigParser()
config.read('config.ini')
CD_SPEED =  int(config.get('cd', 'cd_speed'))
FFMPEG_OPTIONS = config.get('ffmpeg', 'options')
FFMPEG_OUT_FORMAT = config.get('ffmpeg', 'format')

log = logging.getLogger("cd rip")

config = configparser.ConfigParser()
config.read('config.ini')

def _rip_done_file(dest_dir: str, track: int):
    return os.path.join(dest_dir, f"track{track+1:02d}.done")

def _rip_doing_file(dest_dir: str, track: int):
    return os.path.join(dest_dir, f"track{track+1:02d}.doing")

def _rip_wav_file(dest_dir: str, track: int):
    return os.path.join(dest_dir, f"track{track+1:02d}.wav")

def _rip_encoded_file(dest_dir: str, track: int):
    return os.path.join(dest_dir, f"track{track+1:02d}.{FFMPEG_OUT_FORMAT}")

def _rip_estimated_len_s(file: str):
    """Return the estimated length in seconds of a wav file that
    is being ripped"""
    HEADER = 44
    BYTES_PER_SEC = 176400

    file_size = os.path.getsize(file)
    file_size -= HEADER
    return file_size / BYTES_PER_SEC

def _rip_chain(dest_dir: str, 
               track: int, 
               ntracks: int, 
               mbid: str, 
               art_path: str, 
               cdparanoia_timeout: int = 6000):
    os.setsid()
    # Schedule the order for the track
    candidate_tracks = list(range(track, ntracks)) + list(range(track))
    track_schedule = []
    for i in candidate_tracks:
        if not os.path.exists(_rip_done_file(dest_dir, i)):
            track_schedule += [i]
    log.debug(f"Track schedule {track_schedule}")

    for i in track_schedule:
        _rip_track(dest_dir, i, cdparanoia_timeout)
        _encode_ripped_track(dest_dir, i)
        tag_file(_rip_encoded_file(dest_dir, i), i+1, mbid, cover_path=art_path)

    for i in range(ntracks):
        _encode_ripped_track(dest_dir, i)
        tag_file(_rip_encoded_file(dest_dir, i), i+1, mbid, cover_path=art_path)

    log.info("CD archiviation completed!")

def _rip_track(dest_dir: str, track: int, cdparanoia_timeout: int = 6000):
    os.makedirs(dest_dir, exist_ok=True)
    have_ffmpeg = bool(shutil.which("ffmpeg"))

    try:
        wav_path = _rip_wav_file(dest_dir, track)
        log.info(f"Ripping track {track} to {wav_path}")

        # Try cdparanoia burst mode first (fast, no error correction).
        # Falls back to MPV if cdparanoia is unavailable.
        ripped_ok = False

        if shutil.which("cdparanoia"):
            try:
                with open(_rip_doing_file(dest_dir, track), "w") as f:
                    print("Doing", file=f)
                log.info("Calling cdparanoia")
                r = subprocess.run(
                    ["cdparanoia", "-Z", "-S", f"{CD_SPEED}", "-d", "/dev/sr0",
                        str(track + 1), wav_path],
                    timeout=cdparanoia_timeout,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                ripped_ok = os.path.exists(wav_path) and os.path.getsize(wav_path) > 4096
                if not ripped_ok:
                    log.error(f"cdparanoia exit={r.returncode}")
            except subprocess.TimeoutExpired:
                log.error("cdparanoia timed out")

        if ripped_ok:
            log.info(f"Ripping track {track} to {wav_path} completed")
            with open(_rip_done_file(dest_dir, track), "w") as f:
                print("Done", file=f)
            os.remove(_rip_doing_file(dest_dir, track))
    except Exception as exc:
        log.error(str(exc))

def _encode_ripped_track(dest_dir, track):
    # Convert WAV → MP3
    have_ffmpeg = bool(shutil.which("ffmpeg"))
    wav_path = _rip_wav_file(dest_dir, track)
    if not have_ffmpeg:
        log.error("ffmpeg executable not found!")
        raise FileExistsError
    if not os.path.exists(wav_path):
        log.error(f"File {wav_path} not found.")

    log.info(f"Encoding track {track+1}")
    mp3_path = _rip_encoded_file(dest_dir, track)
    subprocess.run(
        ["ffmpeg", "-y", "-i", wav_path] + FFMPEG_OPTIONS.split() + [mp3_path],
        timeout=120,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    log.info(f"Done encoding track {track+1}")