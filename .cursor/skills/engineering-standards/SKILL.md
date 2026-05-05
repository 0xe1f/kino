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

## Networking

Do not leave any ports open to the public that are not necessary. Generally speaking, anything that's not a web service should be inaccessible from the public internet.

## Deployment

Docker containers might be deployed to other hosts. Whenever possible, configuration should be part of the container, not read from host.

# Application

## Access control

Viewing videos and playlists of other users is ok. Modifying them is not.
