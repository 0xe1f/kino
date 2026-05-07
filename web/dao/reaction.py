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
