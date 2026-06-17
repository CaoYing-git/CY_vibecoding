"""Tests for cyprof.query."""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import pytest

from cyprof.query import (
    QueryEngine,
    QueryResult,
    parse_time,
)
from cyprof.storage import MetadataStore, SampleRecord


# ── helpers ────────────────────────────────────────────────────

def _make_record(
    store: MetadataStore,
    start_ts: float,
    file_path: str = "",
    sample_count: int = 42,
    file_size: int = 5000,
) -> int:
    if not file_path:
        dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
        file_path = dt.strftime("%Y%m%d_%H%M%S_11hz.perf.data.zst")
    return store.insert(SampleRecord(
        id=0,
        start_ts=start_ts,
        end_ts=start_ts + 10,
        frequency_hz=11,
        duration_sec=10,
        file_path=file_path,
        file_size_bytes=file_size,
        sample_count=sample_count,
        trigger_mode="normal",
    ))


@pytest.fixture
def engine(tmp_path: Path) -> QueryEngine:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = tmp_path / "meta.db"
    return QueryEngine(data_dir, db_path)


# ── parse_time ─────────────────────────────────────────────────

def test_parse_time_full():
    dt = parse_time("2025-06-01 03:12:00")
    assert dt.year == 2025
    assert dt.month == 6
    assert dt.day == 1
    assert dt.hour == 3
    assert dt.minute == 12
    assert dt.tzinfo is not None


def test_parse_time_no_seconds():
    dt = parse_time("2025-06-01 03:12")
    assert dt.hour == 3
    assert dt.minute == 12


def test_parse_time_iso():
    dt = parse_time("2025-06-01T03:12:00")
    assert dt.hour == 3


def test_parse_time_today():
    """Time-only parses to today UTC at the given time."""
    dt = parse_time("14:30")
    now = datetime.now(timezone.utc)
    assert dt.year == now.year
    assert dt.month == now.month
    assert dt.day == now.day
    assert dt.hour == 14
    assert dt.minute == 30


def test_parse_time_today_with_seconds():
    dt = parse_time("14:30:45")
    assert dt.second == 45


def test_parse_time_invalid():
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_time("not a time")


# ── find_closest ───────────────────────────────────────────────

def test_find_closest_exact(engine: QueryEngine):
    base = time.time()
    _make_record(engine._store, base)

    result = engine.find_closest(
        datetime.fromtimestamp(base + 5, tz=timezone.utc)
    )
    assert len(result.records) == 1
    assert result.match_method == "closest"


def test_find_closest_picks_nearest(engine: QueryEngine):
    base = time.time()
    _make_record(engine._store, base - 300)       # 5 min ago
    _make_record(engine._store, base + 60)         # 1 min ahead (closer)
    _make_record(engine._store, base - 600)        # 10 min ago

    result = engine.find_closest(
        datetime.fromtimestamp(base, tz=timezone.utc),
        within_sec=900,
    )
    assert len(result.records) == 1
    # should match the one 60s ahead (closest midpoint to base)
    rec = result.records[0]
    assert abs(rec.start_ts - (base + 60)) < 1


def test_find_closest_empty_when_none_in_window(engine: QueryEngine):
    base = time.time()
    _make_record(engine._store, base - 3600)  # 1 hour ago

    result = engine.find_closest(
        datetime.fromtimestamp(base, tz=timezone.utc),
        within_sec=60,  # narrow window
    )
    assert len(result.records) == 0


def test_find_closest_empty_store(engine: QueryEngine):
    result = engine.find_closest(
        datetime.now(timezone.utc)
    )
    assert len(result.records) == 0


# ── find_range ─────────────────────────────────────────────────

def test_find_range(engine: QueryEngine):
    base = time.time()
    _make_record(engine._store, base - 3600)
    _make_record(engine._store, base - 1800)
    _make_record(engine._store, base)

    result = engine.find_range(
        datetime.fromtimestamp(base - 2000, tz=timezone.utc),
        datetime.fromtimestamp(base, tz=timezone.utc),
    )
    # should match the middle one and the last one
    assert len(result.records) >= 1
    assert result.match_method == "range"


def test_find_range_chronological_order(engine: QueryEngine):
    base = time.time()
    _make_record(engine._store, base)          # newest first in DB
    _make_record(engine._store, base - 100)
    _make_record(engine._store, base - 200)    # oldest

    result = engine.find_range(
        datetime.fromtimestamp(base - 300, tz=timezone.utc),
        datetime.fromtimestamp(base + 1, tz=timezone.utc),
    )
    # returned in chronological order (oldest first)
    assert len(result.records) == 3
    for i in range(len(result.records) - 1):
        assert result.records[i].start_ts <= result.records[i + 1].start_ts


# ── list_recent ────────────────────────────────────────────────

def test_list_recent(engine: QueryEngine):
    base = time.time()
    for i in range(30):
        _make_record(engine._store, base - i * 60)

    records = engine.list_recent(n=10)
    assert len(records) == 10


# ── generate_flamegraph ────────────────────────────────────────

def test_generate_flamegraph_closest(engine: QueryEngine, tmp_path: Path):
    base = time.time()
    _make_record(engine._store, base, file_path="test.zst")

    result = QueryResult(
        records=engine._store.query(),
        target_time=datetime.fromtimestamp(base, tz=timezone.utc),
        time_range=None,
        match_method="closest",
    )

    out_dir = tmp_path / "flame_out"

    with mock.patch.object(engine._flamegraph, "generate") as m_gen:
        from cyprof.flamegraph import FlameGraphResult
        m_gen.return_value = FlameGraphResult(
            svg_path=out_dir / "flamegraph.svg",
            title="Test",
            sample_count=42,
            input_files=1,
        )
        fg = engine.generate_flamegraph(result, out_dir)

    assert fg is not None
    assert fg.sample_count == 42
    m_gen.assert_called_once()


def test_generate_flamegraph_empty_records(engine: QueryEngine, tmp_path: Path):
    result = QueryResult(
        records=[],
        target_time=None,
        time_range=None,
        match_method="closest",
    )
    fg = engine.generate_flamegraph(result, tmp_path / "out")
    assert fg is None


def test_generate_flamegraph_range(engine: QueryEngine, tmp_path: Path):
    base = time.time()
    _make_record(engine._store, base - 300, file_path="a.zst")
    _make_record(engine._store, base, file_path="b.zst")

    result = QueryResult(
        records=engine._store.query(),
        target_time=None,
        time_range=(
            datetime.fromtimestamp(base - 600, tz=timezone.utc),
            datetime.fromtimestamp(base + 10, tz=timezone.utc),
        ),
        match_method="range",
    )

    out_dir = tmp_path / "flame_out"
    with mock.patch.object(engine._flamegraph, "generate") as m_gen:
        from cyprof.flamegraph import FlameGraphResult
        m_gen.return_value = FlameGraphResult(
            svg_path=out_dir / "flamegraph.svg",
            title="Test",
            sample_count=84,
            input_files=2,
        )
        fg = engine.generate_flamegraph(result, out_dir)

    assert fg is not None
    assert fg.input_files == 2
    # should pass correct subtitle with time range
    call_kwargs = m_gen.call_args.kwargs
    assert "subtitle" in call_kwargs
    assert "—" in call_kwargs["subtitle"]


# ── properties ─────────────────────────────────────────────────

def test_properties_empty(engine: QueryEngine):
    assert engine.record_count == 0
    assert engine.newest is None
    assert engine.oldest is None


def test_properties_populated(engine: QueryEngine):
    base = time.time()
    _make_record(engine._store, base - 3600)
    _make_record(engine._store, base)

    assert engine.record_count == 2
    assert engine.newest is not None
    assert engine.oldest is not None
    assert engine.newest.start_ts >= engine.oldest.start_ts


# ── flamegraph error handling ───────────────────────────────────

def test_generate_flamegraph_handles_error(engine: QueryEngine, tmp_path: Path):
    """When FlameGraphGenerator raises, generate_flamegraph returns None."""
    base = time.time()
    _make_record(engine._store, base, file_path="test.zst")

    result = QueryResult(
        records=engine._store.query(),
        target_time=datetime.fromtimestamp(base, tz=timezone.utc),
        time_range=None,
        match_method="closest",
    )

    with mock.patch.object(engine._flamegraph, "generate",
                           side_effect=__import__("cyprof.flamegraph").flamegraph.FlameGraphError("boom")):
        fg = engine.generate_flamegraph(result, tmp_path / "out")
        assert fg is None


# ── parse_time edge cases ───────────────────────────────────────

def test_parse_time_time_only_invalid():
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_time("abc")


def test_parse_time_empty():
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_time("")


def test_parse_time_partial():
    """Partial match like '14' should fail — not enough for time-only."""
    with pytest.raises(ValueError, match="Cannot parse"):
        parse_time("14")


# ── generate_flamegraph: all match_methods ──────────────────────

def test_generate_flamegraph_no_subtitle(engine: QueryEngine, tmp_path: Path):
    """When there's no target_time and no time_range, subtitle is empty."""
    base = time.time()
    _make_record(engine._store, base, file_path="test.zst")

    # simulate an "all" match method (or any without time context)
    result = QueryResult(
        records=engine._store.query(),
        target_time=None,
        time_range=None,
        match_method="all",
    )
    out_dir = tmp_path / "flame_out"
    with mock.patch.object(engine._flamegraph, "generate") as m_gen:
        from cyprof.flamegraph import FlameGraphResult
        m_gen.return_value = FlameGraphResult(
            svg_path=out_dir / "flamegraph.svg",
            title="Test",
            sample_count=42,
            input_files=1,
        )
        fg = engine.generate_flamegraph(result, out_dir)
        assert fg is not None
        kwargs = m_gen.call_args.kwargs
        assert kwargs["subtitle"] == ""
