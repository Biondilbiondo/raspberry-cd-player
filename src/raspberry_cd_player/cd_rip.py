import os
import shutil
import subprocess
import configparser
import logging

log = logging.getLogger("cd rip")

config = configparser.ConfigParser()
config.read('config.ini')

def _rip_done_file(dest_dir: str, track: int):
    return os.path.join(dest_dir, f"track{track+1:02d}.done")

def _rip_doing_file(dest_dir: str, track: int):
    return os.path.join(dest_dir, f"track{track+1:02d}.doing")

def _rip_wav_file(dest_dir: str, track: int):
    return os.path.join(dest_dir, f"track{track+1:02d}.wav")

def _rip_chain(dest_dir: str, track: int, ntracks: int, cdparanoia_timeout: int = 6000, run_ffmpeg: bool = False):
    os.setsid()
    # Schedule the order for the track
    candidate_tracks = list(range(track, ntracks)) + list(range(track))
    track_schedule = []
    for i in candidate_tracks:
        if not os.path.exists(_rip_done_file(dest_dir, i)):
            track_schedule += [i]
    log.info(f"Track schedule {track_schedule}")
    
    for i in track_schedule:
        _rip_track(dest_dir, i, cdparanoia_timeout, run_ffmpeg)

def _rip_track(dest_dir: str, track: int, cdparanoia_timeout: int = 6000, run_ffmpeg: bool = False):
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
                    ["cdparanoia", "-Z", "-S", "4", "-d", "/dev/sr0",
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

        # Convert WAV → MP3
        if have_ffmpeg and os.path.exists(wav_path) and run_ffmpeg:
            mp3_path = os.path.join(dest_dir, f"track{track+1:02d}.mp3")
            subprocess.run(
                ["ffmpeg", "-y", "-i", wav_path, "-q:a", "2", mp3_path],
                timeout=120,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                os.remove(wav_path)
            except OSError:
                pass

    except Exception as exc:
        log.error(str(exc))