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
    """Create the tracks table if it doesn't exist yet."""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS tracks (
                vault_key TEXT NOT NULL,
                track     INTEGER NOT NULL,
                ntracks   INTEGER NOT NULL,
                path      TEXT NOT NULL,
                UNIQUE(vault_key, track)
            );
            CREATE INDEX IF NOT EXISTS idx_vault ON tracks(vault_key);
        """)


def db_add_entry(vault_key, ntracks, track, path):
    """Insert (or ignore) a track record. Thread-safe."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO tracks (vault_key, track, ntracks, path) VALUES (?, ?, ?, ?)",
            (vault_key, track, ntracks, path),
        )
        conn.commit()


def db_get_track(vault_key, track):
    """Return a dict for the given track, or None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT vault_key, ntracks, track, path FROM tracks "
            "WHERE vault_key = ? AND track = ?",
            (vault_key, track),
        ).fetchone()
        return {"vault_key": row[0], "ntracks": row[1], "track": row[2], "path": row[3]} if row else None


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
