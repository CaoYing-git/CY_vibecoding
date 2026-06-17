"""cyprof configuration — load from YAML file with env-var override support."""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# ── default paths ──────────────────────────────────────────────
DEFAULT_CONFIG_PATH = Path("/etc/cyprof/cyprof.yaml")
DEFAULT_DATA_DIR = Path("/var/lib/cyprof/data")
DEFAULT_DB_PATH = Path("/var/lib/cyprof/metadata.db")


@dataclass
class CollectorConfig:
    """Perf-record knobs."""

    frequency_hz: int = 11
    duration_sec: int = 10
    callgraph: bool = True
    perf_path: str = "perf"  # allow override for non-standard installs
    extra_args: tuple[str, ...] = ()  # e.g. ("--pid", "1234")


@dataclass
class StorageConfig:
    """On-disk storage and rotation policy."""

    data_dir: Path = DEFAULT_DATA_DIR
    db_path: Path = DEFAULT_DB_PATH
    max_size_mb: int = 500
    max_age_hours: int = 24
    comp_level: int = 3  # zstd compression level (1=fast, 19=best)


@dataclass
class DaemonConfig:
    """Daemon lifecycle parameters."""

    sample_interval_sec: int = 60
    health_check_sec: int = 30


@dataclass
class ProfilerConfig:
    """Root configuration object."""

    collector: CollectorConfig = field(default_factory=CollectorConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)


# ── env → field mapping (collector) ────────────────────────────
_ENV_MAP: dict[str, tuple[str, type]] = {
    # env var                      -> (dotted-field,    type)
    "CYPROF_FREQUENCY_HZ":         ("collector.frequency_hz",       int),
    "CYPROF_DURATION_SEC":         ("collector.duration_sec",       int),
    "CYPROF_CALLGRAPH":            ("collector.callgraph",          lambda v: v.lower() in ("1","true","yes")),
    "CYPROF_PERF_PATH":            ("collector.perf_path",          str),
    "CYPROF_EXTRA_ARGS":           ("collector.extra_args",         lambda v: tuple(v.split())),
    "CYPROF_DATA_DIR":             ("storage.data_dir",             Path),
    "CYPROF_DB_PATH":              ("storage.db_path",              Path),
    "CYPROF_MAX_SIZE_MB":          ("storage.max_size_mb",          int),
    "CYPROF_MAX_AGE_HOURS":        ("storage.max_age_hours",        int),
    "CYPROF_COMP_LEVEL":           ("storage.comp_level",           int),
    "CYPROF_SAMPLE_INTERVAL_SEC":  ("daemon.sample_interval_sec",   int),
    "CYPROF_HEALTH_CHECK_SEC":     ("daemon.health_check_sec",      int),
}


def _apply_env_overrides(cfg: ProfilerConfig) -> ProfilerConfig:
    """Walk **_ENV_MAP** and set fields when the env var is defined."""
    for env_name, (dotted, caster) in _ENV_MAP.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        value = caster(raw)

        # dotted: "collector.frequency_hz" → setattr chain
        parts = dotted.split(".")
        obj: object = cfg
        for part in parts[:-1]:
            obj = getattr(obj, part)
        setattr(obj, parts[-1], value)
        logger.debug("env override %s=%s", env_name, value)
    return cfg


def _validate_config(cfg: ProfilerConfig) -> None:
    """Raise **ValueError** on nonsensical values."""
    col = cfg.collector
    if not (1 <= col.frequency_hz <= 100_000):
        raise ValueError(
            f"frequency_hz must be 1-100000, got {col.frequency_hz}"
        )
    if not (1 <= col.duration_sec <= 3600):
        raise ValueError(
            f"duration_sec must be 1-3600, got {col.duration_sec}"
        )

    sto = cfg.storage
    if sto.max_size_mb < 10:
        raise ValueError(f"max_size_mb too small: {sto.max_size_mb}")
    if sto.max_age_hours < 1:
        raise ValueError(f"max_age_hours must be >= 1: {sto.max_age_hours}")
    if not (1 <= sto.comp_level <= 19):
        raise ValueError(f"comp_level must be 1-19, got {sto.comp_level}")


def load_config(
    config_path: str | Path | None = None,
) -> ProfilerConfig:
    """Load configuration from YAML file, falling back on defaults.

    Resolution order (later wins):
        1. code defaults (``ProfilerConfig()``)
        2. YAML file  (``--config`` / ``CYPROF_CONFIG`` / ``/etc/cyprof/cyprof.yaml``)
        3. env vars   (see ``_ENV_MAP``)

    Returns:
        Validated ``ProfilerConfig``.
    """
    cfg = ProfilerConfig()

    # ── resolve config file path ───────────────────────────────
    if config_path is None:
        config_path = os.environ.get(
            "CYPROF_CONFIG", str(DEFAULT_CONFIG_PATH)
        )
    resolved = Path(config_path)

    if resolved.is_file():
        with resolved.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        _merge_yaml(cfg, raw)
        logger.info("loaded config from %s", resolved)
    else:
        if config_path is not None:
            logger.debug("config file %s not found, using defaults + env", resolved)

    _apply_env_overrides(cfg)
    _validate_config(cfg)
    logger.debug("final config: %s", cfg)
    return cfg


def _merge_yaml(cfg: ProfilerConfig, raw: dict) -> None:
    """Overlay parsed YAML dict onto the dataclass, section by section."""
    if "collector" in raw:
        _set_attrs(cfg.collector, raw["collector"])
    if "storage" in raw:
        s = raw["storage"]
        # allow YAML to use string paths
        if "data_dir" in s:
            s["data_dir"] = Path(s["data_dir"])
        if "db_path" in s:
            s["db_path"] = Path(s["db_path"])
        _set_attrs(cfg.storage, s)
    if "daemon" in raw:
        _set_attrs(cfg.daemon, raw["daemon"])


def _set_attrs(obj: object, raw: dict) -> None:
    for k, v in raw.items():
        if hasattr(obj, k):
            setattr(obj, k, v)
