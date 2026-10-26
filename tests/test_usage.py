import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import usage  # noqa: E402

NOW = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)

# Trimmed from a real response: the shape that matters, none of the unused keys.
SAMPLE = {
    "five_hour": {"utilization": 46.0, "resets_at": "2026-10-27T05:59:59.586113+00:00"},
    "seven_day": {"utilization": 51.0, "resets_at": "2026-10-30T06:59:59.586130+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 46, "severity": "normal",
         "resets_at": "2026-10-27T05:59:59.586113+00:00", "scope": None},
        {"kind": "weekly_all", "group": "weekly", "percent": 91, "severity": "normal",
         "resets_at": "2026-10-30T06:59:59.586130+00:00", "scope": None},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "severity": "normal",
         "resets_at": "2026-10-30T07:00:00+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
    "seven_day_breakdown": {"rows": [
        {"key": "chat", "display_name": "Chats", "percent": 4},
        {"key": "claude_code", "display_name": "Claude Code", "percent": 96},
        {"key": "cowork", "display_name": "Cowork", "percent": 0},
    ]},
}


class ParseUsage(unittest.TestCase):
    def test_limits_list(self):
        u = usage.parse_usage(SAMPLE, now=NOW)
        self.assertEqual([l.label for l in u.limits],
                         ["Session (5h)", "Weekly (all models)", "Weekly (Fable)"])
        self.assertEqual([l.severity for l in u.limits], ["normal", "critical", "normal"])
        self.assertEqual(u.session.percent, 46)
        self.assertEqual(u.limits[0].resets_at, datetime(2026, 10, 27, 5, 59, 59, 586113, tzinfo=timezone.utc))

    def test_breakdown_sorted_without_zero_rows(self):
        u = usage.parse_usage(SAMPLE, now=NOW)
        self.assertEqual(u.breakdown, [("Claude Code", 96), ("Chats", 4)])

    def test_falls_back_to_windows_without_limits(self):
        data = {k: v for k, v in SAMPLE.items() if k != "limits"}
        u = usage.parse_usage(data, now=NOW)
        self.assertEqual([(l.label, l.percent) for l in u.limits],
                         [("Session (5h)", 46.0), ("Weekly (all models)", 51.0)])

    def test_unknown_kind_gets_readable_label(self):
        u = usage.parse_usage({"limits": [{"kind": "monthly_all", "percent": 5}]}, now=NOW)
        self.assertEqual(u.limits[0].label, "Monthly all")
        self.assertIsNone(u.limits[0].resets_at)


class Severity(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(usage.severity_for(74.9), "normal")
        self.assertEqual(usage.severity_for(75), "warning")
        self.assertEqual(usage.severity_for(90), "critical")

    def test_api_severity_can_raise_but_not_lower(self):
        self.assertEqual(usage.severity_for(10, "critical"), "critical")
        self.assertEqual(usage.severity_for(10, "elevated"), "warning")
        self.assertEqual(usage.severity_for(95, "warning"), "critical")


class Formatting(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(usage.format_duration(59), "0m")
        self.assertEqual(usage.format_duration(2 * 3600 + 5 * 60), "2h 5m")
        self.assertEqual(usage.format_duration(3 * 86400 + 4 * 3600), "3d 4h")

    def test_reset_within_a_day_shows_countdown_first(self):
        text = usage.format_reset(NOW + timedelta(hours=2, minutes=41), now=NOW)
        self.assertTrue(text.startswith("Resets in 2h 41m ("), text)

    def test_reset_beyond_a_day_shows_weekday_first(self):
        text = usage.format_reset(NOW + timedelta(days=3, hours=4), now=NOW)
        self.assertTrue(text.endswith("(in 3d 4h)"), text)
        self.assertNotIn("Resets in", text)

    def test_reset_in_the_past(self):
        self.assertEqual(usage.format_reset(NOW - timedelta(minutes=1), now=NOW), "Resetting now")

    def test_age(self):
        self.assertEqual(usage.format_age(NOW - timedelta(seconds=30), now=NOW), "just now")
        self.assertEqual(usage.format_age(NOW - timedelta(minutes=5), now=NOW), "5m ago")


class ReadToken(unittest.TestCase):
    def write(self, payload):
        f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(payload, f)
        f.close()
        self.addCleanup(Path(f.name).unlink)
        return f.name

    def test_valid(self):
        path = self.write({"claudeAiOauth": {"accessToken": "tok", "expiresAt": (NOW.timestamp() + 60) * 1000}})
        self.assertEqual(usage.read_token(path, now=NOW.timestamp()), "tok")

    def test_expired(self):
        path = self.write({"claudeAiOauth": {"accessToken": "tok", "expiresAt": (NOW.timestamp() - 60) * 1000}})
        with self.assertRaises(usage.UsageError) as ctx:
            usage.read_token(path, now=NOW.timestamp())
        self.assertEqual(ctx.exception.kind, "login")

    def test_missing_file(self):
        with self.assertRaises(usage.UsageError) as ctx:
            usage.read_token(Path(tempfile.gettempdir()) / "no_such_credentials.json")
        self.assertEqual(ctx.exception.kind, "login")


if __name__ == "__main__":
    unittest.main()
