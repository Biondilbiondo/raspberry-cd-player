#!/usr/bin/env python3
"""
ui.py - Pygame UI for the HiFiBerry CD player (DRM/KMS, 480x800 portrait design).

All drawing happens on a fixed 480x800 portrait canvas (UI.screen).
UI.present() rotates and scales that canvas onto the real display, so the same
code works whether the kernel exposes the panel as portrait (1080x1920) or as
landscape (1920x1080). Call ui.present() instead of pygame.display.flip().

Environment variables
---------------------
UI_ROT=0|90|180|270   force software rotation (default: auto-detect from the
                      panel's size; landscape panel -> 90, portrait panel -> 0).
                      If the picture is upside down, swap 90 <-> 270.
UI_WINDOWED=1         run in a window instead of fullscreen (desktop testing)

Playback screen layout (canvas coordinates)
-------------------------------------------
  6 -  48   Artist
 52 -  88   Album (year)
 92 - 492   Album art 400x400
500 - 530   Now playing title
530 - 538   Progress bar
548 - 800   Scrollable tracklist (8 rows)
"""

import logging
import os
import time
from typing import Dict, List, Optional, Tuple

import pygame

log = logging.getLogger("ui")

WIDTH = 540
HEIGHT = 960

# Colour palette
C_BG        = (15,  15,  20)
C_ACCENT    = (255, 165,  0)   # amber
C_TEXT      = (230, 230, 230)
C_SUBTEXT   = (150, 150, 160)
C_HIGHLIGHT = (40,  40,  55)
C_PLAYING   = (255, 165,  0)
C_WHITE     = (255, 255, 255)
C_PROGRESS  = (80,  80,  90)
C_RIPPED    = (150, 150, 150)
C_ART_BG    = (40,  40,  50)

# Default pygame font sizes
FONT_TITLE_SIZE    = 40   # artist name
FONT_SUBTITLE_SIZE = 32   # album name
FONT_BOLD_SIZE     = 32
FONT_NORMAL_SIZE   = 26
FONT_SMALL_SIZE    = 21

ELLIPSIS = "..."          # ASCII on purpose: the default font may lack U+2026

ART_SIZE = (400, 400)
ART_Y    = 92
ART_CACHE_MAX    = 8
ART_RETRY_SECS   = 5.0    # retry a failed cover load after this long (art may arrive late)

TRACK_LIST_Y   = 548
VISIBLE_TRACKS = 8
INDICATOR_X    = WIDTH - 70
INDICATOR_R    = 10
BACK_BUTTON_RECT = pygame.Rect(0, 0, 40, 40)


def _pick_rotation() -> int:
    """Software rotation (degrees counter-clockwise) needed to show a portrait canvas."""
    env = os.environ.get("UI_ROT")
    if env is not None:
        try:
            rot = int(env)
            if rot in (0, 90, 180, 270):
                return rot
        except ValueError:
            pass
        log.warning("Ignoring invalid UI_ROT=%r", env)
    try:
        dw, dh = pygame.display.get_desktop_sizes()[0]
    except (pygame.error, IndexError):
        return 0
    return 90 if dw > dh else 0


class UI:
    def __init__(self):
        if not pygame.display.get_init():
            pygame.display.init()
        if not pygame.font.get_init():
            pygame.font.init()

        self._rot = _pick_rotation()
        self._frame_size = (HEIGHT, WIDTH) if self._rot in (90, 270) else (WIDTH, HEIGHT)

        flags = 0 if os.environ.get("UI_WINDOWED") else pygame.FULLSCREEN
        self._scaled = True
        try:
            # SCALED: SDL stretches this small surface to the panel (aspect kept, black bars)
            self._display = pygame.display.set_mode(self._frame_size, flags | pygame.SCALED)
        except pygame.error as e:
            log.warning("SCALED mode failed (%s); scaling manually", e)
            self._scaled = False
            self._display = pygame.display.set_mode((0, 0), flags)

        dw, dh = self._display.get_size()
        self._dest = pygame.Rect(0, 0, dw, dh)
        if not self._scaled:
            fw, fh = self._frame_size
            k = min(dw / fw, dh / fh)
            self._dest.size = (int(fw * k), int(fh * k))
            self._dest.center = (dw // 2, dh // 2)
        log.info("Display %dx%d, rotation %d, scaled=%s", dw, dh, self._rot, self._scaled)

        # All drawing code uses self.screen: an off-screen 480x800 canvas
        self.screen = pygame.Surface((WIDTH, HEIGHT))

        pygame.display.set_caption("HiFiBerry Player")
        pygame.mouse.set_visible(False)

        self._load_fonts()
        self._text_cache: Dict[tuple, pygame.Surface] = {}
        self._art_cache: Dict[str, pygame.Surface] = {}
        self._art_failed: Dict[str, float] = {}
        self._placeholder = self._make_placeholder()

        self._overlay = pygame.Surface((WIDTH, 60), pygame.SRCALPHA)
        self._overlay.fill((0, 0, 0, 180))
        self._message_until = 0.0
        self._message_text = ""
        self._loading_text = ""
        self._is_loading = False

        self._track_scroll = 0

    # -- Output ---------------------------------------------------------------
    def present(self):
        """Rotate/scale the canvas onto the real display and flip."""
        if os.environ.get("SHOW_CURSOR") == "1":
            mouse_pos = pygame.mouse.get_pos()
            canvas_pos = self.to_canvas(mouse_pos)
            pygame.draw.circle(self.screen, (255, 0, 0), canvas_pos, 5)

        frame = pygame.transform.rotate(self.screen, self._rot) if self._rot else self.screen
        if self._scaled:
            self._display.blit(frame, (0, 0))
        else:
            self._display.fill((0, 0, 0))
            self._display.blit(pygame.transform.scale(frame, self._dest.size), self._dest.topleft)
        pygame.display.flip()

    def to_canvas(self, pos: Tuple[int, int]) -> Tuple[int, int]:
        """Map a mouse/touch position from the real display to canvas coordinates."""
        x, y = pos
        fw, fh = self._frame_size
        if not self._scaled:
            x = (x - self._dest.x) * fw / self._dest.w
            y = (y - self._dest.y) * fh / self._dest.h
        x = max(0, min(fw - 1, int(x)))
        y = max(0, min(fh - 1, int(y)))
        if self._rot == 90:
            return WIDTH - 1 - y, x
        if self._rot == 270:
            return y, HEIGHT - 1 - x
        if self._rot == 180:
            return WIDTH - 1 - x, HEIGHT - 1 - y
        return x, y

    # -- Fonts ----------------------------------------------------------------
    def _load_fonts(self):
        self.font_title    = pygame.font.Font(None, FONT_TITLE_SIZE)
        self.font_subtitle = pygame.font.Font(None, FONT_SUBTITLE_SIZE)
        self.font_bold     = pygame.font.Font(None, FONT_BOLD_SIZE)
        self.font_normal   = pygame.font.Font(None, FONT_NORMAL_SIZE)
        self.font_small    = pygame.font.Font(None, FONT_SMALL_SIZE)

    # -- Helpers --------------------------------------------------------------
    def _make_placeholder(self) -> pygame.Surface:
        """Cover placeholder with a drawn music note (no font glyph needed)."""
        surf = pygame.Surface(ART_SIZE)
        surf.fill(C_ART_BG)
        cx, cy = ART_SIZE[0] // 2, ART_SIZE[1] // 2
        pygame.draw.ellipse(surf, C_ACCENT, (cx - 60, cy + 20, 70, 50))
        pygame.draw.rect(surf, C_ACCENT, (cx + 2, cy - 80, 10, 105))
        pygame.draw.polygon(surf, C_ACCENT,
                            [(cx + 12, cy - 80), (cx + 60, cy - 50), (cx + 60, cy - 28), (cx + 12, cy - 55)])
        return surf

    def _render_text(self, text: str, font, color=C_TEXT, max_width: int = WIDTH - 20) -> pygame.Surface:
        """Render text, truncating with an ellipsis if too wide. Results are cached."""
        text = str(text)
        key = (text, id(font), color, max_width)
        surf = self._text_cache.get(key)
        if surf is not None:
            return surf

        if font.size(text)[0] <= max_width:
            surf = font.render(text, True, color)
        else:
            lo, hi = 0, len(text)          # binary search: longest prefix that fits
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if font.size(text[:mid].rstrip() + ELLIPSIS)[0] <= max_width:
                    lo = mid
                else:
                    hi = mid - 1
            surf = font.render(text[:lo].rstrip() + ELLIPSIS, True, color)

        if len(self._text_cache) > 300:
            self._text_cache.clear()
        self._text_cache[key] = surf
        return surf

    def _draw_text_centered(self, text: str, font, y: int, color=C_TEXT, max_width: int = WIDTH - 20):
        surf = self._render_text(text, font, color, max_width)
        self.screen.blit(surf, surf.get_rect(centerx=WIDTH // 2, y=y))

    def _draw_text_left(self, text: str, font, x: int, y: int, color=C_TEXT, max_width: int = WIDTH - 20):
        self.screen.blit(self._render_text(text, font, color, max_width), (x, y))

    def _load_art(self, path: Optional[str]) -> pygame.Surface:
        if not path:
            return self._placeholder
        if path in self._art_cache:
            return self._art_cache[path]
        failed_at = self._art_failed.get(path)
        if failed_at is not None and time.monotonic() - failed_at < ART_RETRY_SECS:
            return self._placeholder
        try:
            img = pygame.image.load(path).convert()
            iw, ih = img.get_size()
            k = min(ART_SIZE[0] / iw, ART_SIZE[1] / ih)       # fit, keep aspect ratio
            img = pygame.transform.smoothscale(img, (max(1, int(iw * k)), max(1, int(ih * k))))
            surf = pygame.Surface(ART_SIZE)
            surf.fill(C_ART_BG)
            surf.blit(img, img.get_rect(center=(ART_SIZE[0] // 2, ART_SIZE[1] // 2)))
        except Exception as e:  # noqa: BLE001
            log.warning("Could not load art %s: %s", path, e)
            self._art_failed[path] = time.monotonic()
            return self._placeholder

        self._art_failed.pop(path, None)
        if len(self._art_cache) >= ART_CACHE_MAX:
            self._art_cache.pop(next(iter(self._art_cache)))
        self._art_cache[path] = surf
        return surf

    @staticmethod
    def _format_seconds(secs: float) -> str:
        secs = max(0, int(secs))
        return f"{secs // 60}:{secs % 60:02d}"

    def draw_back_button(self, x: int, y: int):
        pygame.draw.circle(self.screen, C_ACCENT, (x, y), 20)
        pygame.draw.line(self.screen, C_BG, (x-10, y), (x+5, y-10), 5)
        pygame.draw.line(self.screen, C_BG, (x-10, y), (x+5, y+10), 5)

    def is_back_button_at(self, x: int, y: int, bx: int=40-20, by: int=25-20) -> bool:
        return bx <= x <= bx + BACK_BUTTON_RECT.width and by <= y <= by + BACK_BUTTON_RECT.height

    # -- Loading / message overlays -------------------------------------------
    def show_loading(self, text: str = "Loading..."):
        self._is_loading = True
        self._loading_text = text
        self.screen.fill(C_BG)
        self._draw_text_centered(text, self.font_bold, HEIGHT // 2 - 20, C_ACCENT)
        self.present()

    def show_message(self, text: str, duration: float = 2.0):
        self._message_text = text
        self._message_until = time.monotonic() + duration

    def _draw_message_overlay(self):
        if time.monotonic() < self._message_until:
            self.screen.blit(self._overlay, (0, HEIGHT // 2 - 30))
            self._draw_text_centered(self._message_text, self.font_bold, HEIGHT // 2 - 12, C_WHITE)

    # -- Screens --------------------------------------------------------------
    def draw_main_menu(self, items: List[str], selected: int):
        self._is_loading = False
        self.screen.fill(C_BG)
        self._draw_text_centered("HiFiBerry Player", self.font_bold, 40, C_ACCENT)
        pygame.draw.line(self.screen, C_ACCENT, (20, 80), (WIDTH - 20, 80), 1)

        item_h = 70
        start_y = 130
        for i, item in enumerate(items):
            y = start_y + i * item_h
            if i == selected:
                pygame.draw.rect(self.screen, C_HIGHLIGHT, (10, y - 8, WIDTH - 20, item_h - 10), border_radius=8)
                color = C_ACCENT
            else:
                color = C_TEXT
            self._draw_text_centered(item, self.font_bold, y + 14, color)

        self._draw_message_overlay()

    def draw_library(self, albums: List[Dict], selected: int):
        self._is_loading = False
        self.screen.fill(C_BG)
        self.draw_back_button(40, 25)
        self._draw_text_centered("Library", self.font_bold, 15, C_ACCENT)
        pygame.draw.line(self.screen, C_ACCENT, (20, 50), (WIDTH - 20, 50), 1)

        if not albums:
            self._draw_text_centered("No albums found", self.font_normal, HEIGHT // 2, C_SUBTEXT)
            self._draw_message_overlay()
            return

        item_h = 60
        vis = (HEIGHT - 60) // item_h
        # Centre the selection, but never scroll past the end of the list
        start_i = max(0, min(selected - vis // 2, len(albums) - vis))
        y = 60

        for i in range(start_i, min(start_i + vis, len(albums))):
            album = albums[i]
            is_sel = (i == selected)
            if is_sel:
                pygame.draw.rect(self.screen, C_HIGHLIGHT, (5, y, WIDTH - 10, item_h - 4), border_radius=6)

            label_w = WIDTH - 28
            year = album.get("year", "")
            if year:
                yr_surf = self.font_small.render(str(year), True, C_SUBTEXT)
                self.screen.blit(yr_surf, (WIDTH - yr_surf.get_width() - 14, y + 10))
                label_w -= yr_surf.get_width() + 10   # keep the label clear of the year

            label = f"{album.get('artist', '?')} - {album.get('title', '?')}"
            self._draw_text_left(label, self.font_normal, 14, y + 10,
                                 C_ACCENT if is_sel else C_TEXT, label_w)
            y += item_h

        self._draw_message_overlay()

    def draw_playback(
        self,
        album_info:    Dict,
        tracklist:     List[Dict],
        current_track: int,
        paused:        bool,
        position:      float = 0,
        ripped_pos:    float = 0,
        ripped_status: Optional[List] = None,
        duration:      float = 0,
    ):
        self._is_loading = False
        self.screen.fill(C_BG)
        tracklist = tracklist or []
        self.draw_back_button(40, 25)

        # Header
        artist = album_info.get("artist", "Unknown Artist")
        album = album_info.get("album", "Unknown Album")
        year = str(album_info.get("year", "") or "")
        album_line = f"{album} ({year})" if year else album
        self._draw_text_centered(artist, self.font_title, 6, C_WHITE)
        self._draw_text_centered(album_line, self.font_subtitle, 52, C_SUBTEXT)

        # Album art
        art = self._load_art(album_info.get("art_path"))
        self.screen.blit(art, ((WIDTH - ART_SIZE[0]) // 2, ART_Y))

        # Now playing
        now_y = ART_Y + ART_SIZE[1] + 8
        if 0 <= current_track < len(tracklist):
            title = tracklist[current_track].get("title", f"Track {current_track + 1}")
            self._draw_text_centered(title, self.font_bold, now_y, C_ACCENT)

        # Progress bar + position dot
        bar_y = now_y + 30
        bar_x = 20
        bar_w = WIDTH - 40
        bar_h = 8
        frac = max(0.0, min(1.0, position / duration)) if duration > 0 else 0.0
        ripped_frac = max(0.0, min(1.0, ripped_pos / duration)) if duration > 0 else 0.0

        pygame.draw.rect(self.screen, C_PROGRESS, (bar_x, bar_y, bar_w, bar_h), border_radius=4)
        if ripped_frac > 0:
            pygame.draw.rect(self.screen, C_RIPPED,
                             (bar_x, bar_y, int(bar_w * ripped_frac), bar_h), border_radius=4)
        if frac > 0:
            pygame.draw.rect(self.screen, C_ACCENT,
                             (bar_x, bar_y, int(bar_w * frac), bar_h), border_radius=4)

        dot_cx = bar_x + int(bar_w * frac)
        dot_cy = bar_y + bar_h // 2
        dot_color = C_ACCENT if not paused else (105, 105, 120)
        glow_col = (100, 65, 0) if not paused else (45, 45, 55)
        pygame.draw.circle(self.screen, glow_col, (dot_cx, dot_cy), 8)
        pygame.draw.circle(self.screen, dot_color, (dot_cx, dot_cy), 5)

        # Tracklist
        tl_y = TRACK_LIST_Y
        pygame.draw.line(self.screen, C_ACCENT, (20, tl_y - 5), (WIDTH - 20, tl_y - 5), 1)

        max_scroll = max(0, len(tracklist) - VISIBLE_TRACKS)
        self._track_scroll = max(0, min(self._track_scroll, max_scroll))   # e.g. after album change
        if 0 <= current_track < len(tracklist):
            if current_track < self._track_scroll:
                self._track_scroll = current_track
            elif current_track >= self._track_scroll + VISIBLE_TRACKS:
                self._track_scroll = current_track - VISIBLE_TRACKS + 1

        title_x = 40
        title_w = INDICATOR_X - INDICATOR_R - 6 - title_x      # keep clear of the status dot
        row_h = (HEIGHT - tl_y) // VISIBLE_TRACKS
        for row, i in enumerate(range(self._track_scroll, self._track_scroll + VISIBLE_TRACKS)):
            if i >= len(tracklist):
                break
            t = tracklist[i]
            y = tl_y + row * row_h
            is_cur = (i == current_track)
            if is_cur:
                pygame.draw.rect(self.screen, C_HIGHLIGHT, (5, y, WIDTH - 10, row_h - 2), border_radius=5)

            try:
                num = int(t.get("num", i + 1))
            except (TypeError, ValueError):
                num = i + 1
            num_s = self.font_small.render(f"{num:02d}", True, C_SUBTEXT)
            self.screen.blit(num_s, (12, y + (row_h - num_s.get_height()) // 2))

            title_s = self._render_text(t.get("title", f"Track {i + 1}"), self.font_small,
                                        C_PLAYING if is_cur else C_TEXT, title_w)
            self.screen.blit(title_s, (title_x, y + (row_h - title_s.get_height()) // 2))

            status = ripped_status[i] if ripped_status and i < len(ripped_status) else None
            if status == "done":
                indicator_col = C_ACCENT
            elif status == "doing":
                indicator_col = C_RIPPED
            else:
                indicator_col = C_PROGRESS
            pygame.draw.circle(self.screen, indicator_col, (INDICATOR_X, y + row_h // 2), INDICATOR_R)

        self._draw_message_overlay()

    # -- Hit Testing ----------------------------------------------------------
    def get_menu_item_at(self, x: int, y: int, items_count: int) -> Optional[int]:
        item_h = 70
        start_y = 130
        for i in range(items_count):
            item_y = start_y + i * item_h
            if 10 <= x <= WIDTH - 10 and item_y - 8 <= y <= item_y + item_h - 18:
                return i
        return None

    def get_library_album_at(self, x: int, y: int, albums_count: int, selected: int) -> Optional[int]:
        if albums_count == 0:
            return None
        item_h = 60
        vis = (HEIGHT - 60) // item_h
        start_i = max(0, min(selected - vis // 2, albums_count - vis))
        y_cursor = 60
        for i in range(start_i, min(start_i + vis, albums_count)):
            if 5 <= x <= WIDTH - 5 and y_cursor <= y <= y_cursor + item_h - 4:
                return i
            y_cursor += item_h
        return None

    def get_playback_track_at(self, x: int, y: int, tracklist_count: int) -> Optional[int]:
        if tracklist_count == 0:
            return None
        tl_y = TRACK_LIST_Y
        row_h = (HEIGHT - tl_y) // VISIBLE_TRACKS
        for row in range(VISIBLE_TRACKS):
            i = self._track_scroll + row
            if i >= tracklist_count:
                break
            y_row = tl_y + row * row_h
            if 0 <= x <= WIDTH and y_row <= y <= y_row + row_h:
                return i
        return None


if __name__ == "__main__":
    # Quick visual test:  uv run python ui.py      (ESC or Q to quit, ends after 20 s)
    logging.basicConfig(level=logging.INFO)
    pygame.init()
    ui = UI()
    info = {"artist": "Demo Artist", "album": "Demo Album", "year": 1999}
    tracks = [{"num": n + 1, "title": f"Demo track {n + 1} with a very long title to test truncation"}
              for n in range(12)]
    clock = pygame.time.Clock()
    t0 = time.monotonic()
    running = True
    while running and time.monotonic() - t0 < 20:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key in (pygame.K_ESCAPE, pygame.K_q)):
                running = False
        t = time.monotonic() - t0
        cur = int(t // 2) % len(tracks)
        status = ["done" if i < cur else "doing" if i == cur else "todo" for i in range(len(tracks))]
        ui.draw_playback(info, tracks, cur, False, position=t % 30, ripped_pos=min(30, t % 30 + 8),
                         ripped_status=status, duration=30)
        ui.present()
        clock.tick(30)
    pygame.quit()
