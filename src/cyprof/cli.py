"""cyprof CLI — query perf samples and generate flamegraphs.

Usage::

    cyprof query --time "2025-06-01 03:12"
    cyprof query --time "14:30"
    cyprof query --from "2025-06-01 03:00" --to "03:15"
    cyprof list
    cyprof list --from "2025-06-01 00:00" --to "2025-06-02 00:00"
    cyprof info
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import load_config, ProfilerConfig
from .query import QueryEngine, QueryResult, parse_time

logger = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s [%(levelname)-7s] %(name)s - %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_query(
    engine: QueryEngine,
    target: Optional[str],
    from_time: Optional[str],
    to_time: Optional[str],
    output: str,
    title: str,
) -> int:
    """Execute a query + flamegraph command."""
    result: QueryResult

    if target:
        # ── point-in-time query ─────────────────────────────────
        dt = parse_time(target)
        result = engine.find_closest(dt)

        if not result.records:
            print(f"No samples found near {dt.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            return 1

        rec = result.records[0]
        print(f"Found 1 sample:")
        print(f"  File:     {rec.file_path}")
        print(f"  Time:     {rec.start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"  Samples:  {rec.sample_count}")
        print(f"  Size:     {rec.file_size_bytes / 1024:.0f} KB")
        print(f"  Freq:     {rec.frequency_hz} Hz")

    elif from_time or to_time:
        # ── range query ──────────────────────────────────────────
        start = parse_time(from_time) if from_time else datetime.min.replace(tzinfo=timezone.utc)
        end = parse_time(to_time) if to_time else datetime.now(timezone.utc)
        result = engine.find_range(start, end)

        if not result.records:
            print(
                f"No samples found in range "
                f"{start.strftime('%Y-%m-%d %H:%M:%S')} — "
                f"{end.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            return 1

        print(f"Found {len(result.records)} sample(s):")
        for r in result.records[:10]:
            print(
                f"  {r.start_time.strftime('%Y-%m-%d %H:%M:%S')}  "
                f"{r.sample_count:6d} samples  "
                f"{r.file_size_bytes / 1024:5.0f} KB  "
                f"{r.trigger_mode}"
            )
        if len(result.records) > 10:
            print(f"  ... and {len(result.records) - 10} more")
    else:
        print("Error: specify --time or --from/--to")
        return 2

    # ── generate flamegraph ─────────────────────────────────────
    fg = engine.generate_flamegraph(result, output, title=title)
    if fg is None:
        print("Flamegraph generation failed")
        return 3

    print(f"\nFlamegraph written to: {fg.svg_path}")
    print(f"  Total samples: {fg.sample_count}")
    if fg.collapsed_lines:
        print(f"  Unique stacks: {fg.collapsed_lines}")
    return 0


def cmd_list(
    engine: QueryEngine,
    from_time: Optional[str],
    to_time: Optional[str],
    limit: int,
) -> int:
    """List sample records."""
    if from_time or to_time:
        start = parse_time(from_time) if from_time else datetime.min.replace(tzinfo=timezone.utc)
        end = parse_time(to_time) if to_time else datetime.now(timezone.utc)
        result = engine.find_range(start, end)
        records = result.records
    else:
        records = engine.list_recent(n=limit)

    if not records:
        print("No samples found.")
        return 0

    print(f"{'Time (UTC)':<22} {'Samples':>8} {'Size':>8} {'Freq':>6} {'Mode':<8} {'File'}")
    print("-" * 90)
    for r in records:
        print(
            f"{r.start_time.strftime('%Y-%m-%d %H:%M:%S'):<22} "
            f"{r.sample_count:>8} "
            f"{r.file_size_bytes / 1024:>7.0f}K "
            f"{r.frequency_hz:>4}Hz "
            f"{r.trigger_mode:<8} "
            f"{r.file_path}"
        )
    return 0


def cmd_info(engine: QueryEngine, config: ProfilerConfig) -> int:
    """Print daemon overview."""
    newest = engine.newest
    oldest = engine.oldest
    total_size = engine._store.total_size_bytes()

    print("cyprof status")
    print(f"  Data dir:     {config.storage.data_dir}")
    print(f"  DB path:      {config.storage.db_path}")
    print(f"  Total files:  {engine.record_count}")
    print(f"  Total size:   {total_size / 1_048_576:.1f} MB")
    print(f"  Max size:     {config.storage.max_size_mb} MB")
    print(f"  Max age:      {config.storage.max_age_hours} h")
    if newest:
        print(f"  Newest:       {newest.start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if oldest:
        print(f"  Oldest:       {oldest.start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    if newest and oldest:
        span = newest.start_ts - oldest.start_ts
        print(f"  Span:         {span / 3600:.1f} hours")

    # trigger stats
    stats = engine._store.get_trigger_stats()
    if stats:
        print(f"  By mode:      {stats}")
    return 0


# ── entry point ────────────────────────────────────────────────


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="cyprof — query CPU profiling data and generate flamegraphs",
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to YAML config (default: /etc/cyprof/cyprof.yaml)",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        help="Log level (default: WARNING)",
    )

    sub = parser.add_subparsers(dest="command", help="sub-command")

    # ── query ───────────────────────────────────────────────────
    p_query = sub.add_parser("query", help="find samples and generate flamegraph")
    p_query.add_argument(
        "--time", "-t",
        default=None,
        help='Point-in-time query (e.g. "2025-06-01 03:12" or "14:30")',
    )
    p_query.add_argument(
        "--from", dest="from_time",
        default=None,
        help='Start of time range (e.g. "2025-06-01 03:00")',
    )
    p_query.add_argument(
        "--to", dest="to_time",
        default=None,
        help='End of time range (e.g. "03:15")',
    )
    p_query.add_argument(
        "--output", "-o",
        default="./flamegraph_output",
        help="Output directory for flamegraph.svg (default: ./flamegraph_output)",
    )
    p_query.add_argument(
        "--title",
        default="CPU Flame Graph",
        help="Flamegraph title",
    )

    # ── list ────────────────────────────────────────────────────
    p_list = sub.add_parser("list", help="list sample records")
    p_list.add_argument("--from", dest="from_time", default=None)
    p_list.add_argument("--to", dest="to_time", default=None)
    p_list.add_argument(
        "--limit", "-n", type=int, default=50,
        help="Max records (default: 50)",
    )

    # ── info ────────────────────────────────────────────────────
    sub.add_parser("info", help="show daemon status overview")

    args = parser.parse_args(argv)
    _setup_logging(args.log_level)

    # load config
    try:
        config = load_config(args.config)
    except ValueError as exc:
        logger.error("config error: %s", exc)
        return 2

    engine = QueryEngine(
        data_dir=config.storage.data_dir,
        db_path=config.storage.db_path,
    )

    if args.command == "query":
        if not args.time and not args.from_time and not args.to_time:
            parser.print_help()
            return 2
        return cmd_query(
            engine,
            target=args.time,
            from_time=args.from_time,
            to_time=args.to_time,
            output=args.output,
            title=args.title,
        )

    elif args.command == "list":
        return cmd_list(
            engine,
            from_time=args.from_time,
            to_time=args.to_time,
            limit=args.limit,
        )

    elif args.command == "info":
        return cmd_info(engine, config)

    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
