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


class FakeDB:
    """Minimal KinoDB stand-in for DAO unit tests."""

    def __init__(
        self,
        page_docs: list | None = None,
        get_many_result: dict | None = None,
        bookmarks: list[str | None] | None = None,
    ):
        self._page_docs = page_docs or []
        self._get_many_result = get_many_result or {}
        self._bookmarks = bookmarks if bookmarks is not None else [None]
        self._bm_index = 0
        self.find_page_calls: list[dict] = []
        self.get_many_calls: list[list] = []

    def find_page(self, selector, sort, limit, bookmark=None, fields=None):
        self.find_page_calls.append(
            {"selector": selector, "sort": sort, "limit": limit,
             "bookmark": bookmark, "fields": fields}
        )
        bm = self._bookmarks[min(self._bm_index, len(self._bookmarks) - 1)]
        self._bm_index += 1
        docs = self._page_docs if fields is None else [{"_id": d["_id"]} for d in self._page_docs]
        return docs, bm

    def get_many(self, ids: list[str]) -> dict:
        self.get_many_calls.append(list(ids))
        return {k: v for k, v in self._get_many_result.items() if k in ids}
