"""Tests for cyprof.cli."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from cyprof.cli import main, cmd_query, cmd_list, cmd_info
from cyprof.query import QueryEngine


# ── fixtures ────────────────────────────────────────────────────

@pytest.fixture
def engine(tmp_path: Path) -> QueryEngine:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return QueryEngine(data_dir, tmp_path / "meta.db")


# ── cmd_query ───────────────────────────────────────────────────

def test_query_missing_params(engine: QueryEngine):
    """Must provide --time or --from/--to."""
    rc = cmd_query(engine, target=None, from_time=None, to_time=None,
                   output="/tmp/out", title="test")
    assert rc == 2


def test_query_time_not_found(engine: QueryEngine):
    """Graceful when nothing matches."""
    rc = cmd_query(engine, target="2025-06-01 12:00", from_time=None, to_time=None,
                   output="/tmp/out", title="test")
    assert rc == 1


def test_query_time_found(engine: QueryEngine, tmp_path: Path):
    """Find a record and generate flamegraph."""
    from cyprof.storage import SampleRecord
    import time
    import calendar
    from datetime import timezone, datetime

    ts = calendar.timegm(time.strptime("2025-06-01 12:00:30", "%Y-%m-%d %H:%M:%S"))
    engine._store.insert(SampleRecord(
        id=0, start_ts=ts, end_ts=ts + 10,
        frequency_hz=11, duration_sec=10,
        file_path="test.zst", file_size_bytes=5000,
        sample_count=42, trigger_mode="normal",
    ))

    out_dir = tmp_path / "flame_out"

    with mock.patch.object(engine._flamegraph, "generate") as m_gen:
        from cyprof.flamegraph import FlameGraphResult
        m_gen.return_value = FlameGraphResult(
            svg_path=out_dir / "flamegraph.svg",
            title="CPU",
            sample_count=42,
            input_files=1,
        )
        rc = cmd_query(engine, target="2025-06-01 12:00", from_time=None, to_time=None,
                       output=str(out_dir), title="CPU")
        assert rc == 0
        m_gen.assert_called_once()


def test_query_range(engine: QueryEngine, tmp_path: Path):
    """Range query finds multiple records."""
    import time
    import calendar

    for offset in [0, 60, 120]:
        ts = calendar.timegm(time.strptime("2025-06-01 12:00:00", "%Y-%m-%d %H:%M:%S")) + offset
        from cyprof.storage import SampleRecord
        engine._store.insert(SampleRecord(
            id=0, start_ts=ts, end_ts=ts + 10,
            frequency_hz=11, duration_sec=10,
            file_path=f"test_{offset}.zst", file_size_bytes=5000,
            sample_count=42, trigger_mode="normal",
        ))

    out_dir = tmp_path / "flame_out"

    with mock.patch.object(engine._flamegraph, "generate") as m_gen:
        from cyprof.flamegraph import FlameGraphResult
        m_gen.return_value = FlameGraphResult(
            svg_path=out_dir / "flamegraph.svg",
            title="CPU",
            sample_count=126,
            input_files=3,
        )
        rc = cmd_query(engine, target=None,
                       from_time="2025-06-01 12:00",
                       to_time="2025-06-01 12:03",
                       output=str(out_dir), title="CPU")
        assert rc == 0


def test_query_range_empty(engine: QueryEngine):
    """Range with no results."""
    rc = cmd_query(engine, target=None,
                   from_time="2025-06-01 12:00",
                   to_time="2025-06-01 12:01",
                   output="/tmp/out", title="test")
    assert rc == 1


# ── cmd_list ────────────────────────────────────────────────────

def test_list_empty(engine: QueryEngine):
    rc = cmd_list(engine, from_time=None, to_time=None, limit=10)
    assert rc == 0  # not an error, just no results


def test_list_with_data(engine: QueryEngine):
    from cyprof.storage import SampleRecord
    ts = 1748779200  # 2025-06-01 12:00 UTC
    engine._store.insert(SampleRecord(
        id=0, start_ts=ts, end_ts=ts + 10,
        frequency_hz=11, duration_sec=10,
        file_path="test.zst", file_size_bytes=5000,
        sample_count=42, trigger_mode="normal",
    ))
    rc = cmd_list(engine, from_time=None, to_time=None, limit=10)
    assert rc == 0


# ── cmd_info ────────────────────────────────────────────────────

def test_info_empty(engine: QueryEngine):
    from cyprof.config import ProfilerConfig
    cfg = ProfilerConfig()
    cfg.storage.data_dir = engine._data_dir
    cfg.storage.db_path = engine._store._db_path
    rc = cmd_info(engine, cfg)
    assert rc == 0


def test_info_with_data(engine: QueryEngine):
    from cyprof.storage import SampleRecord
    from cyprof.config import ProfilerConfig
    import time

    ts = time.time()
    engine._store.insert(SampleRecord(
        id=0, start_ts=ts - 3600, end_ts=ts - 3590,
        frequency_hz=11, duration_sec=10,
        file_path="old.zst", file_size_bytes=5000,
        sample_count=42, trigger_mode="alert",
    ))
    engine._store.insert(SampleRecord(
        id=0, start_ts=ts, end_ts=ts + 10,
        frequency_hz=99, duration_sec=10,
        file_path="new.zst", file_size_bytes=20000,
        sample_count=380, trigger_mode="normal",
    ))

    cfg = ProfilerConfig()
    cfg.storage.data_dir = engine._data_dir
    cfg.storage.db_path = engine._store._db_path
    rc = cmd_info(engine, cfg)
    assert rc == 0


# ── main() CLI entry point ──────────────────────────────────────

def test_main_no_args_shows_help():
    rc = main([])
    assert rc == 0


def test_main_query_time(monkeypatch, tmp_path: Path):
    """Integration: main with --time."""
    import time
    import calendar

    cfg_file = tmp_path / "cyprof.yaml"
    cfg_file.write_text("""
collector:
  frequency_hz: 11
storage:
  data_dir: {data}
  db_path: {meta}
""".format(data=(tmp_path / "data").as_posix(),
           meta=(tmp_path / "meta.db").as_posix()))

    # pre-populate a record at a known time
    from cyprof.storage import MetadataStore, SampleRecord
    ts = calendar.timegm(time.strptime("2025-06-01 12:00:30", "%Y-%m-%d %H:%M:%S"))
    store = MetadataStore(tmp_path / "meta.db")
    store.insert(SampleRecord(
        id=0, start_ts=ts, end_ts=ts + 10,
        frequency_hz=11, duration_sec=10,
        file_path="test.zst", file_size_bytes=5000,
        sample_count=42, trigger_mode="normal",
    ))

    with mock.patch("cyprof.query.FlameGraphGenerator.generate") as m_gen:
        from cyprof.flamegraph import FlameGraphResult
        m_gen.return_value = FlameGraphResult(
            svg_path=tmp_path / "out" / "flamegraph.svg",
            title="CPU",
            sample_count=42,
            input_files=1,
        )
        rc = main([
            "--config", str(cfg_file),
            "--log-level", "ERROR",
            "query",
            "--time", "2025-06-01 12:00",
            "--output", str(tmp_path / "out"),
        ])
        assert rc == 0


def test_main_list(monkeypatch, tmp_path: Path):
    cfg_file = tmp_path / "cyprof.yaml"
    cfg_file.write_text(f"""
collector:
  frequency_hz: 11
storage:
  data_dir: {tmp_path / 'data'}
  db_path: {tmp_path / 'meta.db'}
""")
    rc = main([
        "--config", str(cfg_file),
        "--log-level", "ERROR",
        "list",
    ])
    assert rc == 0


def test_main_info(monkeypatch, tmp_path: Path):
    cfg_file = tmp_path / "cyprof.yaml"
    cfg_file.write_text(f"""
collector:
  frequency_hz: 11
storage:
  data_dir: {tmp_path / 'data'}
  db_path: {tmp_path / 'meta.db'}
""")
    rc = main([
        "--config", str(cfg_file),
        "--log-level", "ERROR",
        "info",
    ])
    assert rc == 0


def test_main_invalid_subcommand():
    """Invalid subcommand exits with code 2."""
    with pytest.raises(SystemExit) as exc:
        main(["unknown_cmd"])
    assert exc.value.code == 2
