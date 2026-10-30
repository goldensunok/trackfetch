"""Repositories: the only place that knows how models map onto tables."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Iterable, Sequence

from trackfetch.models.common import (
    BatchItem,
    InputKind,
    SearchCandidate,
    SearchQuery,
    TransferJob,
    TransferStatus,
    new_id,
)
from trackfetch.storage.db import Database


def _json_loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bool_or_none(value: Any) -> bool | None:
    return None if value is None else bool(value)


class BatchRepository:
    def __init__(self, db: Database):
        self.db = db

    def create_batch(self, name: str = "", source: str = "pasted") -> str:
        batch_id = new_id()
        self.db.execute(
            "INSERT INTO batches(id, name, source, created_at) VALUES(?, ?, ?, ?)",
            (batch_id, name, source, time.time()),
        )
        return batch_id

    def add_items(self, batch_id: str, items: Sequence[BatchItem]) -> None:
        rows = []
        for index, item in enumerate(items):
            item.batch_id = batch_id
            if not item.position:
                item.position = index
            rows.append(
                (
                    item.id,
                    batch_id,
                    item.position,
                    item.raw_text,
                    str(item.kind),
                    json.dumps(item.extra),
                    item.source,
                    int(item.skipped),
                )
            )
        self.db.executemany(
            "INSERT OR REPLACE INTO batch_items"
            "(id, batch_id, position, raw_text, kind, extra_json, source, skipped)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    def items(self, batch_id: str) -> list[BatchItem]:
        rows = self.db.query(
            "SELECT * FROM batch_items WHERE batch_id = ? ORDER BY position", (batch_id,)
        )
        return [self._to_item(r) for r in rows]

    def item(self, item_id: str) -> BatchItem | None:
        row = self.db.query_one("SELECT * FROM batch_items WHERE id = ?", (item_id,))
        return self._to_item(row) if row else None

    def set_skipped(self, item_id: str, skipped: bool) -> None:
        self.db.execute(
            "UPDATE batch_items SET skipped = ? WHERE id = ?", (int(skipped), item_id)
        )

    def update_text(self, item_id: str, text: str) -> None:
        self.db.execute("UPDATE batch_items SET raw_text = ? WHERE id = ?", (text, item_id))

    def latest_batch_id(self) -> str | None:
        row = self.db.query_one(
            "SELECT id FROM batches ORDER BY created_at DESC LIMIT 1"
        )
        return row["id"] if row else None

    def batches(self, limit: int = 25) -> list[sqlite3.Row]:
        return self.db.query(
            "SELECT b.*, (SELECT COUNT(*) FROM batch_items i WHERE i.batch_id = b.id)"
            " AS item_count FROM batches b ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    def delete_batch(self, batch_id: str) -> None:
        self.db.execute("DELETE FROM batches WHERE id = ?", (batch_id,))

    @staticmethod
    def _to_item(row: sqlite3.Row) -> BatchItem:
        try:
            kind = InputKind(row["kind"])
        except ValueError:
            kind = InputKind.QUERY
        return BatchItem(
            id=row["id"],
            batch_id=row["batch_id"],
            raw_text=row["raw_text"],
            kind=kind,
            extra={str(k): str(v) for k, v in _json_loads(row["extra_json"]).items()},
            source=row["source"],
            position=row["position"],
            skipped=bool(row["skipped"]),
        )


class QueryRepository:
    def __init__(self, db: Database):
        self.db = db

    def save(self, query: SearchQuery) -> None:
        self.db.execute(
            "INSERT INTO queries(id, batch_item_id, provider_id, text, remote_id, state,"
            " error, created_at, completed_at, result_count)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET remote_id = excluded.remote_id,"
            " state = excluded.state, error = excluded.error,"
            " completed_at = excluded.completed_at, result_count = excluded.result_count",
            (
                query.id,
                query.batch_item_id,
                query.provider_id,
                query.text,
                query.remote_id,
                query.state,
                query.error,
                query.created_at,
                query.completed_at,
                query.result_count,
            ),
        )

    def for_item(self, batch_item_id: str) -> list[SearchQuery]:
        rows = self.db.query(
            "SELECT * FROM queries WHERE batch_item_id = ? ORDER BY created_at",
            (batch_item_id,),
        )
        return [self._to_query(r) for r in rows]

    def get(self, query_id: str) -> SearchQuery | None:
        row = self.db.query_one("SELECT * FROM queries WHERE id = ?", (query_id,))
        return self._to_query(row) if row else None

    @staticmethod
    def _to_query(row: sqlite3.Row) -> SearchQuery:
        return SearchQuery(
            id=row["id"],
            batch_item_id=row["batch_item_id"],
            provider_id=row["provider_id"],
            text=row["text"],
            remote_id=row["remote_id"],
            state=row["state"],
            error=row["error"],
            created_at=row["created_at"],
            completed_at=row["completed_at"],
            result_count=row["result_count"],
        )


class CandidateRepository:
    COLUMNS = (
        "id, query_id, provider_id, filename, directory, extension, size, bitrate,"
        " is_vbr, duration, sample_rate, bit_depth, codec, remote_user, queue_length,"
        " has_free_slot, upload_speed, locked, relevance, selected, payload_json, raw_json"
    )

    def __init__(self, db: Database):
        self.db = db

    def save_many(self, candidates: Iterable[SearchCandidate]) -> None:
        rows = [
            (
                c.id,
                c.query_id,
                c.provider_id,
                c.filename,
                c.directory,
                c.extension,
                c.size,
                c.bitrate,
                None if c.is_vbr is None else int(c.is_vbr),
                c.duration,
                c.sample_rate,
                c.bit_depth,
                c.codec,
                c.remote_user,
                c.queue_length,
                None if c.has_free_slot is None else int(c.has_free_slot),
                c.upload_speed,
                int(c.locked),
                c.relevance,
                int(c.selected),
                json.dumps(c.payload),
                json.dumps(c.raw)[:200000],
            )
            for c in candidates
        ]
        placeholders = ", ".join(["?"] * 22)
        self.db.executemany(
            f"INSERT OR REPLACE INTO candidates({self.COLUMNS}) VALUES({placeholders})",
            rows,
        )

    def for_query(self, query_id: str) -> list[SearchCandidate]:
        rows = self.db.query(
            "SELECT * FROM candidates WHERE query_id = ? ORDER BY relevance DESC",
            (query_id,),
        )
        return [self._to_candidate(r) for r in rows]

    def for_item(self, batch_item_id: str) -> list[SearchCandidate]:
        rows = self.db.query(
            "SELECT c.* FROM candidates c JOIN queries q ON q.id = c.query_id"
            " WHERE q.batch_item_id = ? ORDER BY c.relevance DESC",
            (batch_item_id,),
        )
        return [self._to_candidate(r) for r in rows]

    def get(self, candidate_id: str) -> SearchCandidate | None:
        row = self.db.query_one("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
        return self._to_candidate(row) if row else None

    def set_selected(self, candidate_id: str, selected: bool) -> None:
        self.db.execute(
            "UPDATE candidates SET selected = ? WHERE id = ?",
            (int(selected), candidate_id),
        )

    def clear_selection_for_item(self, batch_item_id: str) -> None:
        self.db.execute(
            "UPDATE candidates SET selected = 0 WHERE query_id IN"
            " (SELECT id FROM queries WHERE batch_item_id = ?)",
            (batch_item_id,),
        )

    def selected_for_batch(self, batch_id: str) -> list[SearchCandidate]:
        rows = self.db.query(
            "SELECT c.* FROM candidates c"
            " JOIN queries q ON q.id = c.query_id"
            " JOIN batch_items i ON i.id = q.batch_item_id"
            " WHERE i.batch_id = ? AND c.selected = 1"
            " ORDER BY i.position",
            (batch_id,),
        )
        return [self._to_candidate(r) for r in rows]

    def count_for_batch(self, batch_id: str) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS n FROM candidates c"
            " JOIN queries q ON q.id = c.query_id"
            " JOIN batch_items i ON i.id = q.batch_item_id"
            " WHERE i.batch_id = ?",
            (batch_id,),
        )
        return int(row["n"]) if row else 0

    @staticmethod
    def _to_candidate(row: sqlite3.Row) -> SearchCandidate:
        return SearchCandidate(
            id=row["id"],
            query_id=row["query_id"],
            provider_id=row["provider_id"],
            filename=row["filename"],
            directory=row["directory"],
            extension=row["extension"],
            size=row["size"],
            bitrate=row["bitrate"],
            is_vbr=_bool_or_none(row["is_vbr"]),
            duration=row["duration"],
            sample_rate=row["sample_rate"],
            bit_depth=row["bit_depth"],
            codec=row["codec"],
            remote_user=row["remote_user"],
            queue_length=row["queue_length"],
            has_free_slot=_bool_or_none(row["has_free_slot"]),
            upload_speed=row["upload_speed"],
            locked=bool(row["locked"]),
            relevance=row["relevance"],
            selected=bool(row["selected"]),
            payload=_json_loads(row["payload_json"]),
            raw=_json_loads(row["raw_json"]),
        )


class TransferRepository:
    def __init__(self, db: Database):
        self.db = db

    def save(self, job: TransferJob) -> None:
        job.touch()
        self.db.execute(
            "INSERT INTO transfers(id, provider_id, label, status, total_bytes,"
            " downloaded_bytes, speed_bps, eta_seconds, destination, error, remedy,"
            " remote_id, payload_json, batch_item_id, candidate_id, created_at,"
            " updated_at, completed_at, attempts)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET status = excluded.status,"
            " label = excluded.label,"
            " total_bytes = excluded.total_bytes,"
            " downloaded_bytes = excluded.downloaded_bytes,"
            " speed_bps = excluded.speed_bps, eta_seconds = excluded.eta_seconds,"
            " destination = excluded.destination, error = excluded.error,"
            " remedy = excluded.remedy, remote_id = excluded.remote_id,"
            " payload_json = excluded.payload_json, updated_at = excluded.updated_at,"
            " completed_at = excluded.completed_at, attempts = excluded.attempts",
            (
                job.id,
                job.provider_id,
                job.label,
                str(job.status),
                job.total_bytes,
                job.downloaded_bytes,
                job.speed_bps,
                job.eta_seconds,
                job.destination,
                job.error,
                job.remedy,
                job.remote_id,
                json.dumps(job.payload),
                job.batch_item_id,
                job.candidate_id,
                job.created_at,
                job.updated_at,
                job.completed_at,
                job.attempts,
            ),
        )

    def save_many(self, jobs: Iterable[TransferJob]) -> None:
        for job in jobs:
            self.save(job)

    def get(self, job_id: str) -> TransferJob | None:
        row = self.db.query_one("SELECT * FROM transfers WHERE id = ?", (job_id,))
        return self._to_job(row) if row else None

    def all(self, limit: int = 5000) -> list[TransferJob]:
        rows = self.db.query(
            "SELECT * FROM transfers ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [self._to_job(r) for r in rows]

    def active(self) -> list[TransferJob]:
        statuses = [
            str(TransferStatus.WAITING),
            str(TransferStatus.QUEUED),
            str(TransferStatus.DOWNLOADING),
            str(TransferStatus.PAUSED),
        ]
        placeholders = ", ".join(["?"] * len(statuses))
        rows = self.db.query(
            f"SELECT * FROM transfers WHERE status IN ({placeholders}) ORDER BY created_at",
            statuses,
        )
        return [self._to_job(r) for r in rows]

    def by_provider(self, provider_id: str) -> list[TransferJob]:
        rows = self.db.query(
            "SELECT * FROM transfers WHERE provider_id = ? ORDER BY created_at",
            (provider_id,),
        )
        return [self._to_job(r) for r in rows]

    def delete(self, job_id: str) -> None:
        self.db.execute("DELETE FROM transfers WHERE id = ?", (job_id,))

    def clear_finished(self) -> None:
        self.db.execute(
            "DELETE FROM transfers WHERE status IN (?, ?)",
            (str(TransferStatus.COMPLETE), str(TransferStatus.CANCELLED)),
        )

    @staticmethod
    def _to_job(row: sqlite3.Row) -> TransferJob:
        try:
            status = TransferStatus(row["status"])
        except ValueError:
            status = TransferStatus.WAITING
        return TransferJob(
            id=row["id"],
            provider_id=row["provider_id"],
            label=row["label"],
            status=status,
            total_bytes=row["total_bytes"],
            downloaded_bytes=row["downloaded_bytes"],
            speed_bps=row["speed_bps"],
            eta_seconds=row["eta_seconds"],
            destination=row["destination"],
            error=row["error"],
            remedy=row["remedy"],
            remote_id=row["remote_id"],
            payload=_json_loads(row["payload_json"]),
            batch_item_id=row["batch_item_id"],
            candidate_id=row["candidate_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            attempts=row["attempts"],
        )


class CompletionRepository:
    """Remembers what has already finished, so nothing is fetched twice."""

    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        fingerprint: str,
        provider_id: str,
        label: str,
        destination: str,
        size: int | None,
    ) -> None:
        self.db.execute(
            "INSERT INTO completions(fingerprint, provider_id, label, destination, size,"
            " completed_at) VALUES(?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(fingerprint) DO UPDATE SET destination = excluded.destination,"
            " size = excluded.size, completed_at = excluded.completed_at",
            (fingerprint, provider_id, label, destination, size, time.time()),
        )

    def get(self, fingerprint: str) -> sqlite3.Row | None:
        return self.db.query_one(
            "SELECT * FROM completions WHERE fingerprint = ?", (fingerprint,)
        )

    def has(self, fingerprint: str) -> bool:
        return self.get(fingerprint) is not None

    def forget(self, fingerprint: str) -> None:
        self.db.execute("DELETE FROM completions WHERE fingerprint = ?", (fingerprint,))

    def count(self) -> int:
        row = self.db.query_one("SELECT COUNT(*) AS n FROM completions")
        return int(row["n"]) if row else 0


class EventRepository:
    """Rolling record of notable events for the diagnostics report."""

    MAX_EVENTS = 500

    def __init__(self, db: Database):
        self.db = db

    def add(self, message: str, level: str = "info", provider_id: str = "") -> None:
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO events(ts, level, provider_id, message) VALUES(?, ?, ?, ?)",
                (time.time(), level, provider_id, message[:2000]),
            )
            conn.execute(
                "DELETE FROM events WHERE id NOT IN"
                " (SELECT id FROM events ORDER BY id DESC LIMIT ?)",
                (self.MAX_EVENTS,),
            )

    def recent(self, limit: int = 50, level: str | None = None) -> list[sqlite3.Row]:
        if level:
            return self.db.query(
                "SELECT * FROM events WHERE level = ? ORDER BY id DESC LIMIT ?",
                (level, limit),
            )
        return self.db.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))


class Repositories:
    """One handle that carries every repository, to keep wiring simple."""

    def __init__(self, db: Database):
        self.db = db
        self.batches = BatchRepository(db)
        self.queries = QueryRepository(db)
        self.candidates = CandidateRepository(db)
        self.transfers = TransferRepository(db)
        self.completions = CompletionRepository(db)
        self.events = EventRepository(db)

    def close(self) -> None:
        self.db.close()
