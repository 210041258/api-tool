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

This file is intentionally verbose and contains extensive documentation,
validation helpers, testing helpers, and examples so that it can also serve
as a learning/reference file.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests


# ============================================================
# Mock Database
# ============================================================

Mock_DB = {
    "users": [
        {"id": 1, "name": "Alice", "role": "Admin"},
        {"id": 2, "name": "Bob", "role": "User"},
    ]
}


# ============================================================
# Configuration
# ============================================================

HOST = "localhost"
REQUEST_TIMEOUT = 5


# ============================================================
# API Request Handler
# ============================================================

class MockAPIHandler(BaseHTTPRequestHandler):

    def _set_headers(self, status_code=200, content_type="application/json"):
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.end_headers()

    def _send_json(self, payload, status_code=200):
        self._set_headers(status_code)
        body = json.dumps(payload).encode("utf-8")
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/users":
            self._send_json(Mock_DB["users"], 200)
            return

        if self.path.startswith("/api/users/"):
            parts = self.path.split("/")

            try:
                user_id = int(parts[-1])
            except ValueError:
                self._send_json(
                    {
                        "error": "Bad Request",
                        "message": "User ID must be an integer",
                    },
                    400,
                )
                return

            user = next(
                (u for u in Mock_DB["users"] if u["id"] == user_id),
                None,
            )

            if user is None:
                self._send_json(
                    {
                        "error": "Not Found",
                        "message": f"User with ID {user_id} not found",
                    },
                    404,
                )
                return

            self._send_json(user, 200)
            return

        self._send_json({"error": "Not Found"}, 404)

    def do_POST(self):
        if self.path != "/api/users":
            self._send_json({"error": "Not Found"}, 404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)
            new_user = json.loads(post_data.decode("utf-8"))

            if not isinstance(new_user, dict):
                raise ValueError("Request body must be a JSON object")

            name = new_user.get("name")
            role = new_user.get("role")

            if not isinstance(name, str) or not name.strip():
                self._send_json(
                    {
                        "error": "Invalid input",
                        "message": (
                            "'name' is required and must be "
                            "a non-empty string"
                        ),
                    },
                    400,
                )
                return

            if not isinstance(role, str) or not role.strip():
                self._send_json(
                    {
                        "error": "Invalid input",
                        "message": (
                            "'role' is required and must be "
                            "a non-empty string"
                        ),
                    },
                    400,
                )
                return

            validated_user = {
                "id": next_user_id(),
                "name": name.strip(),
                "role": role.strip(),
            }

            Mock_DB["users"].append(validated_user)
            self._send_json(validated_user, 201)

        except json.JSONDecodeError as exc:
            self._send_json(
                {"error": "Invalid JSON", "message": str(exc)},
                400,
            )

        except Exception as exc:
            self._send_json(
                {
                    "error": "Invalid JSON or structure",
                    "message": str(exc),
                },
                400,
            )

    def log_message(self, format, *args):
        return


# ============================================================
# Database Helpers
# ============================================================

def next_user_id():
    if not Mock_DB["users"]:
        return 1
    return max(user["id"] for user in Mock_DB["users"]) + 1


def reset_database():
    Mock_DB["users"].clear()
    Mock_DB["users"].extend(
        [
            {"id": 1, "name": "Alice", "role": "Admin"},
            {"id": 2, "name": "Bob", "role": "User"},
        ]
    )


def get_user_by_id(user_id):
    return next(
        (user for user in Mock_DB["users"] if user["id"] == user_id),
        None,
    )


# ============================================================
# HTTP Server
# ============================================================

class ThreadedHTTPServer(HTTPServer):
    allow_reuse_address = True


def get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("", 0))
        return sock.getsockname()[1]


def run_server(port, ready_event=None):
    server_address = ("", port)
    httpd = ThreadedHTTPServer(server_address, MockAPIHandler)

    if ready_event is not None:
        ready_event.set()

    print(f"Starting API Simulator on port {port}...")
    print(f"API Base URL: http://{HOST}:{port}")

    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


# ============================================================
# Client Helpers
# ============================================================

def api_url(port, path):
    return f"http://{HOST}:{port}{path}"


def get_users(port):
    response = requests.get(
        api_url(port, "/api/users"),
        timeout=REQUEST_TIMEOUT,
    )
    return response


def get_user(port, user_id):
    response = requests.get(
        api_url(port, f"/api/users/{user_id}"),
        timeout=REQUEST_TIMEOUT,
    )
    return response


def create_user(port, name, role):
    response = requests.post(
        api_url(port, "/api/users"),
        json={"name": name, "role": role},
        timeout=REQUEST_TIMEOUT,
    )
    return response


def print_response(response):
    print("Status Code:", response.status_code)
    try:
        print("Response:", response.json())
    except ValueError:
        print("Response:", response.text)


def list_users_as_table(api_port):
    try:
        response = get_users(api_port)

        if response.status_code != 200:
            print(
                f"Failed to fetch users. "
                f"Status code: {response.status_code}"
            )
            return

        users = response.json()
        headers = ["ID", "Name", "Role"]
        col_widths = [6, 20, 15]

        row_format = (
            "| "
            + " | ".join(
                f"{{:<{width}}}" for width in col_widths
            )
            + " |"
        )

        border = (
            "+"
            + "+".join("-" * (width + 2) for width in col_widths)
            + "+"
        )

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

    except requests.RequestException as exc:
        print(f"An error occurred while fetching users: {exc}")


# ============================================================
# Test Functions
# ============================================================

def test_get_existing_user(port):
    print("\n--- Testing GET /api/users/1 ---")
    print_response(get_user(port, 1))


def test_get_nonexistent_user(port):
    print("\n--- Testing GET /api/users/99 ---")
    print_response(get_user(port, 99))


def test_get_invalid_user_id(port):
    print("\n--- Testing GET /api/users/invalid_id ---")
    print_response(
        requests.get(
            api_url(port, "/api/users/invalid_id"),
            timeout=REQUEST_TIMEOUT,
        )
    )


def test_create_user(port):
    print("\n--- Testing POST /api/users ---")
    print_response(create_user(port, "Charlie", "Developer"))


def test_invalid_user(port):
    print("\n--- Testing POST /api/users with invalid data ---")
    print_response(create_user(port, "", "User"))


def test_missing_name(port):
    print("\n--- Testing POST /api/users with missing name ---")
    response = requests.post(
        api_url(port, "/api/users"),
        json={"role": "User"},
        timeout=REQUEST_TIMEOUT,
    )
    print_response(response)


def test_missing_role(port):
    print("\n--- Testing POST /api/users with missing role ---")
    response = requests.post(
        api_url(port, "/api/users"),
        json={"name": "Eve"},
        timeout=REQUEST_TIMEOUT,
    )
    print_response(response)


def test_invalid_json(port):
    print("\n--- Testing POST /api/users with invalid JSON ---")
    response = requests.post(
        api_url(port, "/api/users"),
        data="{not valid json",
        headers={"Content-Type": "application/json"},
        timeout=REQUEST_TIMEOUT,
    )
    print_response(response)


# ============================================================
# Automated Assertions
# ============================================================

def assert_status(response, expected):
    assert response.status_code == expected, (
        f"Expected {expected}, got {response.status_code}"
    )


def run_assertion_tests(port):
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

    print("All automated API assertions passed.")


# ============================================================
# Main Program
# ============================================================

def main():
    reset_database()

    port = get_free_port()
    ready_event = threading.Event()

    thread = threading.Thread(
        target=run_server,
        args=(port, ready_event),
        daemon=True,
    )
    thread.start()

    if not ready_event.wait(timeout=5):
        raise RuntimeError("API server failed to start.")

    time.sleep(0.1)

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

    print("\n--- Final User List ---")
    list_users_as_table(port)

    run_assertion_tests(port)

    print("\n" + "=" * 60)
    print("API simulator is still running.")
    print(f"Base URL: http://{HOST}:{port}")
    print("Press Ctrl+C to stop.")
    print("=" * 60)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down client...")


if __name__ == "__main__":
    main()
