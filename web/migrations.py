# Copyright 2026 Akop Karapetyan
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import threading
from typing import Any

from db import db
from utils import normalize_username, now_iso

_migration_lock = threading.Lock()
_migration_done = False

LEGACY_USERNAME_BASE_ENV = "KINO_LEGACY_USERNAME_BASE"


def run_phase3_migration() -> None:
    global _migration_done
    if _migration_done:
        return
    with _migration_lock:
        if _migration_done:
            return
        marker = db.get("migration:phase3-account-ui")
        if marker:
            _migration_done = True
            return

        user_rows = db.query_view_range(
            "kino", "docs_by_type",
            startkey=["user", None], endkey=["user", {}],
            include_docs=True,
        )
        users = sorted(
            [r["doc"] for r in user_rows if r.get("doc")],
            key=lambda u: (u.get("created_at", ""), u.get("user_id", "")),
        )
        used_usernames: set[str] = {
            normalize_username(u.get("username", "")).lower()
            for u in users
            if normalize_username(u.get("username", ""))
        }
        missing_username_users = [
            u for u in users if not normalize_username(u.get("username", ""))
        ]

        users_updated = 0
        if missing_username_users:
            base = normalize_username(os.getenv(LEGACY_USERNAME_BASE_ENV, ""))
            if not base:
                raise RuntimeError(
                    f"{LEGACY_USERNAME_BASE_ENV} is required to migrate existing users "
                    "missing usernames."
                )
            next_suffix = 1
            for user in missing_username_users:
                while True:
                    candidate = base if next_suffix == 1 else f"{base}{next_suffix}"
                    next_suffix += 1
                    if candidate.lower() not in used_usernames:
                        break
                user["username"] = candidate
                user["updated_at"] = now_iso()
                db.save(user)
                used_usernames.add(candidate.lower())
                users_updated += 1

        videos_updated = 0
        video_rows = db.query_view_range(
            "kino", "docs_by_type",
            startkey=["video", None], endkey=["video", {}],
            include_docs=True,
        )
        for row in video_rows:
            video_doc = row.get("doc")
            if not video_doc or "dislikes" not in video_doc:
                continue
            video_doc.pop("dislikes", None)
            video_doc["updated_at"] = now_iso()
            db.save(video_doc)
            videos_updated += 1

        reactions_deleted = 0
        reaction_rows = db.query_view_range(
            "kino", "docs_by_type",
            startkey=["reaction", None], endkey=["reaction", {}],
            include_docs=True,
        )
        for row in reaction_rows:
            reaction_doc = row.get("doc")
            if not reaction_doc or reaction_doc.get("value") == "like":
                continue
            db.delete(reaction_doc)
            reactions_deleted += 1

        db.save(
            {
                "_id": "migration:phase3-account-ui",
                "type": "migration",
                "users_updated": users_updated,
                "videos_updated": videos_updated,
                "reactions_deleted": reactions_deleted,
                "completed_at": now_iso(),
            }
        )
        _migration_done = True
