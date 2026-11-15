import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import sessions  # noqa: E402
import usage  # noqa: E402
import view  # noqa: E402
from test_usage import NOW, SAMPLE  # noqa: E402


def session(pid, state, minutes=3, name=None, waiting_for=None, context=100_000, compacted=False):
    since = NOW - timedelta(minutes=minutes)
    return sessions.Session(
        pid=pid, session_id=f"s{pid}", name=name or f"site-{pid}", title="A title", cwd="/work/site",
        kind="interactive", entrypoint="cli", version=None, state=state, waiting_for=waiting_for, since=since,
        status_since=since, started_at=since, model="claude-opus-5-5", context=context, window=1_000_000,
        window_source="default", compacted=compacted, main=sessions.Tokens(10, 20, 3_000_000, 40))


def usage_data(**changes):
    return usage.parse_usage({**SAMPLE, **changes}, now=NOW)


class Build(unittest.TestCase):
    def test_before_the_first_numbers(self):
        v = view.build(None, None, [], NOW)
        self.assertEqual((v["state"], v["message"], v["limits"]), ("starting", "Loading...", []))
        self.assertEqual(v["bar"], {"text": "", "severity": "stale", "attention": 0,
                                    "tooltip": "Claude usage: loading"})

    def test_limits_carry_the_share_the_reset_and_the_warning(self):
        v = view.build(usage_data(), None, [], NOW + timedelta(minutes=2))
        self.assertEqual(v["state"], "ready")
        self.assertEqual(v["note"], "updated 2m ago")
        self.assertEqual([(r["label"], r["percentText"], r["severity"], r["status"]) for r in v["limits"]],
                         [("Session (5h)", "46%", "normal", ""),
                          ("Weekly (all models)", "91%", "critical", "Almost out"),
                          ("Weekly (Fable)", "0%", "normal", "")])
        self.assertTrue(v["limits"][0]["reset"].startswith("Resets in 1h 58m"))
        self.assertEqual(v["breakdown"], "This week: Claude Code 96%, Chats 4%")

    def test_the_bar_shows_the_session_limit(self):
        bar = view.build(usage_data(), None, [], NOW)["bar"]
        self.assertEqual((bar["text"], bar["severity"]), ("46%", "normal"))
        self.assertTrue(bar["tooltip"].splitlines()[0].startswith("Session 46%, resets "))

    def test_a_plan_without_a_session_limit_shows_the_weekly_one(self):
        data = usage_data(five_hour=None, limits=SAMPLE["limits"][1:])
        bar = view.build(data, None, [], NOW)["bar"]
        self.assertEqual((bar["text"], bar["severity"]), ("91%", "critical"))

    def test_a_failed_fetch_keeps_the_numbers_and_greys_them(self):
        v = view.build(usage_data(), usage.UsageError("network", "Offline or unreachable (URLError)."), [], NOW)
        self.assertEqual((v["note"], v["noteStrong"], v["message"]), ("Offline", True, ""))
        self.assertEqual({r["severity"] for r in v["limits"]}, {"stale"})
        self.assertEqual({r["status"] for r in v["limits"]}, {""})
        self.assertEqual(v["bar"]["severity"], "stale")
        self.assertEqual(v["bar"]["tooltip"].splitlines()[-1], "Offline")

    def test_an_expired_login_says_what_to_do(self):
        error = usage.UsageError("login", "Login expired. Run Claude Code once to refresh it.")
        self.assertEqual(view.build(usage_data(), error, [], NOW)["message"], str(error))
        v = view.build(None, error, [], NOW)
        self.assertEqual((v["state"], v["note"], v["message"]), ("error", "Login expired", str(error)))

    def test_session_rows(self):
        rows = view.build(None, None, [session(1, sessions.RUNNING), session(2, sessions.YOUR_TURN, context=None),
                                       session(3, sessions.RUNNING, context=850_000, compacted=True)], NOW)["sessions"]
        self.assertEqual({k: rows[0][k] for k in ("pid", "name", "state", "stateText", "total", "percent",
                                                  "percentText", "contextSeverity")},
                         {"pid": 1, "name": "site-1", "state": "running", "stateText": "Running 3m",
                          "total": "3.0M tokens", "percent": 10, "percentText": "10%", "contextSeverity": "normal"})
        self.assertEqual((rows[1]["percent"], rows[1]["percentText"], rows[1]["contextSeverity"]), (None, "", "stale"))
        self.assertEqual((rows[2]["percent"], rows[2]["stateText"]), (None, "Running 3m · compacted"))
        self.assertIn("Context: 100,000 of 1,000,000 tokens (10.0%)", rows[0]["details"])
        self.assertTrue(rows[0]["details"].endswith(view.OMARCHY_HINT))

    def test_sessions_that_need_you_are_counted(self):
        waiting = [session(1, sessions.NEEDS_YOU, waiting_for="permission"), session(2, sessions.STALLED),
                   session(3, sessions.RUNNING)]
        v = view.build(usage_data(), None, waiting, NOW)
        self.assertEqual((v["sessionsNote"], v["sessionsUrgent"], v["bar"]["attention"]), ("2 need you", True, 2))
        self.assertEqual(v["bar"]["tooltip"].splitlines()[0], "2 sessions need you")
        self.assertEqual(v["sessions"][0]["stateText"], "Needs you 3m · permission")
        one = view.build(None, None, waiting[:1], NOW)
        self.assertEqual((one["sessionsNote"], one["bar"]["tooltip"]), ("1 needs you", "1 session needs you"))
        calm = view.build(None, None, waiting[2:], NOW)
        self.assertEqual((calm["sessionsNote"], calm["sessionsUrgent"]), ("1 session", False))

    def test_the_note_of_what_a_click_did_rides_along(self):
        self.assertEqual(view.build(None, None, [], NOW, "Resume command copied")["flash"], "Resume command copied")

    def test_the_view_is_json(self):
        v = view.build(usage_data(), None, [session(1, sessions.NEEDS_YOU, name="<b>markup</b>")], NOW)
        self.assertEqual(json.loads(json.dumps(v))["sessions"][0]["name"], "<b>markup</b>")


class ContextSeverity(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual([view.context_severity(p) for p in (None, 59.9, 60, 79.9, 80)],
                         ["stale", "normal", "warning", "warning", "critical"])


if __name__ == "__main__":
    unittest.main()
