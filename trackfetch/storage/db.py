"""Thread-safe SQLite access.

Rules this module enforces:

* One connection per thread. SQLite connections are not safe to share between
  threads, and Trackfetch polls several providers on background threads at the
  same time as the interface reads from the same database.
* Write-ahead logging, so readers never block the writer.
* A process-wide write lock, so two background threads cannot interleave two
  write transactions and produce "database is locked".
* A generous busy timeout as a second line of defence.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
BUSY_TIMEOUT_MS = 15000

#: How long close() waits for work already in progress on other threads before
#: giving up and leaving those connections open.
CLOSE_GRACE_SECONDS = 5.0


class DatabaseClosed(RuntimeError):
    """Raised when something tries to use the database after it was closed.

    A clean exception on a background thread. The alternative, which this
    replaces, was sqlite closing a connection out from under a thread that was
    still using it, and that ends the whole process with a segmentation fault.
    """


class Database:
    """A small, deliberately boring wrapper around ``sqlite3``."""

    def __init__(self, path: Path | str):
        self._path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._all_connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        #: Counts operations in flight on any thread, so close() can wait for
        #: them instead of pulling the connection out from underneath.
        self._gate = threading.Condition()
        self._in_flight = 0
        self._closed = False
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._shared_memory_conn: sqlite3.Connection | None = None
        self._initialise()

    # -- connection handling ----------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def _new_connection(self) -> sqlite3.Connection:
        if self._path == ":memory:":
            # A plain in-memory database is private per connection, which would
            # defeat the point. Use a shared cache so every thread sees the same
            # data, exactly like a real file would behave.
            conn = sqlite3.connect(
                "file:trackfetch_memory?mode=memory&cache=shared",
                uri=True,
                timeout=BUSY_TIMEOUT_MS / 1000,
                check_same_thread=False,
                isolation_level=None,
            )
        else:
            conn = sqlite3.connect(
                self._path,
                timeout=BUSY_TIMEOUT_MS / 1000,
                check_same_thread=False,
                isolation_level=None,  # explicit transaction control
            )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys = ON")
        if self._path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
        with self._connections_lock:
            self._all_connections.append(conn)
        return conn

    @property
    def connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def _initialise(self) -> None:
        if self._path == ":memory:":
            # Hold one connection open for the lifetime of the object, otherwise
            # a shared-cache memory database is discarded when the last
            # connection closes.
            self._shared_memory_conn = self._new_connection()
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        conn = self.connection
        with self._write_lock:
            # executescript commits any open transaction itself, so this runs
            # outside the normal write() helper rather than inside it.
            conn.executescript(schema)
            conn.execute(
                "INSERT OR REPLACE INTO schema_info(key, value) VALUES('version', '1')"
            )

    @property
    def closed(self) -> bool:
        return self._closed

    @contextmanager
    def _in_use(self) -> Iterator[None]:
        """Mark one database operation as in progress.

        Closing waits for these to finish. Without it, a download finishing on
        a background thread at the same moment as shutdown would have its
        connection closed mid-statement, which takes the whole program down
        with a segmentation fault and no message at all.
        """
        with self._gate:
            if self._closed:
                raise DatabaseClosed("The database has been closed.")
            self._in_flight += 1
        try:
            yield
        finally:
            with self._gate:
                self._in_flight -= 1
                if self._in_flight <= 0:
                    self._gate.notify_all()

    def close(self, grace: float = CLOSE_GRACE_SECONDS) -> None:
        """Stop accepting work, let what is running finish, then close.

        If something is still running after the grace period the connections are
        left open rather than closed underneath it. Leaking a handle at shutdown
        is harmless; crashing is not.
        """
        deadline = None
        with self._gate:
            already = self._closed
            self._closed = True
            if self._in_flight > 0:
                import time as _time

                deadline = _time.monotonic() + max(0.0, grace)
                while self._in_flight > 0:
                    remaining = deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                    self._gate.wait(remaining)
            busy = self._in_flight
        if already:
            return
        if busy > 0:
            log.warning(
                "Closing the database with %s operation(s) still running; "
                "leaving their connections open",
                busy,
            )
            return
        with self._connections_lock:
            for conn in self._all_connections:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover - defensive
                    pass
            self._all_connections.clear()
        self._local = threading.local()
        self._shared_memory_conn = None

    # -- transactions ------------------------------------------------------

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Run a write transaction. Serialised process-wide, commits on exit.

        Nesting is allowed: an inner ``write()`` joins the transaction the outer
        one already opened rather than trying to begin a second.
        """
        with self._in_use():
            conn = self.connection
            with self._write_lock:
                depth = getattr(self._local, "depth", 0)
                if depth == 0:
                    conn.execute("BEGIN IMMEDIATE")
                self._local.depth = depth + 1
                try:
                    yield conn
                except Exception:
                    self._local.depth = depth
                    if depth == 0 and conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                else:
                    self._local.depth = depth
                    if depth == 0 and conn.in_transaction:
                        conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._in_use():
            yield self.connection

    # -- convenience -------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.write() as conn:
            conn.execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        with self.write() as conn:
            conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._in_use():
            return list(self.connection.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._in_use():
            return self.connection.execute(sql, params).fetchone()

    # -- key/value app state ----------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO app_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_state(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM app_state WHERE key = ?", (key,))
        return row["value"] if row else default
