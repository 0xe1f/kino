"""Tests for KinoDB.query_view_range — verifies the HTTP request body it sends to CouchDB,
including the include_docs parameter and cursor-based pagination support."""

from unittest.mock import MagicMock, patch

import pytest

from db import KinoDB


@pytest.fixture
def db():
    return KinoDB("http://admin:admin@localhost:5984/", "kino_test")


def _mock_response(rows: list) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"rows": rows}
    return resp


def _posted_body(mock_post: MagicMock) -> dict:
    return mock_post.call_args[1]["json"]


def _make_row(key, doc_id, value=None, doc=None) -> dict:
    row: dict = {"key": key, "id": doc_id, "value": value}
    if doc is not None:
        row["doc"] = doc
    return row


# ---------------------------------------------------------------------------
# Basic contract
# ---------------------------------------------------------------------------

def test_query_view_range_returns_rows(db):
    rows = [_make_row(["pl:1", "video", 0], "pi:1", "v1")]
    with patch("db._http.post", return_value=_mock_response(rows)):
        result = db.query_view_range("kino", "playlist_items_by_playlist_type",
                                     startkey=["pl:1", None, None],
                                     endkey=["pl:1", {}, {}])
    assert result == rows


def test_query_view_range_returns_empty_list_when_no_rows(db):
    with patch("db._http.post", return_value=_mock_response([])):
        result = db.query_view_range("kino", "docs_by_type",
                                     startkey=["video", None], endkey=["video", {}])
    assert result == []


# ---------------------------------------------------------------------------
# include_docs
# ---------------------------------------------------------------------------

def test_query_view_range_omits_include_docs_by_default(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view_range("kino", "docs_by_type",
                            startkey=["video", None], endkey=["video", {}])
    assert "include_docs" not in _posted_body(mock_post)


def test_query_view_range_sends_include_docs_true(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view_range("kino", "docs_by_type",
                            startkey=["video", None], endkey=["video", {}],
                            include_docs=True)
    assert _posted_body(mock_post)["include_docs"] is True


def test_query_view_rows_carry_doc_when_include_docs(db):
    doc = {"_id": "video:abc", "type": "video", "title": "Test"}
    rows = [_make_row(["video", None], "video:abc", doc=doc)]
    with patch("db._http.post", return_value=_mock_response(rows)):
        result = db.query_view_range("kino", "docs_by_type",
                                     startkey=["video", None], endkey=["video", {}],
                                     include_docs=True)
    assert result[0]["doc"] == doc


def test_query_view_include_docs_omitted_by_default(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view("kino", "users_by_email", keys=["a@b.com"])
    assert "include_docs" not in _posted_body(mock_post)


def test_query_view_sends_include_docs_true(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view("kino", "users_by_email", keys=["a@b.com"], include_docs=True)
    assert _posted_body(mock_post)["include_docs"] is True


# ---------------------------------------------------------------------------
# Cursor pagination
# ---------------------------------------------------------------------------

def test_query_view_range_sends_startkey_and_endkey(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view_range("kino", "playlist_items_by_playlist_type",
                            startkey=["pl:1", None, None],
                            endkey=["pl:1", {}, {}])
    body = _posted_body(mock_post)
    assert body["startkey"] == ["pl:1", None, None]
    assert body["endkey"] == ["pl:1", {}, {}]


def test_query_view_range_sends_startkey_docid_when_provided(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view_range("kino", "playlist_items_by_playlist_type",
                            startkey=["pl:1", "video", 5],
                            endkey=["pl:1", {}, {}],
                            startkey_docid="pi:xyz")
    assert _posted_body(mock_post)["startkey_docid"] == "pi:xyz"


def test_query_view_range_omits_startkey_docid_when_none(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view_range("kino", "playlist_items_by_playlist_type",
                            startkey=["pl:1", None, None],
                            endkey=["pl:1", {}, {}])
    assert "startkey_docid" not in _posted_body(mock_post)


def test_query_view_range_sends_skip_when_nonzero(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view_range("kino", "playlist_items_by_playlist_type",
                            startkey=["pl:1", "video", 5],
                            endkey=["pl:1", {}, {}],
                            skip=1)
    assert _posted_body(mock_post)["skip"] == 1


def test_query_view_range_omits_skip_when_zero(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view_range("kino", "playlist_items_by_playlist_type",
                            startkey=["pl:1", None, None],
                            endkey=["pl:1", {}, {}])
    assert "skip" not in _posted_body(mock_post)


def test_query_view_range_sends_limit(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view_range("kino", "docs_by_type",
                            startkey=["video", None], endkey=["video", {}],
                            limit=1)
    assert _posted_body(mock_post)["limit"] == 1


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------

def test_query_view_range_posts_to_correct_url(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view_range("kino", "playlist_items_by_playlist_type",
                            startkey=["pl:1", None, None],
                            endkey=["pl:1", {}, {}])
    url = mock_post.call_args[0][0]
    assert url.endswith("/kino_test/_design/kino/_view/playlist_items_by_playlist_type")


def test_query_view_posts_to_correct_url(db):
    with patch("db._http.post", return_value=_mock_response([])) as mock_post:
        db.query_view("kino", "users_by_email", keys=["a@b.com"])
    url = mock_post.call_args[0][0]
    assert url.endswith("/kino_test/_design/kino/_view/users_by_email")
