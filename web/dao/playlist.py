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

    def removable_doc_for_user(
        self,
        user: dict[str, Any] | None,
        playlist: dict[str, Any] | None,
    ) -> bool:
        if not user or not playlist:
            return False
        if playlist.get("owner_type") != "user":
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
        return self.removable_doc_for_user(user, playlist)

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

    def top_level(self, user: dict[str, Any] | None) -> list[dict[str, Any]]:
        system = [
            p for p in self._db.find_many("playlist", owner_type="system")
            if not p.get("parent_playlist_id") and not p.get("hidden_from_lists")
        ]
        user_top: list[dict[str, Any]] = []
        if user:
            user_top = [
                p for p in self._db.find_many("playlist", owner_type="user", owner_id=user["user_id"])
                if not p.get("parent_playlist_id") and not p.get("hidden_from_lists")
            ]
        return sorted(
            system + user_top,
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

    def count_items(self, playlist_id: str) -> int:
        """Lightweight total count for use in page headers. Called once on initial render only."""
        selector = {"type": "playlist_item", "playlist_id": playlist_id}
        sort = [{"type": "asc"}, {"playlist_id": "asc"}, {"position": "asc"}]
        docs, _ = self._db.find_page(selector, sort, limit=25000, fields=["_id"])
        return len(docs)

    def items_page(
        self, playlist_id: str, bookmark: str | None, start: int, limit: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Returns (collected, next_bookmark). Caller infers has_more from len(collected) == limit."""
        selector = {"type": "playlist_item", "playlist_id": playlist_id}
        sort = [{"type": "asc"}, {"playlist_id": "asc"}, {"position": "asc"}]
        page_items, next_bookmark = self._db.find_page(
            selector, sort, limit=limit, bookmark=bookmark or None
        )
        target_ids = [item["item_id"] for item in page_items if item.get("item_id")]
        targets = self._db.get_many(target_ids)
        collected = []
        for i, item in enumerate(page_items):
            target = targets.get(item.get("item_id"))
            if target:
                collected.append({"item": item, "target": target, "position_label": start + i + 1})
        return collected, next_bookmark

    def nav_metadata(
        self, playlist_id: str, current_video_id: str
    ) -> dict[str, Any]:
        """Return lightweight nav info (count, prev/next IDs) with no video doc fetches."""
        raw_items = sorted(
            self._db.find_many("playlist_item", playlist_id=playlist_id),
            key=lambda item: item.get("position", 0),
        )
        video_items = [it for it in raw_items if it.get("item_type") == "video"]
        ids = [it["item_id"] for it in video_items]
        total = len(ids)

        try:
            current_idx = ids.index(current_video_id)
        except ValueError:
            current_idx = 0

        return {
            "count": total,
            "current_index": current_idx,
            "previous_video_id": ids[current_idx - 1] if current_idx > 0 else None,
            "next_video_id": ids[current_idx + 1] if current_idx < total - 1 else None,
        }

    def video_items_all(self, playlist_id: str) -> list[dict[str, Any]]:
        """Return all video items in a playlist, hydrated, in position order."""
        raw_items = sorted(
            self._db.find_many("playlist_item", playlist_id=playlist_id),
            key=lambda item: item.get("position", 0),
        )
        video_items = [it for it in raw_items if it.get("item_type") == "video"]
        target_ids = [it["item_id"] for it in video_items if it.get("item_id")]
        targets = self._db.get_many(target_ids)
        collected = []
        for i, item in enumerate(video_items):
            target = targets.get(item.get("item_id"))
            if target:
                collected.append({"item": item, "target": target, "position_label": i + 1})
        return collected

    def list_custom_for_user_page(
        self, user: dict[str, Any], offset: int, limit: int
    ) -> tuple[list[dict[str, Any]], int]:
        all_playlists = self.list_custom_for_user(user)
        total = len(all_playlists)
        return all_playlists[offset : offset + limit], total

    def items(self, playlist_id: str) -> list[dict[str, Any]]:
        raw_items = sorted(
            self._db.find_many("playlist_item", playlist_id=playlist_id),
            key=lambda item: item.get("position", 0),
        )
        target_ids = [item["item_id"] for item in raw_items if item.get("item_id")]
        targets = self._db.get_many(target_ids)
        collected = []
        for item in raw_items:
            target = targets.get(item.get("item_id"))
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
        items_map = self._db.get_many(ordered_item_ids)
        to_save = []
        for index, item_id in enumerate(ordered_item_ids):
            item = items_map.get(item_id)
            if not item or item.get("playlist_id") != playlist_id:
                continue
            item["position"] = index
            to_save.append(item)
        self._db.bulk_save(to_save)
