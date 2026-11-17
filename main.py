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
# API Request Handler
# ============================================================

class MockAPIHandler(BaseHTTPRequestHandler):

    def _set_headers(self, status_code=200, content_type="application/json"):
        """Send HTTP response headers."""
        self.send_response(status_code)
        self.send_header("Content-Type", content_type)
        self.end_headers()

    # --------------------------------------------------------
    # GET Requests
    # --------------------------------------------------------

    def do_GET(self):
        """Handle GET requests."""

        # GET /api/users
        if self.path == "/api/users":
            self._set_headers(200)

            response = json.dumps(Mock_DB["users"]).encode("utf-8")
            self.wfile.write(response)

        # GET /api/users/<id>
        elif self.path.startswith("/api/users/"):

            # Extract the ID from the URL path
            parts = self.path.split("/")

            try:
                user_id = int(parts[-1])

                user = next(
                    (u for u in Mock_DB["users"] if u["id"] == user_id),
                    None
                )

                if user:
                    self._set_headers(200)

                    response = json.dumps(user).encode("utf-8")
                    self.wfile.write(response)

                else:
                    self._set_headers(404)

                    response = {
                        "error": "Not Found",
                        "message": f"User with ID {user_id} not found",
                    }

                    self.wfile.write(
                        json.dumps(response).encode("utf-8")
                    )

            except ValueError:
                self._set_headers(400)

                response = {
                    "error": "Bad Request",
                    "message": "User ID must be an integer",
                }

                self.wfile.write(
                    json.dumps(response).encode("utf-8")
                )

        # Unknown endpoint
        else:
            self._set_headers(404)

            response = {
                "error": "Not Found"
            }

            self.wfile.write(
                json.dumps(response).encode("utf-8")
            )

    # --------------------------------------------------------
    # POST Requests
    # --------------------------------------------------------

    def do_POST(self):
        """Handle POST requests."""

        # POST /api/users
        if self.path == "/api/users":

            try:
                # Get request body size
                content_length = int(
                    self.headers.get("Content-Length", 0)
                )

                # Read request body
                post_data = self.rfile.read(content_length)

                # Parse JSON
                new_user = json.loads(
                    post_data.decode("utf-8")
                )

                # Basic Input Validation
                if not isinstance(new_user, dict):
                    raise ValueError(
                        "Request body must be a JSON object"
                    )

                # Validate name
                if (
                    "name" not in new_user
                    or not isinstance(new_user["name"], str)
                    or not new_user["name"].strip()
                ):
                    self._set_headers(400)

                    response = {
                        "error": "Invalid input",
                        "message": (
                            "'name' is required and must be "
                            "a non-empty string"
                        ),
                    }

                    self.wfile.write(
                        json.dumps(response).encode("utf-8")
                    )

                    return

                # Validate role
                if (
                    "role" not in new_user
                    or not isinstance(new_user["role"], str)
                    or not new_user["role"].strip()
                ):
                    self._set_headers(400)

                    response = {
                        "error": "Invalid input",
                        "message": (
                            "'role' is required and must be "
                            "a non-empty string"
                        ),
                    }

                    self.wfile.write(
                        json.dumps(response).encode("utf-8")
                    )

                    return

                # Sanitize and add new user
                validated_user = {
                    "id": len(Mock_DB["users"]) + 1,
                    "name": new_user["name"].strip(),
                    "role": new_user["role"].strip(),
                }

                Mock_DB["users"].append(validated_user)

                # Return newly created user
                self._set_headers(201)

                self.wfile.write(
                    json.dumps(validated_user).encode("utf-8")
                )

            except json.JSONDecodeError as e:
                self._set_headers(400)

                response = {
                    "error": "Invalid JSON",
                    "message": str(e),
                }

                self.wfile.write(
                    json.dumps(response).encode("utf-8")
                )

            except Exception as e:
                self._set_headers(400)

                response = {
                    "error": "Invalid JSON or structure",
                    "message": str(e),
                }

                self.wfile.write(
                    json.dumps(response).encode("utf-8")
                )

        # Unknown endpoint
        else:
            self._set_headers(404)

            response = {
                "error": "Not Found"
            }

            self.wfile.write(
                json.dumps(response).encode("utf-8")
            )

    # --------------------------------------------------------
    # Disable Default HTTP Server Logging
    # --------------------------------------------------------

    def log_message(self, format, *args):
        """Suppress default HTTP server logging."""
        return


# ============================================================
# HTTP Server
# ============================================================

class ThreadedHTTPServer(HTTPServer):
    """
    HTTP server that allows fast re-binding of the port.
    """

    allow_reuse_address = True


# ============================================================
# Find a Free TCP Port
# ============================================================

def get_free_port():
    """Find and return an available TCP port."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        port = s.getsockname()[1]

    return port


# ============================================================
# Run API Server
# ============================================================

def run_server(port):
    """Start the mock API server."""

    server_address = ("", port)

    httpd = ThreadedHTTPServer(
        server_address,
        MockAPIHandler
    )

    print(f"Starting API Simulator on port {port}...")
    print(f"API Base URL: http://localhost:{port}")
    print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping API server...")
    finally:
        httpd.server_close()


# ============================================================
# List Users as a Table
# ============================================================

def list_users_as_table(api_port):
    """Fetch users from the API and display them as a table."""

    try:
        response = requests.get(
            f"http://localhost:{api_port}/api/users",
            timeout=5
        )

        if response.status_code == 200:

            users = response.json()

            # Define headers and column widths
            headers = ["ID", "Name", "Role"]
            col_widths = [6, 20, 15]

            # Create format string
            row_format = (
                "| "
                + " | ".join(
                    f"{{:<{width}}}"
                    for width in col_widths
                )
                + " |"
            )

            border = (
                "+"
                + "+".join(
                    "-" * (width + 2)
                    for width in col_widths
                )
                + "+"
            )

            # Print table
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

        else:
            print(
                f"Failed to fetch users. "
                f"Status code: {response.status_code}"
            )

    except requests.RequestException as e:
        print(f"An error occurred while fetching users: {e}")


# ============================================================
# Test GET /api/users/<id>
# ============================================================

def test_get_existing_user(port):
    print("\n--- Testing GET /api/users/1 (Existing User) ---")

    response = requests.get(
        f"http://localhost:{port}/api/users/1",
        timeout=5
    )

    print("Status Code:", response.status_code)
    print("Response:", response.json())


def test_get_nonexistent_user(port):
    print("\n--- Testing GET /api/users/99 (Non-existent User) ---")

    response = requests.get(
        f"http://localhost:{port}/api/users/99",
        timeout=5
    )

    print("Status Code:", response.status_code)
    print("Response:", response.json())


def test_get_invalid_user_id(port):
    print("\n--- Testing GET /api/users/invalid_id (Bad Request) ---")

    response = requests.get(
        f"http://localhost:{port}/api/users/invalid_id",
        timeout=5
    )

    print("Status Code:", response.status_code)
    print("Response:", response.json())


# ============================================================
# Test POST /api/users
# ============================================================

def test_create_user(port):
    print("\n--- Testing POST /api/users (Create User) ---")

    new_user = {
        "name": "Charlie",
        "role": "Developer",
    }

    response = requests.post(
        f"http://localhost:{port}/api/users",
        json=new_user,
        timeout=5
    )

    print("Status Code:", response.status_code)
    print("Response:", response.json())


def test_invalid_user(port):
    print("\n--- Testing POST /api/users (Invalid User) ---")

    invalid_user = {
        "name": "",
        "role": "User",
    }

    response = requests.post(
        f"http://localhost:{port}/api/users",
        json=invalid_user,
        timeout=5
    )

    print("Status Code:", response.status_code)
    print("Response:", response.json())


# ============================================================
# Main Program
# ============================================================

def main():
    # Dynamically find a free port
    port = get_free_port()

    # Start API server in background thread
    thread = threading.Thread(
        target=run_server,
        args=(port,),
        daemon=True,
    )

    thread.start()

    # Wait a moment for the server to start
    time.sleep(1)

    print("=" * 60)
    print("MOCK API TEST")
    print("=" * 60)

    # Run GET tests
    test_get_existing_user(port)
    test_get_nonexistent_user(port)
    test_get_invalid_user_id(port)

    # Display all users
    print("\n--- Testing GET /api/users ---")
    list_users_as_table(port)

    # Run POST tests
    test_create_user(port)

    # Display users after POST
    print("\n--- Users After Creating Charlie ---")
    list_users_as_table(port)

    # Test invalid POST request
    test_invalid_user(port)

    print("\n" + "=" * 60)
    print("API simulator is still running.")
    print(f"Base URL: http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    print("=" * 60)

    # Keep main program alive while the daemon server runs
    try:
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\nShutting down client...")


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    main()
