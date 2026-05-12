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
from utils import normalize_email, normalize_username, now_iso


class UserDAO:
    def __init__(self, database: KinoDB) -> None:
        self._db = database

    def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self._db.get(f"user:{user_id}")

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        rows = self._db.query_view("kino", "users_by_email", keys=[normalize_email(email)], include_docs=True)
        return rows[0]["doc"] if rows else None

    def list_all(self) -> list[dict[str, Any]]:
        rows = self._db.query_view_range(
            "kino", "docs_by_type",
            startkey=["user", None], endkey=["user", {}],
            include_docs=True,
        )
        return [row["doc"] for row in rows if row.get("doc")]

    def create(
        self,
        user_id: str,
        username: str,
        email: str,
        password_hash: str,
        preferred_theme: str = "night",
    ) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "_id": f"user:{user_id}",
            "type": "user",
            "user_id": user_id,
            "username": normalize_username(username),
            "email": normalize_email(email),
            "password_hash": password_hash,
            "preferred_theme": preferred_theme,
            "created_at": now_iso(),
        }
        return self._db.save(doc)

    def update(self, user: dict[str, Any], **fields: Any) -> dict[str, Any]:
        for k, v in fields.items():
            user[k] = v
        user["updated_at"] = now_iso()
        return self._db.save(user)

    def delete(self, user: dict[str, Any]) -> None:
        self._db.delete(user)

    def email_taken(self, email: str, exclude_user_id: str | None = None) -> bool:
        user = self.get_by_email(email)
        if not user:
            return False
        if exclude_user_id and user.get("user_id") == exclude_user_id:
            return False
        return True

    def username_taken(self, username: str, exclude_user_id: str | None = None) -> bool:
        normalized = normalize_username(username).lower()
        rows = self._db.query_view("kino", "users_by_username", keys=[normalized], include_docs=True)
        if not rows:
            return False
        user = rows[0]["doc"]
        return user.get("user_id") != exclude_user_id

    def hard_delete(self, user: dict[str, Any]) -> None:
        user_id = user["user_id"]

        playlist_rows = self._db.query_view_range(
            "kino", "playlists_by_owner",
            startkey=["user", user_id], endkey=["user", user_id],
            include_docs=True,
        )
        for row in playlist_rows:
            playlist = row.get("doc")
            if playlist:
                self._delete_playlist_tree(user_id, playlist["_id"])

        for doc_type in ("playback_history", "watch_progress", "reaction"):
            rows = self._db.query_view_range(
                "kino", "docs_by_type",
                startkey=[doc_type, user_id], endkey=[doc_type, user_id],
                include_docs=True,
            )
            docs_to_delete = [
                {"_id": r["doc"]["_id"], "_rev": r["doc"]["_rev"], "_deleted": True}
                for r in rows if r.get("doc")
            ]
            self._db.bulk_save(docs_to_delete)

        user_doc = self._db.get(f"user:{user_id}")
        if user_doc:
            self._db.delete(user_doc)

    def _delete_playlist_tree(self, owner_user_id: str, playlist_id: str) -> None:
        child_rows = self._db.query_view(
            "kino", "playlists_by_parent",
            keys=[playlist_id],
            include_docs=True,
        )
        for row in child_rows:
            child = row.get("doc")
            if child and child.get("owner_type") == "user" and child.get("owner_id") == owner_user_id:
                self._delete_playlist_tree(owner_user_id, child["_id"])

        item_rows = self._db.query_view_range(
            "kino", "playlist_items_by_playlist_type",
            startkey=[playlist_id, None, None],
            endkey=[playlist_id, {}, {}],
            include_docs=True,
        )
        items_to_delete = [
            {"_id": r["doc"]["_id"], "_rev": r["doc"]["_rev"], "_deleted": True}
            for r in item_rows if r.get("doc")
        ]
        self._db.bulk_save(items_to_delete)

        playlist = self._db.get(playlist_id)
        if playlist:
            self._db.delete(playlist)
