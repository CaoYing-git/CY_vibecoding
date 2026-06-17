"""cyprofiled — background daemon main loop.

Ties together:  config → collector → storage → rotation → health-check.

Can be run directly or managed by systemd::

    systemctl start cyprofiled
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .collector import PerfCollector, CollectResult
from .config import load_config, ProfilerConfig
from .storage import MetadataStore, RingBuffer, SampleRecord, DiskWatermark

logger = logging.getLogger(__name__)

# ── systemd watchdog support ───────────────────────────────────
# systemd sets WATCHDOG_USEC and NOTIFY_SOCKET env vars.
# We call sd_notify periodically so systemd knows we're alive.
_WATCHDOG_USEC = int(os.environ.get("WATCHDOG_USEC", 0))
_NOTIFY_SOCKET = os.environ.get("NOTIFY_SOCKET", "")
_HAS_SYSTEMD = bool(_NOTIFY_SOCKET)

# ping interval: half of watchdog timeout (as systemd docs recommend)
_WATCHDOG_INTERVAL_SEC = (_WATCHDOG_USEC / 2_000_000) if _WATCHDOG_USEC else 0


def _sd_notify(state: str) -> None:
    """Send a status string to systemd over the notification socket."""
    if not _HAS_SYSTEMD:
        return
    try:
        import socket
        addr = _NOTIFY_SOCKET
        if addr.startswith("@"):
            addr = "\0" + addr[1:]
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.settimeout(1)
        sock.sendto(state.encode("utf-8"), addr)
        sock.close()
    except Exception:
        logger.debug("sd_notify failed", exc_info=True)


def _sd_watchdog_ping() -> None:
    """Ping systemd watchdog — must be called at < half the watchdog interval."""
    _sd_notify("WATCHDOG=1")


# ── health file ────────────────────────────────────────────────

def _write_health_file(path: Path, status: str, detail: dict | None = None) -> None:
    """Atomically write a JSON health status file."""
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }
    if detail:
        payload.update(detail)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic rename


# ── daemon class ───────────────────────────────────────────────


class Daemon:
    """Main cyprofiled daemon."""

    def __init__(self, config: ProfilerConfig) -> None:
        self._cfg = config
        self._data_dir = config.storage.data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)

        self._collector = PerfCollector(
            config.collector,
            data_dir=self._data_dir,
            comp_level=config.storage.comp_level,
        )
        self._store = MetadataStore(config.storage.db_path)
        self._ringbuf = RingBuffer(
            self._data_dir,
            self._store,
            max_size_mb=config.storage.max_size_mb,
            max_age_hours=config.storage.max_age_hours,
        )
        self._health_path = self._data_dir.parent / "health.json"

        self._running = False
        self._last_collect_ok = True
        self._collect_count = 0
        self._error_count = 0
        self._disk_level = "normal"

        # register signal handlers
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    # ── public API ──────────────────────────────────────────────

    def run(self) -> int:
        """Start the main loop.  Returns exit code (0 = clean shutdown)."""
        logger.info("cyprofiled starting  pid=%d  data_dir=%s",
                      os.getpid(), self._data_dir)
        if not self._collector.has_perf:
            logger.error(
                "perf binary not found — install linux-tools or set perf_path in config"
            )
            return 1

        _sd_notify("READY=1")
        _write_health_file(self._health_path, "starting")

        self._running = True
        exit_code = 0

        while self._running:
            tick_start = time.time()

            try:
                self._tick()
            except Exception:
                logger.exception("unhandled error in main loop tick")
                self._error_count += 1

            # ── health heartbeat ─────────────────────────────────
            self._write_status()

            # ── systemd watchdog ping ────────────────────────────
            if _WATCHDOG_USEC:
                _sd_watchdog_ping()

            # ── sleep until next interval ────────────────────────
            elapsed = time.time() - tick_start
            sleep_for = max(0, self._cfg.daemon.sample_interval_sec - elapsed)
            if sleep_for > 0:
                self._interruptible_sleep(sleep_for)

        logger.info("cyprofiled shutting down  samples=%d  errors=%d",
                      self._collect_count, self._error_count)
        _write_health_file(self._health_path, "stopped",
                           {"samples": self._collect_count, "errors": self._error_count})
        _sd_notify("STOPPING=1")
        return exit_code

    def shutdown(self) -> None:
        """Request graceful shutdown (can be called from another thread)."""
        logger.info("shutdown requested")
        self._running = False

    # ── tick ────────────────────────────────────────────────────

    def _tick(self) -> None:
        """One iteration of the main loop."""
        # 1. check disk before collecting (prevent pile-up)
        wm = self._ringbuf.check_disk_watermark()
        self._disk_level = wm.level
        if wm.level == "fatal":
            logger.critical("disk fatal — stopping daemon")
            self._running = False
            return
        if wm.level == "emergency":
            logger.warning("disk emergency — aggressive rotation + pause")
            self._ringbuf.emergency_rotate()
            self._write_status()
            time.sleep(30)  # back off
            return

        # 2. rotate before collecting (guarantee space)
        self._ringbuf.rotate()

        # 3. collect
        result = self._collector.collect()
        if result is None:
            logger.warning("collection returned empty — skipping index")
            self._error_count += 1
            self._last_collect_ok = False
            return

        self._collect_count += 1
        self._last_collect_ok = True

        # 4. index
        record = SampleRecord(
            id=0,  # auto-assigned by SQLite
            start_ts=result.start_ts,
            end_ts=result.end_ts,
            frequency_hz=self._cfg.collector.frequency_hz,
            duration_sec=self._cfg.collector.duration_sec,
            file_path=result.path.name,
            file_size_bytes=result.file_size_bytes,
            sample_count=result.sample_count,
            trigger_mode=self._current_trigger_mode(),
        )
        self._store.insert(record)

        # 5. rotate after collecting (enforce limits)
        self._ringbuf.rotate()

    # ── signal handling ─────────────────────────────────────────

    def _handle_signal(self, signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
        logger.info("received signal %d", signum)
        self._running = False

    # ── helpers ─────────────────────────────────────────────────

    def _current_trigger_mode(self) -> str:
        """Determine the trigger mode for this sample.

        Can be extended later with anomaly-detection logic.
        """
        if self._disk_level in ("emergency", "cap"):
            return "alert"
        return "normal"

    def _write_status(self) -> None:
        """Write health file with current daemon state."""
        try:
            _write_health_file(self._health_path, "running", {
                "samples": self._collect_count,
                "errors": self._error_count,
                "last_collect_ok": self._last_collect_ok,
                "disk_level": self._disk_level,
                "store_records": self._store.count(),
                "store_size_mb": round(self._store.total_size_bytes() / 1_048_576, 2),
            })
        except Exception:
            logger.debug("health file write failed", exc_info=True)

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in small slices so signals can interrupt."""
        while seconds > 0 and self._running:
            chunk = min(seconds, 1.0)
            time.sleep(chunk)
            seconds -= chunk


# ── CLI entry point ─────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    """Entry point for ``cyprofiled`` console script.

    Parses ``--config`` from argv and starts the daemon loop.
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="cyprofiled — Linux continuous CPU profiling daemon",
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to YAML config file (default: /etc/cyprof/cyprof.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("CYPROF_LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Log level (default: INFO)",
    )
    args = parser.parse_args(argv)

    # ── logging setup ───────────────────────────────────────────
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)-7s] %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,  # systemd journal picks up stderr
    )

    # suppress noisy library logs
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        config = load_config(args.config)
    except ValueError as exc:
        logger.error("invalid config: %s", exc)
        return 2

    daemon = Daemon(config)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())
