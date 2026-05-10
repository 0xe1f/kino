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

import os
import threading
from typing import Any

import couchdb
import requests as _http


class KinoDB:
    def __init__(self, url: str, db_name: str):
        self.url = url
        self.db_name = db_name
        self._db = None
        self._lock = threading.Lock()

    def _connect(self):
        server = couchdb.Server(self.url)
        if self.db_name in server:
            return server[self.db_name]
        return server.create(self.db_name)

    @property
    def db(self):
        with self._lock:
            if self._db is None:
                self._db = self._connect()
        return self._db

    def _reset(self) -> None:
        with self._lock:
            self._db = None

    def get(self, doc_id: str) -> dict[str, Any] | None:
        try:
            return self.db.get(doc_id)
        except Exception:
            self._reset()
            return self.db.get(doc_id)

    def save(self, doc: dict[str, Any]) -> dict[str, Any]:
        _, rev = self.db.save(doc)
        doc["_rev"] = rev
        return doc

    def delete(self, doc: dict[str, Any]) -> None:
        self.db.delete(doc)

    # --- Mango query helpers ---

    def get_many(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch multiple documents by ID in a single request. Returns {id: doc}."""
        if not ids:
            return {}
        url = f"{self.url.rstrip('/')}/{self.db_name}/_all_docs?include_docs=true"
        resp = _http.post(url, json={"keys": ids}, timeout=30)
        resp.raise_for_status()
        result: dict[str, dict[str, Any]] = {}
        for row in resp.json().get("rows", []):
            doc = row.get("doc")
            if doc and not row.get("error"):
                result[row["id"]] = doc
        return result

    def bulk_save(self, docs: list[dict[str, Any]]) -> None:
        """Save multiple documents in a single request."""
        if not docs:
            return
        url = f"{self.url.rstrip('/')}/{self.db_name}/_bulk_docs"
        resp = _http.post(url, json={"docs": docs}, timeout=30)
        resp.raise_for_status()
        for result, doc in zip(resp.json(), docs):
            if result.get("rev"):
                doc["_rev"] = result["rev"]

    def find_by_mango(
        self,
        selector: dict[str, Any],
        limit: int = 25000,
    ) -> list[dict[str, Any]]:
        url = f"{self.url.rstrip('/')}/{self.db_name}/_find"
        resp = _http.post(url, json={"selector": selector, "limit": limit}, timeout=30)
        resp.raise_for_status()
        return resp.json().get("docs", [])

    def find_many(self, doc_type: str, **filters: Any) -> list[dict[str, Any]]:
        selector: dict[str, Any] = {"type": doc_type, **filters}
        return self.find_by_mango(selector)

    def find_page(
        self,
        selector: dict[str, Any],
        sort: list[dict[str, str]],
        limit: int,
        bookmark: str | None = None,
        fields: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Bookmark-based paginated query. Returns (docs, next_bookmark)."""
        body: dict[str, Any] = {"selector": selector, "sort": sort, "limit": limit}
        if bookmark:
            body["bookmark"] = bookmark
        if fields:
            body["fields"] = fields
        url = f"{self.url.rstrip('/')}/{self.db_name}/_find"
        resp = _http.post(url, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("docs", []), data.get("bookmark")

    def query_view(
        self,
        ddoc: str,
        view: str,
        keys: list | None = None,
        group: bool = False,
        reduce: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Multi-key or grouped reduce query. Returns the rows list."""
        url = f"{self.url.rstrip('/')}/{self.db_name}/_design/{ddoc}/_view/{view}"
        body: dict[str, Any] = {}
        if keys is not None:
            body["keys"] = keys
        if group:
            body["group"] = True
        if reduce is not None:
            body["reduce"] = reduce
        resp = _http.post(url, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json().get("rows", [])

    def query_view_range(
        self,
        ddoc: str,
        view: str,
        startkey: Any = None,
        endkey: Any = None,
        descending: bool = False,
        limit: int | None = None,
        startkey_docid: str | None = None,
        skip: int = 0,
        reduce: bool | None = None,
        group_level: int | None = None,
    ) -> list[dict[str, Any]]:
        """Range query against a view. Supports cursor pagination via startkey_docid."""
        url = f"{self.url.rstrip('/')}/{self.db_name}/_design/{ddoc}/_view/{view}"
        body: dict[str, Any] = {}
        if startkey is not None:
            body["startkey"] = startkey
        if endkey is not None:
            body["endkey"] = endkey
        if descending:
            body["descending"] = True
        if limit is not None:
            body["limit"] = limit
        if startkey_docid:
            body["startkey_docid"] = startkey_docid
        if skip:
            body["skip"] = skip
        if reduce is not None:
            body["reduce"] = reduce
        if group_level is not None:
            body["group_level"] = group_level
            body["group"] = True
        resp = _http.post(url, json=body, timeout=30)
        resp.raise_for_status()
        return resp.json().get("rows", [])

    def ensure_design_docs(self) -> None:
        """Create or update the _design/kino design document with all views."""
        ddoc_id = "_design/kino"
        url = f"{self.url.rstrip('/')}/{self.db_name}/{ddoc_id}"

        item_counts_map = (
            "function(doc) {"
            " if (doc.type === 'playlist_item' && doc.playlist_id && doc.item_type) {"
            "  emit([doc.playlist_id, doc.item_type], null);"
            " }"
            "}"
        )

        first_video_map = (
            "function(doc) {"
            " if (doc.type === 'playlist_item' && doc.item_type === 'video'"
            "     && doc.playlist_id && doc.item_id) {"
            "  emit(doc.playlist_id, {pos: doc.position || 0, id: doc.item_id});"
            " }"
            "}"
        )
        first_video_reduce = (
            "function(keys, values, rereduce) {"
            " var best = values[0];"
            " for (var i = 1; i < values.length; i++) {"
            "  if (values[i].pos < best.pos) { best = values[i]; }"
            " }"
            " return best;"
            "}"
        )

        last_watched_map = (
            "function(doc) {"
            " if (doc.type === 'playback_history' && doc.user_id && doc.playlist_id"
            "     && doc.video_id && doc.watched_at) {"
            "  emit([doc.user_id, doc.playlist_id],"
            "       {watched_at: doc.watched_at, video_id: doc.video_id});"
            " }"
            "}"
        )
        last_watched_reduce = (
            "function(keys, values, rereduce) {"
            " var best = values[0];"
            " for (var i = 1; i < values.length; i++) {"
            "  if (values[i].watched_at > best.watched_at) { best = values[i]; }"
            " }"
            " return best;"
            "}"
        )

        history_by_user_date_map = (
            "function(doc) {"
            " if (doc.type === 'playback_history' && doc.user_id && doc.video_id && doc.watched_at) {"
            "  emit([doc.user_id, doc.watched_at],"
            "       {video_id: doc.video_id, playlist_id: doc.playlist_id || null,"
            "        watched_at: doc.watched_at});"
            " }"
            "}"
        )

        item_count_by_playlist_map = (
            "function(doc) {"
            " if (doc.type === 'playlist_item' && doc.playlist_id) {"
            "  emit(doc.playlist_id, null);"
            " }"
            "}"
        )

        playlist_count_by_owner_map = (
            "function(doc) {"
            " if (doc.type === 'playlist' && doc.owner_type && doc.owner_id"
            "     && !doc.builtin_kind && !doc.hidden_from_lists) {"
            "  emit([doc.owner_type, doc.owner_id], null);"
            " }"
            "}"
        )

        playlist_items_by_playlist_type_map = (
            "function(doc) {"
            " if (doc.type === 'playlist_item' && doc.playlist_id && doc.item_type && doc.item_id) {"
            "  emit([doc.playlist_id, doc.item_type, doc.position || 0], doc.item_id);"
            " }"
            "}"
        )

        playlist_names_by_owner_map = (
            "function(doc) {"
            " if (doc.type === 'playlist' && doc.owner_type === 'user' && doc.owner_id"
            "     && doc.name && !doc.builtin_kind && !doc.hidden_from_lists) {"
            "  emit([doc.owner_id, doc.name.trim().toLowerCase()], null);"
            " }"
            "}"
        )

        ddoc: dict[str, Any] = {
            "_id": ddoc_id,
            "views": {
                "item_counts_by_playlist": {
                    "map": item_counts_map,
                    "reduce": "_count",
                },
                "first_video_by_playlist": {
                    "map": first_video_map,
                    "reduce": first_video_reduce,
                },
                "last_watched_by_user_playlist": {
                    "map": last_watched_map,
                    "reduce": last_watched_reduce,
                },
                "history_by_user_date": {
                    "map": history_by_user_date_map,
                    "reduce": "_count",
                },
                "item_count_by_playlist": {
                    "map": item_count_by_playlist_map,
                    "reduce": "_count",
                },
                "playlist_count_by_owner": {
                    "map": playlist_count_by_owner_map,
                    "reduce": "_count",
                },
                "playlist_items_by_playlist_type": {
                    "map": playlist_items_by_playlist_type_map,
                },
                "playlist_names_by_owner": {
                    "map": playlist_names_by_owner_map,
                },
            },
        }

        existing = self.get(ddoc_id)
        if existing:
            ddoc["_rev"] = existing["_rev"]
        resp = _http.put(url, json=ddoc, timeout=10)
        resp.raise_for_status()

    def find_one(self, doc_type: str, **filters: Any) -> dict[str, Any] | None:
        selector: dict[str, Any] = {"type": doc_type, **filters}
        docs = self.find_by_mango(selector, limit=1)
        return docs[0] if docs else None

    def ensure_indexes(self) -> None:
        url = f"{self.url.rstrip('/')}/{self.db_name}/_index"
        index_groups = [
            ["type"],
            ["type", "user_id"],
            ["type", "owner_id"],
            ["type", "owner_type"],
            ["type", "owner_type", "owner_id"],
            ["type", "playlist_id"],
            ["type", "playlist_id", "position"],
            ["type", "email"],
            ["type", "video_id"],
            ["type", "source"],
            ["type", "builtin_kind"],
            ["type", "parent_playlist_id"],
            ["type", "username"],
            ["type", "user_id", "playlist_id"],
            ["type", "owner_type", "owner_id", "name"],
        ]
        for fields in index_groups:
            try:
                _http.post(
                    url,
                    json={"index": {"fields": fields}},
                    timeout=10,
                )
            except Exception:
                pass


db = KinoDB(
    url=os.getenv("COUCHDB_URL", "http://admin:admin@localhost:5984/"),
    db_name=os.getenv("COUCHDB_DB_NAME", "kino"),
)
