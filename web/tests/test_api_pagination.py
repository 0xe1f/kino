"""Tests for pagination API endpoints — verifies response shape and has_more logic."""

from unittest.mock import MagicMock, patch

import pytest

PAGE_SIZE = 40  # must match app.PAGE_SIZE


def _make_collected_items(n: int, start: int = 0) -> list[dict]:
    """Build a list of hydrated playlist items as items_page would return."""
    return [
        {
            "item": {"_id": f"pi:v{start + i}", "item_type": "video", "item_id": f"v{start + i}"},
            "target": {"_id": f"v{start + i}", "type": "video", "title": f"Video {i}", "duration": 60},
            "position_label": start + i + 1,
        }
        for i in range(n)
    ]


# Use a fully-normalized ID so normalize_playlist_id("playlist:1") == "playlist:1" (no change).
PLAYLIST_RAW_ID = "playlist:1"
FAKE_PLAYLIST = {"_id": PLAYLIST_RAW_ID, "type": "playlist", "name": "Test Playlist"}


# ---------------------------------------------------------------------------
# GET /api/playlist/<pid>/items
# ---------------------------------------------------------------------------

class TestApiPlaylistItems:
    def _request(self, client, pid=PLAYLIST_RAW_ID, bookmark="", start=0):
        return client.get(f"/api/playlist/{pid}/items?bookmark={bookmark}&start={start}")

    def _patch(self, items, next_bookmark="bm2"):
        patcher_get = patch("app.playlists.get", return_value=FAKE_PLAYLIST)
        patcher_page = patch("app.playlists.items_page", return_value=(items, next_bookmark))
        patcher_removable = patch("app.playlists.removable_doc_for_user", return_value=False)
        return patcher_get, patcher_page, patcher_removable

    def test_returns_200_for_known_playlist(self, client):
        items = _make_collected_items(3)
        p1, p2, p3 = self._patch(items)
        with p1, p2, p3:
            resp = self._request(client)
        assert resp.status_code == 200

    def test_returns_404_for_unknown_playlist(self, client):
        with patch("app.playlists.get", return_value=None):
            resp = self._request(client, pid="playlist:missing")
        assert resp.status_code == 404

    def test_has_more_true_when_full_page_returned(self, client):
        items = _make_collected_items(PAGE_SIZE)
        p1, p2, p3 = self._patch(items)
        with p1, p2, p3:
            data = self._request(client).get_json()
        assert data["has_more"] is True

    def test_has_more_false_when_partial_page_returned(self, client):
        items = _make_collected_items(PAGE_SIZE - 1)
        p1, p2, p3 = self._patch(items)
        with p1, p2, p3:
            data = self._request(client).get_json()
        assert data["has_more"] is False

    def test_has_more_false_for_empty_page(self, client):
        p1, p2, p3 = self._patch([])
        with p1, p2, p3:
            data = self._request(client).get_json()
        assert data["has_more"] is False

    def test_next_bookmark_passed_through(self, client):
        items = _make_collected_items(PAGE_SIZE)
        p1, p2, p3 = self._patch(items, next_bookmark="cursor_xyz")
        with p1, p2, p3:
            data = self._request(client).get_json()
        assert data["next_bookmark"] == "cursor_xyz"

    def test_next_bookmark_empty_string_when_dao_returns_none(self, client):
        items = _make_collected_items(3)
        p1, p2, p3 = self._patch(items, next_bookmark=None)
        with p1, p2, p3:
            data = self._request(client).get_json()
        assert data["next_bookmark"] == ""

    def test_next_start_equals_start_plus_item_count(self, client):
        items = _make_collected_items(10)
        p1, p2, p3 = self._patch(items)
        with p1, p2, p3:
            data = self._request(client, start=30).get_json()
        assert data["next_start"] == 40

    def test_bookmark_and_start_forwarded_to_dao(self, client):
        with patch("app.playlists.get", return_value=FAKE_PLAYLIST), \
             patch("app.playlists.items_page", return_value=([], None)) as mock_page, \
             patch("app.playlists.removable_doc_for_user", return_value=False):
            self._request(client, bookmark="cursor_abc", start=25)

        mock_page.assert_called_once_with(PLAYLIST_RAW_ID, "cursor_abc", 25, PAGE_SIZE)

    def test_empty_bookmark_forwarded_as_none_to_dao(self, client):
        """Empty string bookmark from client should arrive at DAO as None."""
        with patch("app.playlists.get", return_value=FAKE_PLAYLIST), \
             patch("app.playlists.items_page", return_value=([], None)) as mock_page, \
             patch("app.playlists.removable_doc_for_user", return_value=False):
            self._request(client, bookmark="", start=0)

        _, dao_bookmark, _, _ = mock_page.call_args[0]
        assert dao_bookmark is None

    def test_response_contains_html_key(self, client):
        p1, p2, p3 = self._patch([])
        with p1, p2, p3:
            data = self._request(client).get_json()
        assert "html" in data


# ---------------------------------------------------------------------------
# has_more edge case: exactly PAGE_SIZE items (boundary)
# ---------------------------------------------------------------------------

class TestHasMoreBoundary:
    def test_exactly_page_size_means_has_more_true(self, client):
        items = _make_collected_items(PAGE_SIZE)
        with patch("app.playlists.get", return_value=FAKE_PLAYLIST), \
             patch("app.playlists.items_page", return_value=(items, "bm")), \
             patch("app.playlists.removable_doc_for_user", return_value=False):
            data = client.get(f"/api/playlist/{PLAYLIST_RAW_ID}/items").get_json()
        assert data["has_more"] is True

    def test_one_less_than_page_size_means_has_more_false(self, client):
        items = _make_collected_items(PAGE_SIZE - 1)
        with patch("app.playlists.get", return_value=FAKE_PLAYLIST), \
             patch("app.playlists.items_page", return_value=(items, None)), \
             patch("app.playlists.removable_doc_for_user", return_value=False):
            data = client.get(f"/api/playlist/{PLAYLIST_RAW_ID}/items").get_json()
        assert data["has_more"] is False
