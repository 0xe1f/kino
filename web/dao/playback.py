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
