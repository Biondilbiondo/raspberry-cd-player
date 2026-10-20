#!/usr/bin/env python3
"""
ui.py - Pygame UI for the HiFiBerry CD player (DRM/KMS, 540x960 portrait design).

All drawing happens on a fixed 540x960 portrait canvas (UI.screen).
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
496 - 530   Now playing title
525 - 585   Transport buttons (+ scroll arrows)
600 - 608   Progress bar
630 - 960   Scrollable tracklist (6 rows)

Visual design notes
-------------------
* Every shape that needs smooth edges (buttons, dots, rounded cards, album art)
  is rendered ONCE at start-up (or on first use) via 4x supersampling and then
  simply blitted, so per-frame cost stays as low as in the plain version.
* The playback background is a gradient tinted with the average colour of the
  current cover; it is cached per cover.
* Geometry used for hit-testing is unchanged from the original UI.
"""

import logging
import os
import time
from typing import Callable, Dict, List, Optional, Tuple

import pygame

log = logging.getLogger("ui")

WIDTH = 540
HEIGHT = 960

# Colour palette
C_BG        = (15,  15,  20)
C_ACCENT    = (255, 165,  0)   # amber
C_BLUE      = (0, 255-165, 255)
C_TEXT      = (230, 230, 230)
C_SUBTEXT   = (150, 150, 160)
C_HIGHLIGHT = (40,  40,  55)
C_PLAYING   = (255, 165,  0)
C_WHITE     = (255, 255, 255)
C_PROGRESS  = (80,  80,  90)
C_RIPPED    = (150, 150, 150)
C_ART_BG    = (40,  40,  50)

# Extra palette (visual polish only)
C_BG_TOP     = (24, 24, 34)       # background gradient, top
C_BG_BOT     = (9,  9,  13)       # background gradient, bottom
C_ACCENT_HI  = (255, 205, 90)     # light amber (gradients, knob)
C_ACCENT_LO  = (235, 120, 0)      # deep amber (gradients)
C_BTN_TOP    = (54, 54, 70)       # secondary button gradient
C_BTN_BOT    = (30, 30, 42)
C_DIM        = (80, 80, 96)       # disabled controls
C_SEPARATOR  = (36, 36, 48)
C_TRACK_BAR  = (44, 44, 58)       # empty part of the progress bar
C_RIPPED_BAR = (92, 92, 108)      # ripped part of the progress bar
C_BLUE_LIGHT = (60, 140, 255)

# Preferred UI fonts: the first family found on the system wins; falls back to
# pygame's built-in font so nothing extra has to be installed.
FONT_FAMILIES = ("inter", "roboto", "notosans", "opensans", "dejavusans",
                 "liberationsans", "freesans")
FONT_SCALE    = 0.92              # global multiplier for the system-font sizes

# Default pygame font sizes (nominal: they are matched to the line height the
# built-in font had at that size, so the layout does not move with the font)
FONT_TITLE_SIZE    = 40   # artist name
FONT_SUBTITLE_SIZE = 32   # album name
FONT_BOLD_SIZE     = 32
FONT_NORMAL_SIZE   = 26
FONT_SMALL_SIZE    = 21

ELLIPSIS = "..."          # ASCII on purpose: the default font may lack U+2026

ART_SIZE = (400, 400)
ART_Y    = 92
ART_RADIUS       = 14     # rounded corners of the cover
ART_CACHE_MAX    = 8
ART_RETRY_SECS   = 5.0    # retry a failed cover load after this long (art may arrive late)

TRACK_LIST_Y   = 610
VISIBLE_TRACKS = 6

# Control button positions
CTRL_Y      = 555                 # between title and progress bar
CTRL_RADIUS = 30                  # button diameter 60
CTRL_GAP    = 15                  # gap between button edges
CTRL_STEP   = CTRL_RADIUS * 2 + CTRL_GAP   # center-to-center
INDICATOR_X    = WIDTH - 70
INDICATOR_R    = 10
BACK_BUTTON_RECT = pygame.Rect(0, 0, 40, 40)

# Scroll button positions (flanking the control bar)
SCROLL_BTN_R    = 16          # radius for scroll buttons (32px diameter)
SCROLL_BTN_Y    = CTRL_Y      # same row as control buttons
SCROLL_BTN_UP_X = WIDTH - 30  # right side, scroll up
SCROLL_BTN_DN_X = 30          # left side, scroll down

# Progress bar geometry (derived exactly as in draw_playback)
BAR_X = 20
BAR_W = WIDTH - 40
BAR_H = 8
BAR_Y = CTRL_Y + CTRL_RADIUS + 15          # buttons bottom + gap
LIST_Y = BAR_Y + 30                        # tracklist top


# ---------------------------------------------------------------------------
# Small drawing helpers (only used while building cached sprites)
# ---------------------------------------------------------------------------
def _lerp(a, b, t: float):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(len(a)))


def _vgradient(size: Tuple[int, int], top, bottom, alpha: bool = False) -> pygame.Surface:
    w, h = size
    surf = pygame.Surface(size, pygame.SRCALPHA) if alpha else pygame.Surface(size)
    for y in range(h):
        pygame.draw.line(surf, _lerp(top, bottom, y / max(1, h - 1)), (0, y), (w, y))
    return surf


def _hgradient(size: Tuple[int, int], left, right) -> pygame.Surface:
    w, h = size
    surf = pygame.Surface(size, pygame.SRCALPHA)
    for x in range(w):
        pygame.draw.line(surf, _lerp(left, right, x / max(1, w - 1)), (x, 0), (x, h))
    return surf


def _supersample(size: Tuple[int, int], paint: Callable, fill=(0, 0, 0, 0), k: int = 4) -> pygame.Surface:
    """Draw with `paint(big_surface, k)` at k-times resolution, then smooth-scale down.
    `fill` should have the colour of the shape (with alpha 0) to avoid dark fringes."""
    w, h = size
    big = pygame.Surface((w * k, h * k), pygame.SRCALPHA)
    big.fill(fill)
    paint(big, k)
    return pygame.transform.smoothscale(big, (w, h))


def _rounded_mask(size: Tuple[int, int], radius: int) -> pygame.Surface:
    def paint(s, k):
        pygame.draw.rect(s, (255, 255, 255, 255), (0, 0, size[0] * k, size[1] * k),
                         border_radius=radius * k)
    return _supersample(size, paint, fill=(255, 255, 255, 0))


def _circle_mask(diameter: int) -> pygame.Surface:
    def paint(s, k):
        pygame.draw.circle(s, (255, 255, 255, 255), (diameter * k // 2, diameter * k // 2),
                           diameter * k // 2)
    return _supersample((diameter, diameter), paint, fill=(255, 255, 255, 0))


def _soft_shadow(size: Tuple[int, int], inset: int, strength: int) -> pygame.Surface:
    """Blurred dark rounded rectangle: stacked translucent rounded rects, each one
    slightly smaller than the last, give a smooth falloff without any real blur."""
    w, h = size
    surf = pygame.Surface(size, pygame.SRCALPHA)
    steps = max(1, inset)
    per = max(1, int(strength / steps * 2.2))
    for i in range(steps):
        r = pygame.Rect(i, i, w - 2 * i, h - 2 * i)
        t = i / steps
        a = int(per * (0.35 + 0.65 * t) * 0.55)
        # accumulate on a temp layer so alphas add up instead of overwriting
        layer = pygame.Surface(size, pygame.SRCALPHA)
        pygame.draw.rect(layer, (0, 0, 0, max(1, a)), r, border_radius=18 + steps - i)
        surf.blit(layer, (0, 0))
    return surf


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

        # All drawing code uses self.screen: an off-screen portrait canvas
        self.screen = pygame.Surface((WIDTH, HEIGHT))

        pygame.display.set_caption("HiFiBerry Player")
        pygame.mouse.set_visible(False)

        self._load_fonts()
        self._text_cache: Dict[tuple, pygame.Surface] = {}
        self._art_cache: Dict[str, pygame.Surface] = {}
        self._art_tint: Dict[str, Optional[tuple]] = {}
        self._art_failed: Dict[str, float] = {}
        self._card_cache: Dict[tuple, pygame.Surface] = {}
        self._bg_cache: Dict[Optional[tuple], pygame.Surface] = {}

        self._build_sprites()
        self._placeholder = self._make_placeholder()

        self._message_until = 0.0
        self._message_text = ""
        self._loading_text = ""
        self._is_loading = False

        self._track_scroll  = 0
        self._prev_track    = -1    # track index last time we auto-scrolled
        self._scroll_up_enabled = False
        self._scroll_dn_enabled = False

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
        """Map a mouse/touch position from the real display to canvas coordinates.

        On KMS/DRM with tslib, touch events arrive as absolute display coordinates.
        We must reverse the display->frame transformation (scale+rotation) to get
        the original portrait-canvas position.
        """
        dx, dy = pos
        fw, fh = self._frame_size
        dw, dh = self._display.get_size()

        # ── 1. Map display → scaled-frame rect (undo letterboxing)
        if self._scaled:
            k = min(dw / fw, dh / fh)                      # scale factor
            rw, rh = int(fw * k), int(fh * k)              # rendered size
            ox, oy = (dw - rw) // 2, (dh - rh) // 2       # letterbox offset
            if not (ox <= dx < ox + rw and oy <= dy < oy + rh):
                return -1, -1                               # hit black bar
            x = (dx - ox) / k
            y = (dy - oy) / k
        else:
            x = (dx - self._dest.x) * fw / self._dest.w
            y = (dy - self._dest.y) * fh / self._dest.h

        # ── 2. Reverse the rotation to get portrait-canvas coords
        if self._rot == 90:
            return WIDTH - 1 - int(y), int(x)
        if self._rot == 270:
            return int(y), HEIGHT - 1 - int(x)
        if self._rot == 180:
            return WIDTH - 1 - int(x), HEIGHT - 1 - int(y)
        return int(x), int(y)

    # -- Fonts ----------------------------------------------------------------
    @staticmethod
    def _find_font_file(bold: bool) -> Optional[str]:
        for fam in FONT_FAMILIES:
            try:
                path = pygame.font.match_font(fam, bold=bold)
            except Exception:  # noqa: BLE001
                path = None
            if path:
                return path
        return None

    @staticmethod
    def _make_font(path: Optional[str], nominal: int) -> pygame.font.Font:
        """Font whose line height matches the built-in font at `nominal` size."""
        if not path:
            return pygame.font.Font(None, nominal)
        try:
            ref_h = pygame.font.Font(None, nominal).get_height()
            probe = pygame.font.Font(path, nominal)
            size = max(8, round(nominal * ref_h / probe.get_height() * FONT_SCALE * 1.18))
            return pygame.font.Font(path, size)
        except Exception as e:  # noqa: BLE001
            log.warning("Font %s unusable (%s); using default", path, e)
            return pygame.font.Font(None, nominal)

    def _load_fonts(self):
        regular = self._find_font_file(False)
        bold    = self._find_font_file(True) or regular
        log.info("UI fonts: regular=%s bold=%s", regular, bold)
        self.font_title    = self._make_font(bold,    FONT_TITLE_SIZE)
        self.font_subtitle = self._make_font(regular, FONT_SUBTITLE_SIZE)
        self.font_bold     = self._make_font(bold,    FONT_BOLD_SIZE)
        self.font_normal   = self._make_font(regular, FONT_NORMAL_SIZE)
        self.font_small    = self._make_font(regular, FONT_SMALL_SIZE)

    # -- Sprite factory -------------------------------------------------------
    def _build_sprites(self):
        """Pre-render every smooth-edged element once."""
        self._spr: Dict[str, pygame.Surface] = {}

        # --- Transport buttons ------------------------------------------------
        R, pad = CTRL_RADIUS, 8
        S = 2 * (R + pad)
        self._btn_pad = pad

        def button_base(primary: bool) -> pygame.Surface:
            base = pygame.Surface((S, S), pygame.SRCALPHA)
            # soft drop shadow / glow
            def glow(s, k):
                for i in range(pad, 0, -1):
                    a = int((90 if primary else 70) * (1 - i / pad) ** 2)
                    col = (*C_ACCENT, a) if primary else (0, 0, 0, a)
                    pygame.draw.circle(s, col, (S * k // 2, S * k // 2 + (0 if primary else 2 * k)),
                                       (R + i) * k)
            base.blit(_supersample((S, S), glow, fill=((*C_ACCENT, 0) if primary else (0, 0, 0, 0))), (0, 0))
            # gradient body
            top, bot = (C_ACCENT_HI, C_ACCENT_LO) if primary else (C_BTN_TOP, C_BTN_BOT)
            body = _vgradient((2 * R, 2 * R), top, bot, alpha=True)
            body.blit(_circle_mask(2 * R), (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            base.blit(body, (pad, pad))
            # ring
            ring_col = (255, 225, 150, 255) if primary else (*C_ACCENT, 255)
            def ring(s, k):
                pygame.draw.circle(s, ring_col, (R * k, R * k), R * k, 2 * k)
            base.blit(_supersample((2 * R, 2 * R), ring, fill=(*ring_col[:3], 0)), (pad, pad))
            return base

        def icon(painter: Callable, col) -> pygame.Surface:
            def paint(s, k):
                painter(s, k, S / 2, col)
            return _supersample((S, S), paint, fill=(*col, 0))

        def poly(s, k, col, cx, pts):
            pygame.draw.polygon(s, col, [((cx + x) * k, (cx + y) * k) for x, y in pts])

        def rect(s, k, col, cx, x, y, w, h, r=0):
            pygame.draw.rect(s, col, ((cx + x) * k, (cx + y) * k, w * k, h * k), border_radius=int(r * k))

        def p_prev(s, k, c, col):
            rect(s, k, col, c, -13, -11, 4, 22, 1.5)
            poly(s, k, col, c, [(-8, 0), (2, -11), (2, 11)])
            poly(s, k, col, c, [(2, 0), (12, -11), (12, 11)])

        def p_next(s, k, c, col):
            rect(s, k, col, c, 9, -11, 4, 22, 1.5)
            poly(s, k, col, c, [(8, 0), (-2, -11), (-2, 11)])
            poly(s, k, col, c, [(-2, 0), (-12, -11), (-12, 11)])

        def p_stop(s, k, c, col):
            rect(s, k, col, c, -11, -11, 22, 22, 3)

        def p_play(s, k, c, col):
            poly(s, k, col, c, [(-9, -15), (-9, 15), (14, 0)])

        def p_pause(s, k, c, col):
            rect(s, k, col, c, -11, -14, 7, 28, 2)
            rect(s, k, col, c, 4, -14, 7, 28, 2)

        sec_base, pri_base = button_base(False), button_base(True)
        for name, painter, primary in (("prev", p_prev, False), ("next", p_next, False),
                                       ("stop", p_stop, False), ("play", p_play, True),
                                       ("pause", p_pause, True)):
            spr = (pri_base if primary else sec_base).copy()
            spr.blit(icon(painter, C_BG if primary else C_ACCENT), (0, 0))
            self._spr[name] = spr

        # --- Scroll buttons -----------------------------------------------------
        r = SCROLL_BTN_R
        for direction in ("up", "down"):
            for enabled in (True, False):
                col = C_ACCENT if enabled else C_DIM
                def paint(s, k, direction=direction, col=col):
                    pygame.draw.circle(s, (*C_BTN_TOP, 255), (r * k, r * k), r * k)
                    sign = -1 if direction == "up" else 1
                    pts = [(r - 8, r - 3 * sign), (r, r + 4 * sign - 3 * sign + 3 * sign), (r + 8, r - 3 * sign)]
                    pts = [(r - 8, r - 3 * sign), (r, r + 4 * sign), (r + 8, r - 3 * sign)]
                    pygame.draw.lines(s, col, False, [(x * k, y * k) for x, y in pts], int(3 * k))
                    for x, y in (pts[0], pts[2]):
                        pygame.draw.circle(s, col, (x * k, y * k), int(1.5 * k))
                    pygame.draw.circle(s, col, (pts[1][0] * k, pts[1][1] * k), int(1.5 * k))
                self._spr[f"scroll_{direction}_{int(enabled)}"] = _supersample(
                    (2 * r, 2 * r), paint, fill=(*C_BTN_TOP, 0))

        # --- Back button --------------------------------------------------------
        def back(s, k):
            pygame.draw.circle(s, (*C_ACCENT, 255), (22 * k, 22 * k), 20 * k)
            pts = [(25, 12), (17, 22), (25, 32)]
            pygame.draw.lines(s, C_BG, False, [(x * k, y * k) for x, y in pts], int(4.5 * k))
            for x, y in pts:
                pygame.draw.circle(s, C_BG, (x * k, y * k), int(2.2 * k))
        back_spr = _supersample((44, 44), back, fill=(*C_ACCENT, 0))
        shadow = pygame.Surface((44, 44), pygame.SRCALPHA)
        self._spr["back"] = back_spr

        # --- Status dots --------------------------------------------------------
        D = 2 * (INDICATOR_R + 4)
        def dot(col, filled: bool, glow: bool, dim: bool = False):
            def paint(s, k):
                c = D * k // 2
                if glow:
                    for i in range(4, 0, -1):
                        pygame.draw.circle(s, (*col, 22 + (4 - i) * 14), (c, c), (INDICATOR_R + i) * k)
                ring_col = _lerp(col, C_BG, 0.55 if dim else 0.0)
                pygame.draw.circle(s, (*ring_col, 255), (c, c), INDICATOR_R * k, int(2 * k))
                if filled:
                    pygame.draw.circle(s, (*col, 255), (c, c), int(5.5 * k))
            return _supersample((D, D), paint, fill=(*col, 0))
        self._spr["dot_archived"] = dot(C_BLUE_LIGHT, True, True)
        self._spr["dot_done"]     = dot(C_ACCENT, True, True)
        self._spr["dot_doing"]    = dot(C_RIPPED, True, False)
        self._spr["dot_todo"]     = dot((62, 62, 76), False, False)

        # --- Progress bar -------------------------------------------------------
        mask = _rounded_mask((BAR_W, BAR_H), BAR_H // 2)
        def bar(col_l, col_r):
            b = _hgradient((BAR_W, BAR_H), col_l, col_r)
            b.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
            return b
        self._spr["bar_track"]  = bar((*C_TRACK_BAR, 255), (*C_TRACK_BAR, 255))
        self._spr["bar_ripped"] = bar((*C_RIPPED_BAR, 255), (*C_RIPPED_BAR, 255))
        self._spr["bar_played"] = bar((*C_ACCENT_LO, 255), (*C_ACCENT_HI, 255))

        KS = 36
        def knob(col, glow_col):
            def paint(s, k):
                c = KS * k // 2
                for i in range(KS // 2, 7, -1):
                    a = int(70 * (1 - (i - 7) / (KS // 2 - 7)) ** 2.2)
                    pygame.draw.circle(s, (*glow_col, a), (c, c), i * k)
                pygame.draw.circle(s, (*col, 255), (c, c), 7 * k)
                pygame.draw.circle(s, (255, 255, 255, 120), (c - 2 * k, c - 2 * k), 2.5 * k)
            return _supersample((KS, KS), paint, fill=(*glow_col, 0))
        self._spr["knob_play"]  = knob(C_ACCENT_HI, C_ACCENT)
        self._spr["knob_pause"] = knob((150, 150, 165), (110, 110, 130))

        # --- Cover shadow, divider line, loading ring -----------------------------
        self._art_shadow = _soft_shadow((ART_SIZE[0] + 100, ART_SIZE[1] + 100), 32, 175)

        fade = pygame.Surface((WIDTH - 40, 1), pygame.SRCALPHA)
        for x in range(WIDTH - 40):
            t = x / (WIDTH - 41)
            a = int(255 * min(1.0, min(t, 1 - t) * 6))
            fade.set_at((x, 0), (*C_ACCENT, a))
        self._spr["fade_line"] = fade

        def ring(s, k):
            import math
            c, rr = 30 * k, 24 * k
            n = 90
            for i in range(n):
                a = -math.pi / 2 + (i / n) * 1.5 * math.pi
                f = i / n
                col = (*C_ACCENT, int(40 + 215 * f))
                pygame.draw.circle(s, col, (c + rr * math.cos(a), c + rr * math.sin(a)), (1.2 + 2.0 * f) * k)
        self._spr["loading_ring"] = _supersample((60, 60), ring, fill=(*C_ACCENT, 0))

    def _card(self, w: int, h: int, radius: int = 10, bar: bool = True) -> pygame.Surface:
        """Highlighted row/menu card: warm gradient, faint amber outline, accent tab."""
        key = (w, h, radius, bar)
        spr = self._card_cache.get(key)
        if spr is not None:
            return spr
        spr = _hgradient((w, h), (62, 50, 30, 255), (42, 42, 58, 255))
        spr.blit(_rounded_mask((w, h), radius), (0, 0), special_flags=pygame.BLEND_RGBA_MULT)

        def outline(s, k):
            pygame.draw.rect(s, (*C_ACCENT, 70), (0, 0, w * k, h * k), int(1.5 * k), border_radius=radius * k)
            if bar:
                pygame.draw.rect(s, (*C_ACCENT, 255), (5 * k, h * k * 0.22, 4 * k, h * k * 0.56),
                                 border_radius=2 * k)
        spr.blit(_supersample((w, h), outline, fill=(*C_ACCENT, 0)), (0, 0))
        self._card_cache[key] = spr
        return spr

    def _background(self, tint: Optional[tuple] = None, playback: bool = False) -> pygame.Surface:
        """Vertical gradient (optionally tinted with the cover colour). The playback
        variant also carries the cover shadow, a darkened tracklist panel and its divider."""
        key = (tint, playback)
        bg = self._bg_cache.get(key)
        if bg is not None:
            return bg
        top, bot = C_BG_TOP, C_BG_BOT
        if tint is not None:
            top = _lerp(C_BG_TOP, tint, 0.30)
            bot = _lerp(C_BG_BOT, tint, 0.07)
        bg = _vgradient((WIDTH, HEIGHT), top, bot)
        if playback:
            ax = (WIDTH - ART_SIZE[0]) // 2
            bg.blit(self._art_shadow, (ax - 50, ART_Y - 50 + 16))
            panel = pygame.Surface((WIDTH, HEIGHT - (LIST_Y - 8)), pygame.SRCALPHA)
            panel.fill((0, 0, 0, 70))
            bg.blit(panel, (0, LIST_Y - 8))
            bg.blit(self._spr["fade_line"], (20, LIST_Y - 5))
        if len(self._bg_cache) >= 12:
            self._bg_cache.pop(next(iter(self._bg_cache)))
        self._bg_cache[key] = bg
        return bg

    # -- Helpers --------------------------------------------------------------
    def _round_art(self, img: pygame.Surface) -> pygame.Surface:
        """Cover (ART_SIZE, opaque) -> rounded corners + hairline border."""
        w, h = ART_SIZE
        out = pygame.Surface(ART_SIZE, pygame.SRCALPHA)
        out.blit(img, (0, 0))
        out.blit(_rounded_mask(ART_SIZE, ART_RADIUS), (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
        def border(s, k):
            pygame.draw.rect(s, (255, 255, 255, 38), (0, 0, w * k, h * k), int(1.5 * k),
                             border_radius=ART_RADIUS * k)
        out.blit(_supersample(ART_SIZE, border, fill=(255, 255, 255, 0)), (0, 0))
        return out.convert_alpha()

    def _make_placeholder(self) -> pygame.Surface:
        """Cover placeholder with a drawn music note (no font glyph needed)."""
        surf = _vgradient(ART_SIZE, (52, 52, 68), (30, 30, 42))
        cx, cy = ART_SIZE[0] // 2, ART_SIZE[1] // 2

        def note(s, k):
            pygame.draw.ellipse(s, (*C_ACCENT, 255), ((cx - 60) * k, (cy + 20) * k, 70 * k, 50 * k))
            pygame.draw.rect(s, (*C_ACCENT, 255), ((cx + 2) * k, (cy - 80) * k, 10 * k, 105 * k))
            pygame.draw.polygon(s, (*C_ACCENT, 255),
                                [((cx + 12) * k, (cy - 80) * k), ((cx + 60) * k, (cy - 50) * k),
                                 ((cx + 60) * k, (cy - 28) * k), ((cx + 12) * k, (cy - 55) * k)])
        surf.blit(_supersample(ART_SIZE, note, fill=(*C_ACCENT, 0)), (0, 0))
        return self._round_art(surf)

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
            tint = self._dominant_tint(img)
            surf = self._round_art(surf)
        except Exception as e:  # noqa: BLE001
            log.warning("Could not load art %s: %s", path, e)
            self._art_failed[path] = time.monotonic()
            return self._placeholder

        self._art_failed.pop(path, None)
        if len(self._art_cache) >= ART_CACHE_MAX:
            old = next(iter(self._art_cache))
            self._art_cache.pop(old)
            self._art_tint.pop(old, None)
        self._art_cache[path] = surf
        self._art_tint[path] = tint
        return surf

    @staticmethod
    def _dominant_tint(img: pygame.Surface) -> Optional[tuple]:
        """Average cover colour, normalised to a vivid hue, quantised (cache-friendly)."""
        try:
            r, g, b = pygame.transform.smoothscale(img, (1, 1)).get_at((0, 0))[:3]
        except Exception:  # noqa: BLE001
            return None
        m = max(r, g, b)
        if m < 12:
            return None
        k = 255 / m
        q = lambda v: min(255, int(v * k) // 16 * 16)
        return (q(r), q(g), q(b))

    @staticmethod
    def _format_seconds(secs: float) -> str:
        secs = max(0, int(secs))
        return f"{secs // 60}:{secs % 60:02d}"

    def draw_back_button(self, x: int, y: int):
        self.screen.blit(self._spr["back"], (x - 22, y - 22))

    def is_back_button_at(self, x: int, y: int, bx: int=40-20, by: int=25-20) -> bool:
        return bx <= x <= bx + BACK_BUTTON_RECT.width and by <= y <= by + BACK_BUTTON_RECT.height

    # -- Loading / message overlays -------------------------------------------
    def show_loading(self, text: str = "Loading..."):
        self._is_loading = True
        self._loading_text = text
        self.screen.blit(self._background(), (0, 0))
        self.screen.blit(self._spr["loading_ring"], (WIDTH // 2 - 30, HEIGHT // 2 - 90))
        self._draw_text_centered(text, self.font_bold, HEIGHT // 2 - 20, C_ACCENT)
        self.present()

    def show_message(self, text: str, duration: float = 2.0):
        self._message_text = text
        self._message_until = time.monotonic() + duration

    def _draw_message_overlay(self):
        if time.monotonic() < self._message_until:
            txt = self._render_text(self._message_text, self.font_bold, C_WHITE, WIDTH - 90)
            w = min(WIDTH - 30, txt.get_width() + 56)
            h = 56
            x, y = (WIDTH - w) // 2, HEIGHT // 2 - h // 2
            self.screen.blit(self._message_pill(w, h), (x, y))
            self.screen.blit(txt, txt.get_rect(center=(WIDTH // 2, HEIGHT // 2)))

    def _message_pill(self, w: int, h: int) -> pygame.Surface:
        key = ("pill", w, h)
        spr = self._card_cache.get(key)
        if spr is None:
            def paint(s, k):
                pygame.draw.rect(s, (12, 12, 18, 225), (0, 0, w * k, h * k), border_radius=h * k // 2)
                pygame.draw.rect(s, (*C_ACCENT, 150), (0, 0, w * k, h * k), int(1.5 * k),
                                 border_radius=h * k // 2)
            spr = _supersample((w, h), paint, fill=(12, 12, 18, 0))
            self._card_cache[key] = spr
        return spr

    # -- Screens --------------------------------------------------------------
    def draw_main_menu(self, items: List[str], selected: int):
        self._is_loading = False
        self.screen.blit(self._background(), (0, 0))
        self._draw_text_centered("HiFiBerry Player", self.font_bold, 40, C_ACCENT)
        self.screen.blit(self._spr["fade_line"], (20, 80))

        item_h = 70
        start_y = 130
        for i, item in enumerate(items):
            y = start_y + i * item_h
            if i == selected:
                self.screen.blit(self._card(WIDTH - 20, item_h - 10, 12), (10, y - 8))
                color = C_ACCENT
            else:
                color = C_TEXT
            self._draw_text_centered(item, self.font_bold, y + 14, color)

        self._draw_message_overlay()

    def draw_library(self, albums: List[Dict], selected: int):
        self._is_loading = False
        self.screen.blit(self._background(), (0, 0))
        self.draw_back_button(40, 25)
        self._draw_text_centered("Library", self.font_bold, 15, C_ACCENT)
        self.screen.blit(self._spr["fade_line"], (20, 50))

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
                self.screen.blit(self._card(WIDTH - 10, item_h - 4, 10), (5, y))
            elif i + 1 < min(start_i + vis, len(albums)) and (i + 1) != selected:
                pygame.draw.line(self.screen, C_SEPARATOR, (20, y + item_h - 3), (WIDTH - 20, y + item_h - 3))

            label_w = WIDTH - 28 - (8 if is_sel else 0)
            lx = 14 + (8 if is_sel else 0)
            year = album.get("year", "")
            if year:
                yr_surf = self.font_small.render(str(year), True, C_ACCENT if is_sel else C_SUBTEXT)
                self.screen.blit(yr_surf, (WIDTH - yr_surf.get_width() - 14, y + 10))
                label_w -= yr_surf.get_width() + 10   # keep the label clear of the year

            label = f"{album.get('artist', '?')} - {album.get('title', '?')}"
            self._draw_text_left(label, self.font_normal, lx, y + 10,
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
        tracklist = tracklist or []

        # Cover first: its colour tints the background
        art = self._load_art(album_info.get("art_path"))
        tint = self._art_tint.get(album_info.get("art_path")) if album_info.get("art_path") else None
        self.screen.blit(self._background(tint, playback=True), (0, 0))
        self.draw_back_button(40, 25)

        # Header (kept clear of the back button on long names)
        artist = album_info.get("artist", "Unknown Artist")
        album = album_info.get("album", "Unknown Album")
        year = str(album_info.get("year", "") or "")
        album_line = f"{album} ({year})" if year else album
        self._draw_text_centered(artist, self.font_title, 6, C_WHITE, WIDTH - 140)
        self._draw_text_centered(album_line, self.font_subtitle, 52, C_SUBTEXT, WIDTH - 60)

        # Album art
        self.screen.blit(art, ((WIDTH - ART_SIZE[0]) // 2, ART_Y))

        # Now playing
        now_y = ART_Y + ART_SIZE[1] + 4
        if 0 <= current_track < len(tracklist):
            title = tracklist[current_track].get("title", f"Track {current_track + 1}")
            self._draw_text_centered(title, self.font_bold, now_y, C_ACCENT)

        # Control buttons + scroll arrows — centred between title and progress bar
        max_scroll = max(0, len(tracklist) - VISIBLE_TRACKS)
        self._scroll_up_enabled = self._track_scroll > 0
        self._scroll_dn_enabled = self._track_scroll < max_scroll
        self._draw_control_buttons(paused)
        self._draw_scroll_button(SCROLL_BTN_UP_X, SCROLL_BTN_Y, "up")
        self._draw_scroll_button(SCROLL_BTN_DN_X, SCROLL_BTN_Y, "down")

        # Progress bar — below buttons
        bar_y = BAR_Y
        bar_x, bar_w, bar_h = BAR_X, BAR_W, BAR_H
        frac = max(0.0, min(1.0, position / duration)) if duration > 0 else 0.0
        ripped_frac = max(0.0, min(1.0, ripped_pos / duration)) if duration > 0 else 0.0

        self.screen.blit(self._spr["bar_track"], (bar_x, bar_y))
        if ripped_frac > 0:
            self.screen.blit(self._spr["bar_ripped"], (bar_x, bar_y),
                             area=(0, 0, max(1, int(bar_w * ripped_frac)), bar_h))
        if frac > 0:
            self.screen.blit(self._spr["bar_played"], (bar_x, bar_y),
                             area=(0, 0, max(1, int(bar_w * frac)), bar_h))

        knob = self._spr["knob_pause" if paused else "knob_play"]
        dot_cx = bar_x + int(bar_w * frac)
        dot_cy = bar_y + bar_h // 2
        self.screen.blit(knob, knob.get_rect(center=(dot_cx, dot_cy)))

        # Tracklist — shorter list starts below progress bar
        tl_y = LIST_Y

        max_scroll = max(0, len(tracklist) - VISIBLE_TRACKS)
        self._track_scroll = max(0, min(self._track_scroll, max_scroll))   # clamp on album change

        # Only auto-scroll when the current track reaches the last visible row
        if current_track != self._prev_track and 0 <= current_track < len(tracklist):
            if current_track < self._track_scroll:
                self._track_scroll = current_track
            elif current_track >= self._track_scroll + VISIBLE_TRACKS - 1:
                self._track_scroll = min(current_track - VISIBLE_TRACKS + 2,
                                         max(0, len(tracklist) - VISIBLE_TRACKS))
            self._prev_track = current_track

        title_x = 44
        title_w = INDICATOR_X - INDICATOR_R - 6 - title_x      # keep clear of the status dot
        row_h = (HEIGHT - tl_y) // VISIBLE_TRACKS
        for row, i in enumerate(range(self._track_scroll, self._track_scroll + VISIBLE_TRACKS)):
            if i >= len(tracklist):
                break
            t = tracklist[i]
            y = tl_y + row * row_h
            is_cur = (i == current_track)
            if is_cur:
                self.screen.blit(self._card(WIDTH - 10, row_h - 2, 8), (5, y))
            elif row + 1 < VISIBLE_TRACKS and i + 1 < len(tracklist) and (i + 1) != current_track:
                pygame.draw.line(self.screen, C_SEPARATOR, (44, y + row_h - 1), (WIDTH - 20, y + row_h - 1))

            try:
                num = int(t.get("num", i + 1))
            except (TypeError, ValueError):
                num = i + 1
            num_s = self.font_small.render(f"{num:02d}", True, C_ACCENT if is_cur else (110, 110, 124))
            self.screen.blit(num_s, (16 if not is_cur else 18, y + (row_h - num_s.get_height()) // 2))

            title_s = self._render_text(t.get("title", f"Track {i + 1}"), self.font_small,
                                        C_PLAYING if is_cur else C_TEXT, title_w)
            self.screen.blit(title_s, (title_x + (4 if is_cur else 0), y + (row_h - title_s.get_height()) // 2))

            status = ripped_status[i] if ripped_status and i < len(ripped_status) else None
            if status == "archived":
                dot = self._spr["dot_archived"]
            elif status == "done":
                dot = self._spr["dot_done"]
            elif status == "doing":
                dot = self._spr["dot_doing"]
            else:
                dot = self._spr["dot_todo"]
            self.screen.blit(dot, dot.get_rect(center=(INDICATOR_X, y + row_h // 2)))

        self._draw_message_overlay()

    def _draw_control_buttons(self, paused: bool):
        """Draw play/pause, stop, next, and previous buttons centred in the UI."""
        # Four buttons centred with equal spacing
        first_x = (WIDTH - (CTRL_RADIUS * 2) * 4 - CTRL_GAP * 3) // 2 + CTRL_RADIUS
        prev_x  = first_x
        play_x  = first_x + CTRL_STEP
        stop_x  = first_x + CTRL_STEP * 2
        next_x  = first_x + CTRL_STEP * 3

        # Same convention as before: the icon shown is the current state
        # (pause bars while paused, play triangle while playing).
        for name, bx in (("prev", prev_x), ("pause" if paused else "play", play_x),
                         ("stop", stop_x), ("next", next_x)):
            spr = self._spr[name]
            self.screen.blit(spr, spr.get_rect(center=(bx, CTRL_Y)))

    def _draw_scroll_button(self, cx: int, cy: int, direction: str):
        """Draw a scroll ▲/▼ button in the track list header.
        Dim when at the top/bottom, accent colour otherwise."""
        enabled = (direction == "up" and self._scroll_up_enabled) or \
                  (direction == "down" and self._scroll_dn_enabled)
        spr = self._spr[f"scroll_{direction}_{int(enabled)}"]
        self.screen.blit(spr, spr.get_rect(center=(cx, cy)))

    def get_scroll_button_at(self, x: int, y: int, btn_y: int) -> Optional[str]:
        """Return 'up' or 'down' if a scroll button was tapped."""
        for name, bx in (("up", SCROLL_BTN_UP_X), ("down", SCROLL_BTN_DN_X)):
            if (bx - SCROLL_BTN_R <= x <= bx + SCROLL_BTN_R and
                btn_y - SCROLL_BTN_R <= y <= btn_y + SCROLL_BTN_R):
                return name
        return None

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

    def get_playback_track_at(self, x: int, y: int, tracklist_count: int, bar_y: int = 0) -> Optional[int]:
        if tracklist_count == 0:
            return None
        tl_y = bar_y if bar_y else TRACK_LIST_Y
        row_h = (HEIGHT - tl_y) // VISIBLE_TRACKS
        for row in range(VISIBLE_TRACKS):
            i = self._track_scroll + row
            if i >= tracklist_count:
                break
            y_row = tl_y + row * row_h
            if 0 <= x <= WIDTH and y_row <= y <= y_row + row_h:
                return i
        return None

    def get_playback_control_at(self, x: int, y: int) -> Optional[str]:
        """Detect which control button was pressed."""
        first_x = (WIDTH - (CTRL_RADIUS * 2) * 4 - CTRL_GAP * 3) // 2 + CTRL_RADIUS
        prev_x  = first_x
        play_x  = first_x + CTRL_STEP
        stop_x  = first_x + CTRL_STEP * 2
        next_x  = first_x + CTRL_STEP * 3

        for name, bx in (("prev", prev_x), ("play_pause", play_x), ("stop", stop_x), ("next", next_x)):
            if (bx - CTRL_RADIUS <= x <= bx + CTRL_RADIUS and
                CTRL_Y - CTRL_RADIUS <= y <= CTRL_Y + CTRL_RADIUS):
                return name
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