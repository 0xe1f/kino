from functools import wraps
from typing import Any

from flask import g, jsonify, redirect, session, url_for

from db import db


def load_user() -> None:
    user_id = session.get("user_id")
    g.user = db.get(f"user:{user_id}") if user_id else None


def current_user() -> dict[str, Any] | None:
    if not hasattr(g, "user"):
        load_user()
    return g.user


def login_required(f):
    """Decorator for page routes — redirects unauthenticated requests to /login."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def api_login_required(f):
    """Decorator for API routes — returns 401 JSON for unauthenticated requests."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user():
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated
