#!/usr/bin/env python3
"""
player.py — MPV controller for HiFiBerry, built on python-mpv (libmpv).
Targets Debian Trixie + MPV 0.40.

Requirements:
    sudo apt install libmpv2
    pip install python-mpv

The player now runs in-process, so no external `mpv --input-ipc-server`
process is needed. An IPC socket is still exposed (optional) so external
tools can keep talking to it.
"""

import locale
import logging
import os
import threading
from typing import Any, Optional

# libmpv requires the C numeric locale; must be set before importing mpv.
locale.setlocale(locale.LC_NUMERIC, "C")
import mpv  # noqa: E402

log = logging.getLogger("player")

IPC_SOCKET = "/tmp/mpv_ipc.sock"          # set to None to disable
AUDIO_DEVICE = os.environ.get("MPV_AUDIO_DEVICE", "auto")  # e.g. "alsa/hifiberry"
CD_DEVICE_URI = "cdda:///dev/sr0"
CD_OPEN_TIMEOUT = 10.0                    # seconds to wait for the disc to open

VOLUME_STEP = 5   # percent per button press
VOLUME_MIN  = 0
VOLUME_MAX  = 100


class Player:
    def __init__(self):
        self._lock = threading.Lock()
        self._volume = 85          # default; synced from MPV on get
        self._cd_loaded = False    # True once cdda:///dev/sr0 is loaded

        log.info("Initialising player (python-mpv)…")
        opts = dict(
            video=False,
            audio_device=AUDIO_DEVICE,
            idle=True,             # stay alive with nothing loaded
            volume=self._volume,
            log_handler=self._on_mpv_log,
            loglevel="warn",
        )
        if IPC_SOCKET:
            opts["input_ipc_server"] = IPC_SOCKET
        self._mpv = mpv.MPV(**opts)

    # ── Helpers ───────────────────────────────────────────────────────────────
    @staticmethod
    def _on_mpv_log(level: str, prefix: str, text: str):
        log.log(
            {"fatal": logging.CRITICAL, "error": logging.ERROR,
             "warn": logging.WARNING}.get(level, logging.DEBUG),
            "mpv[%s] %s", prefix, text.rstrip(),
        )

    def _get(self, name: str, default: Any = None) -> Any:
        """Read a property; return `default` if unavailable or MPV is gone."""
        try:
            value = getattr(self._mpv, name)
        except (mpv.MPVError, mpv.ShutdownError, AttributeError) as e:
            log.debug("get %s failed: %s", name, e)
            return default
        return default if value is None else value

    def _set(self, name: str, value: Any):
        try:
            setattr(self._mpv, name, value)
        except (mpv.MPVError, mpv.ShutdownError) as e:
            log.warning("set %s=%r failed: %s", name, value, e)

    def _command(self, *args):
        try:
            self._mpv.command(*args)
        except (mpv.MPVError, mpv.ShutdownError) as e:
            log.warning("command %s failed: %s", args, e)

    # ── Playback ──────────────────────────────────────────────────────────────
    def play(self, uri: str):
        log.info("Play: %s", uri)
        if uri.startswith("cdda://"):
            try:
                track_num = int(uri.replace("cdda://", ""))
            except ValueError:
                log.error("Invalid CD track URI: %s", uri)
                return
            # MPV does NOT support cdda:///dev/sr0:N (track suffix) in v0.40.
            # Load the whole disc once; each track is exposed as a chapter.
            with self._lock:
                already_loaded = self._cd_loaded
                self._cd_loaded = True

            if not already_loaded:
                log.info("Loading CD disc (%s)", CD_DEVICE_URI)
                self._command("loadfile", CD_DEVICE_URI, "replace")
                self._set("pause", False)
                if track_num > 1:
                    # Wait for the chapter list instead of sleeping blindly.
                    try:
                        self._mpv.wait_for_property(
                            "chapter-list", lambda v: bool(v),
                            timeout=CD_OPEN_TIMEOUT,
                        )
                    except TimeoutError:
                        log.error("Timed out waiting for CD to open")
                        with self._lock:
                            self._cd_loaded = False
                        return
                    log.info("Seeking to chapter %d (track %d)",
                             track_num - 1, track_num)
                    self._set("chapter", track_num - 1)
            else:
                log.info("Seeking to chapter %d (track %d)",
                         track_num - 1, track_num)
                self._set("chapter", track_num - 1)
                self._set("pause", False)
        else:
            with self._lock:
                self._cd_loaded = False
            self._command("loadfile", uri, "replace")
            self._set("pause", False)

    def pause(self):
        self._set("pause", True)

    def resume(self):
        self._set("pause", False)

    def toggle_pause(self):
        """Toggle between paused and playing."""
        self._command("cycle", "pause")

    def stop(self):
        with self._lock:
            self._cd_loaded = False
        self._command("stop")

    # ── State queries ─────────────────────────────────────────────────────────
    def get_current_chapter(self) -> int:
        """Return current MPV chapter index (0-based), or -1 on error."""
        return int(self._get("chapter", -1))

    def get_position(self) -> float:
        return float(self._get("time_pos", 0.0))

    def get_duration(self) -> float:
        """Return duration of current file/stream in seconds."""
        return float(self._get("duration", 0.0))

    def get_chapter_list(self) -> list:
        """Return chapter list (CDDA: one entry per track, each has 'time' = start offset)."""
        return list(self._get("chapter_list", []))

    def is_idle(self) -> bool:
        return bool(self._get("idle_active", True))

    # ── Volume ────────────────────────────────────────────────────────────────
    def get_volume(self) -> int:
        vol = self._get("volume")
        if vol is not None:
            self._volume = int(vol)
        return self._volume

    def set_volume(self, level: int):
        level = max(VOLUME_MIN, min(VOLUME_MAX, level))
        self._volume = level
        self._set("volume", level)
        log.debug("Volume → %d", level)

    def volume_up(self):
        self.set_volume(self.get_volume() + VOLUME_STEP)

    def volume_down(self):
        self.set_volume(self.get_volume() - VOLUME_STEP)

    # ── Cleanup ───────────────────────────────────────────────────────────────
    def quit(self):
        try:
            self._mpv.terminate()
        except Exception:
            pass