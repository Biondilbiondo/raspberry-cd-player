import locale
import logging
import threading
from typing import Any

# libmpv requires the C numeric locale; must be set before importing mpv.
locale.setlocale(locale.LC_NUMERIC, "C")
import mpv  # noqa: E402
import configparser

log = logging.getLogger("player")
config = configparser.ConfigParser()
config.read('config.ini')

CD_DEVICE_URI = config.get('cd', 'cd_device_uri')
CD_OPEN_TIMEOUT = config.get('cd', 'cd_open_timeout')

VOLUME_STEP = 5   # percent per button press
VOLUME_MIN  = 0
VOLUME_MAX  = 100

class Player:
    def __init__(self):
        self._lock = threading.Lock()
        self._cd_loaded = False    # True once cdda:///dev/sr0 is loaded

        log.info("Initialising player (python-mpv)…")
        opts = dict(
            video=False,
            idle=True,             # stay alive with nothing loaded
            log_handler=self._on_mpv_log,
            loglevel="warn",
        )
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
            log.error("Playing directly from cdda is no longer supported!")
            raise NotImplemented("Cannot play directly from cdda://")
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
    def get_position(self) -> float:
        return float(self._get("time_pos", 0.0))

    def get_duration(self) -> float:
        """Return duration of current file/stream in seconds."""
        return float(self._get("duration", 0.0))

    def is_idle(self) -> bool:
        return bool(self._get("idle_active", True))

    # ── Volume ────────────────────────────────────────────────────────────────
    def get_volume(self) -> int:
        vol = self._get("volume")
        if vol is not None:
            self._volume = int(vol)
        return self._volume

    # ── Cleanup ───────────────────────────────────────────────────────────────
    def quit(self):
        try:
            self._mpv.terminate()
        except Exception:
            pass