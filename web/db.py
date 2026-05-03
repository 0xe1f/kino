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
            ["type", "playlist_id"],
            ["type", "email"],
            ["type", "video_id"],
            ["type", "source"],
            ["type", "builtin_kind"],
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
