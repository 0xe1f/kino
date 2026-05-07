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

import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from flask_socketio import SocketIO

from db import db
from utils import (
    VIDEO_EXTENSIONS,
    media_root,
    now_iso,
    parse_ffprobe_duration,
    scan_lock_ttl,
    sha1_text,
    find_existing_video_thumbnail,
    find_existing_directory_thumbnail,
    ensure_generated_video_thumbnail,
)


class ScannerManager:
    def __init__(self, socketio: SocketIO) -> None:
        self._socketio = socketio
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "running": False,
            "last_started_at": None,
            "last_finished_at": None,
            "last_error": None,
            "progress": None,
        }
        self._log = logging.getLogger("kino.scanner")

    @property
    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def _update_status(self, **kwargs: Any) -> None:
        with self._status_lock:
            self._status.update(kwargs)

    def _emit(self, event_name: str, payload: dict[str, Any]) -> None:
        if event_name == "scan_progress":
            self._update_status(progress=payload)
        self._socketio.emit(event_name, payload)

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
        payload: dict[str, Any] = {
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
        tree: dict[str, Any] = {}
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
                    {"name": d, "child_dirs": [], "videos": []},
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
                        "duration": None,
                    }
                )
            self._emit("scan_progress", {"phase": "discovering", "current_dir": rel or "/"})
        return tree

    def _upsert_scanned_videos(self, tree: dict[str, Any]) -> set[str]:
        found_paths: set[str] = set()
        total_videos = sum(len(entry["videos"]) for entry in tree.values())
        processed = 0

        static_dir = Path(os.getenv("STATIC_DIR", "./static"))

        for entry in tree.values():
            for video in entry["videos"]:
                rel_path = video["relative_path"]
                found_paths.add(rel_path)
                video_id = f"video:{sha1_text(rel_path)}"
                existing = db.get(video_id) or {}

                self._log.info("[%d/%d] %s", processed + 1, total_videos, rel_path)

                duration = video["duration"]
                if duration is None:
                    duration = parse_ffprobe_duration(video["absolute_path"])

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
                    if existing_static and (static_dir / existing_static).exists():
                        thumbnail_kind = "static"
                        thumbnail_path = existing_static
                if not thumbnail_path:
                    self._log.info("  generating thumbnail for %s", rel_path)
                    generated = ensure_generated_video_thumbnail(
                        rel_path, video["absolute_path"], duration
                    )
                    if generated:
                        thumbnail_kind = "static"
                        thumbnail_path = generated

                now = now_iso()
                doc: dict[str, Any] = {
                    "_id": video_id,
                    "type": "video",
                    "source": "scanner",
                    "relative_path": rel_path,
                    "title": video["title"],
                    "description": video["description"],
                    "thumbnail_kind": thumbnail_kind,
                    "thumbnail_path": thumbnail_path,
                    "duration": duration,
                    "views": existing.get("views", 0),
                    "likes": existing.get("likes", 0),
                    "created_at": existing.get("created_at") or now,
                    "updated_at": now,
                }
                if existing.get("_rev"):
                    doc["_rev"] = existing["_rev"]
                db.save(doc)

                processed += 1
                if total_videos > 0 and (processed == total_videos or processed % 10 == 0):
                    self._emit(
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

    def _rebuild_system_playlists(
        self,
        tree: dict[str, Any],
        valid_video_ids: set[str],
    ) -> None:
        self._emit(
            "scan_progress",
            {"phase": "rebuilding_playlists", "processed": 0, "total": len(tree)},
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
            parent_id = (
                f"playlist:system:{sha1_text(parent_rel or '__root__')}" if rel else None
            )
            doc: dict[str, Any] = {
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
            child_dirs = sorted(
                tree[rel]["child_dirs"], key=lambda v: tree[v]["name"].lower()
            )
            for child_rel in child_dirs:
                child_id = f"playlist:system:{sha1_text(child_rel or '__root__')}"
                db.save(
                    {
                        "_id": f"playlist_item:system:{sha1_text(f'{playlist_id}:{child_id}')}",
                        "type": "playlist_item",
                        "owner_type": "system",
                        "playlist_id": playlist_id,
                        "item_type": "playlist",
                        "item_id": child_id,
                        "position": position,
                        "created_at": now_iso(),
                    }
                )
                position += 1

        for rel in sorted(sorted_dirs, key=lambda x: (-x.count("/"), x)):
            playlist_id = f"playlist:system:{sha1_text(rel or '__root__')}"
            playlist = db.get(playlist_id)
            if not playlist:
                continue

            child_dirs = sorted(
                tree[rel]["child_dirs"], key=lambda v: tree[v]["name"].lower()
            )

            thumb_kind = None
            thumb_path = None
            manual_thumb = find_existing_directory_thumbnail(rel)
            if manual_thumb:
                thumb_kind = "media"
                thumb_path = manual_thumb
            else:
                videos_sorted = sorted(
                    tree[rel]["videos"], key=lambda v: v["title"].lower()
                )
                for video in videos_sorted:
                    vid = f"video:{sha1_text(video['relative_path'])}"
                    video_doc = db.get(vid)
                    if video_doc and video_doc.get("thumbnail_path"):
                        thumb_kind = video_doc.get("thumbnail_kind")
                        thumb_path = video_doc.get("thumbnail_path")
                        break
            if not thumb_path:
                for child_rel in sorted(
                    tree[rel]["child_dirs"], key=lambda v: tree[v]["name"].lower()
                ):
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

            position = len(child_dirs)
            for video in sorted(tree[rel]["videos"], key=lambda v: v["title"].lower()):
                vid = f"video:{sha1_text(video['relative_path'])}"
                if vid not in valid_video_ids:
                    continue
                db.save(
                    {
                        "_id": f"playlist_item:system:{sha1_text(f'{playlist_id}:{vid}')}",
                        "type": "playlist_item",
                        "owner_type": "system",
                        "playlist_id": playlist_id,
                        "item_type": "video",
                        "item_id": vid,
                        "position": position,
                        "created_at": now_iso(),
                    }
                )
                position += 1

            rebuilt += 1
            self._emit(
                "scan_progress",
                {
                    "phase": "rebuilding_playlists",
                    "processed": rebuilt,
                    "total": len(sorted_dirs),
                },
            )

    def _run(self) -> None:
        self._log.info("Scan started")
        self._update_status(
            running=True,
            last_started_at=now_iso(),
            last_error=None,
            progress=None,
        )
        self._emit("scan_started", {"started_at": self.status["last_started_at"]})

        if not self._acquire_lock():
            self._update_status(
                last_error="Scan lock is already held by another worker",
                running=False,
                last_finished_at=now_iso(),
                progress=None,
            )
            self._emit(
                "scan_failed",
                {
                    "error": self.status["last_error"],
                    "finished_at": self.status["last_finished_at"],
                },
            )
            return

        try:
            tree = self._discover()
            self._emit(
                "scan_progress",
                {
                    "phase": "discovered_library",
                    "directories": len(tree),
                    "videos": sum(len(e["videos"]) for e in tree.values()),
                },
            )
            valid_video_ids = self._upsert_scanned_videos(tree)
            self._rebuild_system_playlists(tree, valid_video_ids)
        except Exception as exc:
            self._log.exception("Scan failed: %s", exc)
            self._update_status(last_error=str(exc))
        finally:
            self._release_lock()
            self._update_status(running=False, last_finished_at=now_iso(), progress=None)
            if self.status["last_error"]:
                self._emit(
                    "scan_failed",
                    {
                        "error": self.status["last_error"],
                        "finished_at": self.status["last_finished_at"],
                    },
                )
            else:
                self._log.info("Scan completed at %s", self.status["last_finished_at"])
                self._emit(
                    "scan_completed",
                    {"finished_at": self.status["last_finished_at"]},
                )
