#!/usr/bin/env python3
"""
cd_handler.py — CD drive interface.
Detects disc presence via kernel IOCTL, reads TOC via cd-discid,
and computes the MusicBrainz disc ID from the TOC data.
"""

import base64
import fcntl
import hashlib
import logging
import os
import subprocess
import discid
import configparser

log = logging.getLogger("cd_handler")

# CDROM IOCTL constants (linux/cdrom.h)
CDROM_DRIVE_STATUS = 0x5326
CDS_DISC_OK        = 4

# Target drive speed (1x = 150 KB/s; 4x is quiet enough for audio)
config = configparser.ConfigParser()
config.read('config.ini')
CD_SPEED =  int(config.get('cd', 'cd_speed'))

class CDHandler:
    def __init__(self, device: str = discid.get_default_device()):
        self.device = device

    def is_disc_present(self) -> bool:
        try:
            fd = os.open(self.device, os.O_RDONLY | os.O_NONBLOCK)
            try:
                status = fcntl.ioctl(fd, CDROM_DRIVE_STATUS)
                return status == CDS_DISC_OK
            finally:
                os.close(fd)
        except Exception as e:
            log.debug("Disc status check failed: %s", e)
            return False

    def set_speed(self, speed: int = CD_SPEED):
        try:
            subprocess.run(
                ["/usr/bin/eject", "-x", str(speed), self.device],
                check=False, timeout=3,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            log.info("CD drive speed set to %dx", speed)
        except Exception as e:
            log.debug("eject -x failed (non-fatal): %s", e)

    def get_disc_info(self) -> str | None:
        try:
            disc = discid.read(self.device)
            offsets = [t.offset for t in disc.tracks]
            total_seconds = disc.sectors // 75
            cd_discid_str = f"{disc.freedb_id} {len(disc.tracks)} "
            cd_discid_str += " ".join([str(a) for a in offsets])
            cd_discid_str += f" {total_seconds}"

            num_tracks = len(disc.tracks)
            offsets.append(total_seconds * 75)

            tracks = []
            for i in range(num_tracks):
                duration_secs = (offsets[i + 1] - offsets[i]) / 75
                tracks.append({
                    "num":      i + 1,
                    "title":    f"Track {i + 1:02d}",
                    "duration": duration_secs,
                })
            log.info("Found %d tracks on disc", len(tracks))

            return {'cd-discid-str': cd_discid_str, 
                    'freedb_id': disc.freedb_id,
                    'mb_id': disc.id,
                    'tracks': tracks,
                    'offsets': offsets[:-1],
                    'total_seconds': total_seconds}
        
        except Exception as e:
            log.error("cd-discid failed: %s", e)
            return None, []

    # ── Data disc (MP3 CD) support ────────────────────────────────────────────
    def is_data_disc(self) -> bool:
        """Returns True if the disc has a filesystem (data/MP3 disc, not audio)."""
        try:
            out = subprocess.check_output(
                ["/sbin/blkid", self.device], timeout=5, stderr=subprocess.DEVNULL
            ).decode().strip()
            result = bool(out)
            log.info("blkid %s → %s (data_disc=%s)", self.device, out or "(empty)", result)
            return result
        except Exception as e:
            log.debug("blkid check: %s", e)
            return False

    @staticmethod
    def compute_mb_disc_id(num_tracks: int, offsets: list[int], total_secs: int) -> str:
        first_track = 1
        last_track  = num_tracks
        lead_out    = total_secs * 75
        parts = [f"{first_track:02X}", f"{last_track:02X}", f"{lead_out:08X}"]
        for i in range(1, 100):
            parts.append(f"{offsets[i - 1]:08X}" if i <= num_tracks else "00000000")
        digest = hashlib.sha1("".join(parts).encode("ascii")).digest()
        b64    = base64.b64encode(digest).decode("ascii")
        return b64.replace("+", ".").replace("/", "_").replace("=", "-")
