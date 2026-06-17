"""Tests for cyprof.collector — mostly unit tests (no real perf needed)."""

from __future__ import annotations

import calendar
import errno
import subprocess
import time
from pathlib import Path
from unittest import mock

import pytest

from cyprof.collector import (
    PerfCollector,
    CollectResult,
    CollectorError,
)
from cyprof.config import CollectorConfig


# ── fixtures ────────────────────────────────────────────────────

@pytest.fixture
def cfg() -> CollectorConfig:
    return CollectorConfig(
        frequency_hz=11,
        duration_sec=10,
        callgraph=True,
        perf_path="perf",
    )


@pytest.fixture
def collector(cfg: CollectorConfig, tmp_path: Path) -> PerfCollector:
    return PerfCollector(cfg, data_dir=tmp_path / "data")


# ── filename generation ─────────────────────────────────────────

def test_make_filename_format(collector: PerfCollector):
    # use calendar.timegm to get UTC epoch (mktime gives local-time epoch)
    ts = calendar.timegm(time.strptime("2026-06-17 14:30:25", "%Y-%m-%d %H:%M:%S"))
    name = collector._make_filename(ts)
    assert name.startswith("20260617_143025_")
    assert "11hz" in name
    assert name.endswith(".perf.data.zst")


def test_make_filename_includes_frequency(collector: PerfCollector):
    collector._cfg.frequency_hz = 99
    name = collector._make_filename(time.time())
    assert "99hz" in name


# ── command building ────────────────────────────────────────────

def test_build_cmd_basic(collector: PerfCollector):
    cmd = collector._build_cmd("/tmp/test.perf.data")
    assert "perf" in cmd[0]
    assert "record" in cmd
    assert "-F" in cmd
    assert "11" in cmd
    assert "-a" in cmd
    assert "--call-graph" in cmd
    assert "dwarf,16384" in cmd
    assert "-o" in cmd
    assert "/tmp/test.perf.data" in cmd
    assert "sleep" in cmd
    assert "10" in cmd


def test_build_cmd_no_callgraph(collector: PerfCollector):
    collector._cfg.callgraph = False
    cmd = collector._build_cmd("/tmp/x")
    assert "--call-graph" not in cmd


def test_build_cmd_extra_args(collector: PerfCollector):
    collector._cfg.extra_args = ("--pid", "1234")
    cmd = collector._build_cmd("/tmp/x")
    idx = cmd.index("--pid")
    assert cmd[idx + 1] == "1234"


def test_build_cmd_pipe_output(collector: PerfCollector):
    cmd = collector._build_cmd("-")
    assert cmd[cmd.index("-o") + 1] == "-"


# ── sample count parsing ────────────────────────────────────────

def test_parse_sample_count_typical():
    text = "[ perf record: Captured and wrote 0.123 MB perf.data (1234 samples) ]"
    assert PerfCollector._parse_sample_count(text) == 1234


def test_parse_sample_count_no_match():
    assert PerfCollector._parse_sample_count("no samples here") == 0


def test_parse_sample_count_singular():
    text = "[ perf record: Captured and wrote 0.001 MB perf.data (1 sample) ]"
    assert PerfCollector._parse_sample_count(text) == 1


# ── availability probes ─────────────────────────────────────────

def test_has_perf(collector: PerfCollector):
    # will be False on this Windows CI, but should never raise
    result = collector.has_perf
    assert isinstance(result, bool)


def test_has_zstd(collector: PerfCollector):
    result = collector.has_zstd
    assert isinstance(result, bool)


# ── collect() with mocked subprocess (pipe mode) ────────────────

def test_collect_pipe_mode_success(collector: PerfCollector, tmp_path: Path):
    """Simulate a successful perf+zstd pipeline."""
    collector._has_zstd = True  # force pipe mode

    with mock.patch("subprocess.Popen") as mp:
        # mock perf (stdout must be non-None since code asserts it)
        mp_perf = mock.MagicMock()
        mp_perf.stdout = mock.MagicMock()  # pipe to zstd
        mp_perf.stderr = mock.MagicMock()
        mp_perf.stderr.read.return_value = (
            b"[ perf record: Captured and wrote 0.050 MB perf.data (42 samples) ]"
        )
        mp_perf.wait.return_value = 0

        # mock zstd
        mp_zstd = mock.MagicMock()
        mp_zstd.stderr.read.return_value = b""
        mp_zstd.wait.return_value = 0

        mp.side_effect = [mp_perf, mp_zstd]

        with mock.patch.object(collector, "_build_cmd", return_value=["mock-perf", "record", "-o", "-"]):
            with mock.patch.object(collector, "_make_filename", return_value="test.zst"):
                with mock.patch.object(Path, "rename") as m_rename:
                    with mock.patch.object(Path, "exists", return_value=True):
                        with mock.patch.object(Path, "stat") as m_stat:
                            m_stat.return_value.st_size = 5000
                            result = collector.collect()
                            assert result is not None
                            assert result.sample_count == 42
                            assert result.file_size_bytes == 5000
                            assert result.exit_code == 0
                            assert m_rename.called


def test_collect_zstd_failure(collector: PerfCollector):
    """When zstd fails, collect() returns None."""
    with mock.patch("subprocess.Popen") as mp:
        mp_perf = mock.MagicMock()
        mp_perf.stdout = mock.MagicMock()
        mp_perf.stderr.read.return_value = b"some samples"
        mp_perf.wait.return_value = 0

        mp_zstd = mock.MagicMock()
        mp_zstd.stderr.read.return_value = b"zstd: error"
        mp_zstd.wait.return_value = 1

        mp.side_effect = [mp_perf, mp_zstd]

        with mock.patch.object(collector, "_build_cmd", return_value=["mock"]):
            with mock.patch.object(collector, "_make_filename", return_value="test.zst"):
                with mock.patch.object(Path, "rename") as m_rename:
                    result = collector.collect()
                    assert result is None
                    # the .tmp file should NOT be renamed
                    m_rename.assert_not_called()


def test_collect_perf_not_found_raises(collector: PerfCollector):
    """When perf binary doesn't exist, FileNotFoundError becomes CollectorError."""
    with mock.patch("subprocess.Popen", side_effect=FileNotFoundError(errno.ENOENT, "perf not found")):
        with pytest.raises(CollectorError, match="Required binary"):
            collector.collect()


def test_collect_empty_output_returns_none(collector: PerfCollector):
    """Zero-byte output files are discarded."""
    with mock.patch("subprocess.Popen") as mp:
        mp_perf = mock.MagicMock()
        mp_perf.stdout = mock.MagicMock()
        mp_perf.stderr.read.return_value = b"nothing"
        mp_perf.wait.return_value = 0

        mp_zstd = mock.MagicMock()
        mp_zstd.stderr.read.return_value = b""
        mp_zstd.wait.return_value = 0
        mp.side_effect = [mp_perf, mp_zstd]

        with mock.patch.object(collector, "_build_cmd", return_value=["mock"]):
            with mock.patch.object(collector, "_make_filename", return_value="test.zst"):
                with mock.patch.object(Path, "rename"):
                    with mock.patch.object(Path, "stat") as m_stat:
                        m_stat.return_value.st_size = 0  # empty!
                        result = collector.collect()
                        assert result is None


# ── cleanup utility ─────────────────────────────────────────────

def test_cleanup_removes_file(tmp_path: Path):
    p = tmp_path / "to_delete"
    p.write_text("x")
    PerfCollector._cleanup(p)
    assert not p.exists()


def test_cleanup_noop_on_missing(tmp_path: Path):
    p = tmp_path / "missing"
    PerfCollector._cleanup(p)  # should not raise


def test_cleanup_oserror_caught(tmp_path: Path, monkeypatch):
    """_cleanup should not raise even when unlink fails."""
    p = tmp_path / "doomed"
    p.write_text("x")
    with mock.patch.object(Path, "unlink", side_effect=OSError("permission denied")):
        PerfCollector._cleanup(p)  # should not raise


# ── collect(): no-zstd (direct-to-file) path ─────────────────────

def test_collect_no_zstd_success(collector: PerfCollector):
    """When zstd is unavailable, perf writes directly to a temp file."""
    collector._has_zstd = False  # force direct-to-file mode

    with mock.patch("subprocess.run") as m_run:
        m_run.return_value = mock.MagicMock(returncode=0, stderr=b"")

        with mock.patch.object(collector, "_build_cmd", return_value=["mock-perf", "record"]):
            with mock.patch.object(collector, "_make_filename", return_value="test.perf.data"):
                with mock.patch.object(Path, "exists", return_value=True):
                    with mock.patch.object(Path, "rename"):
                        with mock.patch.object(Path, "stat") as m_stat:
                            m_stat.return_value.st_size = 5000
                            result = collector.collect()
                            assert result is not None
                            assert result.exit_code == 0


def test_collect_timeout(collector: PerfCollector):
    """subprocess.TimeoutExpired returns None."""
    collector._has_zstd = False
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("perf", 120)):
        with mock.patch.object(collector, "_build_cmd", return_value=["mock"]):
            with mock.patch.object(collector, "_make_filename", return_value="test"):
                result = collector.collect()
                assert result is None


def test_collect_perf_nonzero_exit(collector: PerfCollector):
    """perf exiting with non-zero code should still produce a result (just logged)."""
    collector._has_zstd = False
    with mock.patch("subprocess.run") as m_run:
        m_run.return_value = mock.MagicMock(
            returncode=1,
            stderr=b"[ perf record: Captured and wrote 0.050 MB perf.data (42 samples) ]",
        )
        with mock.patch.object(collector, "_build_cmd", return_value=["mock"]):
            with mock.patch.object(collector, "_make_filename", return_value="test.perf.data"):
                with mock.patch.object(Path, "exists", return_value=True):
                    with mock.patch.object(Path, "rename"):
                        with mock.patch.object(Path, "stat") as m_stat:
                            m_stat.return_value.st_size = 5000
                            result = collector.collect()
                            assert result is not None
                            assert result.exit_code == 1  # non-zero, but data exists


def test_collect_unexpected_exception(collector: PerfCollector):
    """Arbitrary exceptions during collection (inside try block) return None."""
    with mock.patch.object(collector, "_build_cmd", return_value=["mock-perf"]):
        with mock.patch.object(collector, "_make_filename", return_value="test"):
            # Inject exception into the try block by breaking subprocess
            collector._has_zstd = False
            with mock.patch("subprocess.run", side_effect=RuntimeError("boom")):
                result = collector.collect()
                assert result is None


def test_collect_zstd_nonzero_keeps_tmp(collector: PerfCollector):
    """When zstd exits non-zero, tmp file is kept for diagnostics."""
    collector._has_zstd = True

    with mock.patch("subprocess.Popen") as mp:
        mp_perf = mock.MagicMock()
        mp_perf.stdout = mock.MagicMock()
        mp_perf.stderr.read.return_value = b"perf ok"
        mp_perf.wait.return_value = 0

        mp_zstd = mock.MagicMock()
        mp_zstd.stderr.read.return_value = b"zstd crash"
        mp_zstd.wait.return_value = 1  # failure
        mp.side_effect = [mp_perf, mp_zstd]

        with mock.patch.object(collector, "_build_cmd", return_value=["mock"]):
            with mock.patch.object(collector, "_make_filename", return_value="test.zst"):
                with mock.patch.object(Path, "rename") as m_rename:
                    result = collector.collect()
                    assert result is None
                    # tmp file should NOT be renamed to final
                    m_rename.assert_not_called()


def test_collect_file_size_zero_after_rename(collector: PerfCollector):
    """When file is 0 bytes after rename, it's cleaned up."""
    collector._has_zstd = False
    with mock.patch("subprocess.run") as m_run:
        m_run.return_value = mock.MagicMock(returncode=0, stderr=b"")
        with mock.patch.object(collector, "_build_cmd", return_value=["mock"]):
            with mock.patch.object(collector, "_make_filename", return_value="test.perf.data"):
                with mock.patch.object(Path, "rename"):
                    with mock.patch.object(Path, "exists", return_value=True):
                        with mock.patch.object(Path, "stat") as m_stat:
                            m_stat.return_value.st_size = 0  # empty
                            result = collector.collect()
                            assert result is None
