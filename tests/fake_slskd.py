"""A stand-in slskd that behaves like the real one, including its cruelties.

This is not a mock in the usual sense. It is a real HTTP server, spoken to over
a real socket by the real client, and it enforces the two rules that actually
bit in use:

1.  ``searchTimeout`` is in MILLISECONDS. slskd's own documentation says
    seconds, but the value is handed straight to Soulseek.NET, whose
    ``SearchOptions(int searchTimeout = 15000)`` becomes a timer interval in
    milliseconds. A search asked to last 15 ends 15 thousandths of a second
    later, before a single peer can answer, and returns nothing at all. This
    server reproduces that exactly, so a regression to seconds shows up as
    empty results rather than as a passing test.

2.  The Soulseek server blocks an account that searches too quickly. Here the
    block is a refusal to answer and a connection reported as lost, which is
    how it appears from the outside.

Responses arrive gradually, as they do on a real network.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "fake-slskd-token"


class FakeSoulseek:
    """The state behind the server: searches, transfers and the block."""

    def __init__(
        self,
        *,
        rate_limit: int = 8,
        rate_window: float = 2.0,
        block_seconds: float = 30.0,
        responses_per_search: int = 3,
        first_response_after: float = 0.12,
    ):
        self.lock = threading.RLock()
        self.rate_limit = rate_limit
        self.rate_window = rate_window
        self.block_seconds = block_seconds
        self.responses_per_search = responses_per_search
        self.first_response_after = first_response_after

        self.searches: dict[str, dict[str, Any]] = {}
        self.transfers: dict[tuple[str, str], dict[str, Any]] = {}
        self.search_starts: list[float] = []
        self.blocked_until = 0.0
        self.block_count = 0
        self.started_searches = 0
        self.rejected_searches = 0
        #: Every searchTimeout value received, so a test can assert the units.
        self.timeouts_seen: list[int] = []
        #: Destinations refused for not being a relative path.
        self.rejected_destinations: list[str] = []
        #: Destinations accepted, so a test can check what was actually sent.
        self.destinations_seen: list[str | None] = []
        #: slskd allows only two enqueue requests at once and answers the rest
        #: with 429 straight away. Reproduced here, along with how long each
        #: one takes, because that is what makes the third request collide.
        self.enqueue_slots = threading.Semaphore(2)
        self.enqueue_duration = 0.25
        self.enqueue_429s = 0

    # -- the Soulseek server's temper --------------------------------------

    def connected(self) -> bool:
        with self.lock:
            return time.monotonic() >= self.blocked_until

    def _note_search_start(self) -> bool:
        """Record a search, and decide whether the server has had enough."""
        now = time.monotonic()
        with self.lock:
            self.search_starts = [t for t in self.search_starts if now - t < self.rate_window]
            if len(self.search_starts) >= self.rate_limit:
                self.blocked_until = now + self.block_seconds
                self.block_count += 1
                self.search_starts.clear()
                return False
            self.search_starts.append(now)
            return True

    # -- searches ----------------------------------------------------------

    def start_search(self, body: dict[str, Any]) -> tuple[int, Any]:
        timeout_ms = int(body.get("searchTimeout") or 0)
        with self.lock:
            self.timeouts_seen.append(timeout_ms)

        if not self.connected():
            return 409, (
                "The server connection must be connected and logged in to "
                "perform a search (currently: Disconnected)"
            )
        if not self._note_search_start():
            with self.lock:
                self.rejected_searches += 1
            return 409, (
                "The server connection must be connected and logged in to "
                "perform a search (currently: Disconnected)"
            )

        search_id = str(body.get("id") or uuid.uuid4())
        text = str(body.get("searchText") or "")
        now = time.monotonic()
        with self.lock:
            self.started_searches += 1
            self.searches[search_id] = {
                "id": search_id,
                "searchText": text,
                "startedAt": now,
                # Milliseconds, exactly like the real thing.
                "endsAt": now + (timeout_ms / 1000.0),
                "state": "InProgress",
                "responses": self._build_responses(text),
            }
        return 200, self._state_of(search_id)

    def _build_responses(self, text: str) -> list[dict[str, Any]]:
        out = []
        for n in range(self.responses_per_search):
            out.append(
                {
                    "username": f"peer{n}",
                    "fileCount": 1,
                    "hasFreeUploadSlot": n == 0,
                    "queueLength": n * 3,
                    "uploadSpeed": 100000 - n,
                    "files": [
                        {
                            "filename": f"music\\{text[:40]}\\0{n} {text[:30]}.mp3",
                            "size": 7_000_000 + n,
                            "extension": ".mp3",
                            "bitRate": 320 if n == 0 else 192,
                            "length": 240,
                            "sampleRate": 44100,
                            "bitDepth": 16,
                            "isVariableBitRate": False,
                        }
                    ],
                    "lockedFileCount": 0,
                    "lockedFiles": [],
                }
            )
        return out

    def _visible_responses(self, search: dict[str, Any]) -> list[dict[str, Any]]:
        """Responses trickle in, and nothing arrives after the search ends.

        The cutoff is the earlier of now and the search's own deadline, so a
        search that ended long before anyone asked about it still reports only
        what had arrived while it was alive. That is the whole point: a search
        lasting fifteen thousandths of a second collects nothing, no matter how
        late the question is put.
        """
        cutoff = min(time.monotonic(), search["endsAt"])
        if cutoff - search["startedAt"] < self.first_response_after:
            return []
        return search["responses"]

    def _state_of(self, search_id: str) -> dict[str, Any]:
        with self.lock:
            search = self.searches[search_id]
            now = time.monotonic()
            ended = now >= search["endsAt"]
            if ended and "finalResponses" not in search:
                # Freeze whatever had arrived by the deadline, as slskd does.
                search["finalResponses"] = self._visible_responses(search)
                search["state"] = "Completed, TimedOut"
            if ended:
                visible = search["finalResponses"]
            else:
                visible = self._visible_responses(search)
            return {
                "id": search_id,
                "searchText": search["searchText"],
                "state": search["state"],
                "isComplete": ended,
                "responseCount": len(visible),
                "fileCount": sum(len(r["files"]) for r in visible),
                "lockedFileCount": 0,
            }

    def search_state(self, search_id: str) -> tuple[int, Any]:
        with self.lock:
            if search_id not in self.searches:
                return 404, "not found"
        return 200, self._state_of(search_id)

    def search_responses(self, search_id: str) -> tuple[int, Any]:
        with self.lock:
            if search_id not in self.searches:
                return 404, "not found"
            search = self.searches[search_id]
        self._state_of(search_id)  # settle the completion state first
        with self.lock:
            return 200, search.get("finalResponses", self._visible_responses(search))

    def delete_search(self, search_id: str) -> tuple[int, Any]:
        with self.lock:
            self.searches.pop(search_id, None)
        return 204, None

    # -- transfers ---------------------------------------------------------

    def enqueue(
        self, username: str, files: list[dict[str, Any]], destination: str | None = None
    ) -> tuple[int, Any]:
        # slskd's own model marks the destination [RelativePath] and
        # [NonTraversingPath]: an absolute path, or one that climbs out of the
        # download folder, is rejected before anything is queued. Trackfetch
        # used to send a full Windows path, and every download failed with
        # nothing to explain it. Reproduce the rule, or the tests bless the bug.
        if destination is not None:
            text = str(destination)
            if (
                not text
                or text.startswith(("/", "\\"))
                or (len(text) > 1 and text[1] == ":")
                or ".." in text.replace("\\", "/").split("/")
            ):
                with self.lock:
                    self.rejected_destinations.append(text)
                return 400, "The Destination field is not a valid relative path"

        if not self.enqueue_slots.acquire(blocking=False):
            with self.lock:
                self.enqueue_429s += 1
            return 429, (
                "Only one concurrent operation is permitted. "
                "Wait until the previous request completes"
            )
        try:
            time.sleep(self.enqueue_duration)
        finally:
            self.enqueue_slots.release()

        now = time.monotonic()
        with self.lock:
            self.destinations_seen.append(destination)
            for f in files:
                tid = str(uuid.uuid4())
                self.transfers[(username, tid)] = {
                    "id": tid,
                    "username": username,
                    "filename": f.get("filename"),
                    "size": int(f.get("size") or 1000),
                    "startedAt": now,
                    "state": "Queued, Remotely",
                }
        return 201, None

    def downloads(self) -> tuple[int, Any]:
        now = time.monotonic()
        out: dict[str, dict[str, Any]] = {}
        with self.lock:
            for (username, tid), t in self.transfers.items():
                age = now - t["startedAt"]
                if age < 0.2:
                    state, got = "Queued, Remotely", 0
                elif age < 0.5:
                    state, got = "InProgress", t["size"] // 2
                else:
                    state, got = "Completed, Succeeded", t["size"]
                t["state"] = state
                entry = out.setdefault(username, {"username": username, "directories": []})
                entry["directories"].append(
                    {
                        "directory": "music",
                        "fileCount": 1,
                        "files": [
                            {
                                "id": tid,
                                "username": username,
                                "filename": t["filename"],
                                "size": t["size"],
                                "bytesTransferred": got,
                                "state": state,
                                "averageSpeed": 500000,
                            }
                        ],
                    }
                )
        return 200, list(out.values())


class _Handler(BaseHTTPRequestHandler):
    server_version = "fake-slskd/0.26.2"

    @property
    def world(self) -> FakeSoulseek:
        return self.server.world  # type: ignore[attr-defined]

    def log_message(self, *args):  # noqa: A003 - silence the test output
        return

    # -- plumbing ----------------------------------------------------------

    def _send(self, status: int, payload: Any) -> None:
        body = b"" if payload is None else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def _authorised(self) -> bool:
        auth = self.headers.get("Authorization") or ""
        return auth == f"Bearer {TOKEN}" or bool(self.headers.get("X-API-Key"))

    # -- routes ------------------------------------------------------------

    def do_GET(self):  # noqa: N802 - http.server naming
        path = self.path.split("?")[0]
        if path == "/api/v0/session/enabled":
            return self._send(200, True)
        if not self._authorised():
            return self._send(401, "unauthorised")
        if path == "/api/v0/session":
            return self._send(200, {"token": TOKEN})
        if path == "/api/v0/server":
            connected = self.world.connected()
            return self._send(
                200,
                {
                    "state": "Connected, LoggedIn" if connected else "Disconnected",
                    "isConnected": connected,
                    "isLoggedIn": connected,
                    "isConnecting": False,
                    "isLoggingIn": False,
                    "isTransitioning": False,
                },
            )
        if path == "/api/v0/application/version":
            return self._send(200, {"full": "0.26.2.0"})
        if path.startswith("/api/v0/searches/"):
            rest = path[len("/api/v0/searches/") :]
            if rest.endswith("/responses"):
                return self._send(*self.world.search_responses(rest[: -len("/responses")]))
            return self._send(*self.world.search_state(rest))
        if path == "/api/v0/transfers/downloads":
            return self._send(*self.world.downloads())
        return self._send(404, "not found")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        body = self._read_json()
        if path == "/api/v0/session":
            return self._send(200, {"token": TOKEN})
        if not self._authorised():
            return self._send(401, "unauthorised")
        if path == "/api/v0/searches":
            return self._send(*self.world.start_search(body))
        if path == "/api/v0/transfers/downloads/batches":
            username = str(body.get("username") or "")
            files = list(body.get("files") or [])
            options = body.get("options") or {}
            destination = options.get("destination") if isinstance(options, dict) else None
            return self._send(*self.world.enqueue(username, files, destination))
        if path.startswith("/api/v0/transfers/downloads/"):
            username = path.rsplit("/", 1)[-1]
            files = body if isinstance(body, list) else list(body.get("files") or [])
            return self._send(*self.world.enqueue(username, files))
        return self._send(404, "not found")

    def do_PUT(self):  # noqa: N802
        self._read_json()
        return self._send(204, None)

    def do_DELETE(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/api/v0/searches/"):
            return self._send(*self.world.delete_search(path[len("/api/v0/searches/") :]))
        return self._send(204, None)


class FakeSlskdServer:
    """Runs the stand-in on a real port for the duration of a test."""

    def __init__(self, **kwargs):
        self.world = FakeSoulseek(**kwargs)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.world = self.world  # type: ignore[attr-defined]
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeSlskdServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
