"""Tests for cyprof.storage — MetadataStore + RingBuffer."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from cyprof.storage import (
    MetadataStore,
    RingBuffer,
    SampleRecord,
    DiskWatermark,
)


# ── helpers ────────────────────────────────────────────────────

def _make_record(
    start_ts: float | None = None,
    file_path: str = "20260617_143000_11hz.perf.data.zst",
    file_size: int = 250_000,
    sample_count: int = 42,
    trigger_mode: str = "normal",
) -> SampleRecord:
    if start_ts is None:
        start_ts = time.time()
    return SampleRecord(
        id=0,
        start_ts=start_ts,
        end_ts=start_ts + 10,
        frequency_hz=11,
        duration_sec=10,
        file_path=file_path,
        file_size_bytes=file_size,
        sample_count=sample_count,
        trigger_mode=trigger_mode,
    )


# ── MetadataStore tests ────────────────────────────────────────


class TestMetadataStore:
    def test_insert_and_query(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        rec = _make_record()
        rid = db.insert(rec)
        assert rid > 0

        results = db.query()
        assert len(results) == 1
        assert results[0].file_path == rec.file_path
        assert results[0].sample_count == 42

    def test_insert_sets_id(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        r1 = db.insert(_make_record())
        r2 = db.insert(_make_record())
        assert r2 == r1 + 1

    def test_query_time_range(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        base = time.time()
        db.insert(_make_record(start_ts=base - 3600))
        db.insert(_make_record(start_ts=base - 1800))
        db.insert(_make_record(start_ts=base))

        # query last 30 min
        results = db.query(start_ts=base - 900, end_ts=base + 1)
        assert len(results) == 1
        assert abs(results[0].start_ts - base) < 60

    def test_query_by_trigger_mode(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        db.insert(_make_record(trigger_mode="normal"))
        db.insert(_make_record(trigger_mode="alert"))
        db.insert(_make_record(trigger_mode="normal"))

        assert len(db.query(trigger_mode="alert")) == 1
        assert len(db.query(trigger_mode="normal")) == 2
        assert len(db.query(trigger_mode="manual")) == 0

    def test_query_limit(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        for i in range(10):
            db.insert(_make_record(start_ts=time.time() + i))

        results = db.query(limit=3)
        assert len(results) == 3

    def test_delete(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        rid = db.insert(_make_record())
        assert db.count() == 1

        assert db.delete(rid)
        assert db.count() == 0

    def test_delete_nonexistent(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        assert not db.delete(99999)

    def test_get_oldest_newest(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        base = time.time()
        db.insert(_make_record(start_ts=base - 7200, file_path="old"))
        db.insert(_make_record(start_ts=base, file_path="new"))

        assert db.get_oldest().file_path == "old"  # type: ignore[union-attr]
        assert db.get_newest().file_path == "new"  # type: ignore[union-attr]

    def test_total_size_bytes(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        db.insert(_make_record(file_size=1000))
        db.insert(_make_record(file_size=2000))
        assert db.total_size_bytes() == 3000

    def test_trigger_stats(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        db.insert(_make_record(trigger_mode="normal"))
        db.insert(_make_record(trigger_mode="normal"))
        db.insert(_make_record(trigger_mode="alert"))

        stats = db.get_trigger_stats()
        assert stats == {"normal": 2, "alert": 1}

    def test_time_properties(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        ts = time.time()
        rec = _make_record(start_ts=ts)
        rid = db.insert(rec)
        result = db.query()[0]
        assert result.start_time is not None
        assert result.end_time is not None
        assert result.end_ts - result.start_ts == 10

    def test_empty_store(self, tmp_path: Path):
        db = MetadataStore(tmp_path / "test.db")
        assert db.count() == 0
        assert db.total_size_bytes() == 0
        assert db.get_oldest() is None
        assert db.get_newest() is None
        assert db.query() == []
        assert db.get_trigger_stats() == {}


# ── RingBuffer tests ───────────────────────────────────────────


class TestRingBuffer:
    def test_no_rotation_when_under_limits(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        store = MetadataStore(tmp_path / "meta.db")
        rb = RingBuffer(data_dir, store, max_size_mb=1000, max_age_hours=48)

        # insert a fresh, small record
        rec = _make_record(file_size=100_000)
        rid = store.insert(rec)
        # create actual file on disk
        (data_dir / rec.file_path).write_text("x" * 100_000)

        deleted = rb.rotate()
        assert deleted == 0
        assert store.count() == 1

    def test_rotate_by_size(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        store = MetadataStore(tmp_path / "meta.db")
        rb = RingBuffer(data_dir, store, max_size_mb=1, max_age_hours=9999)

        # insert 3 records, total > 1 MB
        for i in range(3):
            rec = _make_record(
                start_ts=time.time() - (3 - i) * 3600,
                file_path=f"file_{i}.zst",
                file_size=500_000,
            )
            store.insert(rec)
            (data_dir / rec.file_path).write_text("x" * 500_000)

        # total = 1.5 MB > 1 MB → should delete oldest 1 or 2
        deleted = rb.rotate()
        assert deleted >= 1
        # oldest should be gone
        assert store.get_oldest().file_path != "file_0.zst"  # type: ignore[union-attr]

    def test_rotate_by_age(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        store = MetadataStore(tmp_path / "meta.db")
        rb = RingBuffer(data_dir, store, max_size_mb=1000, max_age_hours=1)

        # insert an old record (2 hours ago)
        old_rec = _make_record(
            start_ts=time.time() - 7200,
            file_path="old.zst",
            file_size=100,
        )
        store.insert(old_rec)
        (data_dir / old_rec.file_path).write_text("x" * 100)

        # insert a fresh record
        new_rec = _make_record(
            start_ts=time.time(),
            file_path="new.zst",
            file_size=100,
        )
        store.insert(new_rec)
        (data_dir / new_rec.file_path).write_text("x" * 100)

        deleted = rb.rotate()
        assert deleted >= 1
        remaining = [r.file_path for r in store.query()]
        assert "old.zst" not in remaining
        assert "new.zst" in remaining

    def test_rotate_removes_disk_file(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        store = MetadataStore(tmp_path / "meta.db")
        rb = RingBuffer(data_dir, store, max_size_mb=0, max_age_hours=0)

        rec = _make_record(file_path="doomed.zst")
        store.insert(rec)
        file_path = data_dir / "doomed.zst"
        file_path.write_text("data")

        assert file_path.exists()
        rb.rotate()
        assert not file_path.exists()
        assert store.count() == 0

    def test_check_disk_watermark(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        store = MetadataStore(tmp_path / "meta.db")
        rb = RingBuffer(data_dir, store)

        wm = rb.check_disk_watermark()
        assert wm.level in ("normal", "warn", "cap", "emergency", "fatal", "unknown")
        assert wm.free_bytes >= 0
        assert isinstance(wm.used_pct, float)

    def test_emergency_rotate(self, tmp_path: Path):
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        store = MetadataStore(tmp_path / "meta.db")

        # old records (> 30 min)
        for i in range(5):
            rec = _make_record(
                start_ts=time.time() - 3600 - i * 60,
                file_path=f"old_{i}.zst",
            )
            store.insert(rec)
            (data_dir / rec.file_path).write_text("x" * 100)

        # fresh records (< 30 min)
        for i in range(3):
            rec = _make_record(
                start_ts=time.time() - i * 60,
                file_path=f"fresh_{i}.zst",
            )
            store.insert(rec)
            (data_dir / rec.file_path).write_text("x" * 100)

        rb = RingBuffer(data_dir, store)
        deleted = rb.emergency_rotate()
        assert deleted == 5  # only old ones removed
        assert store.count() == 3
        assert all("fresh" in r.file_path for r in store.query())

    def test_remove_record_handles_missing_file(self, tmp_path: Path):
        """Rotation should tolerate file already deleted from disk."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        store = MetadataStore(tmp_path / "meta.db")
        rb = RingBuffer(data_dir, store, max_size_mb=0, max_age_hours=0)

        rec = _make_record(file_path="ghost.zst")
        store.insert(rec)
        # don't create the file — it's already missing

        deleted = rb.rotate()
        assert deleted >= 1 or store.count() == 0  # should not crash
