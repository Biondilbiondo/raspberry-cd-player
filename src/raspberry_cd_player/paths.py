from .config import config
import os

def wav_file_path(vault_key: str, track: int) -> str:
    """Path to the WAV file in the cache."""
    return os.path.join(config.cache_dir, vault_key, f"track{track+1:02d}.wav")

def encoded_file_path(vault_key: str, track: int) -> str:
    """Path to the WAV file in the cache."""
    return os.path.join(config.cache_dir, vault_key, f"track{track+1:02d}.{config.ffmpeg_format}")
    