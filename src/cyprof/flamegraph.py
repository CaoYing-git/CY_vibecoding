"""FlameGraph generation — perf.data → folded stacks → interactive SVG.

Pipeline::

    perf script -i <file> ...
        │
        ▼  (stack traces text)
    stackcollapse-perf.pl
        │
        ▼  (folded stacks: "func1;func2;func3 42")
    flamegraph.pl
        │
        ▼
    flamegraph.svg
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

# ── resolve bundled scripts relative to this file ───────────────
_SCRIPTS_DIR = Path(__file__).resolve().parent / "scripts"
_DEFAULT_COLLAPSE = _SCRIPTS_DIR / "stackcollapse-perf.pl"
_DEFAULT_FLAMEGRAPH = _SCRIPTS_DIR / "flamegraph.pl"


@dataclass
class FlameGraphResult:
    """Result of a flamegraph generation run."""

    svg_path: Path
    title: str
    sample_count: int  # approximate, from folded-stack total
    input_files: int
    collapsed_lines: int = 0


class FlameGraphError(Exception):
    """Fatal error during flamegraph pipeline."""


class FlameGraphGenerator:
    """Orchestrate the three-stage pipeline.

    Each stage can use a custom script/binary path; defaults to bundled
    Brendan Gregg FlameGraph scripts.
    """

    def __init__(
        self,
        perf_path: str = "perf",
        collapse_script: str | Path | None = None,
        flamegraph_script: str | Path | None = None,
    ) -> None:
        self._perf = shutil.which(perf_path) or perf_path
        self._collapse = str(collapse_script or _DEFAULT_COLLAPSE)
        self._flamegraph = str(flamegraph_script or _DEFAULT_FLAMEGRAPH)
        self._perl = shutil.which("perl") or "perl"

    # ── public API ──────────────────────────────────────────────

    def generate(
        self,
        perf_data_paths: Sequence[str | Path],
        output_dir: str | Path,
        *,
        title: str = "CPU Flame Graph",
        subtitle: str = "",
    ) -> FlameGraphResult:
        """Run the full pipeline on one or more ``perf.data`` files.

        Args:
            perf_data_paths: One or more ``perf.data`` / ``perf.data.zst``
                files.  Multiple files are merged in the *perf-script* stage.
            output_dir: Directory where ``flamegraph.svg`` is written.
            title: Chart title.
            subtitle: Optional subtitle (e.g. time range).

        Returns:
            ``FlameGraphResult`` with path to the SVG.
        """
        paths = [Path(p) for p in perf_data_paths]
        for p in paths:
            if not p.is_file():
                raise FlameGraphError(f"perf data file not found: {p}")

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        svg_path = out_dir / "flamegraph.svg"

        # ── stage 1: perf script ────────────────────────────────
        logger.info("stage 1/3: perf script on %d file(s)", len(paths))
        perf_text = self._run_perf_script(paths)
        if not perf_text.strip():
            raise FlameGraphError(
                "perf script produced no output — the perf.data may be empty or corrupted"
            )
        logger.debug("perf script output: %d bytes", len(perf_text))

        # ── stage 2: stack collapse ─────────────────────────────
        logger.info("stage 2/3: stackcollapse-perf")
        folded = self._collapse_stacks(perf_text)
        collapsed_lines = folded.count("\n")
        sample_count = _estimate_sample_count(folded)
        logger.debug(
            "folded: %d lines, ~%d samples", collapsed_lines, sample_count
        )

        # ── stage 3: render SVG ─────────────────────────────────
        logger.info("stage 3/3: flamegraph → %s", svg_path)
        full_title = title
        if subtitle:
            full_title = f"{title} — {subtitle}"
        svg = self._render_svg(folded, title=full_title)
        svg_path.write_text(svg, encoding="utf-8")
        logger.info("flamegraph written  %d bytes", len(svg))

        return FlameGraphResult(
            svg_path=svg_path.resolve(),
            title=full_title,
            sample_count=sample_count,
            input_files=len(paths),
            collapsed_lines=collapsed_lines,
        )

    # ── pipeline stages (public for partial reuse) ──────────────

    def perf_script(self, perf_data_path: str | Path) -> str:
        """Run ``perf script`` on a single file, return stdout text.

        Handles ``.zst`` files by decompressing through ``zstd -d`` first.
        """
        return self._run_perf_script([Path(perf_data_path)])

    def collapse(self, perf_script_text: str) -> str:
        """Fold perf-script output into ``func1;func2 count`` lines."""
        return self._collapse_stacks(perf_script_text)

    def render(self, folded_stacks: str, title: str = "CPU Flame Graph") -> str:
        """Render folded stacks into an SVG string."""
        return self._render_svg(folded_stacks, title)

    # ── internals ───────────────────────────────────────────────

    def _run_perf_script(self, paths: list[Path]) -> str:
        """Run ``perf script`` over one or more files, merging outputs."""
        all_text = ""
        for i, p in enumerate(paths):
            if p.suffix == ".zst":
                # pipe: zstd -d → perf script
                text = self._perf_script_zst(p)
            else:
                text = self._perf_script_raw(p)
            if not text.strip():
                logger.warning("perf script produced no output for %s", p)
            all_text += text
            if i < len(paths) - 1:
                all_text += "\n"
        return all_text

    def _perf_script_raw(self, path: Path) -> str:
        cmd = [self._perf, "script", "-i", str(path)]
        return self._run_cmd(cmd, desc="perf script")

    def _perf_script_zst(self, path: Path) -> str:
        zstd = shutil.which("zstd") or "zstd"
        zstd_proc = subprocess.Popen(
            [zstd, "-d", "-c", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        perf_proc = subprocess.Popen(
            [self._perf, "script", "-i", "-"],
            stdin=zstd_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if zstd_proc.stdout is not None:
            zstd_proc.stdout.close()

        stdout, stderr = perf_proc.communicate(timeout=120)
        zstd_rc = zstd_proc.wait(timeout=30)
        perf_rc = perf_proc.returncode

        if zstd_rc != 0:
            zstderr = zstd_proc.stderr.read().decode(errors="replace") if zstd_proc.stderr else ""
            raise FlameGraphError(f"zstd decompress failed rc={zstd_rc}: {zstderr}")
        if perf_rc not in (0, -15):
            logger.warning("perf script exited %d for %s", perf_rc, path)

        return stdout.decode(errors="replace")

    def _collapse_stacks(self, perf_text: str) -> str:
        """Pipe *perf_text* through stackcollapse-perf.pl."""
        return self._pipe_through_script(
            self._collapse,
            perf_text,
            desc="stackcollapse-perf",
        )

    def _render_svg(self, folded: str, title: str) -> str:
        """Pipe folded stacks through flamegraph.pl."""
        escaped_title = title.replace("'", "'\\''")
        cmd = [
            self._perl, self._flamegraph,
            "--title", escaped_title,
            "--countname", "samples",
        ]
        return self._run_cmd(cmd, stdin=folded, desc="flamegraph")

    def _pipe_through_script(self, script: str, stdin_text: str, *, desc: str = "") -> str:
        """Execute *script* (a Perl file) with *stdin_text* as stdin."""
        perl = shutil.which("perl") or "perl"
        return self._run_cmd(
            [perl, script],
            stdin=stdin_text,
            desc=desc,
        )

    def _run_cmd(
        self,
        cmd: list[str],
        *,
        stdin: str | None = None,
        desc: str = "",
        timeout: int = 120,
    ) -> str:
        """Run *cmd*, return stdout as a string.

        Raises ``FlameGraphError`` on non-zero exit or timeout.
        """
        logger.debug("[%s] %s", desc, " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd,
                input=stdin.encode("utf-8") if stdin else None,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise FlameGraphError(
                f"{desc or cmd[0]} timed out after {timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise FlameGraphError(
                f"Required binary not found: {exc.filename}"
            ) from exc

        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            raise FlameGraphError(
                f"{desc or cmd[0]} exited {proc.returncode}: {stderr}"
            )
        return proc.stdout.decode(errors="replace")


def _estimate_sample_count(folded_text: str) -> int:
    """Sum the counts column from folded-stack lines.

    Each line looks like: ``bash;func1;func2 42``
    """
    total = 0
    for line in folded_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.rsplit(None, 1)  # split on last whitespace
        if len(parts) == 2:
            try:
                total += int(parts[1])
            except ValueError:
                pass
    return total
