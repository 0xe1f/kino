from typing import Any

from db import KinoDB
from utils import now_iso


class VideoDAO:
    def __init__(self, database: KinoDB) -> None:
        self._db = database

    def get(self, video_id: str) -> dict[str, Any] | None:
        return self._db.get(video_id)

    def list_all(self) -> list[dict[str, Any]]:
        return self._db.find_many("video")

    def exists_any(self) -> bool:
        return self._db.find_one("video") is not None

    def list_by_source(self, source: str) -> list[dict[str, Any]]:
        return self._db.find_many("video", source=source)

    def save(self, doc: dict[str, Any]) -> dict[str, Any]:
        return self._db.save(doc)

    def delete(self, doc: dict[str, Any]) -> None:
        self._db.delete(doc)

    def increment_views(self, video: dict[str, Any]) -> dict[str, Any]:
        video["views"] = int(video.get("views", 0)) + 1
        video["updated_at"] = now_iso()
        return self._db.save(video)
