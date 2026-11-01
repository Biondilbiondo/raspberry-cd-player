import sqlite3
import logging
import os

from .config import config

log = logging.getLogger("database")


def _get_conn():
    """Return a WAL-mode connection to the vault database."""
    conn = sqlite3.connect(config.database_file)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create the tracks table if it doesn't exist yet, with migration.

    Schema:
        vault_key TEXT NOT NULL
        track     INTEGER NOT NULL
        ntracks   INTEGER NOT NULL
        path      TEXT NOT NULL
        state     TEXT NOT NULL DEFAULT 'pending'   -- pending|ripping|ripped|done
        wav_path  TEXT                               -- cache WAV path
        untagged  INTEGER NOT NULL DEFAULT 0       -- 1 = archived without metadata

    Migration for pre-existing databases: adds the `untagged` column and, when the
    column had to be added, flags all already-archived tracks (state = 'done') as
    untagged — archives created before the untagged feature exist had no metadata.
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
                untagged  INTEGER NOT NULL DEFAULT 0,
                UNIQUE(vault_key, track)
            );
            CREATE INDEX IF NOT EXISTS idx_vault ON tracks(vault_key);
            CREATE INDEX IF NOT EXISTS idx_state ON tracks(state);
        """)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(tracks)").fetchall()]
        migrated = False
        if "untagged" not in cols:
            conn.execute("ALTER TABLE tracks ADD COLUMN untagged INTEGER NOT NULL DEFAULT 0")
            migrated = True
        if migrated:
            conn.execute("UPDATE tracks SET untagged = 1 WHERE state = 'done'")
        conn.commit()


def db_add_entry(vault_key, ntracks, track, path="", state="pending", wav_path="", untagged=1):
    """Insert or update a track record. Thread-safe.
    
    On conflict (same vault_key + track), updates path, ntracks, state and untagged.
    """
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO tracks (vault_key, track, ntracks, path, state, wav_path, untagged) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(vault_key, track) DO UPDATE SET "
            "    path=excluded.path, "
            "    ntracks=excluded.ntracks, "
            "    wav_path=excluded.wav_path, "
            "    state=excluded.state, "
            "    untagged=excluded.untagged",
            (vault_key, track, ntracks, path, state, wav_path, untagged),
        )
        conn.commit()


def db_get_track(vault_key, track):
    """Return a dict for the given track, or None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT vault_key, ntracks, track, path, state, wav_path, untagged "
            "FROM tracks WHERE vault_key = ? AND track = ?",
            (vault_key, track),
        ).fetchone()
        if row is None:
            return None
        return {
            "vault_key": row[0], "ntracks": row[1], "track": row[2],
            "path": row[3], "state": row[4], "wav_path": row[5],
            "untagged": bool(row[6]),
        }


# ── Core CRUD ─────────────────────────────────────────────────────────────────

def db_update_path(old_path: str, new_path: str) -> None:
    """Update a track's path by searching on the old path.
    
    Raises ValueError if no track with the given old_path is found.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT vault_key, track FROM tracks WHERE path = ?",
            (old_path,),
        ).fetchone()
        if row is None:
            raise ValueError(f"db_update_path: no track found with path={old_path}")
        vault_key, track = row
        conn.execute(
            "UPDATE tracks SET path = ? WHERE vault_key = ? AND track = ?",
            (new_path, vault_key, track),
        )
        conn.commit()


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
    """Return True if all tracks are done and their files exist on disk.
    
    If any track has a missing file, reset its state to pending and return False.
    """
    from .paths import encoded_file_path, wav_file_path
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT track, ntracks, path, wav_path, state FROM tracks "
            "WHERE vault_key = ? ORDER BY track",
            (vault_key,),
        ).fetchall()
        if not rows:
            return False
        ntracks = rows[0][1]
        if len(rows) != ntracks:
            return False
        for track, _, path, wav_path, state in rows:
            if not os.path.exists(path):
                if os.path.exists(wav_path) and state != 'ripped':
                    log.warning(f"Correcting db for track {vault_key}/{track} {state}->ripped")
                    conn.execute(
                        "UPDATE tracks SET state = 'ripped', "
                        f"path='{encoded_file_path(vault_key, track)}' "
                        "WHERE vault_key = ? AND track = ?",
                        (vault_key, track),
                    )
                    state = 'ripped'
                    conn.commit()

                elif state != 'pending':
                    log.warning(f"Correcting db for track {vault_key}/{track} {state}->pending")
                    conn.execute(
                        "UPDATE tracks SET state = 'pending', "
                        f"path='{encoded_file_path(vault_key, track)}', wav_path='{wav_file_path(vault_key, track)}' "
                        "WHERE vault_key = ? AND track = ?",
                        (vault_key, track),
                    )
                    state = 'pending'
                    conn.commit()

            if state != 'done':
                return False
            
        return True


def db_get_all_keys():
    """Return a sorted list of all unique vault keys."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT vault_key FROM tracks ORDER BY vault_key"
        ).fetchall()
        return [r[0] for r in rows]


def db_get_all_keys_with_order():
    """Return (vault_key, first_rowid) for every vault key, ordered by DB insertion order."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT vault_key, MIN(rowid) FROM tracks GROUP BY vault_key ORDER BY MIN(rowid)"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]


if __name__ == "__main__":
    init_db()
    print("OK - init_db ran without errors.")
