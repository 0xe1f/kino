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

from typing import Any

from db import KinoDB
from utils import now_iso


class PlaybackDAO:
    def __init__(self, database: KinoDB) -> None:
        self._db = database

    def get_progress(self, user_id: str, video_id: str) -> dict[str, Any] | None:
        return self._db.get(f"watch_progress:{user_id}:{video_id}")

    def upsert_progress(
        self,
        user_id: str,
        video_id: str,
        playlist_id: str | None,
        position: float,
    ) -> None:
        progress_id = f"watch_progress:{user_id}:{video_id}"
        doc = self._db.get(progress_id) or {"_id": progress_id}
        doc["type"] = "watch_progress"
        doc["user_id"] = user_id
        doc["video_id"] = video_id
        doc["playlist_id"] = playlist_id
        doc["last_position_seconds"] = position
        doc["last_watched_at"] = now_iso()
        self._db.save(doc)

    def upsert_history(
        self,
        user_id: str,
        video_id: str,
        playlist_id: str | None,
        position: float | None = None,
    ) -> None:
        history_id = f"playback_history:{user_id}:{video_id}"
        doc = self._db.get(history_id) or {"_id": history_id}
        doc["type"] = "playback_history"
        doc["user_id"] = user_id
        doc["video_id"] = video_id
        doc["playlist_id"] = playlist_id
        if position is not None:
            doc["position_seconds"] = float(position)
        doc["watched_at"] = now_iso()
        self._db.save(doc)

    def last_videos_in_playlists(
        self,
        user_id: str,
        playlist_ids: list[str],
    ) -> dict[str, str]:
        """Return {playlist_id: video_id} for the most recently watched video in each playlist.
        Only considers authenticated users. Uses a single query to avoid fetching in a loop."""
        if not playlist_ids:
            return {}
        docs = self._db.find_by_mango(
            {
                "type": "playback_history",
                "user_id": user_id,
                "playlist_id": {"$in": playlist_ids},
            }
        )
        best: dict[str, tuple[str, str]] = {}
        for doc in docs:
            pid = doc.get("playlist_id")
            vid = doc.get("video_id")
            watched_at = doc.get("watched_at", "")
            if pid and vid:
                if pid not in best or watched_at > best[pid][0]:
                    best[pid] = (watched_at, vid)
        return {pid: vid for pid, (_, vid) in best.items()}

    def list_history_page(
        self,
        user: dict[str, Any],
        playlist_dao: Any,
        offset: int,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int]:
        history_docs = self._db.find_many("playback_history", user_id=user["user_id"])
        history_docs.sort(key=lambda d: d.get("watched_at", ""), reverse=True)
        total = len(history_docs)
        page_docs = history_docs[offset : offset + limit]
        video_ids = [d["video_id"] for d in page_docs if d.get("video_id")]
        videos_map = self._db.get_many(video_ids)
        rows = []
        for doc in page_docs:
            video = videos_map.get(doc.get("video_id"))
            if not video:
                continue
            context_playlist_id = doc.get("playlist_id")
            rows.append(
                {
                    "history": doc,
                    "video": video,
                    "context_playlist_id": context_playlist_id,
                    "context_playlist_removable": playlist_dao.removable_for_user(
                        user, context_playlist_id
                    ),
                }
            )
        return rows, total

    def list_history_for_user(
        self,
        user: dict[str, Any],
        playlist_dao: Any,
    ) -> list[dict[str, Any]]:
        history_docs = self._db.find_many("playback_history", user_id=user["user_id"])
        video_ids = [d["video_id"] for d in history_docs if d.get("video_id")]
        videos_map = self._db.get_many(video_ids)
        rows = []
        for doc in history_docs:
            video = videos_map.get(doc.get("video_id"))
            if not video:
                continue
            context_playlist_id = doc.get("playlist_id")
            rows.append(
                {
                    "history": doc,
                    "video": video,
                    "context_playlist_id": context_playlist_id,
                    "context_playlist_removable": playlist_dao.removable_for_user(
                        user, context_playlist_id
                    ),
                }
            )
        rows.sort(key=lambda row: row["history"].get("watched_at", ""), reverse=True)
        return rows
