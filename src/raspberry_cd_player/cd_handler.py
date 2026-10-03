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

AUDIO_EXTS   = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".aac", ".wma"}

log = logging.getLogger("cd_handler")

# CDROM IOCTL constants (linux/cdrom.h)
CDROM_DRIVE_STATUS = 0x5326
CDS_DISC_OK        = 4

# Target drive speed (1x = 150 KB/s; 4x is quiet enough for audio)
CD_SPEED = 4


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

    def get_disc_id(self) -> str | None:
        try:
            disc = discid.read(self.device)
            offsets = [t.offset for t in disc.tracks]
            total_seconds = disc.sectors // 75  # leadout offset / 75
            out = f"{disc.freedb_id} {len(disc.tracks)} "
            out += " ".join([str(a) for a in offsets])
            out += f" {total_seconds}"
            return out
        except Exception as e:
            log.error("cd-discid failed: %s", e)
            return None

    def get_tracks(self) -> list[dict]:
        self.set_speed()
        raw_id = self.get_disc_id()
        if not raw_id:
            return []
        parts = raw_id.split()
        if len(parts) < 3:
            return []
        num_tracks = int(parts[1])
        offsets    = [int(p) for p in parts[2 : 2 + num_tracks]]
        total_secs = int(parts[-1])
        offsets.append(total_secs * 75)
        tracks = []
        for i in range(num_tracks):
            duration_secs = (offsets[i + 1] - offsets[i]) / 75
            tracks.append({
                "num":      i + 1,
                "title":    f"Track {i + 1:02d}",
                "duration": duration_secs,
            })
        log.info("Found %d tracks on disc", len(tracks))
        return tracks

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

    def parse_toc(self, raw_id: str) -> dict | None:
        if not raw_id:
            return None
        parts = raw_id.split()
        if len(parts) < 3:
            return None
        cddb_id    = parts[0]
        num_tracks = int(parts[1])
        offsets    = [int(p) for p in parts[2 : 2 + num_tracks]]
        total_secs = int(parts[-1])
        mb_id      = self.compute_mb_disc_id(num_tracks, offsets, total_secs)
        return {
            "cddb_id":    cddb_id,
            "mb_disc_id": mb_id,
            "num_tracks": num_tracks,
            "offsets":    offsets,
            "total_secs": total_secs,
        }

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
