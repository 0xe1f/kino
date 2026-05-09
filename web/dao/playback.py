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

import json
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
        """Return {playlist_id: video_id} for the most recently watched video in each playlist."""
        if not playlist_ids:
            return {}
        keys = [[user_id, pid] for pid in playlist_ids]
        rows = self._db.query_view("kino", "last_watched_by_user_playlist", keys=keys, reduce=False)
        result: dict[str, str] = {}
        best_ts: dict[str, str] = {}
        for row in rows:
            pid = row["key"][1]
            val = row["value"]
            ts = val["watched_at"]
            if pid not in best_ts or ts > best_ts[pid]:
                best_ts[pid] = ts
                result[pid] = val["video_id"]
        return result

    def count_history_for_user(self, user_id: str) -> int:
        """Return total number of history entries for a user."""
        rows = self._db.query_view_range(
            "kino", "history_by_user_date",
            startkey=[user_id, None],
            endkey=[user_id, {}],
            reduce=True,
            group_level=1,
        )
        return rows[0]["value"] if rows else 0

    def list_history_page(
        self,
        user: dict[str, Any],
        playlist_dao: Any,
        bookmark: str | None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return (rows, next_bookmark) for one page of history, newest first."""
        user_id = user["user_id"]
        cursor: dict[str, Any] = json.loads(bookmark) if bookmark else {}
        startkey = cursor.get("startkey", [user_id, {}])
        startkey_docid: str | None = cursor.get("startkey_docid")

        view_rows = self._db.query_view_range(
            "kino", "history_by_user_date",
            startkey=startkey,
            endkey=[user_id, None],
            descending=True,
            limit=limit,
            startkey_docid=startkey_docid,
            skip=1 if startkey_docid else 0,
            reduce=False,
        )

        video_ids = [r["value"]["video_id"] for r in view_rows if r.get("value", {}).get("video_id")]
        videos_map = self._db.get_many(video_ids)
        context_playlist_ids = [r["value"].get("playlist_id") for r in view_rows if r.get("value")]
        removable_map = playlist_dao.removable_batch_for_user(user, context_playlist_ids)
        rows = []
        for r in view_rows:
            val = r.get("value") or {}
            video = videos_map.get(val.get("video_id"))
            if not video:
                continue
            context_playlist_id = val.get("playlist_id")
            rows.append(
                {
                    "history": {
                        "playlist_id": context_playlist_id,
                        "watched_at": val.get("watched_at", ""),
                    },
                    "video": video,
                    "context_playlist_id": context_playlist_id,
                    "context_playlist_removable": removable_map.get(context_playlist_id, False),
                }
            )

        next_bookmark: str | None = None
        if len(view_rows) == limit:
            last = view_rows[-1]
            next_bookmark = json.dumps({"startkey": last["key"], "startkey_docid": last["id"]})

        return rows, next_bookmark

    def list_history_for_user(
        self,
        user: dict[str, Any],
        playlist_dao: Any,
    ) -> list[dict[str, Any]]:
        """Return all history for a user, newest first."""
        user_id = user["user_id"]
        view_rows = self._db.query_view_range(
            "kino", "history_by_user_date",
            startkey=[user_id, {}],
            endkey=[user_id, None],
            descending=True,
            reduce=False,
        )
        video_ids = [r["value"]["video_id"] for r in view_rows if r.get("value", {}).get("video_id")]
        videos_map = self._db.get_many(video_ids)
        context_playlist_ids = [r["value"].get("playlist_id") for r in view_rows if r.get("value")]
        removable_map = playlist_dao.removable_batch_for_user(user, context_playlist_ids)
        rows = []
        for r in view_rows:
            val = r.get("value") or {}
            video = videos_map.get(val.get("video_id"))
            if not video:
                continue
            context_playlist_id = val.get("playlist_id")
            rows.append(
                {
                    "history": {
                        "playlist_id": context_playlist_id,
                        "watched_at": val.get("watched_at", ""),
                    },
                    "video": video,
                    "context_playlist_id": context_playlist_id,
                    "context_playlist_removable": removable_map.get(context_playlist_id, False),
                }
            )
        return rows
