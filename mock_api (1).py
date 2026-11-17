"""
Mock API Simulator (v1.2)
=========================

A self-contained local HTTP API simulator built on Python's standard
library, plus the external ``requests`` package for the client-side
smoke tests.

Endpoints
---------
GET    /                       Service index (name, version, link list)
GET    /health                 Liveness probe (status, version, uptime)
GET    /api/stats              Aggregate request + data statistics
GET    /openapi.json           OpenAPI 3.0 document for the whole API

GET    /api/users              List users (supports ?page, ?per_page,
                               ?role, ?q filtering / pagination)
GET    /api/users/<id>         Fetch a single user
POST   /api/users              Create a user
PUT    /api/users/<id>         Replace a user (full update)
PATCH  /api/users/<id>         Partially update a user
DELETE /api/users/<id>         Delete a user
HEAD  (any GET route)          Same headers as GET, empty body

Every response carries an ``X-Request-ID`` header (echoed when the
client sends one, generated otherwise) for tracing. ``OPTIONS`` is
supported on every route and returns an ``Allow`` header.

Configuration
-------------
By default the server listens on port 4221. Override with ``--port`` or
the ``MOCK_API_PORT`` environment variable.

Usage
-----
    python mock_api.py                 # :4221 + smoke tests
    python mock_api.py --no-tests      # server only
    python mock_api.py --port 8080
    MOCK_API_PORT=9000 python mock_api.py -v
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import socket
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

import requests


# ============================================================
# Configuration
# ============================================================

HOST = "localhost"
DEFAULT_PORT = 4221
REQUEST_TIMEOUT = 5
MAX_BODY_BYTES = 1_000_000  # 1 MB cap on incoming request bodies
API_VERSION = "1.3.0"

# Validation limits (stricter than v1.1).
MAX_NAME_LEN = 100
MAX_ROLE_LEN = 50
ALLOWED_ROLES = {"Admin", "User", "Developer", "Maintainer", "Guest"}

# Pagination defaults and bounds.
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 100

log = logging.getLogger("mockapi")


# ============================================================
# Mock Database
# ============================================================

_INITIAL_USERS = [
    {"id": 1, "name": "Alice", "role": "Admin"},
    {"id": 2, "name": "Bob", "role": "User"},
]

Mock_DB: dict[str, list[dict[str, Any]]] = {
    "users": [dict(u) for u in _INITIAL_USERS],
}

# RLock keeps us safe even if a helper calls another lock-acquiring helper.
_db_lock = threading.RLock()


def reset_database() -> None:
    with _db_lock:
        Mock_DB["users"].clear()
        Mock_DB["users"].extend(dict(u) for u in _INITIAL_USERS)


def next_user_id() -> int:
    """Return the next available user ID. Caller must not hold a stale view."""
    with _db_lock:
        if not Mock_DB["users"]:
            return 1
        return max(u["id"] for u in Mock_DB["users"]) + 1


def get_user_by_id(user_id: int) -> Optional[dict[str, Any]]:
    """Return a *copy* of the user, or ``None`` if not found."""
    with _db_lock:
        for user in Mock_DB["users"]:
            if user["id"] == user_id:
                return dict(user)
    return None


def list_users() -> list[dict[str, Any]]:
    with _db_lock:
        return [dict(u) for u in Mock_DB["users"]]


def add_user(name: str, role: str) -> dict[str, Any]:
    with _db_lock:
        new_user = {"id": next_user_id(), "name": name, "role": role}
        Mock_DB["users"].append(new_user)
        return dict(new_user)


def update_user(user_id: int, name: str, role: str) -> Optional[dict[str, Any]]:
    """Full replacement update. Returns a copy or ``None`` if missing."""
    with _db_lock:
        for user in Mock_DB["users"]:
            if user["id"] == user_id:
                user["name"] = name
                user["role"] = role
                return dict(user)
    return None


def patch_user(
    user_id: int, *, name: Optional[str] = None, role: Optional[str] = None
) -> Optional[dict[str, Any]]:
    """Partial update. Only supplied fields are changed."""
    with _db_lock:
        for user in Mock_DB["users"]:
            if user["id"] == user_id:
                if name is not None:
                    user["name"] = name
                if role is not None:
                    user["role"] = role
                return dict(user)
    return None


def delete_user(user_id: int) -> bool:
    with _db_lock:
        for i, user in enumerate(Mock_DB["users"]):
            if user["id"] == user_id:
                Mock_DB["users"].pop(i)
                return True
    return False


# ============================================================
# Request Statistics
# ============================================================

_stats_lock = threading.Lock()
_stats: dict[str, Any] = {
    "started_at": datetime.now(timezone.utc).isoformat(),
    "start_perf": time.monotonic(),
    "requests_total": 0,
    "by_method": {},
    "by_path": {},
}


def _bump_stats(method: str, path: str) -> None:
    with _stats_lock:
        _stats["requests_total"] += 1
        _stats["by_method"][method] = _stats["by_method"].get(method, 0) + 1
        _stats["by_path"][path] = _stats["by_path"].get(path, 0) + 1


def _snapshot_stats() -> dict[str, Any]:
    with _stats_lock:
        return {
            "requests_total": _stats["requests_total"],
            "by_method": dict(_stats["by_method"]),
            "by_path": dict(_stats["by_path"]),
            "started_at": _stats["started_at"],
            "uptime_seconds": round(time.monotonic() - _stats["start_perf"], 3),
        }


def _reset_stats() -> None:
    with _stats_lock:
        _stats["started_at"] = datetime.now(timezone.utc).isoformat()
        _stats["start_perf"] = time.monotonic()
        _stats["requests_total"] = 0
        _stats["by_method"] = {}
        _stats["by_path"] = {}


# ============================================================
# Validation
# ============================================================

class UnsupportedMediaType(ValueError):
    """Raised when the request's Content-Type is not application/json."""

    status = 415


def validate_user_payload(
    payload: dict[str, Any], require_all: bool = True
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Validate a user payload.

    Returns ``(name, role, error)`` where ``error`` is ``None`` on success.
    When ``require_all`` is False (PATCH semantics) only present fields are
    validated and missing ones come back as ``None`` without error.
    """
    name = payload.get("name")
    role = payload.get("role")

    if require_all:
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LEN:
            return (
                None,
                None,
                f"'name' is required and must be a non-empty "
                f"string of at most {MAX_NAME_LEN} characters",
            )
        if not isinstance(role, str) or not role.strip() or len(role) > MAX_ROLE_LEN:
            return (
                None,
                None,
                f"'role' is required and must be a non-empty "
                f"string of at most {MAX_ROLE_LEN} characters",
            )
        if role.strip() not in ALLOWED_ROLES:
            return (
                None,
                None,
                f"'role' must be one of {sorted(ALLOWED_ROLES)}",
            )
        return name.strip(), role.strip(), None

    # PATCH: validate only provided fields.
    clean_name: Optional[str] = None
    clean_role: Optional[str] = None
    if name is not None:
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LEN:
            return (
                None,
                None,
                f"'name' must be a non-empty string of at most "
                f"{MAX_NAME_LEN} characters",
            )
        clean_name = name.strip()
    if role is not None:
        if not isinstance(role, str) or not role.strip() or len(role) > MAX_ROLE_LEN:
            return (
                None,
                None,
                f"'role' must be a non-empty string of at most "
                f"{MAX_ROLE_LEN} characters",
            )
        if role.strip() not in ALLOWED_ROLES:
            return None, None, f"'role' must be one of {sorted(ALLOWED_ROLES)}"
        clean_role = role.strip()
    return clean_name, clean_role, None


# ============================================================
# Router
# ============================================================

class Route:
    """A single (method, regex) -> handler binding."""

    __slots__ = ("method", "pattern", "handler")

    def __init__(
        self, method: str, pattern: str, handler: Callable[..., None]
    ) -> None:
        self.method = method
        self.pattern = re.compile(pattern)
        self.handler = handler


ROUTES: list[Route] = []


def route(method: str, pattern: str) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Register a handler for ``method`` matching ``pattern`` (anchored)."""

    def decorator(fn: Callable[..., None]) -> Callable[..., None]:
        ROUTES.append(Route(method, pattern, fn))
        return fn

    return decorator


def _allowed_methods_for(path: str) -> list[str]:
    return sorted({r.method for r in ROUTES if r.pattern.match(path)})


def _parse_int(qs: dict[str, list[str]], key: str, default: int) -> int:
    value = qs.get(key, [None])[0]  # type: ignore[list-item]
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"'{key}' must be an integer") from exc


# ============================================================
# API Request Handler
# ============================================================

class MockAPIHandler(BaseHTTPRequestHandler):
    """Routes the mock API. Each method must write exactly one response."""

    protocol_version = "HTTP/1.1"
    server_version = f"MockAPI/{API_VERSION}"

    # ---------------- response helpers ----------------

    def _send_json(self, payload: Any, status_code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header("X-Request-ID", rid)
        self.end_headers()
        # HEAD requests get the same headers (incl. Content-Length) but no body.
        if not getattr(self, "_head_mode", False):
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Client hung up before we could finish; nothing to do.
                pass
        self.close_connection = True

    def _send_error_json(
        self, status: int, error: str, message: str
    ) -> None:
        self._send_json({"error": error, "message": message}, status)

    def _method_not_allowed(self, *allowed: str) -> None:
        body = json.dumps(
            {
                "error": "Method Not Allowed",
                "message": f"Allowed methods: {', '.join(allowed)}",
            }
        ).encode("utf-8")
        self.send_response(405)
        self.send_header("Allow", ", ".join(allowed))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        rid = getattr(self, "_request_id", None)
        if rid:
            self.send_header("X-Request-ID", rid)
        self.end_headers()
        if not getattr(self, "_head_mode", False):
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
        self.close_connection = True

    # ---------------- request parsing ----------------

    def _get_path(self) -> str:
        return urlsplit(self.path).path

    def _get_query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query, keep_blank_values=True)

    def _ensure_request_id(self) -> str:
        rid = self.headers.get("X-Request-ID") or uuid.uuid4().hex
        self._request_id = rid
        return rid

    def _read_json_object(self) -> dict[str, Any]:
        """Read and validate the JSON body.

        Raises ``UnsupportedMediaType`` for a wrong Content-Type and
        ``ValueError`` for any body-level problem.
        """
        ctype = self.headers.get("Content-Type", "")
        if ctype and "application/json" not in ctype.lower():
            raise UnsupportedMediaType(
                "Content-Type must be application/json"
            )

        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValueError("Content-Length header is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError("Content-Length must be an integer") from exc
        if length < 0:
            raise ValueError("Content-Length must be non-negative")
        if length > MAX_BODY_BYTES:
            raise ValueError(
                f"Request body exceeds limit of {MAX_BODY_BYTES} bytes"
            )

        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Request body must be a JSON object")
        return parsed

    # ---------------- dispatch ----------------

    def _dispatch(self, method: str) -> None:
        path = self._get_path()
        query = self._get_query()
        request_id = self._ensure_request_id()
        # HEAD behaves like GET but omits the response body.
        self._head_mode = method == "HEAD"
        lookup_method = "GET" if self._head_mode else method
        _bump_stats(method, path)
        log.debug("[%s] %s %s", request_id, method, path)

        try:
            if method == "OPTIONS":
                allowed = _allowed_methods_for(path)
                if not allowed:
                    self._send_error_json(404, "Not Found", f"No route for {path}")
                    return
                # 204 No Content with an Allow header.
                self.send_response(204)
                self.send_header("Allow", ", ".join(allowed))
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.send_header("X-Request-ID", request_id)
                self.end_headers()
                self.close_connection = True
                return

            for r in ROUTES:
                if r.method != lookup_method:
                    continue
                m = r.pattern.match(path)
                if m:
                    r.handler(self, m, query)
                    return

            allowed = _allowed_methods_for(path)
            if allowed:
                self._method_not_allowed(*allowed)
            else:
                self._send_error_json(404, "Not Found", f"No route for {path}")
        except UnsupportedMediaType as exc:
            self._send_error_json(415, "Unsupported Media Type", str(exc))
        except ValueError as exc:
            self._send_error_json(400, "Bad Request", str(exc))
        except Exception:  # pragma: no cover - defensive
            log.exception("[%s] Unhandled error", request_id)
            self._send_error_json(
                500, "Internal Server Error", "Unexpected server error"
            )

    # Standard HTTP verbs all funnel through _dispatch.
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch("HEAD")

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)


# ============================================================
# Route Handlers
# ============================================================
#
# Registration order matters: integer-id routes (``\d+``) must be
# registered before the catch-all bad-id routes (``[^/]+``) so that a
# numeric suffix is matched by the right handler.

@route("GET", r"^/api/users/?$")
def handle_list_users(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    users = list_users()

    role = qs.get("role", [None])[0]  # type: ignore[list-item]
    search = qs.get("q", [None])[0]  # type: ignore[list-item]
    if role:
        users = [u for u in users if u["role"].lower() == role.lower()]
    if search:
        needle = search.lower()
        users = [u for u in users if needle in u["name"].lower()]

    has_paging = any(k in qs for k in ("page", "per_page"))
    if has_paging:
        page = _parse_int(qs, "page", 1)
        per_page = _parse_int(qs, "per_page", DEFAULT_PAGE_SIZE)
        if page < 1:
            raise ValueError("'page' must be >= 1")
        if per_page < 1 or per_page > MAX_PAGE_SIZE:
            raise ValueError(
                f"'per_page' must be between 1 and {MAX_PAGE_SIZE}"
            )
        total = len(users)
        total_pages = (total + per_page - 1) // per_page if total else 0
        start = (page - 1) * per_page
        page_items = users[start:start + per_page]
        h._send_json(
            {
                "data": page_items,
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
            },
            200,
        )
    else:
        h._send_json(users, 200)


@route("GET", r"^/api/users/(\d+)/?$")
def handle_get_user(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    user_id = int(m.group(1))
    user = get_user_by_id(user_id)
    if user is None:
        h._send_error_json(404, "Not Found", f"User with ID {user_id} not found")
        return
    h._send_json(user, 200)


@route("GET", r"^/api/users/([^/]+)/?$")
def handle_get_user_bad_id(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    h._send_error_json(400, "Bad Request", "User ID must be an integer")


@route("POST", r"^/api/users/?$")
def handle_create_user(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    payload = h._read_json_object()
    name, role, err = validate_user_payload(payload, require_all=True)
    if err:
        h._send_error_json(400, "Invalid input", err)
        return
    created = add_user(name, role)  # type: ignore[arg-type]
    h._send_json(created, 201)


@route("PUT", r"^/api/users/(\d+)/?$")
def handle_replace_user(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    user_id = int(m.group(1))
    if get_user_by_id(user_id) is None:
        h._send_error_json(404, "Not Found", f"User with ID {user_id} not found")
        return
    payload = h._read_json_object()
    name, role, err = validate_user_payload(payload, require_all=True)
    if err:
        h._send_error_json(400, "Invalid input", err)
        return
    updated = update_user(user_id, name, role)  # type: ignore[arg-type]
    h._send_json(updated, 200)


@route("PUT", r"^/api/users/([^/]+)/?$")
def handle_replace_user_bad_id(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    h._send_error_json(400, "Bad Request", "User ID must be an integer")


@route("PATCH", r"^/api/users/(\d+)/?$")
def handle_patch_user(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    user_id = int(m.group(1))
    if get_user_by_id(user_id) is None:
        h._send_error_json(404, "Not Found", f"User with ID {user_id} not found")
        return
    payload = h._read_json_object()
    name, role, err = validate_user_payload(payload, require_all=False)
    if err:
        h._send_error_json(400, "Invalid input", err)
        return
    if name is None and role is None:
        h._send_error_json(
            400, "Invalid input", "No valid fields to update; supply 'name' and/or 'role'"
        )
        return
    updated = patch_user(user_id, name=name, role=role)
    h._send_json(updated, 200)


@route("PATCH", r"^/api/users/([^/]+)/?$")
def handle_patch_user_bad_id(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    h._send_error_json(400, "Bad Request", "User ID must be an integer")


@route("DELETE", r"^/api/users/(\d+)/?$")
def handle_delete_user(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    user_id = int(m.group(1))
    if delete_user(user_id):
        h._send_json({"deleted": True, "id": user_id}, 200)
    else:
        h._send_error_json(404, "Not Found", f"User with ID {user_id} not found")


@route("DELETE", r"^/api/users/([^/]+)/?$")
def handle_delete_user_bad_id(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    h._send_error_json(400, "Bad Request", "User ID must be an integer")


@route("GET", r"^/health/?$")
def handle_health(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    uptime = time.monotonic() - _stats["start_perf"]
    h._send_json(
        {"status": "ok", "version": API_VERSION, "uptime_seconds": round(uptime, 3)},
        200,
    )


@route("GET", r"^/api/stats/?$")
def handle_stats(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    snapshot = _snapshot_stats()
    roles: dict[str, int] = {}
    for u in list_users():
        roles[u["role"]] = roles.get(u["role"], 0) + 1
    snapshot["user_count"] = len(list_users())
    snapshot["roles"] = roles
    h._send_json(snapshot, 200)


@route("GET", r"^/openapi\.json/?$")
def handle_openapi(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    h._send_json(OPENAPI_SPEC, 200)


@route("GET", r"^/?$")
def handle_root(h: MockAPIHandler, m: re.Match[str], qs: dict[str, list[str]]) -> None:
    h._send_json(
        {
            "name": "Mock API",
            "version": API_VERSION,
            "endpoints": [
                "GET /api/users",
                "GET /api/users/<id>",
                "POST /api/users",
                "PUT /api/users/<id>",
                "PATCH /api/users/<id>",
                "DELETE /api/users/<id>",
                "GET /health",
                "GET /api/stats",
                "GET /openapi.json",
            ],
        },
        200,
    )


# ============================================================
# OpenAPI 3.0 Document
# ============================================================

OPENAPI_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {
        "title": "Mock API",
        "version": API_VERSION,
        "description": (
            "A self-contained local mock HTTP API simulator built on "
            "Python's standard library. Provides CRUD over an in-memory "
            "user collection plus health, stats and a self-describing "
            "OpenAPI document. Every response carries an X-Request-ID "
            "header (echoed when supplied, generated otherwise) for "
            "request tracing. HEAD is supported on every GET route."
        ),
        "license": {"name": "MIT"},
    },
    "servers": [
        {
            "url": f"http://{HOST}:{DEFAULT_PORT}",
            "description": "Local mock server",
        }
    ],
    "tags": [
        {"name": "users", "description": "User CRUD operations"},
        {"name": "system", "description": "Health, stats and documentation"},
    ],
    "paths": {
        "/": {
            "get": {
                "tags": ["system"],
                "summary": "Service index",
                "description": "Service name, version and a list of available endpoints.",
                "responses": {
                    "200": {
                        "description": "Service index",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Index"}
                            }
                        },
                    }
                },
            }
        },
        "/health": {
            "get": {
                "tags": ["system"],
                "summary": "Liveness probe",
                "responses": {
                    "200": {
                        "description": "Service health",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Health"}
                            }
                        },
                    }
                },
            }
        },
        "/api/stats": {
            "get": {
                "tags": ["system"],
                "summary": "Request and data statistics",
                "responses": {
                    "200": {
                        "description": "Statistics snapshot",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Stats"}
                            }
                        },
                    }
                },
            }
        },
        "/openapi.json": {
            "get": {
                "tags": ["system"],
                "summary": "OpenAPI document",
                "responses": {
                    "200": {
                        "description": "OpenAPI 3.0 JSON document",
                        "content": {
                            "application/json": {"schema": {"type": "object"}}
                        },
                    }
                },
            }
        },
        "/api/users": {
            "get": {
                "tags": ["users"],
                "summary": "List users",
                "description": (
                    "Returns all users as a bare JSON array. Add `page` or "
                    "`per_page` to receive a paginated envelope; narrow "
                    "results with `role` (exact, case-insensitive match) "
                    "and `q` (substring match against the user's name)."
                ),
                "parameters": [
                    {
                        "name": "page",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "integer", "minimum": 1, "default": 1},
                    },
                    {
                        "name": "per_page",
                        "in": "query",
                        "required": False,
                        "schema": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": MAX_PAGE_SIZE,
                            "default": DEFAULT_PAGE_SIZE,
                        },
                    },
                    {
                        "name": "role",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string", "enum": sorted(ALLOWED_ROLES)},
                    },
                    {
                        "name": "q",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string"},
                    },
                ],
                "responses": {
                    "200": {
                        "description": (
                            "Bare list, or a paginated envelope when "
                            "`page`/`per_page` is supplied."
                        ),
                        "content": {
                            "application/json": {
                                "schema": {
                                    "oneOf": [
                                        {
                                            "type": "array",
                                            "items": {"$ref": "#/components/schemas/User"},
                                        },
                                        {"$ref": "#/components/schemas/UserPage"},
                                    ]
                                }
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/BadRequest"},
                },
            },
            "post": {
                "tags": ["users"],
                "summary": "Create a user",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/UserCreate"}
                        }
                    },
                },
                "responses": {
                    "201": {
                        "description": "Created user",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/User"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/BadRequest"},
                    "415": {"$ref": "#/components/responses/UnsupportedMediaType"},
                },
            },
        },
        "/api/users/{id}": {
            "parameters": [
                {
                    "name": "id",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "integer", "minimum": 1},
                    "description": "Numeric user identifier.",
                }
            ],
            "get": {
                "tags": ["users"],
                "summary": "Fetch a user",
                "responses": {
                    "200": {
                        "description": "The user",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/User"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/BadRequest"},
                    "404": {"$ref": "#/components/responses/NotFound"},
                },
            },
            "put": {
                "tags": ["users"],
                "summary": "Replace a user (full update)",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/UserCreate"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Updated user",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/User"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/BadRequest"},
                    "404": {"$ref": "#/components/responses/NotFound"},
                    "415": {"$ref": "#/components/responses/UnsupportedMediaType"},
                },
            },
            "patch": {
                "tags": ["users"],
                "summary": "Partially update a user",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/UserUpdate"}
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Updated user",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/User"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/BadRequest"},
                    "404": {"$ref": "#/components/responses/NotFound"},
                    "415": {"$ref": "#/components/responses/UnsupportedMediaType"},
                },
            },
            "delete": {
                "tags": ["users"],
                "summary": "Delete a user",
                "responses": {
                    "200": {
                        "description": "Deleted",
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/DeleteResult"}
                            }
                        },
                    },
                    "400": {"$ref": "#/components/responses/BadRequest"},
                    "404": {"$ref": "#/components/responses/NotFound"},
                },
            },
        },
    },
    "components": {
        "responses": {
            "BadRequest": {
                "description": "Malformed or invalid request.",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                    }
                },
            },
            "NotFound": {
                "description": "Resource not found.",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                    }
                },
            },
            "UnsupportedMediaType": {
                "description": "Content-Type is not application/json.",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ErrorResponse"}
                    }
                },
            },
        },
        "schemas": {
            "User": {
                "type": "object",
                "required": ["id", "name", "role"],
                "properties": {
                    "id": {"type": "integer", "minimum": 1, "example": 1},
                    "name": {"type": "string", "example": "Alice"},
                    "role": {
                        "type": "string",
                        "enum": sorted(ALLOWED_ROLES),
                        "example": "Admin",
                    },
                },
            },
            "UserCreate": {
                "type": "object",
                "required": ["name", "role"],
                "properties": {
                    "name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": MAX_NAME_LEN,
                        "example": "Alice",
                    },
                    "role": {
                        "type": "string",
                        "enum": sorted(ALLOWED_ROLES),
                        "example": "Admin",
                    },
                },
            },
            "UserUpdate": {
                "type": "object",
                "description": "Partial update; at least one of `name`/`role` must be supplied.",
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": MAX_NAME_LEN},
                    "role": {"type": "string", "enum": sorted(ALLOWED_ROLES)},
                },
            },
            "UserPage": {
                "type": "object",
                "required": ["data", "page", "per_page", "total", "total_pages"],
                "properties": {
                    "data": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/User"},
                    },
                    "page": {"type": "integer", "minimum": 1},
                    "per_page": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE},
                    "total": {"type": "integer", "minimum": 0},
                    "total_pages": {"type": "integer", "minimum": 0},
                },
            },
            "DeleteResult": {
                "type": "object",
                "required": ["deleted", "id"],
                "properties": {
                    "deleted": {"type": "boolean", "example": True},
                    "id": {"type": "integer", "example": 4},
                },
            },
            "ErrorResponse": {
                "type": "object",
                "required": ["error", "message"],
                "properties": {
                    "error": {"type": "string", "example": "Bad Request"},
                    "message": {
                        "type": "string",
                        "example": "User ID must be an integer",
                    },
                },
            },
            "Index": {
                "type": "object",
                "required": ["name", "version", "endpoints"],
                "properties": {
                    "name": {"type": "string", "example": "Mock API"},
                    "version": {"type": "string", "example": API_VERSION},
                    "endpoints": {"type": "array", "items": {"type": "string"}},
                },
            },
            "Health": {
                "type": "object",
                "required": ["status", "version", "uptime_seconds"],
                "properties": {
                    "status": {"type": "string", "example": "ok"},
                    "version": {"type": "string", "example": API_VERSION},
                    "uptime_seconds": {"type": "number", "example": 12.345},
                },
            },
            "Stats": {
                "type": "object",
                "required": [
                    "requests_total",
                    "by_method",
                    "by_path",
                    "started_at",
                    "uptime_seconds",
                    "user_count",
                    "roles",
                ],
                "properties": {
                    "requests_total": {"type": "integer", "minimum": 0},
                    "by_method": {
                        "type": "object",
                        "additionalProperties": {"type": "integer"},
                    },
                    "by_path": {
                        "type": "object",
                        "additionalProperties": {"type": "integer"},
                    },
                    "started_at": {"type": "string", "format": "date-time"},
                    "uptime_seconds": {"type": "number"},
                    "user_count": {"type": "integer", "minimum": 0},
                    "roles": {
                        "type": "object",
                        "additionalProperties": {"type": "integer"},
                    },
                },
            },
        }
    },
}


# ============================================================
# HTTP Server
# ============================================================

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each request on its own daemon thread."""

    allow_reuse_address = True
    daemon_threads = True

    def handle_error(
        self, request: socket.socket, client_address: Any
    ) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def run_server(
    port: int,
    ready_event: Optional[threading.Event] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Bind, listen, and serve until ``stop_event`` is set."""
    server = ThreadingHTTPServer(("", port), MockAPIHandler)

    if ready_event is not None:
        ready_event.set()

    log.info("Listening on http://%s:%d", HOST, port)

    if stop_event is not None:
        def _watch_stop() -> None:
            stop_event.wait()
            server.shutdown()

        threading.Thread(
            target=_watch_stop, name="server-shutdown", daemon=True
        ).start()

    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        log.info("Server stopped")


# ============================================================
# Client Helpers
# ============================================================

def api_url(port: int, path: str) -> str:
    return f"http://{HOST}:{port}{path}"


def get_users(port: int) -> requests.Response:
    return requests.get(
        api_url(port, "/api/users"), timeout=REQUEST_TIMEOUT
    )


def get_user(port: int, user_id: int) -> requests.Response:
    return requests.get(
        api_url(port, f"/api/users/{user_id}"), timeout=REQUEST_TIMEOUT
    )


def create_user(port: int, name: str, role: str) -> requests.Response:
    return requests.post(
        api_url(port, "/api/users"),
        json={"name": name, "role": role},
        timeout=REQUEST_TIMEOUT,
    )


def replace_user(port: int, user_id: int, name: str, role: str) -> requests.Response:
    return requests.put(
        api_url(port, f"/api/users/{user_id}"),
        json={"name": name, "role": role},
        timeout=REQUEST_TIMEOUT,
    )


def patch_user_api(port: int, user_id: int, **fields: Any) -> requests.Response:
    return requests.patch(
        api_url(port, f"/api/users/{user_id}"),
        json=fields,
        timeout=REQUEST_TIMEOUT,
    )


def delete_user_api(port: int, user_id: int) -> requests.Response:
    return requests.delete(
        api_url(port, f"/api/users/{user_id}"), timeout=REQUEST_TIMEOUT
    )


def print_response(response: requests.Response) -> None:
    print("Status Code:", response.status_code)
    rid = response.headers.get("X-Request-ID")
    if rid:
        print("X-Request-ID:", rid)
    try:
        print("Response:", response.json())
    except ValueError:
        print("Response:", response.text)


def list_users_as_table(api_port: int) -> None:
    try:
        response = get_users(api_port)
    except requests.RequestException as exc:
        print(f"An error occurred while fetching users: {exc}")
        return

    if response.status_code != 200:
        print(
            f"Failed to fetch users. Status code: {response.status_code}"
        )
        return

    users = response.json()
    headers = ["ID", "Name", "Role"]
    col_widths = [6, 20, 15]

    row_format = (
        "| " + " | ".join(f"{{:<{w}}}" for w in col_widths) + " |"
    )
    border = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"

    print(border)
    print(row_format.format(*headers))
    print(border)
    for user in users:
        print(
            row_format.format(
                user.get("id", ""),
                user.get("name", ""),
                user.get("role", ""),
            )
        )
    print(border)


# ============================================================
# Demo / Test Functions
# ============================================================

def test_get_existing_user(port: int) -> None:
    print("\n--- Testing GET /api/users/1 ---")
    print_response(get_user(port, 1))


def test_get_nonexistent_user(port: int) -> None:
    print("\n--- Testing GET /api/users/99 ---")
    print_response(get_user(port, 99))


def test_get_invalid_user_id(port: int) -> None:
    print("\n--- Testing GET /api/users/invalid_id ---")
    print_response(
        requests.get(
            api_url(port, "/api/users/invalid_id"),
            timeout=REQUEST_TIMEOUT,
        )
    )


def test_create_user(port: int) -> None:
    print("\n--- Testing POST /api/users ---")
    print_response(create_user(port, "Charlie", "Developer"))


def test_invalid_user(port: int) -> None:
    print("\n--- Testing POST /api/users with invalid data ---")
    print_response(create_user(port, "", "User"))


def test_invalid_role(port: int) -> None:
    print("\n--- Testing POST /api/users with disallowed role ---")
    print_response(create_user(port, "Frank", "Superuser"))


def test_missing_name(port: int) -> None:
    print("\n--- Testing POST /api/users with missing name ---")
    print_response(
        requests.post(
            api_url(port, "/api/users"),
            json={"role": "User"},
            timeout=REQUEST_TIMEOUT,
        )
    )


def test_missing_role(port: int) -> None:
    print("\n--- Testing POST /api/users with missing role ---")
    print_response(
        requests.post(
            api_url(port, "/api/users"),
            json={"name": "Eve"},
            timeout=REQUEST_TIMEOUT,
        )
    )


def test_invalid_json(port: int) -> None:
    print("\n--- Testing POST /api/users with invalid JSON ---")
    print_response(
        requests.post(
            api_url(port, "/api/users"),
            data="{not valid json",
            headers={"Content-Type": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
    )


def test_unsupported_media_type(port: int) -> None:
    print("\n--- Testing POST /api/users with wrong Content-Type ---")
    print_response(
        requests.post(
            api_url(port, "/api/users"),
            data="name=Grace&role=User",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        )
    )


def test_replace_user(port: int) -> None:
    print("\n--- Testing PUT /api/users/2 ---")
    print_response(replace_user(port, 2, "Bobby", "Maintainer"))


def test_patch_user(port: int) -> None:
    print("\n--- Testing PATCH /api/users/1 (role only) ---")
    print_response(patch_user_api(port, 1, role="Developer"))


def test_delete_user(port: int) -> None:
    # Create a throwaway user so we don't disturb the seeded data.
    created = create_user(port, "Temporary", "Guest").json()
    uid = created["id"]
    print(f"\n--- Testing DELETE /api/users/{uid} ---")
    print_response(delete_user_api(port, uid))


def test_method_not_allowed(port: int) -> None:
    # DELETE on the *collection* is not defined -> 405.
    print("\n--- Testing DELETE /api/users (method not allowed) ---")
    print_response(
        requests.delete(api_url(port, "/api/users"), timeout=REQUEST_TIMEOUT)
    )


def test_pagination(port: int) -> None:
    print("\n--- Testing GET /api/users?page=1&per_page=1 ---")
    print_response(
        requests.get(
            api_url(port, "/api/users"),
            params={"page": 1, "per_page": 1},
            timeout=REQUEST_TIMEOUT,
        )
    )


def test_filter_role(port: int) -> None:
    print("\n--- Testing GET /api/users?role=Admin ---")
    print_response(
        requests.get(
            api_url(port, "/api/users"),
            params={"role": "Admin"},
            timeout=REQUEST_TIMEOUT,
        )
    )


def test_search(port: int) -> None:
    print("\n--- Testing GET /api/users?q=ali ---")
    print_response(
        requests.get(
            api_url(port, "/api/users"),
            params={"q": "ali"},
            timeout=REQUEST_TIMEOUT,
        )
    )


def test_health(port: int) -> None:
    print("\n--- Testing GET /health ---")
    print_response(
        requests.get(api_url(port, "/health"), timeout=REQUEST_TIMEOUT)
    )


def test_stats(port: int) -> None:
    print("\n--- Testing GET /api/stats ---")
    print_response(
        requests.get(api_url(port, "/api/stats"), timeout=REQUEST_TIMEOUT)
    )


def test_openapi(port: int) -> None:
    print("\n--- Testing GET /openapi.json ---")
    print_response(
        requests.get(api_url(port, "/openapi.json"), timeout=REQUEST_TIMEOUT)
    )


def test_request_id_echo(port: int) -> None:
    print("\n--- Testing X-Request-ID echo ---")
    custom = "test-trace-123"
    response = requests.get(
        api_url(port, "/health"),
        headers={"X-Request-ID": custom},
        timeout=REQUEST_TIMEOUT,
    )
    print("Sent X-Request-ID:", custom)
    print("Status Code:", response.status_code)
    print("Echoed X-Request-ID:", response.headers.get("X-Request-ID"))


def test_head(port: int) -> None:
    print("\n--- Testing HEAD /api/users/1 ---")
    response = requests.head(
        api_url(port, "/api/users/1"), timeout=REQUEST_TIMEOUT
    )
    print("Status Code:", response.status_code)
    print("Content-Type:", response.headers.get("Content-Type"))
    print("Content-Length:", response.headers.get("Content-Length"))
    print("X-Request-ID:", response.headers.get("X-Request-ID"))
    print("Body empty:", len(response.content) == 0)


# ============================================================
# Automated Assertions
# ============================================================

def assert_status(
    response: requests.Response, expected: int
) -> None:
    assert response.status_code == expected, (
        f"Expected {expected}, got {response.status_code}: "
        f"{response.text!r}"
    )


def run_assertion_tests(port: int) -> None:
    print("\n--- Running automated API assertions ---")

    # --- GET list (bare-list backward compatibility) ---
    response = get_users(port)
    assert_status(response, 200)
    assert isinstance(response.json(), list)

    # --- GET single (existing) ---
    response = get_user(port, 1)
    assert_status(response, 200)
    assert response.json()["name"] == "Alice"

    # --- GET single (missing) ---
    response = get_user(port, 9999)
    assert_status(response, 404)

    # --- GET single (bad id) ---
    response = requests.get(
        api_url(port, "/api/users/not-an-int"),
        timeout=REQUEST_TIMEOUT,
    )
    assert_status(response, 400)

    # --- POST create ---
    response = create_user(port, "Test User", "Maintainer")
    assert_status(response, 201)
    assert response.json()["name"] == "Test User"
    created_id = response.json()["id"]

    # --- POST validation: empty name ---
    response = create_user(port, "", "Tester")
    assert_status(response, 400)

    # --- POST validation: disallowed role ---
    response = create_user(port, "Nobody", "Wizard")
    assert_status(response, 400)

    # --- POST validation: missing role ---
    response = requests.post(
        api_url(port, "/api/users"),
        json={"name": "Alone"},
        timeout=REQUEST_TIMEOUT,
    )
    assert_status(response, 400)

    # --- POST validation: invalid JSON ---
    response = requests.post(
        api_url(port, "/api/users"),
        data="{broken",
        headers={"Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    assert_status(response, 400)

    # --- POST validation: wrong Content-Type -> 415 ---
    response = requests.post(
        api_url(port, "/api/users"),
        data="name=x&role=User",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=REQUEST_TIMEOUT,
    )
    assert_status(response, 415)

    # --- PUT replace ---
    response = replace_user(port, created_id, "Renamed", "User")
    assert_status(response, 200)
    assert response.json()["name"] == "Renamed"
    assert response.json()["role"] == "User"

    # --- PUT on missing user ---
    response = replace_user(port, 9999, "Ghost", "User")
    assert_status(response, 404)

    # --- PUT with bad id ---
    response = requests.put(
        api_url(port, "/api/users/abc"),
        json={"name": "X", "role": "User"},
        timeout=REQUEST_TIMEOUT,
    )
    assert_status(response, 400)

    # --- PATCH partial ---
    response = patch_user_api(port, created_id, role="Admin")
    assert_status(response, 200)
    assert response.json()["role"] == "Admin"
    assert response.json()["name"] == "Renamed"  # unchanged

    # --- PATCH with no updatable fields ---
    response = requests.patch(
        api_url(port, f"/api/users/{created_id}"),
        json={"id": 99},
        timeout=REQUEST_TIMEOUT,
    )
    assert_status(response, 400)

    # --- PATCH on missing user ---
    response = patch_user_api(port, 9999, role="Admin")
    assert_status(response, 404)

    # --- DELETE ---
    response = delete_user_api(port, created_id)
    assert_status(response, 200)
    assert response.json()["deleted"] is True
    # Confirm gone.
    response = get_user(port, created_id)
    assert_status(response, 404)

    # --- DELETE missing ---
    response = delete_user_api(port, 9999)
    assert_status(response, 404)

    # --- Method not allowed (DELETE on collection) ---
    response = requests.delete(
        api_url(port, "/api/users"), timeout=REQUEST_TIMEOUT
    )
    assert_status(response, 405)
    assert "GET" in response.headers.get("Allow", "")
    assert "POST" in response.headers.get("Allow", "")

    # --- Pagination envelope ---
    response = requests.get(
        api_url(port, "/api/users"),
        params={"page": 1, "per_page": 1},
        timeout=REQUEST_TIMEOUT,
    )
    assert_status(response, 200)
    body = response.json()
    assert "data" in body and "total" in body and "total_pages" in body
    assert len(body["data"]) <= 1

    # --- Pagination out of range ---
    response = requests.get(
        api_url(port, "/api/users"),
        params={"page": 0},
        timeout=REQUEST_TIMEOUT,
    )
    assert_status(response, 400)

    # --- Filter by role ---
    response = requests.get(
        api_url(port, "/api/users"),
        params={"role": "Admin"},
        timeout=REQUEST_TIMEOUT,
    )
    assert_status(response, 200)
    assert all(u["role"] == "Admin" for u in response.json())

    # --- Health ---
    response = requests.get(api_url(port, "/health"), timeout=REQUEST_TIMEOUT)
    assert_status(response, 200)
    assert response.json()["status"] == "ok"
    assert response.json()["version"] == API_VERSION

    # --- Stats ---
    response = requests.get(api_url(port, "/api/stats"), timeout=REQUEST_TIMEOUT)
    assert_status(response, 200)
    assert response.json()["requests_total"] > 0
    assert "user_count" in response.json()

    # --- OpenAPI ---
    response = requests.get(api_url(port, "/openapi.json"), timeout=REQUEST_TIMEOUT)
    assert_status(response, 200)
    assert response.json()["openapi"].startswith("3.")

    # --- X-Request-ID echo ---
    custom = "abc-echo-xyz"
    response = requests.get(
        api_url(port, "/health"),
        headers={"X-Request-ID": custom},
        timeout=REQUEST_TIMEOUT,
    )
    assert response.headers.get("X-Request-ID") == custom

    # --- X-Request-ID auto-generated ---
    response = get_users(port)
    assert response.headers.get("X-Request-ID")  # present and truthy

    # --- HEAD mirrors GET with no body ---
    response = requests.head(
        api_url(port, "/api/users/1"), timeout=REQUEST_TIMEOUT
    )
    assert_status(response, 200)
    assert response.headers.get("Content-Type") == "application/json"
    assert int(response.headers.get("Content-Length", "0")) > 0
    assert response.content == b""

    # --- OPTIONS advertises allowed methods ---
    response = requests.options(
        api_url(port, "/api/users"), timeout=REQUEST_TIMEOUT
    )
    assert_status(response, 204)
    allow = response.headers.get("Allow", "")
    assert "GET" in allow and "POST" in allow

    print("All automated API assertions passed.")


# ============================================================
# CLI / Entry Point
# ============================================================

def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Local mock HTTP API simulator.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Port to listen on. Defaults to MOCK_API_PORT, "
            f"then {DEFAULT_PORT}."
        ),
    )
    parser.add_argument(
        "--no-tests",
        action="store_true",
        help="Start the server without running the demo suite.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    return parser.parse_args(argv)


def _resolve_port(cli_port: Optional[int]) -> int:
    if cli_port is not None:
        return cli_port
    env = os.environ.get("MOCK_API_PORT")
    if env:
        try:
            return int(env)
        except ValueError as exc:
            raise SystemExit(
                f"MOCK_API_PORT must be an integer, got {env!r}"
            ) from exc
    return DEFAULT_PORT


def _run_demo(port: int) -> None:
    print("=" * 60)
    print("MOCK API TEST")
    print("=" * 60)

    test_get_existing_user(port)
    test_get_nonexistent_user(port)
    test_get_invalid_user_id(port)

    print("\n--- Testing GET /api/users ---")
    list_users_as_table(port)

    test_create_user(port)

    print("\n--- Users After Creating Charlie ---")
    list_users_as_table(port)

    test_invalid_user(port)
    test_invalid_role(port)
    test_missing_name(port)
    test_missing_role(port)
    test_invalid_json(port)
    test_unsupported_media_type(port)

    # New CRUD demos.
    test_replace_user(port)
    test_patch_user(port)
    test_delete_user(port)

    # New query/feature demos.
    test_pagination(port)
    test_filter_role(port)
    test_search(port)
    test_health(port)
    test_stats(port)
    test_openapi(port)
    test_request_id_echo(port)
    test_head(port)

    test_method_not_allowed(port)

    print("\n--- Final User List ---")
    list_users_as_table(port)

    run_assertion_tests(port)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    reset_database()
    _reset_stats()
    port = _resolve_port(args.port)

    ready = threading.Event()
    stop = threading.Event()

    server_thread = threading.Thread(
        target=run_server,
        args=(port, ready, stop),
        name="mock-api-server",
        daemon=True,
    )
    server_thread.start()

    if not ready.wait(timeout=5):
        log.error("Server failed to start within 5 seconds")
        return 1

    if not args.no_tests:
        try:
            _run_demo(port)
        except Exception:
            log.exception("Demo suite crashed")
            stop.set()
            server_thread.join(timeout=5)
            return 2

    log.info(
        "API simulator is running at http://%s:%d (Ctrl+C to stop)",
        HOST,
        port,
    )

    try:
        server_thread.join()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        stop.set()
        server_thread.join(timeout=5)

    return 0


if __name__ == "__main__":
    sys.exit(main())
