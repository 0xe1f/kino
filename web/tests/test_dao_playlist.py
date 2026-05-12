"""Tests for PlaylistDAO.items_page and count_items using a FakeDB stub."""

import json

import pytest

from dao.playlist import PlaylistDAO
from tests.conftest import FakeDB, _make_view_row, make_playlist_item, make_video


PAGE_SIZE = 5  # intentionally small for tests


# ---------------------------------------------------------------------------
# items_page
# ---------------------------------------------------------------------------

class TestItemsPage:
    def _dao(self, items=(), videos=(), cursors=None):
        items = list(items)
        videos_map = {v["_id"]: v for v in videos}
        # Build view rows: each row carries the playlist_item doc and item_id as value
        rows = [_make_view_row(item) for item in items]
        db = FakeDB(view_range_rows=rows, get_many_result=videos_map, cursors=cursors or [None])
        return PlaylistDAO(db), db

    def test_returns_collected_and_bookmark(self):
        items = [make_playlist_item("v1"), make_playlist_item("v2")]
        videos = [make_video("v1"), make_video("v2")]
        dao, _ = self._dao(items, videos)

        collected, bm = dao.items_page("pl:test", None, 0, PAGE_SIZE)

        assert len(collected) == 2
        assert bm is not None

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

    def test_cursor_forwarded_to_view_range(self):
        dao, db = self._dao()
        last_key = ["pl:test", "video", 3]
        last_docid = "pi:v3"
        bookmark = json.dumps([last_key, last_docid])

        dao.items_page("pl:test", bookmark, start=0, limit=PAGE_SIZE)

        call = db.query_view_range_calls[0]
        assert call["startkey"] == last_key
        assert call["startkey_docid"] == last_docid
        assert call["skip"] == 1

    def test_none_bookmark_uses_default_startkey(self):
        dao, db = self._dao()

        dao.items_page("pl:test", None, start=0, limit=PAGE_SIZE)

        call = db.query_view_range_calls[0]
        assert call["startkey"] == ["pl:test", None, None]
        assert call["startkey_docid"] is None
        assert call["skip"] == 0

    def test_empty_bookmark_string_treated_as_no_cursor(self):
        """Empty string bookmark from API should produce default startkey."""
        dao, db = self._dao()

        dao.items_page("pl:test", "" or None, start=0, limit=PAGE_SIZE)

        call = db.query_view_range_calls[0]
        assert call["startkey"] == ["pl:test", None, None]

    def test_playlist_id_in_endkey(self):
        dao, db = self._dao()

        dao.items_page("pl:my_list", None, start=0, limit=PAGE_SIZE)

        call = db.query_view_range_calls[0]
        assert call["endkey"][0] == "pl:my_list"

    def test_limit_forwarded(self):
        dao, db = self._dao()

        dao.items_page("pl:test", None, start=0, limit=12)

        assert db.query_view_range_calls[0]["limit"] == 12

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

    def test_bookmark_encodes_last_row_key_and_docid(self):
        items = [make_playlist_item("v1", position=0), make_playlist_item("v2", position=1)]
        videos = [make_video("v1"), make_video("v2")]
        dao, _ = self._dao(items, videos)

        _, bm = dao.items_page("pl:test", None, 0, PAGE_SIZE)

        assert bm is not None
        key, docid = json.loads(bm)
        assert docid == "pi:v2"

    def test_no_bookmark_when_no_rows_returned(self):
        dao, _ = self._dao()

        _, bm = dao.items_page("pl:test", None, 0, PAGE_SIZE)

        assert bm is None

    def test_uses_playlist_items_by_playlist_type_view(self):
        dao, db = self._dao()

        dao.items_page("pl:test", None, 0, PAGE_SIZE)

        assert db.query_view_range_calls[0]["view"] == "playlist_items_by_playlist_type"

    def test_include_docs_enabled(self):
        dao, db = self._dao()

        dao.items_page("pl:test", None, 0, PAGE_SIZE)

        assert db.query_view_range_calls[0]["include_docs"] is True


# ---------------------------------------------------------------------------
# count_items
# ---------------------------------------------------------------------------

class TestCountItems:
    def _dao(self, count: int):
        rows = [{"key": ["pl:test"], "value": count}] if count > 0 else []
        db = FakeDB(view_range_rows=rows)
        return PlaylistDAO(db), db

    def test_returns_correct_count(self):
        dao, _ = self._dao(7)
        assert dao.count_items("pl:test") == 7

    def test_zero_items(self):
        dao, _ = self._dao(0)
        assert dao.count_items("pl:test") == 0

    def test_uses_item_counts_by_playlist_view(self):
        dao, db = self._dao(2)
        dao.count_items("pl:my_list")
        call = db.query_view_range_calls[0]
        assert call["view"] == "item_counts_by_playlist"
        assert call["startkey"] == ["pl:my_list", None]
        assert call["endkey"] == ["pl:my_list", {}]
        assert call["reduce"] is True
        assert call["group_level"] == 1


# ---------------------------------------------------------------------------
# removable_batch_for_user
# ---------------------------------------------------------------------------

def _make_playlist(playlist_id: str, owner_type: str = "user", owner_id: str = "u1", builtin_kind: str | None = None) -> dict:
    doc: dict = {"_id": playlist_id, "type": "playlist", "owner_type": owner_type, "owner_id": owner_id}
    if builtin_kind:
        doc["builtin_kind"] = builtin_kind
    return doc


class TestRemovableBatchForUser:
    def _dao(self, get_many_result: dict):
        db = FakeDB(get_many_result=get_many_result)
        return PlaylistDAO(db), db

    def test_returns_empty_for_no_user(self):
        dao, _ = self._dao({})
        assert dao.removable_batch_for_user(None, ["pl:1"]) == {}

    def test_returns_empty_for_no_ids(self):
        user = {"user_id": "u1"}
        dao, _ = self._dao({})
        assert dao.removable_batch_for_user(user, []) == {}

    def test_user_owned_non_builtin_is_removable(self):
        user = {"user_id": "u1"}
        playlist = _make_playlist("pl:1", owner_type="user", owner_id="u1")
        dao, _ = self._dao({"pl:1": playlist})
        result = dao.removable_batch_for_user(user, ["pl:1"])
        assert result["pl:1"] is True

    def test_system_playlist_not_removable(self):
        user = {"user_id": "u1"}
        playlist = _make_playlist("pl:sys", owner_type="system", owner_id="")
        dao, _ = self._dao({"pl:sys": playlist})
        result = dao.removable_batch_for_user(user, ["pl:sys"])
        assert result["pl:sys"] is False

    def test_other_users_playlist_not_removable(self):
        user = {"user_id": "u1"}
        playlist = _make_playlist("pl:other", owner_type="user", owner_id="u2")
        dao, _ = self._dao({"pl:other": playlist})
        result = dao.removable_batch_for_user(user, ["pl:other"])
        assert result["pl:other"] is False

    def test_builtin_playlist_not_removable(self):
        user = {"user_id": "u1"}
        playlist = _make_playlist("pl:fav", owner_type="user", owner_id="u1", builtin_kind="favorites")
        dao, _ = self._dao({"pl:fav": playlist})
        result = dao.removable_batch_for_user(user, ["pl:fav"])
        assert result["pl:fav"] is False

    def test_deduplicates_ids_into_single_get_many(self):
        user = {"user_id": "u1"}
        dao, db = self._dao({})
        dao.removable_batch_for_user(user, ["pl:1", "pl:1", "pl:1"])
        assert len(db.get_many_calls) == 1
        assert len(db.get_many_calls[0]) == 1

    def test_none_ids_are_filtered(self):
        user = {"user_id": "u1"}
        dao, db = self._dao({})
        dao.removable_batch_for_user(user, [None, "pl:1"])
        assert None not in db.get_many_calls[0]
