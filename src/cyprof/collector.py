"""perf-record wrapper — one sampling window, compress on the fly."""

from __future__ import annotations

import logging
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import CollectorConfig

logger = logging.getLogger(__name__)

# ── sentinel for perf-not-available ─────────────────────────────
_PERF_NOT_FOUND = shutil.which("perf") is None


@dataclass
class CollectResult:
    """Outcome of a single collection window."""

    path: Path  # absolute path to the compressed file
    start_ts: float  # epoch seconds (UTC) when window started
    end_ts: float
    sample_count: int  # approximate, parsed from perf stderr
    file_size_bytes: int
    exit_code: int  # perf exit code (0 = clean)


class CollectorError(Exception):
    """Fatal error that should stop the daemon."""


class PerfCollector:
    """Run a single ``perf record`` sampling window.

    Pipe flow::

        perf record ... -o -  →  zstd -T1 -3  →  <data_dir>/<ts>.perf.data.zst
    """

    def __init__(
        self,
        config: CollectorConfig,
        data_dir: Path,
        comp_level: int = 3,
    ) -> None:
        self._cfg = config
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._comp_level = comp_level
        self._perf: str = shutil.which(config.perf_path) or config.perf_path
        self._zstd: str = shutil.which("zstd") or "zstd"

        if shutil.which(self._perf) is None:
            logger.warning("perf binary %r not found on PATH", self._perf)

        # zstd is optional — we degrade to uncompressed if missing
        self._has_zstd = shutil.which(self._zstd) is not None
        if not self._has_zstd:
            logger.warning("zstd not found — perf.data will be stored uncompressed")

    # ── public API ──────────────────────────────────────────────

    def collect(self) -> CollectResult | None:
        """Execute one sampling window.

        Returns:
            ``CollectResult`` on success, ``None`` when the sampling produced
            no data (e.g. perf was killed early).
        """
        ts_start = time.time()
        filename = self._make_filename(ts_start)
        out_path = self._data_dir / filename
        tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")

        cmd_parts: list[str]
        suffix: str  # filename extension hint
        if self._has_zstd:
            # pipe: perf → zstd → file
            cmd_parts = self._build_cmd("-")
            suffix = ".perf.data.zst"
        else:
            cmd_parts = self._build_cmd(str(tmp_path))
            suffix = ".perf.data"

        try:
            logger.info("sampling start  frequency=%dHz  duration=%ds",
                         self._cfg.frequency_hz, self._cfg.duration_sec)
            logger.debug("cmd: %s", shlex.join(cmd_parts))

            if self._has_zstd:
                # ── pipe mode ───────────────────────────────────
                perf_proc = subprocess.Popen(
                    cmd_parts,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                zstd_proc = subprocess.Popen(
                    [self._zstd, f"-{self._comp_level}", "-T1", "-o", str(tmp_path)],
                    stdin=perf_proc.stdout,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                assert perf_proc.stdout is not None
                perf_proc.stdout.close()  # let zstd read the pipe

                perf_stderr = perf_proc.stderr.read() if perf_proc.stderr else b""
                zstd_stderr = zstd_proc.stderr.read() if zstd_proc.stderr else b""
                perf_rc = perf_proc.wait()
                zstd_rc = zstd_proc.wait(timeout=30)

                if zstd_rc != 0:
                    logger.error("zstd exited %d: %s", zstd_rc, zstd_stderr.decode(errors="replace"))
                    # keep the tmp file for diagnostics
                    return None

                # rename atomically only on success
                tmp_path.rename(out_path)
            else:
                # ── direct-to-file (no compression) ────────────
                proc = subprocess.run(
                    cmd_parts,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=self._cfg.duration_sec + 30,
                )
                perf_rc = proc.returncode
                perf_stderr = proc.stderr
                zstd_stderr = b""
                # perf wrote directly to out_path (not tmp in this path)
                # Let's use tmp then rename for consistency
                if tmp_path.exists():
                    tmp_path.rename(out_path)

            ts_end = time.time()

        except subprocess.TimeoutExpired as exc:
            logger.error("perf/zstd timed out after %ds", exc.timeout)
            self._cleanup(tmp_path)
            return None
        except FileNotFoundError as exc:
            raise CollectorError(
                f"Required binary not found: {exc.filename}. "
                f"Install linux-tools or set perf_path in config."
            ) from exc
        except Exception:
            logger.exception("unexpected error during collection")
            self._cleanup(tmp_path)
            return None

        # ── parse sample count from perf stderr ─────────────────
        sample_count = self._parse_sample_count(
            perf_stderr.decode(errors="replace") if isinstance(perf_stderr, bytes) else (perf_stderr or "")
        )

        # ── perf exit  != 0 isn't always fatal; SIGTERM is normal ──
        if perf_rc not in (0, -15):  # 0=clean, -15=SIGTERM (ok)
            logger.warning("perf exited %d (stderr follows)", perf_rc)
            for line in (perf_stderr.decode(errors="replace") if perf_stderr else "").splitlines():
                logger.warning("perf | %s", line)

        file_size = out_path.stat().st_size if out_path.exists() else 0

        if file_size == 0:
            logger.debug("empty output file → discarding window")
            self._cleanup(out_path)
            return None

        result = CollectResult(
            path=out_path,
            start_ts=ts_start,
            end_ts=ts_end,
            sample_count=sample_count,
            file_size_bytes=file_size,
            exit_code=perf_rc,
        )

        logger.info(
            "sampling done  %d samples  %d bytes  %.1fs wall",
            sample_count, file_size, ts_end - ts_start,
        )
        return result

    # ── internals ───────────────────────────────────────────────

    def _build_cmd(self, output: str) -> list[str]:
        """Assemble the ``perf record`` argument vector."""
        cmd: list[str] = [
            self._perf, "record",
            "-F", str(self._cfg.frequency_hz),
            "-a",  # system-wide
            "--running-time",  # don't adjust period for time running
        ]
        if self._cfg.callgraph:
            cmd += ["--call-graph", "dwarf,16384"]
        cmd += list(self._cfg.extra_args)
        cmd += [
            "-o", output,
            "--", "sleep", str(self._cfg.duration_sec),
        ]
        return cmd

    def _make_filename(self, ts_epoch: float) -> str:
        """Generate timestamp-based filename.

        Example: ``20260617_143025_11hz.perf.data.zst.tmp``
        """
        ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc)
        stamp = ts.strftime("%Y%m%d_%H%M%S")
        return f"{stamp}_{self._cfg.frequency_hz}hz.perf.data.zst"

    @staticmethod
    def _parse_sample_count(stderr_text: str) -> int:
        """Pull approximate sample count from perf stderr.

        Typical output: ``[ perf record: Captured and wrote 0.123 MB perf.data (1234 samples) ]``
        """
        import re
        m = re.search(r"\((\d+)\s+samples?\)", stderr_text)
        return int(m.group(1)) if m else 0

    @staticmethod
    def _cleanup(path: Path) -> None:
        """Remove *path* if it exists, log on failure."""
        try:
            if path.exists():
                path.unlink()
        except OSError:
            logger.debug("cleanup failed for %s", path, exc_info=True)

    # ── helper for tests ────────────────────────────────────────

    @property
    def has_perf(self) -> bool:
        """``True`` when the ``perf`` binary is resolvable."""
        return shutil.which(self._perf) is not None

    @property
    def has_zstd(self) -> bool:
        """``True`` when ``zstd`` compression is available."""
        return self._has_zstd
