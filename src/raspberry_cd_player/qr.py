"""
qr.py - Address of the web editor and its QR code.

* `editor_url(vault_key)`  -> "http://<lan-ip>:<port>/disc/<vault_key>" or None
* `qr_matrix(text)`        -> list of rows of 0/1 (no quiet zone) or None

The host in the URL is, in this order:
  1. `[web] public_host` from config.ini (use it for a fixed name such as
     "cdplayer.local"),
  2. the IP address of the interface that has the default route.
If the Pi has no network address, `editor_url()` returns None and the UI shows
a "no network" message instead of a code.

`segno` is a pure-Python QR encoder (no Pillow needed). If it is not installed
the feature switches itself off and the rest of the player is unaffected.
"""

import logging
import socket
from typing import List, Optional
from urllib.parse import quote

from .config import config

log = logging.getLogger("qr")

try:
    import segno
except ImportError:  # pragma: no cover - optional dependency
    segno = None
    log.warning("segno is not installed - QR codes are disabled (uv sync)")


def available() -> bool:
    return segno is not None


def local_ip() -> Optional[str]:
    """IPv4 address of the interface used to reach the LAN, or None.

    A UDP "connect" sends no packet; it only makes the kernel pick the outgoing
    interface, whose address we then read back.
    """
    for target in ("10.255.255.255", "192.0.2.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(0.2)
            s.connect((target, 1))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127.") and ip != "0.0.0.0":
                return ip
        except OSError:
            pass
        finally:
            s.close()
    return None


def editor_url(vault_key: str) -> Optional[str]:
    host = config.web_public_host or local_ip()
    if not host:
        return None
    return f"http://{host}:{config.web_port}/disc/{quote(str(vault_key), safe='')}"


def qr_matrix(text: str) -> Optional[List[List[int]]]:
    """QR modules for `text` (1 = dark). ECC level M, no quiet zone."""
    if segno is None:
        return None
    try:
        code = segno.make(text, error="m", micro=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("QR encoding failed: %s", exc)
        return None
    return [list(row) for row in code.matrix]
