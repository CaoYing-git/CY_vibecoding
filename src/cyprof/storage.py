"""Metadata index (SQLite) + ring-buffer rotation policy."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator, Sequence

from .config import StorageConfig

logger = logging.getLogger(__name__)

# ── data classes ────────────────────────────────────────────────


@dataclass
class SampleRecord:
    """A single sampling window stored in the index."""

    id: int
    start_ts: float  # epoch seconds, UTC
    end_ts: float
    frequency_hz: int
    duration_sec: int
    file_path: str  # relative to data_dir
    file_size_bytes: int
    sample_count: int
    trigger_mode: str  # "normal" | "alert" | "manual"

    @property
    def start_time(self) -> datetime:
        return datetime.fromtimestamp(self.start_ts, tz=timezone.utc)

    @property
    def end_time(self) -> datetime:
        return datetime.fromtimestamp(self.end_ts, tz=timezone.utc)


@dataclass
class DiskWatermark:
    """Current disk state for the data partition."""

    free_bytes: int
    total_bytes: int
    used_pct: float
    level: str  # "normal" | "warn" | "cap" | "emergency" | "fatal"


# ── SQLite metadata store ───────────────────────────────────────


class MetadataStore:
    """SQLite index of collected perf samples.

    Thread-safe at the connection level; caller should serialize writes.
    """

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS samples (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        start_ts        REAL    NOT NULL,
        end_ts          REAL    NOT NULL,
        frequency_hz    INTEGER NOT NULL,
        duration_sec    INTEGER NOT NULL,
        file_path       TEXT    NOT NULL,
        file_size_bytes INTEGER NOT NULL DEFAULT 0,
        sample_count    INTEGER NOT NULL DEFAULT 0,
        trigger_mode    TEXT    NOT NULL DEFAULT 'normal'
    );

    CREATE INDEX IF NOT EXISTS idx_samples_time
        ON samples(start_ts, end_ts);

    CREATE INDEX IF NOT EXISTS idx_samples_trigger
        ON samples(trigger_mode);
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── write ───────────────────────────────────────────────────

    def insert(self, record: SampleRecord) -> int:
        """Insert a record and return its row id."""
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO samples
                   (start_ts, end_ts, frequency_hz, duration_sec,
                    file_path, file_size_bytes, sample_count, trigger_mode)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.start_ts,
                    record.end_ts,
                    record.frequency_hz,
                    record.duration_sec,
                    record.file_path,
                    record.file_size_bytes,
                    record.sample_count,
                    record.trigger_mode,
                ),
            )
            conn.commit()
            return cur.lastrowid or 0

    def delete(self, record_id: int) -> bool:
        """Remove a record by id.  Returns ``True`` if a row was deleted."""
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM samples WHERE id = ?", (record_id,))
            conn.commit()
            return cur.rowcount > 0

    # ── query ───────────────────────────────────────────────────

    def query(
        self,
        start_ts: float | None = None,
        end_ts: float | None = None,
        trigger_mode: str | None = None,
        limit: int = 1000,
    ) -> list[SampleRecord]:
        """Return records in a time window, newest first."""
        clauses: list[str] = ["1=1"]
        params: list[object] = []

        if start_ts is not None:
            clauses.append("end_ts >= ?")
            params.append(start_ts)
        if end_ts is not None:
            clauses.append("start_ts <= ?")
            params.append(end_ts)
        if trigger_mode is not None:
            clauses.append("trigger_mode = ?")
            params.append(trigger_mode)

        where = " AND ".join(clauses)
        sql = f"SELECT * FROM samples WHERE {where} ORDER BY start_ts DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_oldest(self) -> SampleRecord | None:
        """Return the record with the earliest ``start_ts``."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM samples ORDER BY start_ts ASC LIMIT 1"
            ).fetchone()
        return self._row_to_record(row) if row else None

    def get_newest(self) -> SampleRecord | None:
        """Return the most recent record."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM samples ORDER BY start_ts DESC LIMIT 1"
            ).fetchone()
        return self._row_to_record(row) if row else None

    # ── stats ───────────────────────────────────────────────────

    def count(self) -> int:
        """Total number of records."""
        with self._conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]

    def total_size_bytes(self) -> int:
        """Sum of ``file_size_bytes`` across all records."""
        with self._conn() as conn:
            row = conn.execute("SELECT COALESCE(SUM(file_size_bytes), 0) FROM samples").fetchone()
            return row[0]

    def get_trigger_stats(self) -> dict[str, int]:
        """Count of records per trigger mode."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT trigger_mode, COUNT(*) FROM samples GROUP BY trigger_mode"
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ── internal ────────────────────────────────────────────────

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(self.SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA journal_size_limit = 10485760")  # 10 MB
            conn.execute("PRAGMA wal_autocheckpoint = 1000")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self._db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> SampleRecord:
        return SampleRecord(
            id=row["id"],
            start_ts=row["start_ts"],
            end_ts=row["end_ts"],
            frequency_hz=row["frequency_hz"],
            duration_sec=row["duration_sec"],
            file_path=row["file_path"],
            file_size_bytes=row["file_size_bytes"],
            sample_count=row["sample_count"],
            trigger_mode=row["trigger_mode"],
        )


# ── ring-buffer rotation ────────────────────────────────────────


class RingBuffer:
    """Enforce storage limits by deleting oldest files first.

    Rotation triggers (any one fires):
        - total data size > *max_size_bytes*
        - oldest record age > *max_age_hours*
        - disk free space below emergency threshold
    """

    # disk watermark thresholds (free space percentage)
    WATERMARK_WARN = 20.0
    WATERMARK_CAP = 15.0
    WATERMARK_EMERGENCY = 10.0
    WATERMARK_FATAL = 5.0

    def __init__(
        self,
        data_dir: str | Path,
        store: MetadataStore,
        max_size_mb: int = 500,
        max_age_hours: int = 24,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._store = store
        self._max_size_bytes = max_size_mb * 1_048_576  # MiB
        self._max_age = timedelta(hours=max_age_hours)

    # ── public API ──────────────────────────────────────────────

    def rotate(self) -> int:
        """Delete oldest files until constraints are satisfied.

        Returns:
            Number of files deleted (0 = nothing to do).
        """
        deleted = 0
        now = time.time()

        while True:
            total = self._total_data_size()
            oldest = self._store.get_oldest()

            size_exceeded = total > self._max_size_bytes
            age_exceeded = (oldest is not None
                            and (now - oldest.start_ts) > self._max_age.total_seconds())

            if not (size_exceeded or age_exceeded):
                break

            if oldest is None:
                break

            logger.info(
                "rotation: deleting %s  (size=%d/%d MB  age=%.1f/%.1f h)",
                oldest.file_path,
                total // 1_048_576,
                self._max_size_bytes // 1_048_576,
                (now - oldest.start_ts) / 3600,
                self._max_age.total_seconds() / 3600,
            )

            if self._remove_record(oldest):
                deleted += 1

        if deleted:
            logger.info("rotation done: %d files removed", deleted)
        return deleted

    def check_disk_watermark(self) -> DiskWatermark:
        """Inspect the filesystem hosting *data_dir*.

        Call this periodically — it only reads ``df``-equivalent stats,
        it does not delete anything.
        """
        try:
            usage = shutil.disk_usage(str(self._data_dir))
        except OSError:
            logger.warning("disk_usage failed for %s", self._data_dir, exc_info=True)
            return DiskWatermark(
                free_bytes=0, total_bytes=0, used_pct=100.0, level="unknown"
            )

        free = usage.free
        total = usage.total
        used_pct = 100.0 * (1.0 - free / total) if total > 0 else 100.0

        # determine level
        if used_pct >= (100.0 - self.WATERMARK_FATAL):
            level = "fatal"
        elif used_pct >= (100.0 - self.WATERMARK_EMERGENCY):
            level = "emergency"
        elif used_pct >= (100.0 - self.WATERMARK_CAP):
            level = "cap"
        elif used_pct >= (100.0 - self.WATERMARK_WARN):
            level = "warn"
        else:
            level = "normal"

        return DiskWatermark(
            free_bytes=free,
            total_bytes=total,
            used_pct=used_pct,
            level=level,
        )

    def emergency_rotate(self) -> int:
        """Aggressive rotation: keep only the most recent 30 minutes.

        Returns:
            Number of files deleted.
        """
        cutoff = time.time() - 1800  # 30 min ago
        old_records = self._store.query(
            end_ts=cutoff, limit=10_000,  # end_ts <= cutoff
        )
        deleted = 0
        for r in old_records:
            if self._remove_record(r):
                deleted += 1
        if deleted:
            logger.warning("emergency rotation: %d files removed", deleted)
        return deleted

    # ── internal ────────────────────────────────────────────────

    def _total_data_size(self) -> int:
        """Get total size from SQLite (fast) or fall back to ``du``."""
        db_size = self._store.total_size_bytes()
        if db_size > 0:
            return db_size

        # fallback: scan filesystem
        total = 0
        try:
            for entry in self._data_dir.iterdir():
                if entry.is_file():
                    total += entry.stat().st_size
        except OSError:
            logger.debug("filesystem size scan failed", exc_info=True)
        return total

    def _remove_record(self, record: SampleRecord) -> bool:
        """Delete the disk file then the SQLite row.  Returns ``True`` on success."""
        file_path = self._data_dir / record.file_path
        # 1. delete disk file first (space is freed even if SQLite fails)
        try:
            if file_path.exists():
                file_path.unlink()
        except OSError:
            logger.warning("failed to delete file: %s", file_path, exc_info=True)

        # 2. remove index entry
        try:
            self._store.delete(record.id)
        except Exception:
            logger.warning("failed to delete DB row %d", record.id, exc_info=True)

        return not file_path.exists()  # success = file is gone
