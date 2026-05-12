import os

# Must be set before app is imported so _startup() is skipped during tests.
os.environ["FLASK_TESTING"] = "1"

import pytest
from app import app as flask_app


@pytest.fixture
def client():
    flask_app.config["TESTING"] = True
    with flask_app.test_client() as c:
        yield c


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_playlist_item(item_id: str, position: int = 0, item_type: str = "video") -> dict:
    return {
        "_id": f"pi:{item_id}",
        "type": "playlist_item",
        "playlist_id": "pl:test",
        "item_id": item_id,
        "item_type": item_type,
        "position": position,
    }


def make_video(video_id: str) -> dict:
    return {
        "_id": video_id,
        "type": "video",
        "title": f"Title {video_id}",
        "duration": 90,
    }


def _make_view_row(item: dict, value: str | None = None) -> dict:
    """Wrap a playlist_item doc in a view row as playlist_items_by_playlist_type returns."""
    return {
        "key": [item["playlist_id"], item.get("item_type", "video"), item.get("position", 0)],
        "id": item["_id"],
        "value": value or item.get("item_id"),
        "doc": item,
    }


class FakeDB:
    """Minimal KinoDB stand-in for DAO unit tests."""

    def __init__(
        self,
        view_range_rows: list | None = None,
        view_range_rows_seq: list[list] | None = None,
        get_many_result: dict | None = None,
        cursors: list[str | None] | None = None,
        query_view_rows: list | None = None,
    ):
        # view_range_rows_seq: sequence of row lists returned on successive calls
        self._view_range_rows_seq: list[list] = view_range_rows_seq or (
            [view_range_rows] if view_range_rows is not None else [[]]
        )
        self._view_range_call_idx = 0
        self._get_many_result = get_many_result or {}
        self._cursors = cursors if cursors is not None else [None]
        self._cursor_idx = 0
        self._query_view_rows = query_view_rows or []
        self.query_view_range_calls: list[dict] = []
        self.get_many_calls: list[list] = []
        self.query_view_calls: list[dict] = []

    def query_view_range(self, ddoc, view, startkey=None, endkey=None,
                         descending=False, limit=None, startkey_docid=None,
                         skip=0, reduce=None, group_level=None, include_docs=False):
        self.query_view_range_calls.append({
            "ddoc": ddoc, "view": view, "startkey": startkey, "endkey": endkey,
            "limit": limit, "startkey_docid": startkey_docid, "skip": skip,
            "reduce": reduce, "group_level": group_level, "include_docs": include_docs,
        })
        idx = min(self._view_range_call_idx, len(self._view_range_rows_seq) - 1)
        self._view_range_call_idx += 1
        return self._view_range_rows_seq[idx]

    def get_many(self, ids: list[str]) -> dict:
        self.get_many_calls.append(list(ids))
        return {k: v for k, v in self._get_many_result.items() if k in ids}

    def query_view(self, ddoc, view, keys=None, group=False, reduce=None, include_docs=False):
        self.query_view_calls.append({"ddoc": ddoc, "view": view, "keys": keys,
                                      "include_docs": include_docs})
        return self._query_view_rows
