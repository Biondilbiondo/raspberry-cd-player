#!/usr/bin/env python3

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional, Union

import pygame

log = logging.getLogger("input")


class InputEvent(Enum):
    UP    = auto()
    DOWN  = auto()
    LEFT  = auto()
    RIGHT = auto()
    FIRE  = auto()
    BACK  = auto()


@dataclass(frozen=True)
class ClickEvent:
    x: int
    y: int


class InputHandler:
    def __init__(self):
        pass

    # ── Public interface ──────────────────────────────────────────────────────
    def process(self, event: pygame.event.Event) -> Optional[Union[InputEvent, ClickEvent]]:
        """
        Call once per pygame event.
        Returns an InputEvent, a ClickEvent or None if the event should be ignored.
        """
        et = event.type

        # ── Mouse / touch ───────────────────────────────────────────────────
        if et == pygame.MOUSEBUTTONDOWN:
            log.debug("MOUSEBUTTONDOWN pos=%s", event.pos)
            return ClickEvent(event.pos[0], event.pos[1])
        if et == pygame.FINGERDOWN:
            log.debug("FINGERDOWN nx=%.2f ny=%.2f", event.x, event.y)
            return self._touch_click(event.x, event.y)

        if et == pygame.KEYDOWN:
            if event.key == pygame.K_RIGHT:
                return InputEvent.RIGHT
            if event.key == pygame.K_LEFT:
                return InputEvent.LEFT
            if event.key == pygame.K_UP:
                return InputEvent.UP
            if event.key == pygame.K_DOWN:
                return InputEvent.DOWN
            if event.key == pygame.K_SPACE:
                return InputEvent.FIRE
            if event.key == pygame.K_BACKSPACE:
                return InputEvent.BACK
        return None

    # ── Private helpers ───────────────────────────────────────────────────────
    def _touch_click(self, nx: float, ny: float) -> Optional[ClickEvent]:
        """Convert normalized touch coords (0..1 over the whole window) to display
        coordinates — the same space MOUSEBUTTONDOWN positions live in.

        FINGERDOWN positions are normalised to the *window*, while mouse events in
        SCALED mode arrive in logical (surface) coordinates with SDL's letterbox
        transform already applied.  We undo that same scale/offset so both input
        types land in one coordinate space before UI.to_canvas() maps them onto
        the portrait canvas.
        """
        surf = pygame.display.get_surface()
        if surf is None:
            return None
        sw, sh = surf.get_size()
        ww, wh = pygame.display.get_window_size()
        if min(sw, sh, ww, wh) <= 0:
            return None
        k  = min(ww / sw, wh / sh)   # SDL's scale factor (aspect preserved)
        ox = (ww - sw * k) / 2.0     # letterbox offsets
        oy = (wh - sh * k) / 2.0
        x  = (nx * ww - ox) / k
        y  = (ny * wh - oy) / k
        return ClickEvent(int(x), int(y))