"""
Mock API Simulator
==================

A self-contained local HTTP API simulator using only Python's standard library
plus the external ``requests`` package for the client-side tests.

Endpoints
---------
GET  /api/users
GET  /api/users/<id>
POST /api/users

By default the server listens on port 4221. Override with ``--port`` or the
``MOCK_API_PORT`` environment variable.

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
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Optional
from urllib.parse import urlsplit

import requests


# ============================================================
# Configuration
# ============================================================

HOST = "localhost"
DEFAULT_PORT = 4221
REQUEST_TIMEOUT = 5
MAX_BODY_BYTES = 1_000_000  # 1 MB cap on incoming request bodies

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


# ============================================================
# API Request Handler
# ============================================================

class MockAPIHandler(BaseHTTPRequestHandler):
    """Routes the mock API. Each method must write exactly one response."""

    protocol_version = "HTTP/1.1"
    server_version = "MockAPI/1.1"

    # ---------------- response helpers ----------------

    def _send_json(self, payload: Any, status_code: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
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
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    # ---------------- request parsing ----------------

    def _get_path(self) -> str:
        return urlsplit(self.path).path

    def _read_json_object(self) -> dict[str, Any]:
        """Read and validate the JSON body. Raises ValueError on any problem."""
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

    # ---------------- routes ----------------

    def do_GET(self) -> None:
        path = self._get_path()
        try:
            if path == "/api/users":
                self._send_json(list_users(), 200)
                return

            if path.startswith("/api/users/"):
                suffix = path[len("/api/users/"):]
                try:
                    user_id = int(suffix)
                except ValueError:
                    self._send_error_json(
                        400, "Bad Request", "User ID must be an integer"
                    )
                    return

                user = get_user_by_id(user_id)
                if user is None:
                    self._send_error_json(
                        404,
                        "Not Found",
                        f"User with ID {user_id} not found",
                    )
                    return
                self._send_json(user, 200)
                return

            self._send_error_json(404, "Not Found", f"No route for {path}")
        except Exception:  # pragma: no cover - defensive
            log.exception("Unhandled error in do_GET")
            self._send_error_json(
                500, "Internal Server Error", "Unexpected server error"
            )

    def do_POST(self) -> None:
        path = self._get_path()
        if path != "/api/users":
            self._send_error_json(404, "Not Found", f"No route for {path}")
            return

        try:
            payload = self._read_json_object()
            name = payload.get("name")
            role = payload.get("role")

            if not isinstance(name, str) or not name.strip():
                self._send_error_json(
                    400,
                    "Invalid input",
                    "'name' is required and must be a non-empty string",
                )
                return
            if not isinstance(role, str) or not role.strip():
                self._send_error_json(
                    400,
                    "Invalid input",
                    "'role' is required and must be a non-empty string",
                )
                return

            created = add_user(name.strip(), role.strip())
            self._send_json(created, 201)
        except ValueError as exc:
            self._send_error_json(400, "Bad Request", str(exc))
        except Exception:  # pragma: no cover - defensive
            log.exception("Unhandled error in do_POST")
            self._send_error_json(
                500, "Internal Server Error", "Unexpected server error"
            )

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed("GET", "POST")

    do_DELETE = do_PATCH = do_PUT  # type: ignore[assignment]

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)


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


def print_response(response: requests.Response) -> None:
    print("Status Code:", response.status_code)
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


def test_unsupported_method(port: int) -> None:
    print("\n--- Testing DELETE /api/users/1 (method not allowed) ---")
    print_response(
        requests.delete(
            api_url(port, "/api/users/1"), timeout=REQUEST_TIMEOUT
        )
    )


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

    response = get_users(port)
    assert_status(response, 200)
    assert isinstance(response.json(), list)

    response = get_user(port, 1)
    assert_status(response, 200)
    assert response.json()["name"] == "Alice"

    response = get_user(port, 9999)
    assert_status(response, 404)

    response = requests.get(
        api_url(port, "/api/users/not-an-int"),
        timeout=REQUEST_TIMEOUT,
    )
    assert_status(response, 400)

    response = create_user(port, "Test User", "Tester")
    assert_status(response, 201)
    assert response.json()["name"] == "Test User"

    response = create_user(port, "", "Tester")
    assert_status(response, 400)

    # New: unsupported method returns 405 with an Allow header.
    response = requests.delete(
        api_url(port, "/api/users/1"), timeout=REQUEST_TIMEOUT
    )
    assert_status(response, 405)
    assert "GET" in response.headers.get("Allow", "")

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
    test_missing_name(port)
    test_missing_role(port)
    test_invalid_json(port)
    test_unsupported_method(port)

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
