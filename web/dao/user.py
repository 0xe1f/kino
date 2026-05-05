from typing import Any

from db import KinoDB
from utils import normalize_email, normalize_username, now_iso


class UserDAO:
    def __init__(self, database: KinoDB) -> None:
        self._db = database

    def get_by_id(self, user_id: str) -> dict[str, Any] | None:
        return self._db.get(f"user:{user_id}")

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        return self._db.find_one("user", email=normalize_email(email))

    def list_all(self) -> list[dict[str, Any]]:
        return self._db.find_many("user")

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
        normalized = normalize_email(email)
        for user in self._db.find_many("user", email=normalized):
            if exclude_user_id and user.get("user_id") == exclude_user_id:
                continue
            return True
        return False

    def username_taken(self, username: str, exclude_user_id: str | None = None) -> bool:
        normalized = normalize_username(username).lower()
        result = self._db.find_one("user", username=normalized)
        if not result:
            return False
        return result.get("user_id") != exclude_user_id

    def hard_delete(self, user: dict[str, Any]) -> None:
        user_id = user["user_id"]

        for playlist in self._db.find_many("playlist", owner_type="user", owner_id=user_id):
            self._delete_playlist_tree(user_id, playlist["_id"])

        for doc in self._db.find_many("playback_history", user_id=user_id):
            self._db.delete(doc)

        for doc in self._db.find_many("watch_progress", user_id=user_id):
            self._db.delete(doc)

        for doc in self._db.find_many("reaction", user_id=user_id):
            self._db.delete(doc)

        user_doc = self._db.get(f"user:{user_id}")
        if user_doc:
            self._db.delete(user_doc)

    def _delete_playlist_tree(self, owner_user_id: str, playlist_id: str) -> None:
        for child in self._db.find_many("playlist", parent_playlist_id=playlist_id):
            if child.get("owner_type") == "user" and child.get("owner_id") == owner_user_id:
                self._delete_playlist_tree(owner_user_id, child["_id"])
        for item in self._db.find_many("playlist_item", playlist_id=playlist_id):
            self._db.delete(item)
        playlist = self._db.get(playlist_id)
        if playlist:
            self._db.delete(playlist)
