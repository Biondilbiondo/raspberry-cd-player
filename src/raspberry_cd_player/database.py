import sqlite3
import logging

from .config import config

log = logging.getLogger("database")


def _get_conn():
    """Return a WAL-mode connection to the vault database."""
    conn = sqlite3.connect(config.database_file)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create the tracks table if it doesn't exist yet.

    Schema:
        vault_key TEXT NOT NULL
        track     INTEGER NOT NULL
        ntracks   INTEGER NOT NULL
        path      TEXT NOT NULL
        state     TEXT NOT NULL DEFAULT 'pending'   -- pending|ripping|ripped|done
        wav_path  TEXT                               -- cache WAV path
    """
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tracks (
                vault_key TEXT NOT NULL,
                track     INTEGER NOT NULL,
                ntracks   INTEGER NOT NULL,
                path      TEXT NOT NULL,
                state     TEXT NOT NULL DEFAULT 'pending',
                wav_path  TEXT,
                UNIQUE(vault_key, track)
            );
            CREATE INDEX IF NOT EXISTS idx_vault ON tracks(vault_key);
            CREATE INDEX IF NOT EXISTS idx_state ON tracks(state);
        """)


# ── Core CRUD ─────────────────────────────────────────────────────────────────

def db_add_entry(vault_key, ntracks, track, path="", state="pending", wav_path=""):
    """Insert or update a track record. Thread-safe.
    
    On conflict (same vault_key + track), updates path, ntracks, and state.
    """
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO tracks (vault_key, track, ntracks, path, state, wav_path) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(vault_key, track) DO UPDATE SET "
            "    path=excluded.path, "
            "    ntracks=excluded.ntracks, "
            "    wav_path=excluded.wav_path, "
            "    state=excluded.state",
            (vault_key, track, ntracks, path, state, wav_path),
        )
        conn.commit()


def db_get_track(vault_key, track):
    """Return a dict for the given track, or None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT vault_key, ntracks, track, path, state, wav_path "
            "FROM tracks WHERE vault_key = ? AND track = ?",
            (vault_key, track),
        ).fetchone()
        if row is None:
            return None
        return {
            "vault_key": row[0], "ntracks": row[1], "track": row[2],
            "path": row[3], "state": row[4], "wav_path": row[5],
        }


def db_update_track(vault_key, track, **fields):
    """Update one or more fields of a track record."""
    if not fields:
        return
    pairs = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [vault_key, track]
    with _get_conn() as conn:
        conn.execute(
            f"UPDATE tracks SET {pairs} WHERE vault_key = ? AND track = ?",
            values,
        )
        conn.commit()


# ── State helpers ─────────────────────────────────────────────────────────────

def db_get_pending_tracks(vault_key):
    """Return list of track numbers that are pending"""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT track FROM tracks "
            "WHERE vault_key = ? AND state = 'pending' "
            "ORDER BY track",
            (vault_key,),
        ).fetchall()
        return [r[0] for r in rows]


def db_get_ripped_tracks(vault_key):
    """Return list of track numbers that have been ripped (WAV ready)."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT track FROM tracks "
            "WHERE vault_key = ? AND state = 'ripped' "
            "ORDER BY track",
            (vault_key,),
        ).fetchall()
        return [r[0] for r in rows]


def db_get_done_tracks(vault_key):
    """Return list of track numbers that are fully archived."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT track FROM tracks "
            "WHERE vault_key = ? AND state = 'done' "
            "ORDER BY track",
            (vault_key,),
        ).fetchall()
        return [r[0] for r in rows]


def db_get_track_state(vault_key, track):
    """Return the state string for a track, or None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT state FROM tracks WHERE vault_key = ? AND track = ?",
            (vault_key, track),
        ).fetchone()
        return row[0] if row else None


def db_get_rip_status(vault_key, ntracks):
    """Return a list of state strings for all tracks 0..ntracks-1."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT track, state FROM tracks WHERE vault_key = ?",
            (vault_key,),
        ).fetchall()
        states = {t: s for t, s in rows}
        return [states.get(i, "pending") for i in range(ntracks)]


def db_is_cd_present(vault_key):
    """Return True if any track for this vault_key exists."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM tracks WHERE vault_key = ? LIMIT 1",
            (vault_key,),
        ).fetchone()
        return row is not None


def db_is_cd_complete(vault_key):
    """Return True if all tracks 0..ntracks-1 exist (no gaps)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT ntracks, COUNT(*) FROM tracks WHERE vault_key = ? GROUP BY ntracks",
            (vault_key,),
        ).fetchone()
        if row is None:
            return False
        ntracks, count = row
        return count == ntracks


def db_get_all_keys():
    """Return a sorted list of all unique vault keys."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT vault_key FROM tracks ORDER BY vault_key"
        ).fetchall()
        return [r[0] for r in rows]


if __name__ == "__main__":
    init_db()
    print("OK - init_db ran without errors.")
