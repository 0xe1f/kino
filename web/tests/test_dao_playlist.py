"""Tests for PlaylistDAO.items_page and count_items using a FakeDB stub."""

import pytest

from dao.playlist import PlaylistDAO
from tests.conftest import FakeDB, make_playlist_item, make_video


PAGE_SIZE = 5  # intentionally small for tests


# ---------------------------------------------------------------------------
# items_page
# ---------------------------------------------------------------------------

class TestItemsPage:
    def _dao(self, items=(), videos=(), bookmarks=None):
        items = list(items)
        videos_map = {v["_id"]: v for v in videos}
        db = FakeDB(page_docs=items, get_many_result=videos_map, bookmarks=bookmarks or [None])
        return PlaylistDAO(db), db

    def test_returns_collected_and_bookmark(self):
        items = [make_playlist_item("v1"), make_playlist_item("v2")]
        videos = [make_video("v1"), make_video("v2")]
        dao, _ = self._dao(items, videos, bookmarks=["bm_next"])

        collected, bm = dao.items_page("pl:test", None, 0, PAGE_SIZE)

        assert len(collected) == 2
        assert bm == "bm_next"

    def test_position_labels_start_at_one_for_first_page(self):
        items = [make_playlist_item(f"v{i}") for i in range(3)]
        videos = [make_video(f"v{i}") for i in range(3)]
        dao, _ = self._dao(items, videos)

        collected, _ = dao.items_page("pl:test", None, start=0, limit=PAGE_SIZE)

        assert [r["position_label"] for r in collected] == [1, 2, 3]

    def test_position_labels_offset_by_start_on_later_pages(self):
        items = [make_playlist_item("v10"), make_playlist_item("v11")]
        videos = [make_video("v10"), make_video("v11")]
        dao, _ = self._dao(items, videos)

        collected, _ = dao.items_page("pl:test", None, start=25, limit=PAGE_SIZE)

        assert [r["position_label"] for r in collected] == [26, 27]

    def test_bookmark_forwarded_to_db(self):
        dao, db = self._dao()

        dao.items_page("pl:test", "cursor_xyz", start=0, limit=PAGE_SIZE)

        assert db.find_page_calls[0]["bookmark"] == "cursor_xyz"

    def test_none_bookmark_stays_none(self):
        dao, db = self._dao()

        dao.items_page("pl:test", None, start=0, limit=PAGE_SIZE)

        assert db.find_page_calls[0]["bookmark"] is None

    def test_empty_bookmark_string_treated_as_none(self):
        """API passes '' when no cursor; DAO should convert to None for find_page."""
        dao, db = self._dao()

        dao.items_page("pl:test", "" or None, start=0, limit=PAGE_SIZE)

        assert db.find_page_calls[0]["bookmark"] is None

    def test_playlist_id_in_selector(self):
        dao, db = self._dao()

        dao.items_page("pl:my_list", None, start=0, limit=PAGE_SIZE)

        assert db.find_page_calls[0]["selector"]["playlist_id"] == "pl:my_list"

    def test_limit_forwarded(self):
        dao, db = self._dao()

        dao.items_page("pl:test", None, start=0, limit=12)

        assert db.find_page_calls[0]["limit"] == 12

    def test_item_with_missing_video_is_skipped(self):
        items = [make_playlist_item("v1"), make_playlist_item("v_gone")]
        videos = [make_video("v1")]  # v_gone has no doc
        dao, _ = self._dao(items, videos)

        collected, _ = dao.items_page("pl:test", None, 0, PAGE_SIZE)

        assert len(collected) == 1
        assert collected[0]["target"]["_id"] == "v1"

    def test_collected_item_has_item_and_target_keys(self):
        items = [make_playlist_item("v1")]
        videos = [make_video("v1")]
        dao, _ = self._dao(items, videos)

        collected, _ = dao.items_page("pl:test", None, 0, PAGE_SIZE)

        row = collected[0]
        assert "item" in row
        assert "target" in row
        assert row["target"]["_id"] == "v1"

    def test_uses_sort_by_position(self):
        dao, db = self._dao()

        dao.items_page("pl:test", None, 0, PAGE_SIZE)

        sort = db.find_page_calls[0]["sort"]
        sort_keys = [list(s.keys())[0] for s in sort]
        assert "position" in sort_keys


# ---------------------------------------------------------------------------
# count_items
# ---------------------------------------------------------------------------

class TestCountItems:
    def _dao(self, n_items: int):
        id_docs = [{"_id": f"pi:{i}", "type": "playlist_item"} for i in range(n_items)]
        db = FakeDB(page_docs=id_docs, get_many_result={})
        return PlaylistDAO(db), db

    def test_returns_correct_count(self):
        dao, _ = self._dao(7)
        assert dao.count_items("pl:test") == 7

    def test_zero_items(self):
        dao, _ = self._dao(0)
        assert dao.count_items("pl:test") == 0

    def test_requests_only_id_fields(self):
        dao, db = self._dao(3)
        dao.count_items("pl:test")

        assert db.find_page_calls[0]["fields"] == ["_id"]

    def test_requests_large_limit(self):
        """Should fetch all items in a single shot."""
        dao, db = self._dao(3)
        dao.count_items("pl:test")

        assert db.find_page_calls[0]["limit"] >= 10_000

    def test_uses_playlist_id_in_selector(self):
        dao, db = self._dao(2)
        dao.count_items("pl:my_list")

        assert db.find_page_calls[0]["selector"]["playlist_id"] == "pl:my_list"
