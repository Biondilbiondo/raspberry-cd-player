#!/usr/bin/env python3
"""
meta_editor.py — Flask web interface for fixing / promoting CD metadata.

Routes:

    GET  /                              → library of fully archived CDs
    GET  /disc/<key>                    → edit one disc (metadata, cover art, track titles)
    GET  /disc/<key>/cover              → embedded cover art of the disc (first track that has one)
    POST /disc/<key>/save               → apply artist/album/year/cover + every track title
    POST /disc/<key>/track/<idx>/save   → apply the disc fields + the title of ONE track
    GET  /health                        → liveness probe

Promoted (untagged → tagged): the track file is moved from
`untagged_CD/<DISCID>/` into the standard vault layout
(`<artist>/<album> (<year>)/NN - Title.<ext>`) and the DB `untagged` flag is cleared.

LAN-only: bound to 0.0.0.0:8080; started together with the player app at boot.

UI: same look as the pygame player — dark gradient background, amber accent,
rounded cards, round track-number badges, pill buttons. Responsive (phone first).
"""

import logging
import os
import shutil
from typing import List, Optional

from flask import (Flask, Response, abort, flash, get_flashed_messages,
                   redirect, render_template, request, url_for)
from jinja2 import DictLoader

from . import add_metadata as am
from . import database as db
from . import metadata_manager as mm
from .config import config

log = logging.getLogger("meta_editor")
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "raspberry-cd-player-dev")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024   # cover uploads

# ── Templates ────────────────────────────────────────────────────────────────
# Palette mirrors ui.py (C_BG, C_ACCENT, C_BTN_*, C_HIGHLIGHT, …).

BASE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0f0f14">
<title>{% block title %}HiFiBerry CD Editor{% endblock %}</title>
<style>
  :root {
    --bg: #0f0f14;  --bg-top: #181822;  --bg-bot: #09090d;
    --accent: #ffa500;  --accent-hi: #ffcd5a;  --accent-lo: #eb7800;
    --text: #e6e6e6;  --sub: #9696a0;  --dim: #505060;
    --card: #1c1c26;  --highlight: #282837;  --sep: #242430;
    --btn-top: #363646;  --btn-bot: #1e1e2a;
    --ok: #5ac878;  --err: #ff6b5e;
    --radius: 16px;
  }
  * { box-sizing: border-box; }
  html { background: var(--bg); }
  body {
    margin: 0; min-height: 100vh; color: var(--text);
    font: 16px/1.45 Inter, Roboto, "Noto Sans", "Open Sans", "DejaVu Sans",
          system-ui, -apple-system, "Segoe UI", sans-serif;
    background: linear-gradient(180deg, var(--bg-top) 0%, var(--bg) 45%, var(--bg-bot) 100%) fixed;
    -webkit-font-smoothing: antialiased;
  }
  a { color: inherit; text-decoration: none; }
  .wrap { max-width: 760px; margin: 0 auto; padding: 0 16px 56px; }

  /* header */
  header.top {
    position: sticky; top: 0; z-index: 10;
    background: rgba(15,15,20,.82); backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--sep);
  }
  header.top .wrap { display: flex; align-items: center; gap: 12px; padding-top: 12px; padding-bottom: 12px; }
  .logo { display: flex; align-items: center; gap: 10px; font-weight: 700; letter-spacing: .2px; }
  .logo svg { flex: none; }
  .logo span em { color: var(--accent); font-style: normal; }
  .spacer { flex: 1; }

  h1 { font-size: 26px; margin: 28px 0 4px; line-height: 1.2; }
  h2 { font-size: 13px; margin: 28px 0 10px; color: var(--sub);
       text-transform: uppercase; letter-spacing: 1.2px; font-weight: 600; }
  .sub { color: var(--sub); margin: 0; }

  /* buttons */
  .btn {
    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
    border: 0; border-radius: 999px; padding: 11px 20px; cursor: pointer;
    font: inherit; font-weight: 600; color: var(--text);
    background: linear-gradient(180deg, var(--btn-top), var(--btn-bot));
    box-shadow: 0 2px 6px rgba(0,0,0,.4), inset 0 1px 0 rgba(255,255,255,.07);
    transition: transform .08s, filter .15s;
  }
  .btn:hover { filter: brightness(1.15); }
  .btn:active { transform: scale(.97); }
  .btn.primary { color: #17120a; background: linear-gradient(180deg, var(--accent-hi), var(--accent) 45%, var(--accent-lo)); }
  .btn.small { padding: 8px 14px; font-size: 14px; }
  .btn.ghost { background: none; box-shadow: none; color: var(--sub); padding-left: 4px; }
  .btn.ghost:hover { color: var(--text); filter: none; }

  /* flash messages */
  .flash { margin: 18px 0 0; padding: 12px 16px; border-radius: 12px; background: var(--card);
           border-left: 4px solid var(--sub); }
  .flash.success { border-color: var(--ok); }
  .flash.error { border-color: var(--err); }

  /* pills */
  .pill { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px;
          font-weight: 700; letter-spacing: .6px; text-transform: uppercase; }
  .pill.untagged { color: var(--accent); border: 1px solid var(--accent); }
  .pill.tagged { color: var(--sub); border: 1px solid var(--dim); }

  /* disc list */
  .grid { display: grid; gap: 12px; margin-top: 18px; }
  .disc {
    display: flex; gap: 16px; align-items: center; padding: 12px; border-radius: var(--radius);
    background: var(--card); border: 1px solid transparent;
    box-shadow: 0 4px 14px rgba(0,0,0,.35);
    transition: border-color .15s, background .15s, transform .08s;
  }
  .disc:hover { background: var(--highlight); border-color: rgba(255,165,0,.45); }
  .disc:active { transform: scale(.99); }
  .disc .meta { min-width: 0; flex: 1; }
  .disc .artist { font-weight: 700; font-size: 18px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .disc .album { color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .disc .info { color: var(--sub); font-size: 14px; margin-top: 4px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .disc .chev { color: var(--dim); font-size: 22px; padding-right: 4px; }

  .art { flex: none; width: 76px; height: 76px; border-radius: 14px; overflow: hidden;
         background: #282832; display: grid; place-items: center; color: var(--dim);
         box-shadow: 0 3px 10px rgba(0,0,0,.5); }
  .art img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .art.big { width: 200px; height: 200px; border-radius: 18px; border: 2px solid var(--accent);
             box-shadow: 0 10px 30px rgba(0,0,0,.6), 0 0 0 4px rgba(255,165,0,.08); }

  .empty { text-align: center; color: var(--sub); padding: 56px 16px; background: var(--card);
           border-radius: var(--radius); margin-top: 18px; }
  .empty svg { opacity: .5; margin-bottom: 10px; }

  /* edit page */
  .hero { display: flex; gap: 22px; align-items: flex-end; margin-top: 22px; flex-wrap: wrap; }
  .hero .grow { flex: 1; min-width: 220px; }
  .card { background: var(--card); border-radius: var(--radius); padding: 18px; box-shadow: 0 4px 14px rgba(0,0,0,.35); }
  label.field { display: block; margin-bottom: 14px; }
  label.field > span { display: block; font-size: 12px; color: var(--sub); text-transform: uppercase;
                       letter-spacing: 1px; font-weight: 600; margin-bottom: 6px; }
  input[type=text], input[type=number] {
    width: 100%; padding: 11px 14px; font: inherit; color: var(--text);
    background: #12121a; border: 1px solid var(--sep); border-radius: 12px; outline: none;
    transition: border-color .15s, box-shadow .15s;
  }
  input[type=text]:focus, input[type=number]:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(255,165,0,.18); }
  .row { display: grid; grid-template-columns: 1fr 110px; gap: 12px; }
  .file { font-size: 14px; color: var(--sub); }
  .file input { color: var(--sub); max-width: 100%; }
  .file input::file-selector-button {
    margin-right: 12px; border: 0; border-radius: 999px; padding: 8px 14px; cursor: pointer; font: inherit; font-weight: 600;
    color: var(--text); background: linear-gradient(180deg, var(--btn-top), var(--btn-bot));
  }

  /* tracks */
  .track { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 14px;
           background: var(--card); margin-bottom: 8px; }
  .track:focus-within { background: var(--highlight); }
  .num { flex: none; width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center;
         font-weight: 700; font-size: 14px; color: #17120a;
         background: linear-gradient(180deg, var(--accent-hi), var(--accent-lo)); }
  .track input[type=text] { background: transparent; border-color: transparent; padding: 8px 10px; }
  .track input[type=text]:hover { border-color: var(--sep); }
  .track input[type=text]:focus { background: #12121a; }
  .dur { flex: none; color: var(--sub); font-variant-numeric: tabular-nums; font-size: 14px; min-width: 42px; text-align: right; }

  /* sticky save bar */
  .bar { position: sticky; bottom: 0; margin: 22px -16px 0; padding: 14px 16px calc(14px + env(safe-area-inset-bottom));
         display: flex; gap: 12px; align-items: center;
         background: linear-gradient(180deg, rgba(9,9,13,0), rgba(9,9,13,.92) 35%); }
  .bar .note { flex: 1; color: var(--sub); font-size: 14px; }
  .bar .note.dirty { color: var(--accent); }

  @media (max-width: 520px) {
    h1 { font-size: 22px; }
    .art.big { width: 100%; height: auto; aspect-ratio: 1; max-width: 320px; }
    .track .btn { padding: 8px 12px; }
    .dur { display: none; }
  }
</style>
</head>
<body>
<header class="top"><div class="wrap">
  <a class="logo" href="{{ url_for('index') }}">
    <svg width="28" height="28" viewBox="0 0 28 28" aria-hidden="true">
      <circle cx="14" cy="14" r="13" fill="#1c1c26" stroke="#ffa500" stroke-width="2"/>
      <circle cx="14" cy="14" r="8" fill="none" stroke="#505060" stroke-width="1"/>
      <circle cx="14" cy="14" r="3.5" fill="#ffa500"/>
    </svg>
    <span>HiFiBerry <em>CD Editor</em></span>
  </a>
  <div class="spacer"></div>
  {% block headeraction %}{% endblock %}
</div></header>

<main class="wrap">
  {% for category, message in get_flashed_messages(with_categories=true) %}
    <div class="flash {{ category }}" role="status">{{ message }}</div>
  {% endfor %}
  {% block content %}{% endblock %}
</main>
{% block scripts %}{% endblock %}
</body>
</html>
"""

LIST_TEMPLATE = """{% extends "base.html" %}
{% block content %}
<h1>Library</h1>
<p class="sub">{{ discs|length }} archived disc{{ '' if discs|length == 1 else 's' }} — tap one to fix its metadata.</p>

{% if discs %}
<div class="grid">
{% for d in discs %}
  <a class="disc" href="{{ url_for('edit', vault_key=d.vault_key) }}">
    <div class="art">
      {% if d.has_art %}
        <img loading="lazy" src="{{ url_for('cover', vault_key=d.vault_key) }}" alt="">
      {% else %}
        <svg width="32" height="32" viewBox="0 0 28 28"><circle cx="14" cy="14" r="12" fill="none" stroke="currentColor" stroke-width="2"/><circle cx="14" cy="14" r="3" fill="currentColor"/></svg>
      {% endif %}
    </div>
    <div class="meta">
      <div class="artist">{{ d.artist }}</div>
      <div class="album">{{ d.album }}{% if d.year %} <span class="sub">({{ d.year }})</span>{% endif %}</div>
      <div class="info">
        <span>{{ d.ntracks }} track{{ '' if d.ntracks == 1 else 's' }}</span>
        <span class="pill {{ 'untagged' if d.untagged else 'tagged' }}">{{ 'Untagged' if d.untagged else 'Tagged' }}</span>
      </div>
    </div>
    <div class="chev">›</div>
  </a>
{% endfor %}
</div>
{% else %}
<div class="empty">
  <svg width="56" height="56" viewBox="0 0 28 28"><circle cx="14" cy="14" r="12" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="14" cy="14" r="3" fill="currentColor"/></svg>
  <div>No fully archived discs yet.</div>
  <div class="sub">A disc shows up here once all its tracks have finished ripping.</div>
</div>
{% endif %}
{% endblock %}
"""

EDIT_TEMPLATE = """{% extends "base.html" %}
{% block title %}{{ album or 'Untitled album' }} — HiFiBerry CD Editor{% endblock %}
{% block headeraction %}<a class="btn ghost small" href="{{ url_for('index') }}">← Library</a>{% endblock %}
{% block content %}
<form method="post" enctype="multipart/form-data" action="{{ url_for('save', vault_key=vault_key) }}" id="editform">

  <div class="hero">
    <div class="art big" id="artbox">
      {% if has_art %}
        <img id="artimg" src="{{ url_for('cover', vault_key=vault_key, v=art_version) }}" alt="Cover">
      {% else %}
        <svg width="64" height="64" viewBox="0 0 28 28" id="artph"><circle cx="14" cy="14" r="12" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="14" cy="14" r="3" fill="currentColor"/></svg>
      {% endif %}
    </div>
    <div class="grow">
      <span class="pill {{ 'untagged' if untagged else 'tagged' }}">{{ 'Untagged' if untagged else 'Tagged' }}</span>
      <h1>{{ album or 'Untitled album' }}</h1>
      <p class="sub">{{ artist or 'Unknown artist' }}{% if year %} · {{ year }}{% endif %}</p>
      {% if untagged %}
        <p class="sub" style="margin-top:8px">Saving moves these tracks into the library as <b>Artist / Album (Year)</b>.</p>
      {% endif %}
    </div>
  </div>

  <h2>Album</h2>
  <div class="card">
    <label class="field"><span>Artist</span>
      <input type="text" name="artist" value="{{ artist }}" placeholder="Artist" autocomplete="off" required></label>
    <div class="row">
      <label class="field"><span>Album</span>
        <input type="text" name="album" value="{{ album }}" placeholder="Album" autocomplete="off" required></label>
      <label class="field"><span>Year</span>
        <input type="text" name="year" value="{{ year }}" inputmode="numeric" pattern="[0-9]{4}" maxlength="4" placeholder="YYYY"></label>
    </div>
    <label class="field file" style="margin-bottom:0"><span>Cover art</span>
      <input type="file" name="cover" id="coverinput" accept="image/jpeg,image/png"></label>
  </div>

  <h2>Tracks</h2>
  {% for t in tracks %}
  <div class="track">
    <div class="num">{{ t.num }}</div>
    <input type="text" name="title_{{ t.index }}" value="{{ t.title or '' }}" autocomplete="off" aria-label="Title of track {{ t.num }}">
    <div class="dur">{{ t.duration_fmt or '' }}</div>
    <button class="btn small" type="submit"
            formaction="{{ url_for('save_track', vault_key=vault_key, idx=t.index) }}">Save</button>
  </div>
  {% endfor %}

  <div class="bar">
    <div class="note" id="note">All changes saved</div>
    <button class="btn primary" type="submit">Save all</button>
  </div>
</form>
{% endblock %}

{% block scripts %}
<script>
(function () {
  var form = document.getElementById('editform');
  var note = document.getElementById('note');
  form.addEventListener('input', function () {
    note.textContent = 'Unsaved changes';
    note.className = 'note dirty';
  });
  document.getElementById('coverinput').addEventListener('change', function (e) {
    var f = e.target.files && e.target.files[0];
    if (!f) return;
    var box = document.getElementById('artbox');
    var img = document.getElementById('artimg');
    if (!img) {
      var ph = document.getElementById('artph'); if (ph) ph.remove();
      img = document.createElement('img'); img.id = 'artimg'; box.appendChild(img);
    }
    img.src = URL.createObjectURL(f);
  });
})();
</script>
{% endblock %}
"""

app.jinja_env.loader = DictLoader({
    "base.html": BASE_TEMPLATE,
    "list.html": LIST_TEMPLATE,
    "edit.html": EDIT_TEMPLATE,
})

# ── Data helpers ─────────────────────────────────────────────────────────────

def _ntracks(vault_key: str) -> int:
    n = 0
    while db.db_get_track(vault_key, n) is not None:
        n += 1
    return n


def _duration_fmt(path: str) -> str:
    """mm:ss from the audio stream itself (works even when the file has no tags)."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(path)
        secs = audio.info.length if audio is not None and audio.info else 0
    except Exception:  # noqa: BLE001
        return ""
    return f"{int(secs // 60):02d}:{int(secs % 60):02d}" if secs else ""


def _track_rows(vault_key: str) -> List[dict]:
    n = _ntracks(vault_key)
    rows = []
    for i in range(n):
        t = db.db_get_track(vault_key, i) or {}
        path = t.get("path") or ""
        meta = mm.read_file_metadata(path) if path and os.path.exists(path) else None
        rows.append({
            "index": i,
            "num": i + 1,
            "state": t.get("state"),
            "untagged": bool(t.get("untagged")),
            "path": path,
            # read_file_metadata falls back to the file name when there is no title tag
            "title": (t_title if (t_title := (meta.get("title") if meta else None))
                      and t_title != os.path.basename(path) else f"Track {i + 1}"),
            "artist": meta.get("artist") if meta else None,
            "album": meta.get("album") if meta else None,
            "year": meta.get("year") if meta else None,
            "duration": meta.get("duration") if meta else None,
            "duration_fmt": (meta.get("duration_fmt") if meta else None) or (_duration_fmt(path) if path else ""),
            "mbid": meta.get("mbid") if meta else None,
            "has_art": bool(meta and meta.get("art_bytes")),
        })
    return rows


def _discs() -> List[dict]:
    out = []
    for key, _first_rowid in db.db_get_all_keys_with_order():
        rows = _track_rows(key)
        if not rows:
            continue
        # Only fully archived CDs are shown in the editor:
        # all tracks must be in state 'done' before metadata can be fixed / promoted.
        if not all(r.get("state") == "done" for r in rows):
            continue
        first = rows[0]
        out.append({
            "vault_key": key,
            "artist": first.get("artist") or "Unknown Artist",
            "album": first.get("album") or "Unknown Album",
            "year": first.get("year") or "",
            "untagged": any(r["untagged"] for r in rows),
            "ntracks": len(rows),
            "has_art": any(r["has_art"] for r in rows),
        })
    return out


def _cover_bytes(vault_key: str) -> Optional[bytes]:
    for i in range(_ntracks(vault_key)):
        row = db.db_get_track(vault_key, i) or {}
        path = row.get("path")
        if path and os.path.exists(path):
            meta = mm.read_file_metadata(path)
            if meta and meta.get("art_bytes"):
                return meta["art_bytes"]
    return None


def _read_cover_upload():
    """Return bytes of an uploaded cover image (JPEG/PNG only), or None."""
    cover = request.files.get("cover")
    if not cover or not cover.filename:
        return None, None
    data = cover.read()
    if not data:
        return None, None
    if data[:8] != b"\x89PNG\r\n\x1a\n" and data[:3] != b"\xff\xd8\xff":
        return None, "Cover must be a JPEG or PNG image."
    return data, None


def _clean_year(raw: str) -> Optional[str]:
    raw = (raw or "").strip()
    if raw and not (raw.isdigit() and len(raw) == 4):
        return None
    return raw


def _unique_dest(dest: str, src: str) -> str:
    """Avoid silently overwriting an unrelated file in the vault."""
    if not os.path.exists(dest) or os.path.samefile(dest, src):
        return dest
    base, ext = os.path.splitext(dest)
    n = 2
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def _apply_track(vault_key: str, idx: int, artist: str, album_artist: str, album: str,
                 year: str, title: str, num: int, cover_bytes: Optional[bytes] = None) -> Optional[str]:
    """Write tags for a single track, promote if untagged, update DB. Returns error or None."""
    src_row = db.db_get_track(vault_key, idx)
    src = src_row.get("path") if src_row else None
    if not src or not os.path.exists(src):
        return f"track {idx + 1}: source file missing"

    # Per-track flag: promoting track 1 must not stop track 2 from being promoted later.
    untagged_before = bool(src_row.get("untagged"))

    meta = mm.read_file_metadata(src)
    ok = am.write_tags(
        src,
        artist=artist,
        album_artist=album_artist,
        album=album,
        title=title,
        year=year,
        track=num,
        total_tracks=_ntracks(vault_key),
        mbid=(meta.get("mbid") if meta else "") or "",
        cover_bytes=cover_bytes,
    )
    if not ok:
        return f"track {idx + 1}: tag write failed"

    db.db_update_track(vault_key, idx, untagged=0, state="done")
    return None


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("list.html", discs=_discs())


@app.route("/disc/")
def disc_index():
    return redirect(url_for("index"))


@app.route("/disc/<vault_key>")
def edit(vault_key: str):
    rows = _track_rows(vault_key)
    if not rows:
        abort(404)
    first = rows[0]
    first_path = first.get("path")
    version = int(os.path.getmtime(first_path)) if first_path and os.path.exists(first_path) else 0

    return render_template(
        "edit.html",
        vault_key=vault_key,
        artist=first.get("artist") or "",
        album=first.get("album") or "",
        year=first.get("year") or "",
        untagged=any(r["untagged"] for r in rows),
        tracks=rows,
        has_art=any(r["has_art"] for r in rows),
        art_version=version,
    )


@app.route("/disc/<vault_key>/cover")
def cover(vault_key: str):
    data = _cover_bytes(vault_key)
    if not data:
        abort(404)
    mime = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    return Response(data, mimetype=mime, headers={"Cache-Control": "private, max-age=60"})


def _form_disc_fields():
    artist = (request.form.get("artist") or "").strip()
    album = (request.form.get("album") or "").strip()
    year = _clean_year(request.form.get("year"))
    return artist, album, year


@app.route("/disc/<vault_key>/save", methods=["POST"])
def save(vault_key: str):
    rows = _track_rows(vault_key)
    if not rows:
        abort(404)

    artist, album, year = _form_disc_fields()
    if year is None:
        flash("Year must be four digits (or empty).", "error")
        return redirect(url_for("edit", vault_key=vault_key))
    if not artist or not album:
        flash("Artist and album are required.", "error")
        return redirect(url_for("edit", vault_key=vault_key))

    cover_bytes, cover_err = _read_cover_upload()
    if cover_err:
        flash(cover_err, "error")
        return redirect(url_for("edit", vault_key=vault_key))

    failures = []
    for t in rows:
        title = (request.form.get(f"title_{t['index']}") or "").strip() \
            or t["title"] or f"Track {t['num']}"
        err = _apply_track(vault_key, t["index"], artist, artist, album, year,
                           title, t["num"], cover_bytes)
        if err:
            failures.append(err)

    if failures:
        flash("Some tracks could not be saved: " + "; ".join(failures), "error")
    else:
        flash("Metadata saved.", "success")
    return redirect(url_for("edit", vault_key=vault_key))


@app.route("/disc/<vault_key>/track/<int:idx>/save", methods=["POST"])
def save_track(vault_key: str, idx: int):
    src_row = db.db_get_track(vault_key, idx)
    if src_row is None:
        abort(404)

    artist, album, year = _form_disc_fields()
    if year is None:
        flash("Year must be four digits (or empty).", "error")
        return redirect(url_for("edit", vault_key=vault_key))

    title = (request.form.get(f"title_{idx}") or "").strip() or f"Track {idx + 1}"
    cover_bytes, cover_err = _read_cover_upload()
    if cover_err:
        flash(cover_err, "error")
        return redirect(url_for("edit", vault_key=vault_key))

    src = src_row.get("path")
    meta = mm.read_file_metadata(src) if src and os.path.exists(src) else None
    artist_eff = artist or (meta.get("artist") if meta else "") or "Unknown Artist"
    album_eff = album or (meta.get("album") if meta else "") or "Unknown Album"
    year_eff = year if year is not None else ""
    year_eff = year_eff or (meta.get("year") if meta else "") or ""

    err = _apply_track(vault_key, idx, artist_eff, artist_eff, album_eff, year_eff,
                       title, idx + 1, cover_bytes)
    flash(err or f"Track {idx + 1} updated.", "error" if err else "success")
    return redirect(url_for("edit", vault_key=vault_key))


@app.route("/health")
def health():
    return {"ok": True}


def run(host: str = "0.0.0.0", port: int = 8080):
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    run()