"""Tests for KinoDB.find_page — verifies the HTTP request body it sends to CouchDB."""

from unittest.mock import MagicMock, patch

import pytest

from db import KinoDB

SELECTOR = {"type": "playlist_item", "playlist_id": "pl:1"}
SORT = [{"type": "asc"}, {"playlist_id": "asc"}, {"position": "asc"}]


@pytest.fixture
def db():
    return KinoDB("http://admin:admin@localhost:5984/", "kino_test")


def _mock_response(docs: list, bookmark: str | None = "bm_next") -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"docs": docs, "bookmark": bookmark}
    return resp


def _posted_body(mock_post: MagicMock) -> dict:
    return mock_post.call_args[1]["json"]


# ---------------------------------------------------------------------------
# Basic contract
# ---------------------------------------------------------------------------

def test_find_page_returns_docs_and_bookmark(db):
    docs = [{"_id": "v1"}, {"_id": "v2"}]
    with patch("db._http.post", return_value=_mock_response(docs, "bm1")) as _:
        result_docs, result_bm = db.find_page(SELECTOR, SORT, limit=10)

    assert result_docs == docs
    assert result_bm == "bm1"


def test_find_page_returns_none_bookmark_when_absent(db):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"docs": []}  # no bookmark key
    with patch("db._http.post", return_value=resp):
        _, bm = db.find_page(SELECTOR, SORT, limit=10)

    assert bm is None


# ---------------------------------------------------------------------------
# Request body construction
# ---------------------------------------------------------------------------

def test_find_page_body_includes_selector_sort_limit(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.find_page(SELECTOR, SORT, limit=25)

    body = _posted_body(mock_post)
    assert body["selector"] == SELECTOR
    assert body["sort"] == SORT
    assert body["limit"] == 25


def test_find_page_omits_bookmark_when_none(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.find_page(SELECTOR, SORT, limit=10, bookmark=None)

    assert "bookmark" not in _posted_body(mock_post)


def test_find_page_includes_bookmark_when_provided(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.find_page(SELECTOR, SORT, limit=10, bookmark="cursor_abc")

    assert _posted_body(mock_post)["bookmark"] == "cursor_abc"


def test_find_page_omits_fields_when_not_specified(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.find_page(SELECTOR, SORT, limit=10)

    assert "fields" not in _posted_body(mock_post)


def test_find_page_includes_fields_when_specified(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.find_page(SELECTOR, SORT, limit=25000, fields=["_id"])

    assert _posted_body(mock_post)["fields"] == ["_id"]


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

def test_find_page_posts_to_correct_url(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.find_page(SELECTOR, SORT, limit=10)

    url = mock_post.call_args[0][0]
    assert url.endswith("/kino_test/_find")
