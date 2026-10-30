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


class Database:
    """A small, deliberately boring wrapper around ``sqlite3``."""

    def __init__(self, path: Path | str):
        self._path = str(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._all_connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
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

    def close(self) -> None:
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
        return list(self.connection.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
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
