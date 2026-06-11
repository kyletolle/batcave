"""Regression tests for sleep_duration.py.

Run from vault root:
    python3 -m pytest "3 Information/Scripts/tests/" -v
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import sleep_duration as sd

SCRIPT = Path(__file__).resolve().parent.parent / "sleep_duration.py"


class TestParseHHMM:
    def test_basic(self):
        assert sd.parse_hhmm("00:00") == 0
        assert sd.parse_hhmm("07:30") == 7 * 60 + 30
        assert sd.parse_hhmm("23:59") == 23 * 60 + 59

    def test_single_digit_hour(self):
        assert sd.parse_hhmm("7:05") == 7 * 60 + 5

    def test_strips_whitespace(self):
        assert sd.parse_hhmm("  22:30  ") == 22 * 60 + 30

    def test_missing_colon_raises(self):
        with pytest.raises(ValueError):
            sd.parse_hhmm("2230")

    @pytest.mark.parametrize("bad", ["24:00", "25:30", "22:60", "22:99"])
    def test_out_of_range_raises(self, bad):
        with pytest.raises(ValueError):
            sd.parse_hhmm(bad)


class TestCompute:
    def test_same_day(self):
        gross, net, crossed = sd.compute("00:20", "07:20")
        assert gross == 7.0
        assert net == 7.0
        assert crossed is False

    def test_overnight(self):
        gross, net, crossed = sd.compute("22:30", "07:00")
        assert gross == 8.5
        assert net == 8.5
        assert crossed is True

    def test_ends_at_midnight(self):
        # 21:00 -> 00:00 crosses into next day (3h)
        gross, _, crossed = sd.compute("21:00", "00:00")
        assert gross == 3.0
        assert crossed is True

    def test_exact_same_time_is_zero(self):
        gross, net, crossed = sd.compute("22:30", "22:30")
        assert gross == 0
        assert net == 0
        assert crossed is False

    def test_awake_subtracted(self):
        gross, net, _ = sd.compute("22:30", "07:00", awake_minutes=30)
        assert gross == 8.5
        assert net == 8.0

    def test_awake_zero_is_noop(self):
        gross, net, _ = sd.compute("22:30", "07:00", awake_minutes=0)
        assert gross == net

    def test_awake_negative_raises(self):
        with pytest.raises(ValueError):
            sd.compute("22:30", "07:00", awake_minutes=-5)

    def test_awake_exceeds_total_raises(self):
        with pytest.raises(ValueError):
            sd.compute("22:30", "07:00", awake_minutes=10_000)

    def test_no_overnight_same_day_ok(self):
        gross, _, crossed = sd.compute("00:20", "07:20", no_overnight=True)
        assert gross == 7.0
        assert crossed is False

    def test_no_overnight_rejects_crossing(self):
        with pytest.raises(ValueError):
            sd.compute("22:30", "07:00", no_overnight=True)

    def test_no_overnight_same_time_ok(self):
        gross, _, crossed = sd.compute("22:30", "22:30", no_overnight=True)
        assert gross == 0
        assert crossed is False

    def test_minute_precision(self):
        # 22:15 -> 06:45 = 8h30m = 8.5h
        gross, _, _ = sd.compute("22:15", "06:45")
        assert gross == 8.5

    def test_fractional_hours(self):
        # 23:00 -> 06:20 = 7h20m
        gross, _, _ = sd.compute("23:00", "06:20")
        assert gross == pytest.approx(7 + 20 / 60)


class TestFmtHours:
    def test_drops_trailing_zero(self):
        assert sd.fmt_hours(8.0) == "8"
        assert sd.fmt_hours(7.0) == "7"

    def test_keeps_one_decimal(self):
        assert sd.fmt_hours(8.5) == "8.5"
        assert sd.fmt_hours(6.3) == "6.3"

    def test_rounds_to_one_decimal(self):
        assert sd.fmt_hours(7.333) == "7.3"

    def test_zero(self):
        assert sd.fmt_hours(0) == "0"


class TestCLI:
    """End-to-end smoke tests through subprocess."""

    def run(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            capture_output=True,
            text=True,
        )

    def test_plain_output_default_prefix(self):
        r = self.run("22:30", "07:00")
        assert r.returncode == 0
        assert r.stdout.strip() == "~8.5h"

    def test_custom_prefix(self):
        r = self.run("22:30", "07:00", "--prefix", "")
        assert r.returncode == 0
        assert r.stdout.strip() == "8.5h"

    def test_json_output(self):
        r = self.run("22:30", "07:00", "--json")
        assert r.returncode == 0
        payload = json.loads(r.stdout)
        assert payload["gross_hours"] == 8.5
        assert payload["net_hours"] == 8.5
        assert payload["crossed_midnight"] is True
        assert payload["formatted"] == "~8.5h"

    def test_json_with_awake(self):
        r = self.run("22:30", "07:00", "--awake", "30", "--json")
        assert r.returncode == 0
        payload = json.loads(r.stdout)
        assert payload["gross_hours"] == 8.5
        assert payload["net_hours"] == 8.0

    def test_no_overnight_error_exits_nonzero(self):
        r = self.run("22:30", "07:00", "--no-overnight")
        assert r.returncode != 0
        assert "Error" in r.stderr

    def test_bad_time_exits_nonzero(self):
        r = self.run("25:00", "07:00")
        assert r.returncode != 0
