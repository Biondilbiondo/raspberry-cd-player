"""
Centralised configuration for the music player.

Reads config.ini once at import time and exposes typed values through the
`config` namespace.  Each module imports from here instead of reading the
INI file themselves.

Overrides (highest → lowest):
  1. Environment variables (e.g. PLAYER_WORKING_DIR, PLAYER_DB_PATH)
  2. config.ini file
  3. Hardcoded defaults below
"""

import configparser
import logging
import os

log = logging.getLogger("player.config")

# ── Defaults ─────────────────────────────────────────────────────────────────

_DEFAULTS = {
    "log.level":           "info",
    "storage.vault_dir":   "./data/vault",
    "storage.library_dir": "./data",
    "storage.database_file": "./data/db.sqlite",
    "storage.art_cache":   "./data/cache/art",
    "storage.cache_dir":   "./data/cache/player",
    "storage.vault_format": "{artist}/{album} ({year})/{track_num:02d} - {track_title}",
    "cd.device_uri":       "cdda:///dev/sr0",
    "cd.open_timeout":     "10",
    "cd.speed":            "16",
    "cd.device":           "/dev/sr0",
    "cdparanoia.flags":    "-Z -S",
    "musicbrainz.agent":   "HiFiBerryMusicPlayer",
    "musicbrainz.version": "3.0",
    "musicbrainz.mail":    "none",
    "ffmpeg.options":      "-codec:a flac -compression_level 8 -sample_fmt s16 -ar 44100",
    "ffmpeg.format":       "flac",
}


def _resolve_path(raw: str) -> str:
    """Convert a relative path into an absolute one, using WORKING_DIR as base."""
    if not os.path.isabs(raw):
        base = os.environ.get("PLAYER_WORKING_DIR", os.getcwd())
        return os.path.normpath(os.path.join(base, raw))
    return os.path.normpath(raw)


# ── Read config ──────────────────────────────────────────────────────────────

_config_parser = configparser.ConfigParser()

# Determine which config file(s) to load.
_config_file = os.environ.get("PLAYER_CONFIG", "config.ini")
if os.path.isfile(_config_file):
    _config_parser.read(_config_file)
    log.info("Loaded config from %s", _config_file)
else:
    log.warning("Config file %s not found — using defaults.", _config_file)


class _Config:
    """Namespace-like object.  Access via config.section or config.option."""

    # -- Logging --
    @property
    def log_level(self) -> int:
        level = _config_parser.get("log", "level", fallback="info")
        return getattr(logging, level.upper(), logging.INFO)

    # -- Storage --
    @property
    def vault_dir(self) -> str:
        return _resolve_path(_config_parser.get("storage", "vault_dir"))

    @property
    def library_dir(self) -> str:
        return _resolve_path(_config_parser.get("storage", "library_dir"))

    @property
    def database_file(self) -> str:
        return _resolve_path(_config_parser.get("storage", "database_file"))

    @property
    def art_cache(self) -> str:
        return _resolve_path(_config_parser.get("storage", "art_cache"))

    @property
    def cache_dir(self) -> str:
        return _resolve_path(_config_parser.get("storage", "cache_dir"))

    @property
    def vault_format(self) -> str:
        return _config_parser.get("storage", "vault_format")

    # -- CD --
    @property
    def cd_device_uri(self) -> str:
        return _config_parser.get("cd", "device_uri", fallback=_DEFAULTS["cd.device_uri"])

    @property
    def cd_open_timeout(self) -> int:
        return int(_config_parser.get("cd", "open_timeout", fallback="10"))

    @property
    def cd_speed(self) -> int:
        return int(_config_parser.get("cd", "speed", fallback="16"))

    @property
    def cd_device(self) -> str:
        return _config_parser.get("cd", "device", fallback=_DEFAULTS["cd.device"])

    @property
    def cdparanoia_flags(self) -> str:
        raw = _config_parser.get("cdparanoia", "flags", fallback=_DEFAULTS["cdparanoia.flags"])
        for f in raw.split():
            if f in ("-S", "-d"):
                log.warning(
                    "cdparanoia flags contain %s — speed and device are set by config "
                    "([cd] cd_speed / cd_device). The flag will be silently ignored.",
                    f,
                )
        return raw

    # -- MusicBrainz --
    @property
    def mb_agent(self) -> str:
        return _config_parser.get("musicbrainz", "agent", fallback="HiFiBerryMusicPlayer")

    @property
    def mb_version(self) -> str:
        return _config_parser.get("musicbrainz", "version", fallback="3.0")

    @property
    def mb_mail(self) -> str:
        return _config_parser.get("musicbrainz", "mail", fallback="none")

    # -- FFmpeg --
    @property
    def ffmpeg_options(self) -> str:
        return _config_parser.get("ffmpeg", "options", fallback=_DEFAULTS["ffmpeg.options"])

    @property
    def ffmpeg_format(self) -> str:
        return _config_parser.get("ffmpeg", "format", fallback=_DEFAULTS["ffmpeg.format"])


# Single shared instance
config = _Config()
