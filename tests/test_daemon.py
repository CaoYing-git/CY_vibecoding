"""Tests for cyprof.daemon."""

from __future__ import annotations

import json
import signal
import time
from pathlib import Path
from unittest import mock

import pytest

from cyprof.config import ProfilerConfig
from cyprof.collector import CollectResult, PerfCollector
from cyprof.storage import MetadataStore, SampleRecord
from cyprof.daemon import Daemon, main, _write_health_file, _sd_notify


# ── fixtures ────────────────────────────────────────────────────

@pytest.fixture
def cfg() -> ProfilerConfig:
    return ProfilerConfig()


@pytest.fixture
def daemon(cfg: ProfilerConfig, tmp_path: Path) -> Daemon:
    cfg.storage.data_dir = tmp_path / "data"
    cfg.storage.db_path = tmp_path / "meta.db"
    return Daemon(cfg)


@pytest.fixture
def mock_result() -> CollectResult:
    return CollectResult(
        path=Path("/tmp/data/20260617_143000_11hz.perf.data.zst"),
        start_ts=time.time(),
        end_ts=time.time() + 10,
        sample_count=42,
        file_size_bytes=5000,
        exit_code=0,
    )


# ── health file ─────────────────────────────────────────────────

def test_write_health_file(tmp_path: Path):
    p = tmp_path / "health.json"
    _write_health_file(p, "running", {"samples": 5})
    assert p.exists()
    data = json.loads(p.read_text())
    assert data["status"] == "running"
    assert data["samples"] == 5
    assert "ts" in data


def test_write_health_file_atomic(tmp_path: Path):
    p = tmp_path / "health.json"
    # pre-existing file should be replaced, not appended
    p.write_text("garbage")
    _write_health_file(p, "running")
    data = json.loads(p.read_text())
    assert data["status"] == "running"
    assert not (tmp_path / "health.json.tmp").exists()


# ── daemon init ─────────────────────────────────────────────────

def test_daemon_creates_data_dir(daemon: Daemon):
    assert daemon._data_dir.exists()


def test_daemon_subcomponents(daemon: Daemon):
    assert daemon._collector is not None
    assert daemon._store is not None
    assert daemon._ringbuf is not None


def test_daemon_health_path(daemon: Daemon):
    assert daemon._health_path.name == "health.json"
    assert daemon._health_path.parent == daemon._data_dir.parent


# ── single tick ─────────────────────────────────────────────────

def test_tick_collect_and_index(
    daemon: Daemon, mock_result: CollectResult
):
    """A successful tick: collect → index → rotate."""
    with mock.patch.object(daemon._collector, "collect", return_value=mock_result):
        daemon._tick()

    assert daemon._collect_count == 1
    assert daemon._last_collect_ok is True
    assert daemon._store.count() == 1

    record = daemon._store.query()[0]
    assert record.sample_count == 42
    assert record.file_size_bytes == 5000


def test_tick_empty_collection(
    daemon: Daemon
):
    """Empty collection (None) increments error, not sample count."""
    with mock.patch.object(daemon._collector, "collect", return_value=None):
        daemon._tick()

    assert daemon._collect_count == 0
    assert daemon._error_count == 1
    assert daemon._last_collect_ok is False
    assert daemon._store.count() == 0


def test_tick_rotates_before_and_after(
    daemon: Daemon, mock_result: CollectResult
):
    """Rotation is called both before and after collection."""
    with mock.patch.object(daemon._ringbuf, "rotate") as m_rotate:
        with mock.patch.object(daemon._collector, "collect", return_value=mock_result):
            daemon._tick()

    assert m_rotate.call_count == 2  # before + after


# ── disk watermark ──────────────────────────────────────────────

def test_tick_fatal_stops_daemon(daemon: Daemon):
    """Fatal disk watermark stops the daemon."""
    with mock.patch.object(daemon._ringbuf, "check_disk_watermark") as m_wm:
        from cyprof.storage import DiskWatermark
        m_wm.return_value = DiskWatermark(
            free_bytes=100, total_bytes=10000, used_pct=99.0, level="fatal"
        )
        daemon._running = True
        daemon._tick()
        assert daemon._running is False


def test_tick_emergency_backs_off(daemon: Daemon):
    """Emergency watermark triggers aggressive rotation + 30s pause."""
    with mock.patch.object(daemon._ringbuf, "check_disk_watermark") as m_wm:
        with mock.patch.object(daemon._ringbuf, "emergency_rotate") as m_er:
            from cyprof.storage import DiskWatermark
            m_wm.return_value = DiskWatermark(
                free_bytes=500, total_bytes=10000, used_pct=95.0, level="emergency"
            )
            # patch time.sleep so we don't actually wait
            with mock.patch("time.sleep") as m_sleep:
                daemon._running = True
                daemon._tick()
                m_er.assert_called_once()
                m_sleep.assert_called_once_with(30)


# ── run loop ────────────────────────────────────────────────────

def test_run_exits_when_no_perf(daemon: Daemon):
    """run() returns 1 when perf binary is not found."""
    with mock.patch.object(PerfCollector, "has_perf",
                           new_callable=mock.PropertyMock, return_value=False):
        ec = daemon.run()
        assert ec == 1


def test_run_one_tick_then_shutdown(
    daemon: Daemon, mock_result: CollectResult
):
    """Simulate one tick then shutdown via signal."""
    tick_count = [0]

    def fake_tick():
        tick_count[0] += 1
        if tick_count[0] >= 1:
            daemon.shutdown()  # stop after one tick

    with mock.patch.object(PerfCollector, "has_perf",
                           new_callable=mock.PropertyMock, return_value=True):
        with mock.patch.object(daemon._collector, "collect", return_value=mock_result):
            with mock.patch.object(daemon, "_tick", side_effect=fake_tick):
                ec = daemon.run()
                assert ec == 0
                assert tick_count[0] == 1


def test_run_writes_health_on_exit(
    daemon: Daemon, mock_result: CollectResult
):
    """Health file should exist with 'stopped' status after run exits."""
    with mock.patch.object(PerfCollector, "has_perf",
                           new_callable=mock.PropertyMock, return_value=True):
        with mock.patch.object(daemon._collector, "collect", return_value=mock_result):
            # make the first tick call shutdown
            with mock.patch.object(daemon, "_tick", side_effect=lambda: daemon.shutdown()):
                daemon.run()

    assert daemon._health_path.exists()
    data = json.loads(daemon._health_path.read_text())
    assert data["status"] == "stopped"


# ── trigger mode ────────────────────────────────────────────────

def test_trigger_mode_normal(daemon: Daemon):
    daemon._disk_level = "normal"
    assert daemon._current_trigger_mode() == "normal"


def test_trigger_mode_alert_on_disk_issue(daemon: Daemon):
    daemon._disk_level = "emergency"
    assert daemon._current_trigger_mode() == "alert"
    daemon._disk_level = "cap"
    assert daemon._current_trigger_mode() == "alert"


# ── signal handling ─────────────────────────────────────────────

def test_signal_handler_stops_daemon(daemon: Daemon):
    daemon._running = True
    daemon._handle_signal(signal.SIGTERM, None)
    assert daemon._running is False


# ── CLI entry point ─────────────────────────────────────────────

def test_main_invalid_config(tmp_path: Path):
    """A config with invalid frequency should return exit code 2."""
    cfg_file = tmp_path / "bad.yaml"
    cfg_file.write_text("collector:\n  frequency_hz: 0\n")
    rc = main(["--config", str(cfg_file), "--log-level", "ERROR"])
    assert rc == 2


def test_main_no_perf_returns_1(monkeypatch, tmp_path: Path):
    """On a system without perf, main should return 1."""
    cfg_file = tmp_path / "test.yaml"
    cfg_file.write_text("collector:\n  frequency_hz: 11\n")

    with mock.patch(
        "cyprof.daemon.Daemon.run", return_value=1
    ) as m_run:
        rc = main(["--config", str(cfg_file), "--log-level", "CRITICAL"])
        assert rc == 1


# ── interruptible sleep ─────────────────────────────────────────

def test_interruptible_sleep_exits_early(daemon: Daemon):
    """Sleep should exit immediately if running=False."""
    daemon._running = False
    t0 = time.time()
    daemon._interruptible_sleep(10.0)
    assert time.time() - t0 < 1.0  # exited quickly


def test_interruptible_sleep_stops_midway(daemon: Daemon):
    """Sleep that's interrupted mid-way should not block forever."""
    daemon._running = True

    # schedule shutdown after 0.1s
    import threading
    def stopper():
        time.sleep(0.1)
        daemon.shutdown()

    t = threading.Thread(target=stopper)
    t0 = time.time()
    t.start()
    daemon._interruptible_sleep(10.0)
    t.join()
    elapsed = time.time() - t0
    assert elapsed < 2.0  # should exit well before 10s
