"""Tests for cyprof.config."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from cyprof.config import (
    CollectorConfig,
    StorageConfig,
    DaemonConfig,
    ProfilerConfig,
    load_config,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DATA_DIR,
    DEFAULT_DB_PATH,
)


# ── default construction ───────────────────────────────────────

def test_default_config():
    cfg = ProfilerConfig()
    assert cfg.collector.frequency_hz == 11
    assert cfg.collector.duration_sec == 10
    assert cfg.storage.max_size_mb == 500
    assert cfg.storage.max_age_hours == 24
    assert cfg.daemon.sample_interval_sec == 60


# ── YAML file loading ─────────────────────────────────────────

def test_yaml_load(tmp_path: Path):
    yml = tmp_path / "test.yaml"
    yml.write_text("""
collector:
  frequency_hz: 99
  duration_sec: 15
storage:
  max_size_mb: 1000
  data_dir: /tmp/cyprof_data
daemon:
  sample_interval_sec: 120
""")
    cfg = load_config(yml)
    assert cfg.collector.frequency_hz == 99
    assert cfg.collector.duration_sec == 15
    assert cfg.storage.max_size_mb == 1000
    assert cfg.storage.data_dir == Path("/tmp/cyprof_data")
    assert cfg.daemon.sample_interval_sec == 120


# ── env override ───────────────────────────────────────────────

def test_env_override(tmp_path: Path, monkeypatch):
    yml = tmp_path / "test.yaml"
    yml.write_text("collector:\n  frequency_hz: 99\n")

    monkeypatch.setenv("CYPROF_FREQUENCY_HZ", "49")
    monkeypatch.setenv("CYPROF_DURATION_SEC", "5")

    cfg = load_config(yml)
    assert cfg.collector.frequency_hz == 49
    assert cfg.collector.duration_sec == 5


def test_env_takes_priority_over_defaults(monkeypatch):
    monkeypatch.setenv("CYPROF_FREQUENCY_HZ", "199")
    monkeypatch.setenv("CYPROF_MAX_SIZE_MB", "200")

    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.collector.frequency_hz == 199
    assert cfg.storage.max_size_mb == 200


def test_env_bool_parsing(monkeypatch):
    monkeypatch.setenv("CYPROF_CALLGRAPH", "false")
    cfg = load_config("/nonexistent/config.yaml")
    assert not cfg.collector.callgraph

    monkeypatch.setenv("CYPROF_CALLGRAPH", "1")
    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.collector.callgraph


def test_env_extra_args(monkeypatch):
    monkeypatch.setenv("CYPROF_EXTRA_ARGS", "--pid 1234 --tid 5678")
    cfg = load_config("/nonexistent/config.yaml")
    assert cfg.collector.extra_args == ("--pid", "1234", "--tid", "5678")


# ── validation ─────────────────────────────────────────────────

def test_rejects_invalid_frequency():
    with pytest.raises(ValueError, match="frequency_hz"):
        load_config(_build_mini_yaml("collector.frequency_hz", 0))
    with pytest.raises(ValueError, match="frequency_hz"):
        load_config(_build_mini_yaml("collector.frequency_hz", 200000))


def test_rejects_invalid_duration():
    with pytest.raises(ValueError, match="duration_sec"):
        load_config(_build_mini_yaml("collector.duration_sec", 0))
    with pytest.raises(ValueError, match="duration_sec"):
        load_config(_build_mini_yaml("collector.duration_sec", 3601))


def test_rejects_tiny_max_size():
    with pytest.raises(ValueError, match="max_size_mb"):
        load_config(_build_mini_yaml("storage.max_size_mb", 5))


def test_rejects_invalid_comp_level():
    with pytest.raises(ValueError, match="comp_level"):
        load_config(_build_mini_yaml("storage.comp_level", 100))


# ── helpers ────────────────────────────────────────────────────

def _build_mini_yaml(dotted_key: str, value: int | str) -> Path:
    """Build a temp YAML file with one deeply-nested key set to *value*.

    ``dotted_key`` is a dot-separated path, e.g.
    ``"collector.frequency_hz"`` → ``{collector: {frequency_hz: <value>}}``
    """
    parts = dotted_key.split(".")
    # walk backwards to build nested dict
    inner: object = value
    for part in reversed(parts):
        inner = {part: inner}
    assert isinstance(inner, dict)

    import yaml
    p = Path(tempfile.mkstemp(suffix=".yaml")[1])
    p.write_text(yaml.safe_dump(inner))
    return p
