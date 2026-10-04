#!/usr/bin/env python3
"""
main.py - Core state machine for the HiFiBerry Music Player
"""

import re
import sys
import time
import multiprocessing
import threading
import logging
import os
import configparser

config = configparser.ConfigParser()
config.read('config.ini')

CD_VAULT = config.get('storage', 'vault_dir')
CACHE_DIR = config.get('storage', 'cache_dir')
VAULT_AUDIO_EXTS = {".flac", ".wav", ".mp3", ".ogg", ".m4a"}
AUTOMATIC_RIPPING = True

import pygame

from .player import Player
from .ui import UI
from .metadata_manager import MetadataManager
from .cd_handler import CDHandler
from .input_handler import InputHandler, InputEvent
from .library_manager import LibraryManager
from .cd_rip import _rip_chain, _rip_done_file, _rip_doing_file, _rip_wav_file, _rip_estimated_len_s

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("main")

# ── States ────────────────────────────────────────────────────────────────────
class State:
    MAIN_MENU       = "main_menu"
    LIBRARY_BROWSE  = "library_browse"
    CD_LOADING      = "cd_loading"
    PLAYBACK        = "playback"
    WRAPPED_SUMMARY = "wrapped_summary"


class MusicPlayer:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("HiFiBerry Player")

        self.player   = Player()
        self.ui       = UI()
        self.metadata = MetadataManager()
        self.cd       = CDHandler()
        self.library  = LibraryManager()
        self.input    = InputHandler()

        self.state          = State.MAIN_MENU
        self.prev_state     = None
        self.album_info     = {}
        self.tracklist      = []
        self.current_track  = 0
        self.paused         = False
        self.source         = None # "CD-RIP", "library"

        self.main_menu_items = ["Play CD", "Library", "Music Wrapped", "Quit"]
        self.main_menu_sel   = 0
        self.library_albums  = []
        self.library_sel     = 0

        self._running        = True
        self._last_play_time = 0
        self._last_chapter_check = 0
        self._disc_id_str    = None   # stored so hot-reload can re-fetch
        self._override_mtime = 0      # mtime of disc_overrides.json at last load

        # Progress bar state
        self._playback_pos   = 0.0
        self._playback_dur   = 0.0

        self._rip_process    = None
        self._rip_dir        = None

        self._cd_monitor = threading.Thread(target=self._monitor_cd, daemon=True)
        self._cd_monitor.start()

    @property
    def is_ripping(self):
        if self._rip_process is not None and self._rip_process.is_alive():
            return True
        return False

    def _rip_async(self, track):
        if not self.is_ripping:
            self._rip_process = multiprocessing.Process(target=_rip_chain, 
                                                        args=(self._rip_dir, 
                                                              track, 
                                                              len(self.tracklist), 
                                                              self.album_info['mbid'],
                                                              self.album_info['art_path']))
            self._rip_process.start()
        else:
            log.error("Cannot start ripping, another ripping process is alive!")

    def get_rip_status(self):
        if self._rip_dir is None:
            return None
        status = []
        for track in range(len(self.tracklist)):
            if os.path.exists(os.path.join(self._rip_dir, f"track{track+1:02d}.doing")):
                status += ['doing']
            elif os.path.exists(os.path.join(self._rip_dir, f"track{track+1:02d}.done")):
                status += ['done']
            else:
                status += ['wait']
        return status

    def _kill_rip(self):
        if not self.is_ripping:
            return
        import signal
        log.info("Trying to kill previous ripping process")
        pid = self._rip_process.pid
        os.killpg(pid, signal.SIGKILL)
        while self.is_ripping:
            log.info("Waiting killed ripping process")
            time.sleep(1)
        log.info("Process killed")
        
        for name in os.listdir(self._rip_dir):
            path = os.path.join(self._rip_dir, name)
            if name.endswith(".doing") and os.path.isfile(path):
                os.remove(path)
                log.info(f"Deleted: {path}")

    def _monitor_cd(self):
        was_present = False
        while self._running:
            present = self.cd.is_disc_present()
            if present and not was_present:
                log.info("CD inserted – triggering load")
                self._schedule(self._load_cd)
                was_present = True
            elif not present and was_present:
                if self.state != State.CD_LOADING:
                    log.info("CD removed")
                    if self.state == State.PLAYBACK and self.source in ("cd-rip"):
                        self.player.stop()
                        self.state = State.MAIN_MENU
                    was_present = False
                # TODO Clean cache
            was_present = present
            time.sleep(2)

    def _schedule(self, fn, *args):
        ev = pygame.event.Event(pygame.USEREVENT, {"fn": fn, "args": args})
        pygame.event.post(ev)

    def _load_cd(self):
        self.state = State.CD_LOADING
        self.ui.show_loading("Reading CD…")

        # Set drive speed so disc is ready before any detection
        self.cd.set_speed()

        # Try data disc first (blkid check) — fallback is tried again below
        # in case blkid requires root and silently fails.
        if self.cd.is_data_disc():
            self.ui.show_message("Data disc – no audio files found", duration=3)
            self.state = State.MAIN_MENU
            return

        _disc_info = self.cd.get_disc_info()

        if not _disc_info['tracks']:
            log.warning("No tracks found on CD")
            self.ui.show_message("Read error or empty CD", duration=3)
            self.state = State.MAIN_MENU
            return

        self._disc_id_str = _disc_info['cd-discid-str']

        # TODO Check if the disc is already in the DB.
        vault_key = _disc_info["mb_id"]

        self._rip_dir = os.path.join(CACHE_DIR, vault_key)
        meta    = self.metadata.fetch_cd_metadata(_disc_info)

        self.album_info = meta
        self.tracklist  = meta.get("tracks", _disc_info['tracks'])
        self._override_mtime = 0

        # Normalise durations to MM:SS strings
        for i, t in enumerate(self.tracklist):
            # Extract duration from disc directly to avoid issues
            dd = float(_disc_info['tracks'][i]['duration'])
            t['disc_duration_s'] = dd
            t['disc_duration'] = f"{int(dd // 60):02d}:{int(dd % 60):02d}"

            d = t.get("duration", 0)
            if isinstance(d, (int, float)):
                t["duration"] = f"{int(d // 60):02d}:{int(d % 60):02d}"
            else:
                t["duration"] = str(d)

        self.source        = "cd-rip"
        self.current_track = 0
        self._start_playback()

    def _load_library_album(self, album):
        self.ui.show_loading(f"Loading {album.get('title', '?')}…")
        local_tracks = album.get("tracks", [])
        meta = self.metadata.fetch_album_metadata(
            album.get("artist", ""), album.get("title", ""), album.get("year")
        )
        # MusicBrainz tracks have no file paths — always use the local tracks
        # which come from the library scanner and contain the actual file paths.
        meta["tracks"] = local_tracks
        # Local cover.jpg always wins — the user placed it intentionally.
        # Fall back to MusicBrainz art only when no local file exists.
        meta["art_path"] = album.get("art_path") or meta.get("art_path")
        self.album_info    = meta
        self.tracklist     = local_tracks
        self.source        = "library"
        self.current_track = 0
        self._start_playback()

    def _reset_progress(self):
        """Clear cached progress state when switching to a new source/album."""
        self._playback_pos  = 0.0
        self._playback_dur  = 0.0
        self._chapter_times = []

    def _start_playback(self):
        self._reset_progress()
        self.state  = State.PLAYBACK
        self.paused = False
        self._play_track(self.current_track)

    def _play_track(self, idx: int):
        log.info(f"Start playing track {idx+1} while ripping")

        if not self.tracklist:
            return
        idx = max(0, min(idx, len(self.tracklist) - 1))
        self.current_track = idx
        track = self.tracklist[idx]

        if not os.path.exists(_rip_done_file(self._rip_dir, idx)):
            self.player.pause()

            if self.is_ripping and not os.path.exists(_rip_doing_file(self._rip_dir, idx)):
                # If ripping is not started, start ripping it now!
                self._kill_rip()
            if not self.is_ripping:
                self._rip_async(idx)

            path = _rip_wav_file(self._rip_dir, idx)

            # Wait for the file to be long enough to be played
            LEAD = 5
            TIMEOUT = 30
            deadline = time.time() + TIMEOUT
            while time.time() < deadline:
                try:
                    if _rip_estimated_len_s(path) >= LEAD:
                        with open(path, 'rb') as f:
                            if f.read(4) == b'RIFF':   # header is really there
                                break
                except FileNotFoundError:
                    pass
                time.sleep(0.1)

            if time.time() >= deadline:
                log.error(f"Waited the start of the rip for {TIMEOUT} s, but didn't started.")
                raise FileNotFoundError(f"File {path} not ready for the player.")
            else:
                log.info(f"Ready to play {path} while ripper runs")

        uri = _rip_wav_file(self._rip_dir, idx)

        self._last_play_time = time.monotonic()
        self._playback_pos   = 0.0
        self._playback_dur   = self.tracklist[idx]['disc_duration_s']
        self.player.play(uri)
        self.paused = False
        log.info("Playing track %d: %s", idx + 1, track.get("title"))

    def _handle_input(self, event: InputEvent):
        s = self.state

        # ── Main menu ─────────────────────────────────────────────────────────
        if s == State.MAIN_MENU:
            if event == InputEvent.UP:
                self.main_menu_sel = (self.main_menu_sel - 1) % len(self.main_menu_items)
            elif event == InputEvent.DOWN:
                self.main_menu_sel = (self.main_menu_sel + 1) % len(self.main_menu_items)
            elif event == InputEvent.FIRE:
                self._main_menu_select()
            elif event == InputEvent.BACK:
                pass   # nothing to go back to from main menu

        # ── Library browser ───────────────────────────────────────────────────
        elif s == State.LIBRARY_BROWSE:
            if event == InputEvent.UP:
                self.library_sel = max(0, self.library_sel - 1)
            elif event == InputEvent.DOWN:
                self.library_sel = min(len(self.library_albums) - 1, self.library_sel + 1)
            elif event == InputEvent.FIRE:
                if self.library_albums:
                    self._load_library_album(self.library_albums[self.library_sel])
            elif event == InputEvent.BACK:
                self.state = State.MAIN_MENU

        # ── Playback ──────────────────────────────────────────────────────────
        elif s == State.PLAYBACK:
            if event == InputEvent.FIRE:
                # Toggle pause / resume
                self.paused = not self.paused
                if self.paused:
                    self.player.pause()
                else:
                    self.player.resume()

            elif event == InputEvent.UP:
                self._play_track(self.current_track - 1)

            elif event == InputEvent.DOWN:
                self._play_track(self.current_track + 1)

            elif event == InputEvent.BACK:
                self.player.stop()
                self.state = self.prev_state or State.MAIN_MENU

        # ── Wrapped summary ───────────────────────────────────────────────────
        elif s == State.WRAPPED_SUMMARY:
            if event in (InputEvent.BACK, InputEvent.FIRE):
                self.state = State.MAIN_MENU

    def _main_menu_select(self):
        item = self.main_menu_items[self.main_menu_sel]
        if item == "Play CD":
            if self.cd.is_disc_present():
                self._load_cd()
            else:
                self.ui.show_message("No CD detected", duration=2)
        elif item == "Library":
            self.library_albums = self.library.get_albums()
            self.library_sel    = 0
            self.prev_state     = State.MAIN_MENU
            self.state          = State.LIBRARY_BROWSE
        elif item == "Music Wrapped":
            self.state = State.WRAPPED_SUMMARY
        elif item == "Quit":
            self._running = False


    def run(self):
        clock = pygame.time.Clock()
        while self._running:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    self._running = False
                elif ev.type == pygame.USEREVENT and hasattr(ev, "fn"):
                    try:
                        ev.fn(*ev.args)
                    except Exception as exc:
                        log.exception("Scheduled call failed: %s", exc)
                else:
                    inp = self.input.process(ev)
                    if inp:
                        self._handle_input(inp)

            s = self.state
            if s == State.MAIN_MENU:
                self.ui.draw_main_menu(self.main_menu_items, self.main_menu_sel)
            elif s == State.LIBRARY_BROWSE:
                self.ui.draw_library(self.library_albums, self.library_sel)
            elif s == State.CD_LOADING:
                pass   # ui.show_loading() already painted
            elif s == State.PLAYBACK:
                if self.player.is_idle():
                    if self.current_track + 1 < len(self.tracklist):
                        self._play_track(self.current_track + 1)
                    else:
                        self.state = State.MAIN_MENU

                pos = self.player.get_position()
                self._playback_pos = pos
                current_rip_pos = _rip_estimated_len_s(_rip_wav_file(self._rip_dir, self.current_track))
                self.ui.draw_playback(
                    album_info    = self.album_info,
                    tracklist     = self.tracklist,
                    current_track = self.current_track,
                    paused        = self.paused,
                    position      = self._playback_pos,
                    ripped_pos    = current_rip_pos,
                    ripped_status = self.get_rip_status(),
                    duration      = self._playback_dur,
                )

            pygame.display.flip()
            clock.tick(30)

        self.player.stop()
        self.player.quit()
        self._kill_rip()
        pygame.quit()
        log.info("Goodbye.")

def main():
    app = MusicPlayer()
    app.run()
