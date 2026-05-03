import hashlib
import json
import os
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import couchdb
from flask import Flask, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from flask_socketio import SocketIO
from werkzeug.security import check_password_hash, generate_password_hash

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
THUMBNAIL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
DIRECTORY_THUMB_BASENAMES = ("cover", "folder", "thumbnail", "poster")
VIDEO_THUMB_BASENAMES = ("thumbnail", "cover", "poster")
THEME_OPTIONS = {"night", "day"}
BUILTIN_PLAYLISTS = {
    "favorites": {"name": "Favorites"},
    "watch_later": {"name": "Watch Later"},
}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "dev-secret-change-me")
socketio = SocketIO(app, cors_allowed_origins="*")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def format_duration(value: float | int | None) -> str:
    if value is None:
        return "0:00"
    total_seconds = max(0, int(round(float(value))))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8"), usedforsecurity=False).hexdigest()


class KinoDB:
    def __init__(self, url: str, db_name: str):
        self.url = url
        self.db_name = db_name
        self._db = None
        self._lock = threading.Lock()

    def _connect(self):
        server = couchdb.Server(self.url)
        if self.db_name in server:
            return server[self.db_name]
        return server.create(self.db_name)

    @property
    def db(self):
        with self._lock:
            if self._db is None:
                self._db = self._connect()
        return self._db

    def get(self, doc_id: str) -> dict[str, Any] | None:
        try:
            return self.db.get(doc_id)
        except Exception:
            self._db = None
            return self.db.get(doc_id)

    def save(self, doc: dict[str, Any]) -> dict[str, Any]:
        _, rev = self.db.save(doc)
        doc["_rev"] = rev
        return doc

    def delete(self, doc: dict[str, Any]) -> None:
        self.db.delete(doc)

    def all_docs(self) -> list[dict[str, Any]]:
        rows = self.db.view("_all_docs", include_docs=True)
        return [row.doc for row in rows if row.doc]

    def find_many(self, doc_type: str, **filters) -> list[dict[str, Any]]:
        docs = []
        for doc in self.all_docs():
            if doc.get("type") != doc_type:
                continue
            if all(doc.get(k) == v for k, v in filters.items()):
                docs.append(doc)
        return docs

    def find_one(self, doc_type: str, **filters) -> dict[str, Any] | None:
        matches = self.find_many(doc_type, **filters)
        return matches[0] if matches else None


db = KinoDB(
    url=os.getenv("COUCHDB_URL", "http://admin:admin@localhost:5984/"),
    db_name=os.getenv("COUCHDB_DB_NAME", "kino"),
)
media_root = os.getenv("MEDIA_ROOT", "/media/library")
scan_lock_ttl = int(os.getenv("SCAN_LOCK_TTL_SECONDS", "600"))
generated_thumb_dir = Path(app.static_folder or "./static") / "generated-thumbs"
phase3_migration_lock = threading.Lock()
phase3_migration_done = False
legacy_username_base_env = "KINO_LEGACY_USERNAME_BASE"


def current_user() -> dict[str, Any] | None:
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.get(f"user:{user_id}")


def ensure_auth() -> tuple[dict[str, Any] | None, Any]:
    user = current_user()
    if not user:
        return None, (jsonify({"error": "Authentication required"}), 401)
    return user, None


def normalize_username(value: str) -> str:
    return (value or "").strip()


def normalize_email(value: str) -> str:
    return (value or "").strip().lower()


def email_taken(email: str, exclude_user_id: str | None = None) -> bool:
    normalized = normalize_email(email)
    for user in db.find_many("user"):
        if user.get("email") != normalized:
            continue
        if exclude_user_id and user.get("user_id") == exclude_user_id:
            continue
        return True
    return False


def username_taken(username: str, exclude_user_id: str | None = None) -> bool:
    normalized = normalize_username(username).lower()
    for user in db.find_many("user"):
        existing = normalize_username(user.get("username", "")).lower()
        if not existing or existing != normalized:
            continue
        if exclude_user_id and user.get("user_id") == exclude_user_id:
            continue
        return True
    return False


def owner_username(playlist: dict[str, Any] | None) -> str | None:
    if not playlist or playlist.get("owner_type") != "user":
        return None
    owner_id = playlist.get("owner_id")
    if not owner_id:
        return None
    user_doc = db.get(f"user:{owner_id}")
    if not user_doc:
        return None
    return user_doc.get("username")


def is_builtin_playlist(playlist: dict[str, Any] | None) -> bool:
    return bool(playlist and playlist.get("builtin_kind") in BUILTIN_PLAYLISTS)


def ensure_builtin_playlist(user: dict[str, Any], builtin_kind: str) -> dict[str, Any]:
    if builtin_kind not in BUILTIN_PLAYLISTS:
        raise ValueError("Unknown builtin playlist kind")
    playlist_id = f"playlist:builtin:{builtin_kind}:{user['user_id']}"
    existing = db.get(playlist_id)
    if existing:
        return existing
    doc = {
        "_id": playlist_id,
        "type": "playlist",
        "playlist_id": playlist_id,
        "name": BUILTIN_PLAYLISTS[builtin_kind]["name"],
        "owner_type": "user",
        "owner_id": user["user_id"],
        "editable": False,
        "hidden_from_lists": True,
        "builtin_kind": builtin_kind,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    db.save(doc)
    return doc


def list_custom_playlists_for(user: dict[str, Any]) -> list[dict[str, Any]]:
    playlists = db.find_many("playlist", owner_type="user", owner_id=user["user_id"])
    filtered = [p for p in playlists if not is_builtin_playlist(p) and not p.get("hidden_from_lists")]
    return sorted(filtered, key=lambda p: p.get("name", "").lower())


def builtin_states_for_user(user: dict[str, Any] | None) -> dict[str, set[str]]:
    states = {"favorites": set(), "watch_later": set()}
    if not user:
        return states
    for kind in states:
        playlist = ensure_builtin_playlist(user, kind)
        for item in db.find_many("playlist_item", playlist_id=playlist["_id"]):
            if item.get("item_type") == "video":
                states[kind].add(item.get("item_id"))
    return states


def playlist_removable_for_user(user: dict[str, Any] | None, playlist_id: str | None) -> bool:
    if not user or not playlist_id:
        return False
    playlist = db.get(playlist_id)
    if not playlist or playlist.get("owner_type") != "user":
        return False
    if is_builtin_playlist(playlist):
        return False
    return playlist.get("owner_id") == user.get("user_id")


def update_playback_history(user_id: str, video_id: str, playlist_id: str | None, position_seconds: float | None = None) -> None:
    history_id = f"playback_history:{user_id}:{video_id}"
    existing = db.get(history_id) or {"_id": history_id}
    existing["type"] = "playback_history"
    existing["user_id"] = user_id
    existing["video_id"] = video_id
    existing["playlist_id"] = playlist_id
    if position_seconds is not None:
        existing["position_seconds"] = float(position_seconds)
    existing["watched_at"] = now_iso()
    db.save(existing)


def playback_history_for(user: dict[str, Any]) -> list[dict[str, Any]]:
    history_docs = db.find_many("playback_history", user_id=user["user_id"])
    rows = []
    for doc in history_docs:
        video = db.get(doc.get("video_id"))
        if not video:
            continue
        context_playlist_id = doc.get("playlist_id")
        rows.append(
            {
                "history": doc,
                "video": video,
                "context_playlist_id": context_playlist_id,
                "context_playlist_removable": playlist_removable_for_user(user, context_playlist_id),
            }
        )
    rows.sort(key=lambda row: row["history"].get("watched_at", ""), reverse=True)
    return rows


def run_phase3_migration() -> None:
    global phase3_migration_done
    if phase3_migration_done:
        return
    with phase3_migration_lock:
        if phase3_migration_done:
            return
        marker = db.get("migration:phase3-account-ui")
        if marker:
            phase3_migration_done = True
            return

        users = sorted(db.find_many("user"), key=lambda user: (user.get("created_at", ""), user.get("user_id", "")))
        used_usernames = {
            normalize_username(user.get("username", "")).lower()
            for user in users
            if normalize_username(user.get("username", ""))
        }
        missing_username_users = [user for user in users if not normalize_username(user.get("username", ""))]

        users_updated = 0
        if missing_username_users:
            base = normalize_username(os.getenv(legacy_username_base_env, ""))
            if not base:
                raise RuntimeError(
                    f"{legacy_username_base_env} is required to migrate existing users missing usernames."
                )
            next_suffix = 1
            for user in missing_username_users:
                while True:
                    candidate = base if next_suffix == 1 else f"{base}{next_suffix}"
                    next_suffix += 1
                    if candidate.lower() not in used_usernames:
                        break
                user["username"] = candidate
                user["updated_at"] = now_iso()
                db.save(user)
                used_usernames.add(candidate.lower())
                users_updated += 1

        videos_updated = 0
        for video_doc in db.find_many("video"):
            if "dislikes" not in video_doc:
                continue
            video_doc.pop("dislikes", None)
            video_doc["updated_at"] = now_iso()
            db.save(video_doc)
            videos_updated += 1

        reactions_deleted = 0
        for reaction_doc in db.find_many("reaction"):
            if reaction_doc.get("value") == "like":
                continue
            db.delete(reaction_doc)
            reactions_deleted += 1

        db.save(
            {
                "_id": "migration:phase3-account-ui",
                "type": "migration",
                "users_updated": users_updated,
                "videos_updated": videos_updated,
                "reactions_deleted": reactions_deleted,
                "completed_at": now_iso(),
            }
        )
        phase3_migration_done = True


def is_valid_theme(theme: str | None) -> bool:
    return theme in THEME_OPTIONS


def selected_theme(user: dict[str, Any] | None = None) -> str:
    user = user or current_user()
    if user and is_valid_theme(user.get("preferred_theme")):
        return user["preferred_theme"]
    cookie_theme = request.cookies.get("theme")
    if is_valid_theme(cookie_theme):
        return cookie_theme
    return "night"


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


def visible_playlists_for(user: dict[str, Any] | None) -> list[dict[str, Any]]:
    playlists = db.find_many("playlist")
    output = []
    for playlist in playlists:
        if playlist.get("hidden_from_lists"):
            continue
        owner_type = playlist.get("owner_type")
        if owner_type == "system":
            output.append(playlist)
            continue
        if user and owner_type == "user" and playlist.get("owner_id") == user["user_id"]:
            output.append(playlist)
    return output


def top_level_playlists(user: dict[str, Any] | None) -> list[dict[str, Any]]:
    playlists = visible_playlists_for(user)
    top = [p for p in playlists if not p.get("parent_playlist_id")]
    return sorted(top, key=lambda p: (p.get("owner_type") != "system", p.get("name", "").lower()))


def playlist_items(playlist_id: str) -> list[dict[str, Any]]:
    items = sorted(
        db.find_many("playlist_item", playlist_id=playlist_id),
        key=lambda item: item.get("position", 0),
    )
    collected = []
    for item in items:
        target = db.get(item.get("item_id"))
        if not target:
            continue
        collected.append({"item": item, "target": target})
    playlists = [x for x in collected if x["item"].get("item_type") == "playlist"]
    videos = [x for x in collected if x["item"].get("item_type") == "video"]
    return playlists + videos


def can_edit_playlist(user: dict[str, Any], playlist: dict[str, Any]) -> bool:
    if playlist.get("owner_type") == "system":
        return False
    if is_builtin_playlist(playlist):
        return False
    return playlist.get("owner_id") == user.get("user_id")


def parse_ffprobe_duration(path: str) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=10)
        data = json.loads(proc.stdout or "{}")
        value = data.get("format", {}).get("duration")
        return float(value) if value else 0.0
    except Exception:
        return 0.0


def _first_existing_file(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _to_media_relative(path: Path) -> str:
    return str(path.relative_to(Path(media_root))).replace("\\", "/")


def find_existing_video_thumbnail(relative_video_path: str) -> str | None:
    video_path = Path(media_root) / relative_video_path
    parent = video_path.parent
    stem = video_path.stem

    candidates = []
    for ext in THUMBNAIL_EXTENSIONS:
        candidates.append(parent / f"{stem}{ext}")
    for base in VIDEO_THUMB_BASENAMES:
        for ext in THUMBNAIL_EXTENSIONS:
            candidates.append(parent / f"{base}{ext}")
    found = _first_existing_file(candidates)
    return _to_media_relative(found) if found else None


def find_existing_directory_thumbnail(relative_dir_path: str) -> str | None:
    root = Path(media_root)
    target_dir = root / relative_dir_path if relative_dir_path else root
    candidates = []
    for base in DIRECTORY_THUMB_BASENAMES:
        for ext in THUMBNAIL_EXTENSIONS:
            candidates.append(target_dir / f"{base}{ext}")
    found = _first_existing_file(candidates)
    return _to_media_relative(found) if found else None


def choose_thumbnail_seek_seconds(duration_seconds: float | int | None) -> float:
    if not duration_seconds or duration_seconds <= 0:
        return 30.0
    duration = float(duration_seconds)
    # Aim past intro cards/credits while avoiding late-scene spoilers.
    seek = duration * 0.20
    seek = max(25.0, seek)
    seek = min(180.0, seek)
    return min(seek, max(5.0, duration - 15.0))


def ensure_generated_video_thumbnail(relative_video_path: str, absolute_video_path: str, duration_seconds: float) -> str | None:
    generated_thumb_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"{sha1_text(relative_video_path)}.jpg"
    output_file = generated_thumb_dir / output_name
    if output_file.exists():
        return f"generated-thumbs/{output_name}"

    seek_seconds = choose_thumbnail_seek_seconds(duration_seconds)
    cmd = (
        f"ffmpeg -hide_banner -loglevel error -y -ss {seek_seconds:.2f} "
        f"-i {shlex.quote(absolute_video_path)} -frames:v 1 -q:v 3 {shlex.quote(str(output_file))}"
    )
    try:
        subprocess.run(cmd, shell=True, check=True, timeout=20)
        if output_file.exists():
            return f"generated-thumbs/{output_name}"
    except Exception:
        return None
    return None


class ScannerManager:
    def __init__(self):
        self._thread = None
        self._lock = threading.Lock()
        self.status = {
            "running": False,
            "last_started_at": None,
            "last_finished_at": None,
            "last_error": None,
        }

    def _emit_scan_event(self, event_name: str, payload: dict[str, Any]) -> None:
        socketio.emit(event_name, payload)

    def trigger(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"started": False, "message": "Scan already running", **self.status}
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return {"started": True, "message": "Scan started", **self.status}

    def _acquire_lock(self) -> bool:
        lock_doc = db.get("scan_lock")
        now = time.time()
        expires_at = now + scan_lock_ttl
        payload = {
            "_id": "scan_lock",
            "type": "scan_lock",
            "holder_id": str(uuid.uuid4()),
            "acquired_at_unix": now,
            "expires_at_unix": expires_at,
            "acquired_at": now_iso(),
        }
        if lock_doc:
            if lock_doc.get("expires_at_unix", 0) > now:
                return False
            payload["_rev"] = lock_doc["_rev"]
        db.save(payload)
        return True

    def _release_lock(self) -> None:
        lock_doc = db.get("scan_lock")
        if lock_doc:
            db.delete(lock_doc)

    def _discover(self) -> dict[str, Any]:
        root = Path(media_root)
        if not root.exists():
            return {}
        tree = {}
        for base, dirs, files in os.walk(root):
            dirs.sort()
            files.sort()
            rel = os.path.relpath(base, root)
            rel = "" if rel == "." else rel
            entry = tree.setdefault(
                rel,
                {
                    "name": root.name if rel == "" else Path(rel).name,
                    "child_dirs": [],
                    "videos": [],
                },
            )
            for d in dirs:
                child_rel = f"{rel}/{d}" if rel else d
                entry["child_dirs"].append(child_rel)
                tree.setdefault(
                    child_rel,
                    {
                        "name": d,
                        "child_dirs": [],
                        "videos": [],
                    },
                )
            for file_name in files:
                ext = Path(file_name).suffix.lower()
                if ext not in VIDEO_EXTENSIONS:
                    continue
                rel_file = f"{rel}/{file_name}" if rel else file_name
                abs_file = str(root / rel_file)
                entry["videos"].append(
                    {
                        "relative_path": rel_file,
                        "absolute_path": abs_file,
                        "title": Path(file_name).stem,
                        "description": "",
                        "duration": parse_ffprobe_duration(abs_file),
                    }
                )
        return tree

    def _upsert_scanned_videos(self, tree: dict[str, Any]) -> set[str]:
        found_paths = set()
        total_videos = sum(len(entry["videos"]) for entry in tree.values())
        processed = 0
        for entry in tree.values():
            for video in entry["videos"]:
                rel_path = video["relative_path"]
                found_paths.add(rel_path)
                video_id = f"video:{sha1_text(rel_path)}"
                existing = db.get(video_id) or {}
                manual_thumb = find_existing_video_thumbnail(rel_path)
                thumbnail_kind = None
                thumbnail_path = None
                if manual_thumb:
                    thumbnail_kind = "media"
                    thumbnail_path = manual_thumb
                elif existing.get("thumbnail_kind") == "media":
                    existing_media = existing.get("thumbnail_path")
                    if existing_media and (Path(media_root) / existing_media).exists():
                        thumbnail_kind = "media"
                        thumbnail_path = existing_media
                elif existing.get("thumbnail_kind") == "static":
                    existing_static = existing.get("thumbnail_path")
                    if existing_static and (Path(app.static_folder or "./static") / existing_static).exists():
                        thumbnail_kind = "static"
                        thumbnail_path = existing_static
                if not thumbnail_path:
                    generated = ensure_generated_video_thumbnail(rel_path, video["absolute_path"], video["duration"])
                    if generated:
                        thumbnail_kind = "static"
                        thumbnail_path = generated
                doc = {
                    "_id": video_id,
                    "type": "video",
                    "source": "scanner",
                    "relative_path": rel_path,
                    "title": video["title"],
                    "description": video["description"],
                    "thumbnail_kind": thumbnail_kind,
                    "thumbnail_path": thumbnail_path,
                    "duration": video["duration"],
                    "views": existing.get("views", 0),
                    "likes": existing.get("likes", 0),
                    "updated_at": now_iso(),
                }
                if existing.get("_rev"):
                    doc["_rev"] = existing["_rev"]
                db.save(doc)
                processed += 1
                if total_videos > 0 and (processed == total_videos or processed % 10 == 0):
                    self._emit_scan_event(
                        "scan_progress",
                        {
                            "phase": "upserting_videos",
                            "processed": processed,
                            "total": total_videos,
                        },
                    )

        for video_doc in db.find_many("video", source="scanner"):
            if video_doc.get("relative_path") not in found_paths:
                db.delete(video_doc)
        return {f"video:{sha1_text(path)}" for path in found_paths}

    def _rebuild_system_playlists(self, tree: dict[str, Any], valid_video_ids: set[str]) -> None:
        self._emit_scan_event(
            "scan_progress",
            {
                "phase": "rebuilding_playlists",
                "processed": 0,
                "total": len(tree),
            },
        )
        for item in db.find_many("playlist_item"):
            if item.get("owner_type") == "system":
                db.delete(item)
        for playlist in db.find_many("playlist", owner_type="system"):
            db.delete(playlist)

        sorted_dirs = sorted(tree.keys(), key=lambda x: (x.count("/"), x))
        for rel in sorted_dirs:
            pid = f"playlist:system:{sha1_text(rel or '__root__')}"
            parent_rel = str(Path(rel).parent) if rel else ""
            if parent_rel == ".":
                parent_rel = ""
            parent_id = f"playlist:system:{sha1_text(parent_rel or '__root__')}" if rel else None
            doc = {
                "_id": pid,
                "type": "playlist",
                "playlist_id": pid,
                "name": tree[rel]["name"],
                "owner_type": "system",
                "owner_id": "system",
                "editable": False,
                "source_path": rel,
                "parent_playlist_id": parent_id,
                "thumbnail_kind": None,
                "thumbnail_path": None,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
            db.save(doc)

        rebuilt = 0
        for rel in sorted_dirs:
            playlist_id = f"playlist:system:{sha1_text(rel or '__root__')}"
            position = 0
            child_dirs = sorted(tree[rel]["child_dirs"], key=lambda v: tree[v]["name"].lower())
            for child_rel in child_dirs:
                child_id = f"playlist:system:{sha1_text(child_rel or '__root__')}"
                item_doc = {
                    "_id": f"playlist_item:system:{sha1_text(f'{playlist_id}:{child_id}')}",
                    "type": "playlist_item",
                    "owner_type": "system",
                    "playlist_id": playlist_id,
                    "item_type": "playlist",
                    "item_id": child_id,
                    "position": position,
                    "created_at": now_iso(),
                }
                db.save(item_doc)
                position += 1

        for rel in sorted(sorted_dirs, key=lambda x: (-x.count("/"), x)):
            playlist_id = f"playlist:system:{sha1_text(rel or '__root__')}"
            playlist = db.get(playlist_id)
            if not playlist:
                continue

            thumb_kind = None
            thumb_path = None
            manual_thumb = find_existing_directory_thumbnail(rel)
            if manual_thumb:
                thumb_kind = "media"
                thumb_path = manual_thumb
            else:
                videos = sorted(tree[rel]["videos"], key=lambda v: v["title"].lower())
                for video in videos:
                    vid = f"video:{sha1_text(video['relative_path'])}"
                    video_doc = db.get(vid)
                    if video_doc and video_doc.get("thumbnail_path"):
                        thumb_kind = video_doc.get("thumbnail_kind")
                        thumb_path = video_doc.get("thumbnail_path")
                        break
            if not thumb_path:
                for child_rel in sorted(tree[rel]["child_dirs"], key=lambda v: tree[v]["name"].lower()):
                    child_id = f"playlist:system:{sha1_text(child_rel or '__root__')}"
                    child_playlist = db.get(child_id)
                    if child_playlist and child_playlist.get("thumbnail_path"):
                        thumb_kind = child_playlist.get("thumbnail_kind")
                        thumb_path = child_playlist.get("thumbnail_path")
                        break

            playlist["thumbnail_kind"] = thumb_kind
            playlist["thumbnail_path"] = thumb_path
            playlist["updated_at"] = now_iso()
            db.save(playlist)

            videos = sorted(tree[rel]["videos"], key=lambda v: v["title"].lower())
            for video in videos:
                vid = f"video:{sha1_text(video['relative_path'])}"
                if vid not in valid_video_ids:
                    continue
                item_doc = {
                    "_id": f"playlist_item:system:{sha1_text(f'{playlist_id}:{vid}')}",
                    "type": "playlist_item",
                    "owner_type": "system",
                    "playlist_id": playlist_id,
                    "item_type": "video",
                    "item_id": vid,
                    "position": position,
                    "created_at": now_iso(),
                }
                db.save(item_doc)
                position += 1
            rebuilt += 1
            self._emit_scan_event(
                "scan_progress",
                {
                    "phase": "rebuilding_playlists",
                    "processed": rebuilt,
                    "total": len(sorted_dirs),
                },
            )

    def _run(self):
        self.status["running"] = True
        self.status["last_started_at"] = now_iso()
        self.status["last_error"] = None
        self._emit_scan_event(
            "scan_started",
            {
                "started_at": self.status["last_started_at"],
            },
        )
        if not self._acquire_lock():
            self.status["last_error"] = "Scan lock is already held by another worker"
            self.status["running"] = False
            self.status["last_finished_at"] = now_iso()
            self._emit_scan_event(
                "scan_failed",
                {
                    "error": self.status["last_error"],
                    "finished_at": self.status["last_finished_at"],
                },
            )
            return
        try:
            tree = self._discover()
            self._emit_scan_event(
                "scan_progress",
                {
                    "phase": "discovered_library",
                    "directories": len(tree),
                    "videos": sum(len(entry["videos"]) for entry in tree.values()),
                },
            )
            valid_video_ids = self._upsert_scanned_videos(tree)
            self._rebuild_system_playlists(tree, valid_video_ids)
        except Exception as exc:
            self.status["last_error"] = str(exc)
        finally:
            self._release_lock()
            self.status["running"] = False
            self.status["last_finished_at"] = now_iso()
            if self.status["last_error"]:
                self._emit_scan_event(
                    "scan_failed",
                    {
                        "error": self.status["last_error"],
                        "finished_at": self.status["last_finished_at"],
                    },
                )
            else:
                self._emit_scan_event(
                    "scan_completed",
                    {
                        "finished_at": self.status["last_finished_at"],
                    },
                )


scanner_manager = ScannerManager()


def delete_user_playlist_tree(user: dict[str, Any], playlist_id: str) -> None:
    for child in db.find_many("playlist", parent_playlist_id=playlist_id):
        if child.get("owner_type") == "user" and child.get("owner_id") == user["user_id"]:
            delete_user_playlist_tree(user, child["_id"])
    for item in db.find_many("playlist_item", playlist_id=playlist_id):
        db.delete(item)
    playlist = db.get(playlist_id)
    if playlist:
        db.delete(playlist)


def hard_delete_user_data(user: dict[str, Any]) -> None:
    for playlist in db.find_many("playlist", owner_type="user", owner_id=user["user_id"]):
        delete_user_playlist_tree(user, playlist["_id"])

    for item in db.find_many("playlist_item", owner_type="user"):
        if item.get("playlist_id", "").startswith("playlist:"):
            maybe_playlist = db.get(item.get("playlist_id"))
            if maybe_playlist and maybe_playlist.get("owner_id") == user["user_id"]:
                db.delete(item)

    for progress in db.find_many("watch_progress", user_id=user["user_id"]):
        db.delete(progress)
    for reaction in db.find_many("reaction", user_id=user["user_id"]):
        db.delete(reaction)
    for event in db.find_many("history_event", user_id=user["user_id"]):
        db.delete(event)

    user_doc = db.get(f"user:{user['user_id']}")
    if user_doc:
        db.delete(user_doc)


@app.before_request
def before_request_migrations():
    run_phase3_migration()


@app.context_processor
def inject_template_globals():
    user = current_user()
    return {
        "user": user,
        "theme": selected_theme(user),
        "thumbnail_url": thumbnail_url,
        "format_duration": format_duration,
        "owner_username": owner_username,
    }


@app.route("/")
def index():
    user = current_user()
    playlists = top_level_playlists(user)
    has_videos = len(db.find_many("video")) > 0
    return render_template("index.html", user=user, playlists=playlists, has_videos=has_videos)


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
    if username_taken(username):
        return render_template("register.html", error="Username already exists.", user=current_user()), 400
    if email_taken(email):
        return render_template("register.html", error="Email already exists.", user=current_user()), 400

    user_id = str(uuid.uuid4())
    doc = {
        "_id": f"user:{user_id}",
        "type": "user",
        "user_id": user_id,
        "username": username,
        "email": email,
        "password_hash": generate_password_hash(password),
        "preferred_theme": selected_theme(None),
        "created_at": now_iso(),
    }
    db.save(doc)
    session["user_id"] = user_id
    return redirect(url_for("index"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", user=current_user())

    email = normalize_email(request.form.get("email") or "")
    password = request.form.get("password") or ""
    user = db.find_one("user", email=email)
    if not user or not check_password_hash(user.get("password_hash", ""), password):
        return render_template("login.html", error="Invalid credentials.", user=current_user()), 401
    session["user_id"] = user["user_id"]
    return redirect(url_for("index"))


@app.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    return redirect(url_for("index"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    user, auth_error = ensure_auth()
    if auth_error:
        return redirect(url_for("login"))

    if request.method == "GET":
        return render_template("profile.html", user=user, profile_section="account")

    action = request.form.get("action")
    error = None
    success = None
    if action == "update_username":
        new_username = normalize_username(request.form.get("username") or "")
        if not new_username:
            error = "Username is required."
        elif username_taken(new_username, exclude_user_id=user["user_id"]):
            error = "Username already exists."
        else:
            user["username"] = new_username
            user["updated_at"] = now_iso()
            db.save(user)
            success = "Username updated."
    elif action == "update_email":
        new_email = normalize_email(request.form.get("email") or "")
        if not new_email:
            error = "Email is required."
        elif email_taken(new_email, exclude_user_id=user["user_id"]):
            error = "Email already exists."
        else:
            user["email"] = new_email
            user["updated_at"] = now_iso()
            db.save(user)
            success = "Email updated."
    elif action == "update_password":
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        if not current_password or not new_password:
            error = "Current and new password are required."
        elif not check_password_hash(user.get("password_hash", ""), current_password):
            error = "Current password is incorrect."
        else:
            user["password_hash"] = generate_password_hash(new_password)
            user["updated_at"] = now_iso()
            db.save(user)
            success = "Password updated."
    elif action == "delete_account":
        hard_delete_user_data(user)
        session.pop("user_id", None)
        return redirect(url_for("index"))
    else:
        error = "Unknown profile action."

    fresh_user = current_user()
    return render_template("profile.html", user=fresh_user, profile_section="account", error=error, success=success)


def _render_library_section(user: dict[str, Any], section: str):
    builtin_states = builtin_states_for_user(user)
    if section == "history":
        content = {"history_rows": playback_history_for(user), "builtin_states": builtin_states}
    elif section == "playlists":
        content = {"playlists": list_custom_playlists_for(user)}
    elif section == "favorites":
        favorites = ensure_builtin_playlist(user, "favorites")
        content = {
            "playlist": favorites,
            "items": playlist_items(favorites["_id"]),
            "builtin_states": builtin_states,
            "context_playlist_id": favorites["_id"],
            "context_playlist_removable": False,
        }
    elif section == "watch_later":
        watch_later = ensure_builtin_playlist(user, "watch_later")
        content = {
            "playlist": watch_later,
            "items": playlist_items(watch_later["_id"]),
            "builtin_states": builtin_states,
            "context_playlist_id": watch_later["_id"],
            "context_playlist_removable": False,
        }
    else:
        return "Not found", 404

    return render_template("library.html", user=user, library_section=section, **content)


@app.route("/history")
def history_page():
    user, auth_error = ensure_auth()
    if auth_error:
        return redirect(url_for("login"))
    return _render_library_section(user, "history")


@app.route("/playlists")
def playlists_page():
    user, auth_error = ensure_auth()
    if auth_error:
        return redirect(url_for("login"))
    return _render_library_section(user, "playlists")


@app.route("/favorites")
def favorites_page():
    user, auth_error = ensure_auth()
    if auth_error:
        return redirect(url_for("login"))
    return _render_library_section(user, "favorites")


@app.route("/watch-later")
def watch_later_page():
    user, auth_error = ensure_auth()
    if auth_error:
        return redirect(url_for("login"))
    return _render_library_section(user, "watch_later")


@app.route("/playlist/<path:playlist_id>")
def playlist_view(playlist_id: str):
    user = current_user()
    pid = playlist_id if playlist_id.startswith("playlist:") else f"playlist:{playlist_id}"
    playlist = db.get(pid)
    if not playlist:
        return "Playlist not found", 404
    if playlist.get("owner_type") == "user" and (not user or playlist.get("owner_id") != user.get("user_id")):
        return "Unauthorized", 403
    items = playlist_items(pid)
    return render_template(
        "playlist.html",
        user=user,
        playlist=playlist,
        items=items,
        playlist_owner=owner_username(playlist),
        builtin_states=builtin_states_for_user(user),
        context_playlist_id=pid,
        context_playlist_removable=playlist_removable_for_user(user, pid),
    )


@app.route("/video/<path:video_id>")
def video_view(video_id: str):
    user = current_user()
    vid = video_id if video_id.startswith("video:") else f"video:{video_id}"
    playlist_id = request.args.get("playlist_id")
    video = db.get(vid)
    if not video:
        return "Video not found", 404

    resume_position = 0.0
    is_liked = False
    playlist = None
    playlist_nav = None
    builtin_states = builtin_states_for_user(user)
    if user:
        progress = db.get(f"watch_progress:{user['user_id']}:{vid}")
        if progress:
            resume_position = float(progress.get("last_position_seconds", 0.0))
        reaction = db.get(f"reaction:{user['user_id']}:{vid}")
        is_liked = bool(reaction and reaction.get("value") == "like")
    if playlist_id:
        playlist = db.get(playlist_id)
        if playlist:
            list_items = playlist_items(playlist["_id"])
            video_items = [pair for pair in list_items if pair["item"].get("item_type") == "video"]
            ids = [pair["target"]["_id"] for pair in video_items]
            if vid in ids:
                index = ids.index(vid)
                total_duration = sum(float(pair["target"].get("duration", 0) or 0) for pair in video_items)
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
        context_playlist_removable=playlist_removable_for_user(user, playlist["_id"] if playlist else None),
    )


@app.route("/media/<path:relative_path>")
def media_file(relative_path: str):
    return send_from_directory(media_root, relative_path)


@app.route("/api/scan/trigger", methods=["POST"])
def api_scan_trigger():
    result = scanner_manager.trigger()
    return jsonify(result)


@app.route("/api/scan/status", methods=["GET"])
def api_scan_status():
    return jsonify(scanner_manager.status)


@app.route("/api/theme", methods=["POST"])
def api_theme():
    payload = request.json or {}
    theme = payload.get("theme")
    if not is_valid_theme(theme):
        return jsonify({"error": "theme must be one of: day, night"}), 400

    user = current_user()
    if user:
        user["preferred_theme"] = theme
        user["updated_at"] = now_iso()
        db.save(user)

    response = jsonify({"ok": True, "theme": theme})
    response.set_cookie("theme", theme, max_age=60 * 60 * 24 * 365, samesite="Lax")
    return response


@app.route("/api/video/<path:video_id>/play", methods=["POST"])
def api_video_play(video_id: str):
    vid = video_id if video_id.startswith("video:") else f"video:{video_id}"
    playlist_id = (request.json or {}).get("playlist_id")
    video = db.get(vid)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    video["views"] = int(video.get("views", 0)) + 1
    video["updated_at"] = now_iso()
    db.save(video)

    user = current_user()
    if user:
        update_playback_history(user["user_id"], vid, playlist_id, position_seconds=0.0)
    return jsonify({"ok": True, "views": video["views"]})


@app.route("/api/video/<path:video_id>/progress", methods=["POST"])
def api_video_progress(video_id: str):
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error

    vid = video_id if video_id.startswith("video:") else f"video:{video_id}"
    payload = request.json or {}
    playlist_id = payload.get("playlist_id")
    position = float(payload.get("position_seconds", 0.0))

    progress_id = f"watch_progress:{user['user_id']}:{vid}"
    existing = db.get(progress_id) or {"_id": progress_id}
    existing["type"] = "watch_progress"
    existing["user_id"] = user["user_id"]
    existing["video_id"] = vid
    existing["playlist_id"] = playlist_id
    existing["last_position_seconds"] = position
    existing["last_watched_at"] = now_iso()
    db.save(existing)

    update_playback_history(user["user_id"], vid, playlist_id, position_seconds=position)
    return jsonify({"ok": True})


@app.route("/api/video/<path:video_id>/reaction", methods=["POST"])
def api_video_reaction(video_id: str):
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error

    vid = video_id if video_id.startswith("video:") else f"video:{video_id}"
    video = db.get(vid)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    if "dislikes" in video:
        video.pop("dislikes", None)

    reaction_id = f"reaction:{user['user_id']}:{vid}"
    current = db.get(reaction_id)
    prev_value = current.get("value") if current else None
    likes = int(video.get("likes", 0))
    if prev_value == "like":
        likes = max(0, likes - 1)
        db.delete(current)
        liked = False
    else:
        likes = likes + 1
        record = current or {"_id": reaction_id}
        record["type"] = "reaction"
        record["user_id"] = user["user_id"]
        record["video_id"] = vid
        record["value"] = "like"
        record["timestamp"] = now_iso()
        db.save(record)
        liked = True

    video["likes"] = likes
    video["updated_at"] = now_iso()
    db.save(video)

    return jsonify({"ok": True, "likes": video["likes"], "liked": liked})


@app.route("/api/video/<path:video_id>/add-to-playlist", methods=["POST"])
def api_video_add_to_playlist(video_id: str):
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error

    vid = video_id if video_id.startswith("video:") else f"video:{video_id}"
    video = db.get(vid)
    if not video:
        return jsonify({"error": "Video not found"}), 404

    payload = request.json or {}
    playlist_id = payload.get("playlist_id")
    new_playlist_name = normalize_username(payload.get("new_playlist_name") or "")
    if not playlist_id and not new_playlist_name:
        return jsonify({"error": "playlist_id or new_playlist_name is required"}), 400

    if new_playlist_name:
        new_id = f"playlist:{uuid.uuid4()}"
        playlist = {
            "_id": new_id,
            "type": "playlist",
            "playlist_id": new_id,
            "name": new_playlist_name,
            "owner_type": "user",
            "owner_id": user["user_id"],
            "editable": True,
            "thumbnail_kind": video.get("thumbnail_kind"),
            "thumbnail_path": video.get("thumbnail_path"),
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        db.save(playlist)
        playlist_id = new_id
    else:
        playlist = db.get(playlist_id)
        if not playlist:
            return jsonify({"error": "playlist not found"}), 404
        if playlist.get("owner_id") != user["user_id"] or is_builtin_playlist(playlist):
            return jsonify({"error": "cannot use this playlist"}), 403

    existing = db.find_many("playlist_item", playlist_id=playlist_id)
    for item in existing:
        if item.get("item_type") == "video" and item.get("item_id") == vid:
            return jsonify({"ok": True, "already_present": True, "playlist_id": playlist_id})
    position = max([i.get("position", 0) for i in existing], default=-1) + 1
    db.save(
        {
            "_id": f"playlist_item:user:{uuid.uuid4()}",
            "type": "playlist_item",
            "owner_type": "user",
            "playlist_id": playlist_id,
            "item_type": "video",
            "item_id": vid,
            "position": position,
            "created_at": now_iso(),
        }
    )
    return jsonify({"ok": True, "playlist_id": playlist_id})


@app.route("/api/video/<path:video_id>/builtin", methods=["POST"])
def api_video_builtin(video_id: str):
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error
    vid = video_id if video_id.startswith("video:") else f"video:{video_id}"
    if not db.get(vid):
        return jsonify({"error": "Video not found"}), 404
    kind = (request.json or {}).get("kind")
    if kind not in BUILTIN_PLAYLISTS:
        return jsonify({"error": "Unknown builtin playlist kind"}), 400

    playlist = ensure_builtin_playlist(user, kind)
    items = db.find_many("playlist_item", playlist_id=playlist["_id"])
    existing = next((item for item in items if item.get("item_type") == "video" and item.get("item_id") == vid), None)
    if existing:
        db.delete(existing)
        return jsonify({"ok": True, "added": False, "kind": kind})

    position = max([item.get("position", 0) for item in items], default=-1) + 1
    db.save(
        {
            "_id": f"playlist_item:builtin:{uuid.uuid4()}",
            "type": "playlist_item",
            "owner_type": "user",
            "playlist_id": playlist["_id"],
            "item_type": "video",
            "item_id": vid,
            "position": position,
            "created_at": now_iso(),
        }
    )
    return jsonify({"ok": True, "added": True, "kind": kind})


@app.route("/api/user/playlists", methods=["GET"])
def api_user_playlists():
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error
    playlists = list_custom_playlists_for(user)
    return jsonify(
        {
            "playlists": [
                {"playlist_id": playlist["_id"], "name": playlist.get("name", "Untitled")}
                for playlist in playlists
            ]
        }
    )


@app.route("/api/playlist", methods=["POST"])
def api_playlist_create():
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error
    payload = request.json or {}
    name = (payload.get("name") or "").strip()
    parent_playlist_id = payload.get("parent_playlist_id")
    if not name:
        return jsonify({"error": "name is required"}), 400
    if parent_playlist_id:
        parent = db.get(parent_playlist_id)
        if not parent:
            return jsonify({"error": "parent playlist not found"}), 404
        if not can_edit_playlist(user, parent):
            return jsonify({"error": "cannot use this parent playlist"}), 403

    playlist_uuid = str(uuid.uuid4())
    playlist_id = f"playlist:{playlist_uuid}"
    doc = {
        "_id": playlist_id,
        "type": "playlist",
        "playlist_id": playlist_id,
        "name": name,
        "owner_type": "user",
        "owner_id": user["user_id"],
        "editable": True,
        "parent_playlist_id": parent_playlist_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    db.save(doc)
    return jsonify({"ok": True, "playlist_id": playlist_id})


@app.route("/api/playlist/<path:playlist_id>/rename", methods=["POST"])
def api_playlist_rename(playlist_id: str):
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error

    pid = playlist_id if playlist_id.startswith("playlist:") else f"playlist:{playlist_id}"
    playlist = db.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not can_edit_playlist(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    playlist["name"] = name
    playlist["updated_at"] = now_iso()
    db.save(playlist)
    return jsonify({"ok": True})


@app.route("/api/playlist/<path:playlist_id>/delete", methods=["POST"])
def api_playlist_delete(playlist_id: str):
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error

    pid = playlist_id if playlist_id.startswith("playlist:") else f"playlist:{playlist_id}"
    playlist = db.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not can_edit_playlist(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    delete_user_playlist_tree(user, pid)
    return jsonify({"ok": True})


@app.route("/api/playlist/<path:playlist_id>/items", methods=["POST"])
def api_playlist_add_item(playlist_id: str):
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error

    pid = playlist_id if playlist_id.startswith("playlist:") else f"playlist:{playlist_id}"
    playlist = db.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not can_edit_playlist(user, playlist):
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

    existing = db.find_many("playlist_item", playlist_id=pid)
    position = max([i.get("position", 0) for i in existing], default=-1) + 1
    iid = f"playlist_item:user:{uuid.uuid4()}"
    doc = {
        "_id": iid,
        "type": "playlist_item",
        "owner_type": "user",
        "playlist_id": pid,
        "item_type": item_type,
        "item_id": item_id,
        "position": position,
        "created_at": now_iso(),
    }
    db.save(doc)
    return jsonify({"ok": True, "item_id": iid})


@app.route("/api/playlist/<path:playlist_id>/items/remove", methods=["POST"])
def api_playlist_remove_item(playlist_id: str):
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error

    pid = playlist_id if playlist_id.startswith("playlist:") else f"playlist:{playlist_id}"
    playlist = db.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not can_edit_playlist(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    item_doc_id = (request.json or {}).get("playlist_item_id")
    item = db.get(item_doc_id)
    if not item or item.get("playlist_id") != pid:
        return jsonify({"error": "playlist item not found"}), 404
    db.delete(item)
    return jsonify({"ok": True})


@app.route("/api/playlist/<path:playlist_id>/remove-video", methods=["POST"])
def api_playlist_remove_video(playlist_id: str):
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error
    pid = playlist_id if playlist_id.startswith("playlist:") else f"playlist:{playlist_id}"
    playlist = db.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not can_edit_playlist(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    video_id = (request.json or {}).get("video_id")
    if not video_id:
        return jsonify({"error": "video_id is required"}), 400
    removed = False
    for item in db.find_many("playlist_item", playlist_id=pid):
        if item.get("item_type") == "video" and item.get("item_id") == video_id:
            db.delete(item)
            removed = True
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/playlist/<path:playlist_id>/items/reorder", methods=["POST"])
def api_playlist_reorder(playlist_id: str):
    user, auth_error = ensure_auth()
    if auth_error:
        return auth_error

    pid = playlist_id if playlist_id.startswith("playlist:") else f"playlist:{playlist_id}"
    playlist = db.get(pid)
    if not playlist:
        return jsonify({"error": "playlist not found"}), 404
    if not can_edit_playlist(user, playlist):
        return jsonify({"error": "cannot edit this playlist"}), 403

    ordered_item_ids = (request.json or {}).get("ordered_playlist_item_ids") or []
    for index, item_id in enumerate(ordered_item_ids):
        item = db.get(item_id)
        if not item or item.get("playlist_id") != pid:
            continue
        item["position"] = index
        db.save(item)
    return jsonify({"ok": True})


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5050)
