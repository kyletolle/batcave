#!/usr/bin/env python3
"""
Sleep Duration Calculator

Given a start time (eyes closed) and end time (woke up), compute the duration
in hours, handling day boundaries correctly.

Examples:
    sleep-duration 22:30 07:00              -> 8.5h (crosses midnight)
    sleep-duration 00:20 07:20              -> 7.0h (same day)
    sleep-duration 21:00 00:00              -> 3.0h (ends exactly at midnight)
    sleep-duration 22:30 07:00 --awake 30   -> 8.0h (8.5h minus 30min awake)
    sleep-duration 22:30 07:00 --json       -> {"hours": 8.5, ...}

Rule: if end_time < start_time on a 24h clock, the end is interpreted as the
next day. Use --no-overnight to force same-day calculation if you really mean it.
"""

import sys
import json
import argparse


def parse_hhmm(s):
    """Parse HH:MM (or H:MM) into total minutes. Raises ValueError on bad input."""
    s = s.strip()
    if ":" not in s:
        raise ValueError(f"time '{s}' must be HH:MM")
    h, m = s.split(":", 1)
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"time '{s}' out of range (HH:MM, 00:00-23:59)")
    return h * 60 + m


def compute(start, end, awake_minutes=0, no_overnight=False):
    """Return (gross_hours, net_hours, crossed_midnight) as floats + bool."""
    start_min = parse_hhmm(start)
    end_min = parse_hhmm(end)

    if no_overnight:
        if end_min < start_min:
            raise ValueError(f"end {end} is before start {start} and --no-overnight set")
        diff_min = end_min - start_min
        crossed = False
    else:
        if end_min < start_min:
            diff_min = (24 * 60 - start_min) + end_min
            crossed = True
        elif end_min == start_min:
            diff_min = 0
            crossed = False
        else:
            diff_min = end_min - start_min
            crossed = False

    if awake_minutes < 0:
        raise ValueError("--awake must be >= 0")
    if awake_minutes > diff_min:
        raise ValueError(f"--awake ({awake_minutes}m) exceeds total time ({diff_min}m)")

    gross_h = diff_min / 60
    net_h = (diff_min - awake_minutes) / 60
    return gross_h, net_h, crossed


def fmt_hours(h):
    """Format hours to 1 decimal, drop trailing .0."""
    rounded = round(h, 1)
    return f"{rounded:g}"


def main():
    parser = argparse.ArgumentParser(description="Compute sleep duration with day-boundary handling")
    parser.add_argument("start", help="Eyes-closed time (HH:MM, 24h)")
    parser.add_argument("end", help="Wake-up time (HH:MM, 24h)")
    parser.add_argument("--awake", type=int, default=0, metavar="MIN",
                        help="Minutes spent awake during the night (subtracted from total)")
    parser.add_argument("--no-overnight", action="store_true",
                        help="Disallow midnight crossing — error if end < start")
    parser.add_argument("--json", action="store_true", help="Output JSON instead of plain text")
    parser.add_argument("--prefix", default="~", help="Prefix for hours output (default: ~). Use '' for none.")
    args = parser.parse_args()

    try:
        gross_h, net_h, crossed = compute(args.start, args.end, args.awake, args.no_overnight)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        out = {
            "start": args.start,
            "end": args.end,
            "awake_minutes": args.awake,
            "crossed_midnight": crossed,
            "gross_hours": round(gross_h, 2),
            "net_hours": round(net_h, 2),
            "formatted": f"{args.prefix}{fmt_hours(net_h)}h",
        }
        print(json.dumps(out, indent=2))
    else:
        # Default: print just the formatted hours so it can be embedded in scripts
        print(f"{args.prefix}{fmt_hours(net_h)}h")


if __name__ == "__main__":
    main()
