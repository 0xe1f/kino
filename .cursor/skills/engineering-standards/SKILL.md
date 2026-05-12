---
name: engineering-standards
description: Enforces project-wide engineering standards for the cino codebase. Apply whenever making any code change — database queries, data modeling, access control, pagination, or infrastructure/networking decisions.
---

# Engineering Standards

## Core principles

In order of importance:

* Security
* Performance
* Responsiveness

## Clarify before acting

When things are unclear, always discuss with me first.

## Database access

All database queries should be indexed, not scanned. If doing so has other implications, discuss with me first. We want data access to value performance over all else, second only to security.

## Batch database fetches

Never fetch documents in a loop. Use `db.get_many(ids)` for reads and `db.bulk_save(docs)` for writes. Any code that calls `db.get()` or `db.save()` inside a loop is a bug.

## Data ownership

Any new data tied to a user should be deleted when the user is deleted.

## Pagination

Any lists expected to generate a large-ish amount of data (100 or more rows) should be paginated. Pagination should be asynchronous on the client side. Paginate using database's own facilities. Do not order, paginate, or otherwise manipulate large result sets in Python - except on the pages themselves.

## CouchDB views

**Always** use CouchDB map/reduce views. Never use Mango `_find`.

Do not call `_find`, `find_by_mango`, `find_many`, `find_page`, or `find_one`. These methods have been removed. Mango queries of any kind are a prohibited pattern — they do not use the B-tree and degrade linearly with collection size.

Use views for:

- **All document lookups by field** (email, username, source, owner, parent, type, etc.). Every access pattern that would have been a Mango filter must have a dedicated view.
- **Aggregations** (counts, sums, first/last in a sorted set). Never fetch a large result set just to count it in Python.
- **Multi-key batch lookups**. Use `query_view(keys=[...], group=True)` instead of any `$in`-style query.
- **Sorted pagination** where the sort order must come from the database (e.g. history sorted by date descending, items by position).

All views live in `_design/kino`, defined in `db.py::ensure_design_docs()`. Any new view must also be warmed at startup (call `query_view` or `query_view_range` with an empty/bounded query after `ensure_design_docs()`).

## Networking

Do not leave any ports open to the public that are not necessary. Generally speaking, anything that's not a web service should be inaccessible from the public internet.

## Deployment

Docker containers might be deployed to other hosts. Whenever possible, configuration should be part of the container, not read from host.

# Application

## Access control

Viewing videos and playlists of other users is ok. Modifying them is not.
