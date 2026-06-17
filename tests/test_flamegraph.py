"""Tests for cyprof.flamegraph."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest import mock

import pytest

from cyprof.flamegraph import (
    FlameGraphGenerator,
    FlameGraphResult,
    FlameGraphError,
    _estimate_sample_count,
)


# ── fixtures ────────────────────────────────────────────────────

@pytest.fixture
def gen() -> FlameGraphGenerator:
    return FlameGraphGenerator(perf_path="perf")


@pytest.fixture
def sample_perf_script() -> str:
    """Realistic perf-script output snippet."""
    return """\
perf 12345 [000] 123456.789: cpu-clock:
\tffffffff81000000 native_write_msr
\tffffffff81000100 nop
\t00007f0000000000 [unknown]

perf 12346 [001] 123456.790: cpu-clock:
\tffffffff81000000 native_write_msr
\tffffffff81000200 do_syscall_64
\t00007f0000000100 libc_start
"""


@pytest.fixture
def sample_folded() -> str:
    """Folded-stack output matching the script above."""
    return (
        "perf;native_write_msr;nop;[unknown] 42\n"
        "perf;native_write_msr;do_syscall_64;libc_start 58\n"
    )


@pytest.fixture
def sample_svg() -> str:
    return "<svg>fake flamegraph</svg>"


# ── estimate_sample_count ───────────────────────────────────────

def test_estimate_sample_count(sample_folded: str):
    assert _estimate_sample_count(sample_folded) == 100  # 42 + 58


def test_estimate_sample_count_empty():
    assert _estimate_sample_count("") == 0


def test_estimate_sample_count_junk():
    assert _estimate_sample_count("no count here\n") == 0


# ── generate(): end-to-end with mocks ───────────────────────────

def test_generate_single_file(
    gen: FlameGraphGenerator,
    tmp_path: Path,
    sample_perf_script: str,
    sample_folded: str,
    sample_svg: str,
):
    """Full pipeline on one uncompressed perf.data."""
    data_file = tmp_path / "perf.data"
    data_file.write_text("dummy")
    out_dir = tmp_path / "out"

    with mock.patch.object(gen, "_run_cmd") as m_run:
        # 1st call: perf script
        # 2nd call: stackcollapse
        # 3rd call: flamegraph
        m_run.side_effect = [sample_perf_script, sample_folded, sample_svg]

        result = gen.generate([data_file], out_dir, title="Test")

    assert m_run.call_count == 3
    assert result.sample_count == 100
    assert result.input_files == 1
    assert result.svg_path.name == "flamegraph.svg"
    assert (out_dir / "flamegraph.svg").exists()


def test_generate_multiple_files(
    gen: FlameGraphGenerator,
    tmp_path: Path,
    sample_perf_script: str,
    sample_folded: str,
    sample_svg: str,
):
    """Merge two perf.data files."""
    f1 = tmp_path / "perf1.data"
    f2 = tmp_path / "perf2.data"
    f1.write_text("dummy")
    f2.write_text("dummy")
    out_dir = tmp_path / "out"

    with mock.patch.object(gen, "_run_cmd") as m_run:
        # perf script called twice (once per file), then collapse, then flamegraph
        m_run.side_effect = [
            sample_perf_script,
            sample_perf_script,
            sample_folded,
            sample_svg,
        ]
        result = gen.generate([f1, f2], out_dir, title="Merged")

    assert m_run.call_count == 4  # 2×perf script + collapse + flamegraph
    assert result.input_files == 2
    assert result.sample_count == 100


def test_generate_zst_file(
    gen: FlameGraphGenerator,
    tmp_path: Path,
    sample_perf_script: str,
    sample_folded: str,
    sample_svg: str,
):
    """Pipeline on a .zst compressed file."""
    data_file = tmp_path / "perf.data.zst"
    data_file.write_text("dummy")
    out_dir = tmp_path / "out"

    with mock.patch("subprocess.Popen") as mp:
        # mock zstd decompress
        mp_zstd = mock.MagicMock()
        mp_zstd.stdout = None
        mp_zstd.wait.return_value = 0

        # mock perf script reading from stdin
        mp_perf = mock.MagicMock()
        mp_perf.communicate.return_value = (sample_perf_script.encode(), b"")
        mp_perf.returncode = 0

        mp.side_effect = [mp_zstd, mp_perf]

        with mock.patch.object(gen, "_collapse_stacks", return_value=sample_folded):
            with mock.patch.object(gen, "_render_svg", return_value=sample_svg):
                result = gen.generate([data_file], out_dir)

    assert result.input_files == 1
    assert result.sample_count == 100
    assert (out_dir / "flamegraph.svg").exists()


# ── error cases ─────────────────────────────────────────────────

def test_file_not_found(gen: FlameGraphGenerator, tmp_path: Path):
    with pytest.raises(FlameGraphError, match="not found"):
        gen.generate([tmp_path / "missing.data"], tmp_path / "out")


def test_perf_script_empty_output(gen: FlameGraphGenerator, tmp_path: Path):
    data = tmp_path / "perf.data"
    data.write_text("dummy")

    with mock.patch.object(gen, "_run_cmd", return_value="   \n"):
        with pytest.raises(FlameGraphError, match="no output"):
            gen.generate([data], tmp_path / "out")


def test_collapse_failure(gen: FlameGraphGenerator, tmp_path: Path, sample_perf_script: str):
    data = tmp_path / "perf.data"
    data.write_text("dummy")

    with mock.patch.object(gen, "_run_cmd") as m_run:
        m_run.side_effect = [
            sample_perf_script,
            FlameGraphError("stackcollapse bombed"),
        ]
        with pytest.raises(FlameGraphError, match="bombed"):
            gen.generate([data], tmp_path / "out")


def test_render_failure(gen: FlameGraphGenerator, tmp_path: Path, sample_perf_script: str, sample_folded: str):
    data = tmp_path / "perf.data"
    data.write_text("dummy")

    with mock.patch.object(gen, "_run_cmd") as m_run:
        m_run.side_effect = [
            sample_perf_script,
            sample_folded,
            FlameGraphError("flamegraph bombed"),
        ]
        with pytest.raises(FlameGraphError, match="bombed"):
            gen.generate([data], tmp_path / "out")


def test_timeout(gen: FlameGraphGenerator, tmp_path: Path):
    data = tmp_path / "perf.data"
    data.write_text("dummy")

    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 120)):
        with pytest.raises(FlameGraphError, match="timed out"):
            gen.generate([data], tmp_path / "out")


def test_file_not_found_error(gen: FlameGraphGenerator, tmp_path: Path):
    data = tmp_path / "perf.data"
    data.write_text("dummy")

    with mock.patch("subprocess.run", side_effect=FileNotFoundError("perf not found")):
        with pytest.raises(FlameGraphError, match="not found"):
            gen.generate([data], tmp_path / "out")


# ── subtitle handling ───────────────────────────────────────────

def test_subtitle_in_title(
    gen: FlameGraphGenerator,
    tmp_path: Path,
    sample_perf_script: str,
    sample_folded: str,
    sample_svg: str,
):
    data = tmp_path / "perf.data"
    data.write_text("x")
    out_dir = tmp_path / "out"

    with mock.patch.object(gen, "_run_cmd") as m_run:
        m_run.side_effect = [sample_perf_script, sample_folded, sample_svg]
        result = gen.generate(
            [data], out_dir,
            title="CPU Flame Graph",
            subtitle="2026-06-17 14:30 - 14:35",
        )

    assert "—" in result.title
    assert "14:30" in result.title


# ── partial API (perf_script / collapse / render) ──────────────

def test_perf_script_method(gen: FlameGraphGenerator, tmp_path: Path, sample_perf_script: str):
    data = tmp_path / "perf.data"
    data.write_text("dummy")

    with mock.patch.object(gen, "_run_cmd", return_value=sample_perf_script):
        result = gen.perf_script(data)
        assert "native_write_msr" in result


def test_collapse_method(gen: FlameGraphGenerator, sample_perf_script: str, sample_folded: str):
    with mock.patch.object(gen, "_run_cmd", return_value=sample_folded):
        result = gen.collapse(sample_perf_script)
        assert ";" in result
        assert "42" in result


def test_render_method(gen: FlameGraphGenerator, sample_folded: str, sample_svg: str):
    with mock.patch.object(gen, "_run_cmd", return_value=sample_svg):
        result = gen.render(sample_folded, title="Test")
        assert "<svg>" in result


# ── edge cases ──────────────────────────────────────────────────

def test_zst_decompress_failure(gen: FlameGraphGenerator, tmp_path: Path):
    """When zstd decompress fails, FlameGraphError is raised."""
    data = tmp_path / "bad.perf.data.zst"
    data.write_text("corrupt")

    with mock.patch("subprocess.Popen") as mp:
        mp_zstd = mock.MagicMock()
        mp_zstd.stdout = mock.MagicMock()
        mp_zstd.wait.return_value = 1  # zstd failure
        mp_zstd.stderr.read.return_value = b"corrupted data"

        mp_perf = mock.MagicMock()
        mp_perf.communicate.return_value = (b"", b"")
        mp_perf.returncode = 0
        mp.side_effect = [mp_zstd, mp_perf]

        with pytest.raises(FlameGraphError, match="decompress"):
            gen.generate([data], tmp_path / "out")


def test_perf_script_nonzero_exit_raises(gen: FlameGraphGenerator, tmp_path: Path):
    """perf script non-zero exit raises FlameGraphError."""
    data = tmp_path / "perf.data"
    data.write_text("dummy")

    with mock.patch("subprocess.run") as m_run:
        m_run.return_value = mock.MagicMock(
            returncode=1,
            stdout=b"",
            stderr=b"perf warning",
        )
        with pytest.raises(FlameGraphError, match="perf script"):
            gen.generate([data], tmp_path / "out")


def test_run_cmd_file_not_found(gen: FlameGraphGenerator, tmp_path: Path):
    """_run_cmd raises FlameGraphError on FileNotFoundError."""
    data = tmp_path / "perf.data"
    data.write_text("dummy")

    with mock.patch("subprocess.run", side_effect=FileNotFoundError("perf not installed")):
        with pytest.raises(FlameGraphError, match="not found"):
            gen.generate([data], tmp_path / "out")


def test_folded_mixed_content():
    """Sample count estimation handles mixed valid/invalid lines."""
    text = "func1;func2 10\nbroken line\nfunc3 20\n\n  \n"
    from cyprof.flamegraph import _estimate_sample_count
    assert _estimate_sample_count(text) == 30


def test_folded_negative_count():
    """Negative counts are skipped (shouldn't happen, but be safe)."""
    from cyprof.flamegraph import _estimate_sample_count
    assert _estimate_sample_count("func -5\n") == 0


def test_flamegraph_escapes_title(gen: FlameGraphGenerator, tmp_path: Path):
    """Title with single quotes gets escaped for shell."""
    data = tmp_path / "perf.data"
    data.write_text("dummy")

    with mock.patch.object(gen, "_run_cmd") as m_run:
        m_run.side_effect = ["stacks\n", "collapsed\n", "<svg></svg>"]
        gen.generate([data], tmp_path / "out", title="CPU's Flame Graph")
        # just ensure no crash with special chars
        assert m_run.call_count == 3


def test_run_cmd_binary_not_found(gen: FlameGraphGenerator):
    """_run_cmd raises FlameGraphError when binary isn't on PATH."""
    with mock.patch("subprocess.run", side_effect=FileNotFoundError("cmd not found")):
        with pytest.raises(FlameGraphError, match="not found"):
            gen._run_cmd(["nonexistent_binary"], desc="test")
