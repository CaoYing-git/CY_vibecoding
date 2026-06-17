"""Time-range query — find perf.data files by timestamp, generate flamegraphs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Sequence

from .storage import MetadataStore, SampleRecord
from .flamegraph import FlameGraphGenerator, FlameGraphResult, FlameGraphError

logger = logging.getLogger(__name__)


@dataclass
class QueryResult:
    """Result of a time query."""

    records: list[SampleRecord]
    target_time: datetime | None  # the requested point-in-time
    time_range: tuple[datetime, datetime] | None  # (from, to)
    match_method: str  # "closest" | "range" | "all"


class QueryEngine:
    """Find perf samples by time and generate flamegraphs.

    Wraps ``MetadataStore`` for lookup and ``FlameGraphGenerator`` for rendering.
    """

    def __init__(
        self,
        data_dir: str | Path,
        db_path: str | Path,
        flamegraph: FlameGraphGenerator | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._store = MetadataStore(db_path)
        self._flamegraph = flamegraph or FlameGraphGenerator()

    # ── find methods ────────────────────────────────────────────

    def find_closest(
        self,
        target: datetime,
        *,
        within_sec: float = 300,
    ) -> QueryResult:
        """Find the single sample record closest to *target*.

        Args:
            target: The point in time to search around (UTC or naive).
            within_sec: Maximum distance in seconds; returns empty if nothing
                        within this window.

        Returns:
            ``QueryResult`` with 0 or 1 records.
        """
        target_ts = _to_epoch(target)
        logger.info(
            "find_closest  target=%s  within=%.0fs",
            target.isoformat(), within_sec,
        )

        # query a ±window around target
        candidates = self._store.query(
            start_ts=target_ts - within_sec,
            end_ts=target_ts + within_sec,
            limit=500,
        )

        if not candidates:
            logger.warning("no samples within ±%.0fs of %s", within_sec, target.isoformat())
            return QueryResult(
                records=[],
                target_time=target,
                time_range=None,
                match_method="closest",
            )

        # pick the one with the smallest midpoint distance to target
        best = min(candidates, key=lambda r: abs((r.start_ts + r.end_ts) / 2 - target_ts))

        logger.info(
            "closest match: %s  (dist=%.1fs  samples=%d)",
            best.file_path,
            abs((best.start_ts + best.end_ts) / 2 - target_ts),
            best.sample_count,
        )

        return QueryResult(
            records=[best],
            target_time=target,
            time_range=None,
            match_method="closest",
        )

    def find_range(
        self,
        start: datetime,
        end: datetime,
    ) -> QueryResult:
        """Find all sample records whose window overlaps [*start*, *end*]."""
        start_ts = _to_epoch(start)
        end_ts = _to_epoch(end)
        logger.info(
            "find_range  from=%s  to=%s",
            start.isoformat(), end.isoformat(),
        )

        records = self._store.query(
            start_ts=start_ts,
            end_ts=end_ts,
            limit=10_000,
        )

        logger.info("range matched %d record(s)", len(records))
        # records come newest-first; reverse to chronological
        records.reverse()

        return QueryResult(
            records=records,
            target_time=None,
            time_range=(start, end),
            match_method="range",
        )

    def list_recent(self, n: int = 20) -> list[SampleRecord]:
        """Return the *n* most recent records."""
        return self._store.query(limit=n)

    # ── flamegraph ──────────────────────────────────────────────

    def generate_flamegraph(
        self,
        result: QueryResult,
        output_dir: str | Path,
        *,
        title: str = "CPU Flame Graph",
    ) -> FlameGraphResult | None:
        """Generate a flamegraph from the records in *result*.

        Returns ``None`` when *result* contains no records.
        """
        if not result.records:
            logger.warning("no records to generate flamegraph from")
            return None

        paths = [self._data_dir / r.file_path for r in result.records]

        # build subtitle from time info
        if result.match_method == "closest" and result.target_time:
            rec = result.records[0]
            subtitle = (
                f"{rec.start_time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"({rec.frequency_hz}Hz, {rec.sample_count} samples)"
            )
        elif result.time_range:
            s, e = result.time_range
            subtitle = (
                f"{s.strftime('%Y-%m-%d %H:%M')} — {e.strftime('%H:%M')}"
            )
        else:
            subtitle = ""

        try:
            return self._flamegraph.generate(
                paths,
                output_dir,
                title=title,
                subtitle=subtitle,
            )
        except FlameGraphError:
            logger.exception("flamegraph generation failed")
            return None

    # ── properties ──────────────────────────────────────────────

    @property
    def record_count(self) -> int:
        return self._store.count()

    @property
    def newest(self) -> SampleRecord | None:
        return self._store.get_newest()

    @property
    def oldest(self) -> SampleRecord | None:
        return self._store.get_oldest()


# ── helpers ────────────────────────────────────────────────────

def _to_epoch(dt: datetime) -> float:
    """Convert a datetime (naive → UTC) to epoch seconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_time(s: str) -> datetime:
    """Parse a human-readable time string into a UTC datetime.

    Accepted formats:
        - ``2025-06-01 03:12``
        - ``2025-06-01T03:12``
        - ``2025-06-01T03:12:00``
        - ``2025-06-01 03:12:00``
        - ``03:12``  (today at that time)
        - ``03:12:00`` (today at that time)
    """
    s = s.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
    ]

    # try full datetime first
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # try time-only (use today UTC)
    time_formats = ["%H:%M:%S", "%H:%M"]
    for fmt in time_formats:
        try:
            t = datetime.strptime(s, fmt).time()
            return datetime.now(timezone.utc).replace(
                hour=t.hour, minute=t.minute, second=t.second, microsecond=0
            )
        except ValueError:
            pass

    raise ValueError(
        f"Cannot parse time: {s!r}. "
        f"Expected e.g. '2025-06-01 03:12' or '03:12'."
    )
