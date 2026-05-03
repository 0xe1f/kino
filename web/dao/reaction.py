from typing import Any

from db import KinoDB
from utils import now_iso


class ReactionDAO:
    def __init__(self, database: KinoDB) -> None:
        self._db = database

    def get(self, user_id: str, video_id: str) -> dict[str, Any] | None:
        return self._db.get(f"reaction:{user_id}:{video_id}")

    def toggle(
        self,
        user: dict[str, Any],
        video: dict[str, Any],
    ) -> tuple[bool, int]:
        """Toggle like for the user on the video.

        Returns (liked, new_like_count).
        """
        video.pop("dislikes", None)

        reaction_id = f"reaction:{user['user_id']}:{video['_id']}"
        current = self._db.get(reaction_id)
        likes = int(video.get("likes", 0))

        if current and current.get("value") == "like":
            likes = max(0, likes - 1)
            self._db.delete(current)
            liked = False
        else:
            likes += 1
            record = current or {"_id": reaction_id}
            record["type"] = "reaction"
            record["user_id"] = user["user_id"]
            record["video_id"] = video["_id"]
            record["value"] = "like"
            record["timestamp"] = now_iso()
            self._db.save(record)
            liked = True

        video["likes"] = likes
        video["updated_at"] = now_iso()
        self._db.save(video)

        return liked, likes
