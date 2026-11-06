"""Shared authentication for bridge and setup operations."""

import json
import secrets
import unicodedata
from urllib.parse import unquote

from aiohttp import web


def validate_api_key(key: str | None) -> None:
    if key and any(unicodedata.category(char) == "Cc" for char in key):
        raise ValueError("API keys must not contain control characters")


def require_api_key(request: web.Request, key: str | None) -> None:
    if not key:
        return
    supplied = request.query.get("key", "") or request.headers.get("X-Api-Key", "")
    try:
        if request.headers.get("X-Api-Key-Encoding") == "percent" and not request.query.get("key"):
            supplied = unquote(supplied, encoding="utf-8", errors="strict")
        matches = secrets.compare_digest(supplied.encode("utf-8"), key.encode("utf-8"))
    except UnicodeError:
        matches = False
    if not matches:
        raise web.HTTPUnauthorized(
            text=json.dumps({"error": "unauthorized"}), content_type="application/json",
        )
