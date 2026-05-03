import uuid
from typing import Any

from db import KinoDB
from utils import BUILTIN_PLAYLISTS, now_iso


class PlaylistDAO:
    def __init__(self, database: KinoDB) -> None:
        self._db = database

    # --- Basic CRUD ---

    def get(self, playlist_id: str) -> dict[str, Any] | None:
        return self._db.get(playlist_id)

    def list_all(self) -> list[dict[str, Any]]:
        return self._db.find_many("playlist")

    def list_by_owner_type(self, owner_type: str) -> list[dict[str, Any]]:
        return self._db.find_many("playlist", owner_type=owner_type)

    def save(self, doc: dict[str, Any]) -> dict[str, Any]:
        return self._db.save(doc)

    def delete(self, playlist: dict[str, Any]) -> None:
        self._db.delete(playlist)

    # --- Business logic ---

    @staticmethod
    def is_builtin(playlist: dict[str, Any] | None) -> bool:
        return bool(playlist and playlist.get("builtin_kind") in BUILTIN_PLAYLISTS)

    def can_edit(self, user: dict[str, Any], playlist: dict[str, Any]) -> bool:
        if playlist.get("owner_type") == "system":
            return False
        if self.is_builtin(playlist):
            return False
        return playlist.get("owner_id") == user.get("user_id")

    def removable_for_user(
        self,
        user: dict[str, Any] | None,
        playlist_id: str | None,
    ) -> bool:
        if not user or not playlist_id:
            return False
        playlist = self._db.get(playlist_id)
        if not playlist or playlist.get("owner_type") != "user":
            return False
        if self.is_builtin(playlist):
            return False
        return playlist.get("owner_id") == user.get("user_id")

    def ensure_builtin(
        self,
        user: dict[str, Any],
        builtin_kind: str,
    ) -> dict[str, Any]:
        if builtin_kind not in BUILTIN_PLAYLISTS:
            raise ValueError("Unknown builtin playlist kind")
        playlist_id = f"playlist:builtin:{builtin_kind}:{user['user_id']}"
        existing = self._db.get(playlist_id)
        if existing:
            return existing
        doc: dict[str, Any] = {
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
        return self._db.save(doc)

    def list_custom_for_user(self, user: dict[str, Any]) -> list[dict[str, Any]]:
        all_playlists = self._db.find_many(
            "playlist", owner_type="user", owner_id=user["user_id"]
        )
        filtered = [
            p
            for p in all_playlists
            if not self.is_builtin(p) and not p.get("hidden_from_lists")
        ]
        return sorted(filtered, key=lambda p: p.get("name", "").lower())

    def builtin_states_for_user(
        self,
        user: dict[str, Any] | None,
    ) -> dict[str, set[str]]:
        states: dict[str, set[str]] = {"favorites": set(), "watch_later": set()}
        if not user:
            return states
        for kind in states:
            playlist = self.ensure_builtin(user, kind)
            for item in self._db.find_many("playlist_item", playlist_id=playlist["_id"]):
                if item.get("item_type") == "video":
                    states[kind].add(item.get("item_id"))
        return states

    def visible_for_user(self, user: dict[str, Any] | None) -> list[dict[str, Any]]:
        output = []
        for playlist in self.list_all():
            if playlist.get("hidden_from_lists"):
                continue
            owner_type = playlist.get("owner_type")
            if owner_type == "system":
                output.append(playlist)
                continue
            if (
                user
                and owner_type == "user"
                and playlist.get("owner_id") == user["user_id"]
            ):
                output.append(playlist)
        return output

    def top_level(self, user: dict[str, Any] | None) -> list[dict[str, Any]]:
        playlists = self.visible_for_user(user)
        top = [p for p in playlists if not p.get("parent_playlist_id")]
        return sorted(
            top,
            key=lambda p: (p.get("owner_type") != "system", p.get("name", "").lower()),
        )

    def owner_username(self, playlist: dict[str, Any] | None) -> str | None:
        if not playlist or playlist.get("owner_type") != "user":
            return None
        owner_id = playlist.get("owner_id")
        if not owner_id:
            return None
        user_doc = self._db.get(f"user:{owner_id}")
        if not user_doc:
            return None
        return user_doc.get("username")

    def items(self, playlist_id: str) -> list[dict[str, Any]]:
        raw_items = sorted(
            self._db.find_many("playlist_item", playlist_id=playlist_id),
            key=lambda item: item.get("position", 0),
        )
        collected = []
        for item in raw_items:
            target = self._db.get(item.get("item_id"))
            if not target:
                continue
            collected.append({"item": item, "target": target})
        playlists = [x for x in collected if x["item"].get("item_type") == "playlist"]
        videos = [x for x in collected if x["item"].get("item_type") == "video"]
        return playlists + videos

    def delete_tree(self, owner_user_id: str, playlist_id: str) -> None:
        for child in self._db.find_many("playlist", parent_playlist_id=playlist_id):
            if (
                child.get("owner_type") == "user"
                and child.get("owner_id") == owner_user_id
            ):
                self.delete_tree(owner_user_id, child["_id"])
        for item in self._db.find_many("playlist_item", playlist_id=playlist_id):
            self._db.delete(item)
        playlist = self._db.get(playlist_id)
        if playlist:
            self._db.delete(playlist)

    def create_user_playlist(
        self,
        user: dict[str, Any],
        name: str,
        parent_playlist_id: str | None = None,
        thumbnail_kind: str | None = None,
        thumbnail_path: str | None = None,
    ) -> dict[str, Any]:
        playlist_id = f"playlist:{uuid.uuid4()}"
        doc: dict[str, Any] = {
            "_id": playlist_id,
            "type": "playlist",
            "playlist_id": playlist_id,
            "name": name,
            "owner_type": "user",
            "owner_id": user["user_id"],
            "editable": True,
            "parent_playlist_id": parent_playlist_id,
            "thumbnail_kind": thumbnail_kind,
            "thumbnail_path": thumbnail_path,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
        return self._db.save(doc)


class PlaylistItemDAO:
    def __init__(self, database: KinoDB) -> None:
        self._db = database

    def list_for_playlist(self, playlist_id: str) -> list[dict[str, Any]]:
        return self._db.find_many("playlist_item", playlist_id=playlist_id)

    def find_video_in_playlist(
        self,
        playlist_id: str,
        video_id: str,
    ) -> dict[str, Any] | None:
        for item in self.list_for_playlist(playlist_id):
            if item.get("item_type") == "video" and item.get("item_id") == video_id:
                return item
        return None

    def add(
        self,
        playlist_id: str,
        item_type: str,
        item_id: str,
        owner_type: str = "user",
    ) -> dict[str, Any]:
        existing = self.list_for_playlist(playlist_id)
        position = max((i.get("position", 0) for i in existing), default=-1) + 1
        doc: dict[str, Any] = {
            "_id": f"playlist_item:{owner_type}:{uuid.uuid4()}",
            "type": "playlist_item",
            "owner_type": owner_type,
            "playlist_id": playlist_id,
            "item_type": item_type,
            "item_id": item_id,
            "position": position,
            "created_at": now_iso(),
        }
        return self._db.save(doc)

    def remove(self, item: dict[str, Any]) -> None:
        self._db.delete(item)

    def reorder(self, playlist_id: str, ordered_item_ids: list[str]) -> None:
        for index, item_id in enumerate(ordered_item_ids):
            item = self._db.get(item_id)
            if not item or item.get("playlist_id") != playlist_id:
                continue
            item["position"] = index
            self._db.save(item)
