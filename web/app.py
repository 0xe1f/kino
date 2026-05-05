import os
import uuid
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

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
socketio = SocketIO(app, cors_allowed_origins="*")
scanner_manager = ScannerManager(socketio)


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
    top_playlists = playlists.top_level(user)
    has_videos = len(videos.list_all()) > 0
    return render_template("index.html", user=user, playlists=top_playlists, has_videos=has_videos)


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


def _render_library_section(user: dict[str, Any], section: str):
    builtin_states = playlists.builtin_states_for_user(user)
    if section == "history":
        content = {
            "history_rows": playback.list_history_for_user(user, playlists),
            "builtin_states": builtin_states,
        }
    elif section == "playlists":
        content = {"playlists": playlists.list_custom_for_user(user)}
    elif section == "favorites":
        favorites = playlists.ensure_builtin(user, "favorites")
        content = {
            "playlist": favorites,
            "items": playlists.items(favorites["_id"]),
            "builtin_states": builtin_states,
            "context_playlist_id": favorites["_id"],
            "context_playlist_removable": False,
        }
    elif section == "watch_later":
        watch_later = playlists.ensure_builtin(user, "watch_later")
        content = {
            "playlist": watch_later,
            "items": playlists.items(watch_later["_id"]),
            "builtin_states": builtin_states,
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


@app.route("/playlists")
@login_required
def playlists_page():
    return _render_library_section(g.user, "playlists")


@app.route("/favorites")
@login_required
def favorites_page():
    return _render_library_section(g.user, "favorites")


@app.route("/watch-later")
@login_required
def watch_later_page():
    return _render_library_section(g.user, "watch_later")


@app.route("/playlist/<path:playlist_id>")
def playlist_view(playlist_id: str):
    user = g.get("user")
    pid = normalize_playlist_id(playlist_id)
    playlist = playlists.get(pid)
    if not playlist:
        return "Playlist not found", 404
    if playlist.get("owner_type") == "user" and (
        not user or playlist.get("owner_id") != user.get("user_id")
    ):
        return "Unauthorized", 403
    items = playlists.items(pid)
    return render_template(
        "playlist.html",
        user=user,
        playlist=playlist,
        items=items,
        playlist_owner=playlists.owner_username(playlist),
        builtin_states=playlists.builtin_states_for_user(user),
        context_playlist_id=pid,
        context_playlist_removable=playlists.removable_for_user(user, pid),
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
    builtin_states = playlists.builtin_states_for_user(user)

    if user:
        progress = playback.get_progress(user["user_id"], vid)
        if progress:
            resume_position = float(progress.get("last_position_seconds", 0.0))
        reaction = reactions.get(user["user_id"], vid)
        is_liked = bool(reaction and reaction.get("value") == "like")

    if playlist_id:
        playlist = playlists.get(playlist_id)
        if playlist:
            list_items = playlists.items(playlist["_id"])
            video_items = [p for p in list_items if p["item"].get("item_type") == "video"]
            ids = [p["target"]["_id"] for p in video_items]
            if vid in ids:
                index = ids.index(vid)
                total_duration = sum(
                    float(p["target"].get("duration", 0) or 0) for p in video_items
                )
                playlist_nav = {
                    "items": video_items,
                    "count": len(video_items),
                    "total_duration": total_duration,
                    "current_index": index,
                    "previous_video_id": ids[index - 1] if index > 0 else None,
                    "next_video_id": ids[index + 1] if index < len(ids) - 1 else None,
                    "auto_advance": True,
                }

    return render_template(
        "video.html",
        user=user,
        video=video,
        playlist_id=playlist_id,
        resume_position=resume_position,
        is_liked=is_liked,
        playlist=playlist,
        playlist_nav=playlist_nav,
        builtin_states=builtin_states,
        context_playlist_id=playlist["_id"] if playlist else None,
        context_playlist_removable=playlists.removable_for_user(
            user, playlist["_id"] if playlist else None
        ),
    )


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
        return jsonify({"ok": True, "already_present": True, "playlist_id": playlist_id})

    playlist_items.add(playlist_id, "video", vid)
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
            ]
        }
    )


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
        parent = playlists.get(parent_playlist_id)
        if not parent:
            return jsonify({"error": "parent playlist not found"}), 404
        if not playlists.can_edit(user, parent):
            return jsonify({"error": "cannot use this parent playlist"}), 403

    playlist = playlists.create_user_playlist(
        user, name=name, parent_playlist_id=parent_playlist_id
    )
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
    import logging
    media_root_host = os.getenv("MEDIA_ROOT_HOST", "not set")
    log = logging.getLogger("kino")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log.info("Starting up")
    log.info("Media root: %s => %s", media_root_host, media_root)
    stale_lock = db.get("scan_lock")
    if stale_lock:
        log.info("Clearing stale scan lock from previous run")
        db.delete(stale_lock)
    db.ensure_indexes()
    run_phase3_migration()
    log.info("Startup complete")


with app.app_context():
    _startup()


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5050)
