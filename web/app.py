# Copyright 2026 Akop Karapetyan
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flask import Flask, g, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_socketio import SocketIO
from werkzeug.security import check_password_hash, generate_password_hash

from auth import api_login_required, current_user, load_user, login_required
from dao import playlist_items, playlists, playback, reactions, users, videos
from db import db
from migrations import run_phase3_migration
from scanner import ScannerManager
from utils import (
    BUILTIN_PLAYLISTS,
    format_duration,
    is_valid_theme,
    media_root,
    normalize_email,
    normalize_playlist_id,
    normalize_username,
    normalize_video_id,
    now_iso,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
    datefmt='%Y-%m-%d %H:%M:%S',
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
socketio = SocketIO(app, cors_allowed_origins="*")
scanner_manager = ScannerManager(socketio)

PAGE_SIZE = 40


# ---------------------------------------------------------------------------
# Request lifecycle
# ---------------------------------------------------------------------------

app.before_request(load_user)


# ---------------------------------------------------------------------------
# Template globals
# ---------------------------------------------------------------------------

def thumbnail_url(doc: dict[str, Any] | None) -> str | None:
    if not doc:
        return None
    kind = doc.get("thumbnail_kind")
    path = doc.get("thumbnail_path")
    if not path:
        return None
    if kind == "media":
        return url_for("media_file", relative_path=path)
    if kind == "static":
        return url_for("static", filename=path)
    return None


def selected_theme(user: dict[str, Any] | None = None) -> str:
    resolved_user = user if user is not None else current_user()
    if resolved_user and is_valid_theme(resolved_user.get("preferred_theme")):
        return resolved_user["preferred_theme"]
    cookie_theme = request.cookies.get("theme")
    if is_valid_theme(cookie_theme):
        return cookie_theme
    return "night"


@app.context_processor
def inject_template_globals():
    user = g.get("user")
    return {
        "user": user,
        "theme": selected_theme(user),
        "thumbnail_url": thumbnail_url,
        "format_duration": format_duration,
        "owner_username": playlists.owner_username,
    }


# ---------------------------------------------------------------------------
# Page routes — public
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    user = g.get("user")
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_top = ex.submit(playlists.top_level, user)
        f_has_videos = ex.submit(videos.exists_any)
        all_top = f_top.result()
        has_videos = f_has_videos.result()
    system_playlists = [p for p in all_top if p.get("owner_type") == "system"]
    user_playlists = [p for p in all_top if p.get("owner_type") == "user"]
    user_playlist_counts = (
        playlists.count_items_by_type_batch([p["_id"] for p in user_playlists])
        if user_playlists else {}
    )
    return render_template(
        "index.html",
        user=user,
        system_playlists=system_playlists,
        user_playlists=user_playlists,
        user_playlist_counts=user_playlist_counts,
        has_videos=has_videos,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html", user=current_user())

    username = normalize_username(request.form.get("username") or "")
    email = normalize_email(request.form.get("email") or "")
    password = request.form.get("password") or ""

    if not username or not email or not password:
        return render_template(
            "register.html",
            error="Username, email, and password are required.",
            user=current_user(),
        ), 400
    if users.username_taken(username):
        return render_template(
            "register.html", error="Username already exists.", user=current_user()
        ), 400
    if users.email_taken(email):
        return render_template(
            "register.html", error="Email already exists.", user=current_user()
        ), 400

    user_id = str(uuid.uuid4())
    users.create(
        user_id=user_id,
        username=username,
        email=email,
        password_hash=generate_password_hash(password),
        preferred_theme=selected_theme(None),
    )
    session["user_id"] = user_id
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", user=current_user())

    email = normalize_email(request.form.get("email") or "")
    password = request.form.get("password") or ""
    user = users.get_by_email(email)
    if not user or not check_password_hash(user.get("password_hash", ""), password):
        return render_template(
            "login.html", error="Invalid credentials.", user=current_user()
        ), 401
    session["user_id"] = user["user_id"]
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Page routes — authenticated
# ---------------------------------------------------------------------------

@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    user = g.user

    if request.method == "GET":
        return render_template("profile.html", user=user, profile_section="account")

    action = request.form.get("action")
    error = None
    success = None

    if action == "update_username":
        new_username = normalize_username(request.form.get("username") or "")
        if not new_username:
            error = "Username is required."
        elif users.username_taken(new_username, exclude_user_id=user["user_id"]):
            error = "Username already exists."
        else:
            users.update(user, username=new_username)
            success = "Username updated."

    elif action == "update_email":
        new_email = normalize_email(request.form.get("email") or "")
        if not new_email:
            error = "Email is required."
        elif users.email_taken(new_email, exclude_user_id=user["user_id"]):
            error = "Email already exists."
        else:
            users.update(user, email=new_email)
            success = "Email updated."

    elif action == "update_password":
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        if not current_password or not new_password:
            error = "Current and new password are required."
        elif not check_password_hash(user.get("password_hash", ""), current_password):
            error = "Current password is incorrect."
        else:
            users.update(user, password_hash=generate_password_hash(new_password))
            success = "Password updated."

    elif action == "delete_account":
        users.hard_delete(user)
        session.pop("user_id", None)
        return redirect(url_for("index"))

    else:
        error = "Unknown profile action."

    fresh_user = current_user()
    return render_template(
        "profile.html",
        user=fresh_user,
        profile_section="account",
        error=error,
        success=success,
    )


def _progress_map_for_items(
    user: dict[str, Any] | None,
    items: list[dict[str, Any]],
) -> dict[str, float]:
    """Return {video_id: last_position_seconds} for video items visible to user."""
    if not user or not items:
        return {}
    video_ids = [
        pair["target"]["_id"]
        for pair in items
        if pair.get("item", {}).get("item_type") != "playlist"
    ]
    return playback.get_progress_batch(user["user_id"], video_ids)


def _render_library_section(user: dict[str, Any], section: str):
    if section == "history":
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_rows = ex.submit(playback.list_history_page, user, playlists, None, PAGE_SIZE)
            f_total = ex.submit(playback.count_history_for_user, user["user_id"])
            history_rows, next_bookmark = f_rows.result()
            total = f_total.result()
        video_ids = [row["video"]["_id"] for row in history_rows]
        progress_map = playback.get_progress_batch(user["user_id"], video_ids)
        content = {
            "history_rows": history_rows,
            "progress_map": progress_map,
            "total": total,
            "has_more": len(history_rows) == PAGE_SIZE,
            "next_bookmark": next_bookmark or "",
            "next_start": 0,
        }
    elif section == "favorites":
        favorites = playlists.ensure_builtin(user, "favorites")
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_total = ex.submit(playlists.count_items, favorites["_id"])
            f_items = ex.submit(playlists.items_page, favorites["_id"], None, 0, PAGE_SIZE)
            total = f_total.result()
            items, next_bookmark = f_items.result()
        content = {
            "playlist": favorites,
            "playlist_id": favorites["_id"],
            "items": items,
            "progress_map": _progress_map_for_items(user, items),
            "total": total,
            "has_more": len(items) == PAGE_SIZE,
            "next_bookmark": next_bookmark or "",
            "next_start": len(items),
            "context_playlist_id": favorites["_id"],
            "context_playlist_removable": False,
        }
    elif section == "watch_later":
        watch_later = playlists.ensure_builtin(user, "watch_later")
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_total = ex.submit(playlists.count_items, watch_later["_id"])
            f_items = ex.submit(playlists.items_page, watch_later["_id"], None, 0, PAGE_SIZE)
            total = f_total.result()
            items, next_bookmark = f_items.result()
        content = {
            "playlist": watch_later,
            "playlist_id": watch_later["_id"],
            "items": items,
            "progress_map": _progress_map_for_items(user, items),
            "total": total,
            "has_more": len(items) == PAGE_SIZE,
            "next_bookmark": next_bookmark or "",
            "next_start": len(items),
            "context_playlist_id": watch_later["_id"],
            "context_playlist_removable": False,
        }
    else:
        return "Not found", 404
    return render_template("library.html", user=user, library_section=section, **content)


@app.route("/history")
@login_required
def history_page():
    return _render_library_section(g.user, "history")


@app.route("/favorites")
@login_required
def favorites_page():
    return _render_library_section(g.user, "favorites")


@app.route("/watch-later")
@login_required
def watch_later_page():
    return _render_library_section(g.user, "watch_later")


def _enrich_subplaylist_items(
    items: list[dict[str, Any]],
    user: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Attach counts, resume_url, first_video_url, navigate_url, resume_label,
    show_navigate, and show_restart to each sub-playlist item. Uses batch queries."""
    if not items:
        return []
    sub_ids = [pair["target"]["_id"] for pair in items]
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_counts = ex.submit(playlists.count_items_by_type_batch, sub_ids)
        f_first = ex.submit(playlists.first_video_in_playlist_batch, sub_ids)
        f_last = ex.submit(
            playback.last_videos_in_playlists, user["user_id"], sub_ids
        ) if user else None
        counts_map = f_counts.result()
        first_map = f_first.result()
        last_map = f_last.result() if f_last else {}
    # Batch-fetch all video docs needed for titles in one request.
    video_ids_needed = list({v for v in (*last_map.values(), *first_map.values()) if v})
    video_docs = db.get_many(video_ids_needed) if video_ids_needed else {}
    enriched = []
    for pair in items:
        sub_id = pair["target"]["_id"]
        counts = counts_map.get(sub_id, {"playlists": 0, "videos": 0})
        first_vid = first_map.get(sub_id)
        last_vid = last_map.get(sub_id)

        navigate_url = url_for("playlist_view", playlist_id=sub_id)

        if last_vid:
            resume_url = url_for("video_view", video_id=last_vid, playlist_id=sub_id)
        elif first_vid:
            resume_url = url_for("video_view", video_id=first_vid, playlist_id=sub_id)
        else:
            resume_url = navigate_url

        first_video_url = (
            url_for("video_view", video_id=first_vid, playlist_id=sub_id)
            if first_vid
            else None
        )

        if last_vid:
            title = (video_docs.get(last_vid) or {}).get("title", "")
            resume_label = f"Resume: {title}" if title else "Resume"
        elif first_vid:
            title = (video_docs.get(first_vid) or {}).get("title", "")
            resume_label = f"Play: {title}" if title else "Play"
        else:
            resume_label = ""

        show_navigate = counts["playlists"] > 0
        show_restart = bool(last_vid and first_vid and first_vid != last_vid)

        enriched.append(
            {
                **pair,
                "counts": counts,
                "resume_url": resume_url,
                "first_video_url": first_video_url,
                "navigate_url": navigate_url,
                "resume_label": resume_label,
                "show_navigate": show_navigate,
                "show_restart": show_restart,
            }
        )
    return enriched


@app.route("/playlist/<path:playlist_id>")
def playlist_view(playlist_id: str):
    user = g.get("user")
    pid = normalize_playlist_id(playlist_id)
    playlist = playlists.get(pid)
    if not playlist:
        return "Playlist not found", 404
    context_playlist_removable = playlists.removable_doc_for_user(user, playlist)
    is_user_playlist = playlist.get("owner_type") == "user"
    if is_user_playlist:
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_total = ex.submit(playlists.count_items, pid)
            f_items = ex.submit(playlists.items_page, pid, None, 0, PAGE_SIZE)
            total = f_total.result()
            items, next_bookmark = f_items.result()
        return render_template(
            "playlist.html",
            user=user,
            playlist=playlist,
            playlist_id=pid,
            is_user_playlist=True,
            items=items,
            progress_map=_progress_map_for_items(user, items),
            total=total,
            has_more=len(items) == PAGE_SIZE,
            next_bookmark=next_bookmark or "",
            next_start=len(items),
            playlist_owner=playlists.owner_username(playlist),
            context_playlist_id=pid,
            context_playlist_removable=context_playlist_removable,
            remove_only_menu=bool(context_playlist_removable),
        )
    raw_items = playlists.playlist_type_items(pid)
    items = _enrich_subplaylist_items(raw_items, user)
    return render_template(
        "playlist.html",
        user=user,
        playlist=playlist,
        playlist_id=pid,
        is_user_playlist=False,
        items=items,
        total=len(items),
        playlist_owner=playlists.owner_username(playlist),
        context_playlist_id=pid,
        context_playlist_removable=context_playlist_removable,
        remove_only_menu=bool(context_playlist_removable),
    )


@app.route("/video/<path:video_id>")
def video_view(video_id: str):
    user = g.get("user")
    vid = normalize_video_id(video_id)
    playlist_id = request.args.get("playlist_id")
    video = videos.get(vid)
    if not video:
        return "Video not found", 404

    resume_position = 0.0
    is_liked = False
    playlist = None
    playlist_nav = None
    if user:
        progress = playback.get_progress(user["user_id"], vid)
        if progress:
            resume_position = float(progress.get("last_position_seconds", 0.0))
        reaction = reactions.get(user["user_id"], vid)
        is_liked = bool(reaction and reaction.get("value") == "like")

    if playlist_id:
        playlist = playlists.get(playlist_id)
        if playlist:
            nav = playlists.nav_metadata(playlist["_id"], vid)
            if nav["count"] > 0:
                playlist_nav = {**nav, "auto_advance": True}

    return render_template(
        "video.html",
        user=user,
        video=video,
        playlist_id=playlist_id,
        resume_position=resume_position,
        is_liked=is_liked,
        playlist=playlist,
        playlist_nav=playlist_nav,
        context_playlist_id=playlist["_id"] if playlist else None,
        context_playlist_removable=playlists.removable_doc_for_user(user, playlist),
    )


@app.route("/api/video/<path:video_id>/watch-data")
def api_video_watch_data(video_id: str):
    user = g.get("user")
    vid = normalize_video_id(video_id)
    playlist_id = request.args.get("playlist_id")
    video = videos.get(vid)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    resume_position = 0.0
    is_liked = False
    if user:
        progress = playback.get_progress(user["user_id"], vid)
        if progress:
            resume_position = float(progress.get("last_position_seconds", 0.0))
        reaction = reactions.get(user["user_id"], vid)
        is_liked = bool(reaction and reaction.get("value") == "like")

    playlist_nav = None
    if playlist_id:
        playlist = playlists.get(playlist_id)
        if playlist:
            nav = playlists.nav_metadata(playlist["_id"], vid)
            if nav["count"] > 0:
                playlist_nav = {
                    "previous_video_id": nav.get("previous_video_id"),
                    "next_video_id": nav.get("next_video_id"),
                    "count": nav["count"],
                }

    return jsonify({
        "video_id": vid,
        "src": url_for("media_file", relative_path=video["relative_path"]),
        "poster": thumbnail_url(video),
        "title": video.get("title", ""),
        "description": video.get("description", ""),
        "created_at": video.get("created_at"),
        "views": video.get("views", 0),
        "likes": video.get("likes", 0),
        "is_liked": is_liked,
        "resume_position": resume_position,
        "playlist_nav": playlist_nav,
    })


@app.route("/static/thumbs/<path:filename>")
def generated_thumb(filename: str):
    resp = send_from_directory("static/thumbs", filename)
    resp.cache_control.max_age = 365 * 24 * 3600
    resp.cache_control.public = True
    return resp


@app.route("/media/<path:relative_path>")
def media_file(relative_path: str):
    resp = send_from_directory(media_root, relative_path)
    resp.cache_control.max_age = 3600
    resp.cache_control.public = True
    return resp


# ---------------------------------------------------------------------------
# API — pagination fragments
# ---------------------------------------------------------------------------

@app.route("/api/playlist/<path:playlist_id>/items")
def api_playlist_items(playlist_id: str):
    pid = normalize_playlist_id(playlist_id)
    playlist = playlists.get(pid)
    if not playlist:
        return jsonify({"error": "not found"}), 404
    user = g.get("user")
    bookmark = request.args.get("bookmark", "")
    start = request.args.get("start", 0, type=int)
    items, next_bookmark = playlists.items_page(pid, bookmark or None, start, PAGE_SIZE)
    next_start = start + len(items)
    removable = playlists.removable_doc_for_user(user, playlist)
    html = render_template(
        "_playlist_items.html",
        items=items,
        progress_map=_progress_map_for_items(user, items),
        playlist_id=pid,
        user=user,
        context_playlist_id=pid,
        context_playlist_removable=removable,
        remove_only_menu=bool(removable),
    )
    return jsonify({
        "html": html,
        "has_more": len(items) == PAGE_SIZE,
        "next_bookmark": next_bookmark or "",
        "next_start": next_start,
    })


@app.route("/api/playlist/<path:playlist_id>/nav-items")
def api_playlist_nav_items(playlist_id: str):
    pid = normalize_playlist_id(playlist_id)
    playlist = playlists.get(pid)
    if not playlist:
        return jsonify({"error": "not found"}), 404
    user = g.get("user")
    current_video_id = normalize_video_id(request.args.get("current_video_id", ""))
    items = playlists.video_items_all(pid)
    progress_map: dict[str, float] = {}
    if user:
        video_ids = [pair["target"]["_id"] for pair in items]
        progress_map = playback.get_progress_batch(user["user_id"], video_ids)
    html = render_template(
        "_nav_video_items.html",
        items=items,
        progress_map=progress_map,
        playlist_id=pid,
        user=user,
        current_video_id=current_video_id,
        context_playlist_id=pid,
        context_playlist_removable=playlists.removable_doc_for_user(user, playlist),
    )
    return jsonify({"html": html})


@app.route("/api/library/history/items")
@login_required
def api_history_items():
    bookmark = request.args.get("bookmark") or None
    rows, next_bookmark = playback.list_history_page(g.user, playlists, bookmark, PAGE_SIZE)
    video_ids = [row["video"]["_id"] for row in rows]
    progress_map = playback.get_progress_batch(g.user["user_id"], video_ids)
    html = render_template("_history_items.html", history_rows=rows, progress_map=progress_map, user=g.user)
    return jsonify({
        "html": html,
        "has_more": len(rows) == PAGE_SIZE,
        "next_bookmark": next_bookmark or "",
        "next_start": 0,
    })



# ---------------------------------------------------------------------------
# API — scan
# ---------------------------------------------------------------------------

@app.route("/api/scan/trigger", methods=["POST"])
def api_scan_trigger():
    return jsonify(scanner_manager.trigger())


@app.route("/api/scan/status", methods=["GET"])
def api_scan_status():
    return jsonify(scanner_manager.status)


# ---------------------------------------------------------------------------
# API — theme
# ---------------------------------------------------------------------------

@app.route("/api/theme", methods=["POST"])
def api_theme():
    payload = request.json or {}
    theme = payload.get("theme")
    if not is_valid_theme(theme):
        return jsonify({"error": "theme must be one of: day, night"}), 400

    user = current_user()
    if user:
        users.update(user, preferred_theme=theme)

    response = jsonify({"ok": True, "theme": theme})
    response.set_cookie("theme", theme, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return response


# ---------------------------------------------------------------------------
# API — video
# ---------------------------------------------------------------------------

@app.route("/api/video/<path:video_id>/play", methods=["POST"])
def api_video_play(video_id: str):
    vid = normalize_video_id(video_id)
    playlist_id = (request.json or {}).get("playlist_id")
    video = videos.get(vid)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    videos.increment_views(video)

    user = current_user()
    if user:
        playback.upsert_history(user["user_id"], vid, playlist_id, position=0.0)

    return jsonify({"ok": True, "views": video["views"]})


@app.route("/api/video/<path:video_id>/progress", methods=["POST"])
@api_login_required
def api_video_progress(video_id: str):
    user = g.user
    vid = normalize_video_id(video_id)
    payload = request.json or {}
    playlist_id = payload.get("playlist_id")
    position = float(payload.get("position_seconds", 0.0))

    playback.upsert_progress(user["user_id"], vid, playlist_id, position)
    playback.upsert_history(user["user_id"], vid, playlist_id, position=position)
    return jsonify({"ok": True})


@app.route("/api/video/<path:video_id>/reaction", methods=["POST"])
@api_login_required
def api_video_reaction(video_id: str):
    user = g.user
    vid = normalize_video_id(video_id)
    video = videos.get(vid)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    liked, like_count = reactions.toggle(user, video)
    return jsonify({"ok": True, "likes": like_count, "liked": liked})


@app.route("/api/video/<path:video_id>/add-to-playlist", methods=["POST"])
@api_login_required
def api_video_add_to_playlist(video_id: str):
    user = g.user
    vid = normalize_video_id(video_id)
    video = videos.get(vid)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    payload = request.json or {}
    playlist_id = payload.get("playlist_id")
    new_playlist_name = normalize_username(payload.get("new_playlist_name") or "")
    if not playlist_id and not new_playlist_name:
        return jsonify({"error": "playlist_id or new_playlist_name is required"}), 400

    if new_playlist_name:
        if playlists.name_taken_for_user(user["user_id"], new_playlist_name):
            return jsonify({"error": "A playlist with that name already exists"}), 409
        playlist = playlists.create_user_playlist(
            user,
            name=new_playlist_name,
            thumbnail_kind=video.get("thumbnail_kind"),
            thumbnail_path=video.get("thumbnail_path"),
        )
        playlist_id = playlist["_id"]
    else:
        playlist = playlists.get(playlist_id)
        if not playlist:
            return jsonify({"error": "playlist not found"}), 404
        if playlist.get("owner_id") != user["user_id"] or playlists.is_builtin(playlist):
            return jsonify({"error": "cannot use this playlist"}), 403

    existing_item = playlist_items.find_video_in_playlist(playlist_id, vid)
    if existing_item:
        return jsonify({"error": "Video is already part of the playlist"}), 409

    playlist_items.add(playlist_id, "video", vid)
    users.update(user, last_playlist_id=playlist_id)
    return jsonify({"ok": True, "playlist_id": playlist_id})


@app.route("/api/video/<path:video_id>/builtin", methods=["POST"])
@api_login_required
def api_video_builtin(video_id: str):
    user = g.user
    vid = normalize_video_id(video_id)
    if not videos.get(vid):
        return jsonify({"error": "Video not found"}), 404

    kind = (request.json or {}).get("kind")
    if kind not in BUILTIN_PLAYLISTS:
        return jsonify({"error": "Unknown builtin playlist kind"}), 400

    playlist = playlists.ensure_builtin(user, kind)
    existing_item = playlist_items.find_video_in_playlist(playlist["_id"], vid)
    if existing_item:
        playlist_items.remove(existing_item)
        return jsonify({"ok": True, "added": False, "kind": kind})

    playlist_items.add(playlist["_id"], "video", vid, owner_type="user")
    return jsonify({"ok": True, "added": True, "kind": kind})


# ---------------------------------------------------------------------------
# API — playlists
# ---------------------------------------------------------------------------

@app.route("/api/user/playlists", methods=["GET"])
@api_login_required
def api_user_playlists():
    user_playlists = playlists.list_custom_for_user(g.user)
    return jsonify(
        {
            "playlists": [
                {"playlist_id": p["_id"], "name": p.get("name", "Untitled")}
                for p in user_playlists
            ],
            "last_playlist_id": g.user.get("last_playlist_id"),
        }
    )


@app.route("/api/user/playlists/name-check", methods=["GET"])
@api_login_required
def api_user_playlist_name_check():
    user = g.user
    name = (request.args.get("name") or "").strip()
    exclude_id = request.args.get("exclude_id") or None
    if not name:
        return jsonify({"available": False, "reason": "Name is required"})
    taken = playlists.name_taken_for_user(user["user_id"], name, exclude_playlist_id=exclude_id)
    if taken:
        return jsonify({"available": False, "reason": "A playlist with that name already exists"})
    return jsonify({"available": True})


@app.route("/api/playlist", methods=["POST"])
@api_login_required
def api_playlist_create():
    user = g.user
    payload = request.json or {}
    name = (payload.get("name") or "").strip()
    parent_playlist_id = payload.get("parent_playlist_id")
    if not name:
        return jsonify({"error": "name is required"}), 400

    if parent_playlist_id:
        return jsonify({"error": "user playlists must be top-level"}), 400

    if playlists.name_taken_for_user(user["user_id"], name):
        return jsonify({"error": "A playlist with that name already exists"}), 409

    playlist = playlists.create_user_playlist(user, name=name)
    return jsonify({"ok": True, "playlist_id": playlist["_id"]})


@app.route("/api/playlist/<path:playlist_id>/rename", methods=["POST"])
@api_login_required
def api_playlist_rename(playlist_id: str):
    user = g.user
    pid = normalize_playlist_id(playlist_id)
    playlist = playlists.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not playlists.can_edit(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400

    if playlists.name_taken_for_user(user["user_id"], name, exclude_playlist_id=pid):
        return jsonify({"error": "A playlist with that name already exists"}), 409

    playlist["name"] = name
    playlist["updated_at"] = now_iso()
    playlists.save(playlist)
    return jsonify({"ok": True})


@app.route("/api/playlist/<path:playlist_id>/delete", methods=["POST"])
@api_login_required
def api_playlist_delete(playlist_id: str):
    user = g.user
    pid = normalize_playlist_id(playlist_id)
    playlist = playlists.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not playlists.can_edit(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    playlists.delete_tree(user["user_id"], pid)
    return jsonify({"ok": True})


@app.route("/api/playlist/<path:playlist_id>/items", methods=["POST"])
@api_login_required
def api_playlist_add_item(playlist_id: str):
    user = g.user
    pid = normalize_playlist_id(playlist_id)
    playlist = playlists.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not playlists.can_edit(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    payload = request.json or {}
    item_type = payload.get("item_type")
    item_id = payload.get("item_id")
    if item_type not in {"video", "playlist"} or not item_id:
        return jsonify({"error": "item_type and item_id are required"}), 400

    target = db.get(item_id)
    if not target:
        return jsonify({"error": "item not found"}), 404
    if item_type == "playlist":
        if target.get("owner_type") == "system":
            return jsonify({"error": "system playlists cannot be nested in user playlists"}), 400
        if target.get("owner_id") != user["user_id"]:
            return jsonify({"error": "cannot attach a playlist you do not own"}), 403
        if target.get("_id") == pid:
            return jsonify({"error": "cannot add playlist to itself"}), 400

    item = playlist_items.add(pid, item_type, item_id)
    return jsonify({"ok": True, "item_id": item["_id"]})


@app.route("/api/playlist/<path:playlist_id>/items/remove", methods=["POST"])
@api_login_required
def api_playlist_remove_item(playlist_id: str):
    user = g.user
    pid = normalize_playlist_id(playlist_id)
    playlist = playlists.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not playlists.can_edit(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    item_doc_id = (request.json or {}).get("playlist_item_id")
    item = db.get(item_doc_id)
    if not item or item.get("playlist_id") != pid:
        return jsonify({"error": "playlist item not found"}), 404

    playlist_items.remove(item)
    return jsonify({"ok": True})


@app.route("/api/playlist/<path:playlist_id>/remove-video", methods=["POST"])
@api_login_required
def api_playlist_remove_video(playlist_id: str):
    user = g.user
    pid = normalize_playlist_id(playlist_id)
    playlist = playlists.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not playlists.can_edit(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    video_id = (request.json or {}).get("video_id")
    if not video_id:
        return jsonify({"error": "video_id is required"}), 400

    removed = False
    for item in playlist_items.list_for_playlist(pid):
        if item.get("item_type") == "video" and item.get("item_id") == video_id:
            playlist_items.remove(item)
            removed = True

    return jsonify({"ok": True, "removed": removed})


@app.route("/api/playlist/<path:playlist_id>/items/reorder", methods=["POST"])
@api_login_required
def api_playlist_reorder(playlist_id: str):
    user = g.user
    pid = normalize_playlist_id(playlist_id)
    playlist = playlists.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not playlists.can_edit(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    ordered_item_ids = (request.json or {}).get("ordered_playlist_item_ids") or []
    playlist_items.reorder(pid, ordered_item_ids)
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _startup() -> None:
    media_root_host = os.getenv("MEDIA_ROOT_HOST", "not set")
    app.logger.info("Starting up")
    app.logger.info("Media root: %s => %s", media_root_host, media_root)
    stale_lock = db.get("scan_lock")
    if stale_lock:
        app.logger.info("Clearing stale scan lock from previous run")
        db.delete(stale_lock)
    db.ensure_design_docs()
    # Warm existing views
    db.query_view("kino", "item_counts_by_playlist", keys=[], group=True)
    db.query_view("kino", "first_video_by_playlist", keys=[], group=True)
    db.query_view("kino", "last_watched_by_user_playlist", keys=[], group=True)
    db.query_view("kino", "history_by_user_date", keys=[], group=True)
    db.query_view("kino", "playlist_count_by_owner", keys=[], group=True)
    db.query_view_range("kino", "playlist_items_by_playlist_type", startkey=None, endkey=None, limit=1)
    db.query_view("kino", "playlist_names_by_owner", keys=[], group=False)
    # Warm new views
    db.query_view_range("kino", "docs_by_type", startkey=None, endkey=None, limit=1)
    db.query_view("kino", "users_by_email", keys=[], reduce=False)
    db.query_view("kino", "users_by_username", keys=[], reduce=False)
    db.query_view("kino", "videos_by_source", keys=[], reduce=False)
    db.query_view_range("kino", "playlists_by_owner", startkey=None, endkey=None, limit=1)
    db.query_view_range("kino", "playlists_by_parent", startkey=None, endkey=None, limit=1)
    db.query_view("kino", "playlist_items_by_owner_type", keys=[], reduce=False)
    run_phase3_migration()
    app.logger.info("Startup complete")


with app.app_context():
    if not os.getenv("FLASK_TESTING"):
        _startup()


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5050)
