import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sessions  # noqa: E402

NOW = datetime(2026, 10, 31, 4, 0, tzinfo=timezone.utc)
MS = lambda dt: int(dt.timestamp() * 1000)  # noqa: E731
ISO = lambda dt: dt.isoformat().replace("+00:00", "Z")  # noqa: E731


def reply(msg_id, minutes, input_tokens=10, cache_read=1000, cache_write=0, output=50, model="claude-opus-5-5",
          sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain, "timestamp": ISO(NOW + timedelta(minutes=minutes)),
            "message": {"id": msg_id, "model": model, "usage": {
                "input_tokens": input_tokens, "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write, "output_tokens": output}}}


def compact(minutes, pre=5000):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": ISO(NOW + timedelta(minutes=minutes)),
            "compactMetadata": {"trigger": "manual", "preTokens": pre}}


def model_attachment(model_id, marketing=None):
    return {"type": "attachment", "attachment": {"type": "model", "identity": {
        "modelId": model_id, "marketingName": marketing}}}


def write_lines(path, records, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="\n") as f:
        for r in records:
            f.write((r if isinstance(r, str) else json.dumps(r)) + "\n")


class TempDir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)


class SessionFile(unittest.TestCase):
    def test_reads_the_fields_claude_code_writes(self):
        sf = sessions.parse_session_file({
            "pid": 32732, "sessionId": "abc", "cwd": "C:\\code\\website", "startedAt": MS(NOW),
            "procStart": "134378589217378226", "kind": "interactive", "entrypoint": "cli", "name": "website-83",
            "status": "waiting", "waitingFor": "input needed", "statusUpdatedAt": MS(NOW), "version": "2.1.314"})
        self.assertEqual((sf.pid, sf.session_id, sf.name, sf.status, sf.waiting_for),
                         (32732, "abc", "website-83", "waiting", "input needed"))
        self.assertEqual(sf.proc_start, 134378589217378226)
        self.assertEqual(sf.status_since, NOW)

    def test_missing_fields_still_give_a_session(self):
        sf = sessions.parse_session_file({"pid": 5, "sessionId": "abc"})
        self.assertEqual((sf.status, sf.name, sf.kind, sf.proc_start), (None, None, "interactive", None))

    def test_unusable_files(self):
        for data in (None, [], {"sessionId": "abc"}, {"pid": "5", "sessionId": "abc"}, {"pid": 5},
                     {"pid": True, "sessionId": "abc"}):
            self.assertIsNone(sessions.parse_session_file(data), data)


class Classify(unittest.TestCase):
    def test_states(self):
        recent, old = NOW - timedelta(minutes=1), NOW - timedelta(minutes=6)
        self.assertEqual(sessions.classify("waiting", old, NOW), sessions.NEEDS_YOU)
        self.assertEqual(sessions.classify("busy", recent, NOW), sessions.RUNNING)
        self.assertEqual(sessions.classify("busy", old, NOW), sessions.STALLED)
        self.assertEqual(sessions.classify("busy", None, NOW), sessions.RUNNING)  # no transcript yet
        self.assertEqual(sessions.classify("idle", old, NOW), sessions.YOUR_TURN)
        self.assertEqual(sessions.classify("parked", old, NOW), sessions.UNKNOWN)
        self.assertEqual(sessions.classify(None, old, NOW), sessions.UNKNOWN)


class Transcript(TempDir):
    def read(self, records):
        path = self.dir / "t.jsonl"
        write_lines(path, records)
        reader = sessions.TranscriptReader(path)
        reader.refresh()
        return reader

    def test_context_is_the_latest_main_reply(self):
        r = self.read([reply("m1", 0, cache_read=1000), reply("m2", 1, input_tokens=5, cache_read=2000, cache_write=300)])
        self.assertEqual(r.last_usage[1], 2305)

    def test_a_reply_split_over_lines_counts_once(self):
        r = self.read([reply("m1", 0), reply("m1", 0), reply("m2", 1)])
        self.assertEqual(r.totals.total, 2 * (10 + 1000 + 50))

    def test_replayed_old_messages_are_history(self):
        # Claude Code re-writes a run of old messages, with their original ids and times, after newer ones.
        r = self.read([reply("m1", 0, cache_read=1000), reply("m2", 5, cache_read=9000), reply("m1", 0, cache_read=1000)])
        self.assertEqual(r.last_usage[1], 9010)
        self.assertEqual(r.totals.cache_read, 10_000)

    def test_sidechain_replies_count_in_totals_not_context(self):
        r = self.read([reply("m1", 0, cache_read=1000), reply("s1", 1, cache_read=50_000, sidechain=True)])
        self.assertEqual(r.last_usage[1], 1010)
        self.assertEqual(r.totals.cache_read, 51_000)

    def test_synthetic_and_error_replies_are_skipped(self):
        error = dict(reply("e1", 2), isApiErrorMessage=True)
        r = self.read([reply("m1", 0), reply("x1", 1, model="<synthetic>"), error])
        self.assertEqual(r.totals.total, 1060)

    def test_compaction_until_the_next_reply(self):
        r = self.read([reply("m1", 0, cache_read=300_000), compact(1, pre=300_010)])
        self.assertTrue(r.compacted)
        self.assertEqual(r.observed(), 300_010)
        write_lines(r.path, [reply("m2", 2, cache_read=20_000)], mode="a")
        r.refresh()
        self.assertFalse(r.compacted)
        self.assertEqual(r.last_usage[1], 20_010)
        self.assertEqual(r.observed(), 300_010)  # a 1M window inferred from usage survives /compact

    def test_titles_and_model_attachment(self):
        r = self.read([{"type": "ai-title", "aiTitle": "Draft post"}, model_attachment("claude-opus-5-5[1m]")])
        self.assertEqual(r.title, "Draft post")
        self.assertEqual(r.attachment, ("claude-opus-5-5[1m]", None))
        write_lines(r.path, [{"type": "custom-title", "customTitle": "website revamp"}], mode="a")
        r.refresh()
        self.assertEqual(r.title, "website revamp")

    def test_reads_only_what_was_added_and_waits_for_a_whole_line(self):
        r = self.read([reply("m1", 0)])
        line = json.dumps(reply("m2", 1, cache_read=4000))
        with open(r.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line[:40])
        self.assertTrue(r.refresh())
        self.assertEqual(r.last_usage[1], 1010)
        with open(r.path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line[40:] + "\n")
        r.refresh()
        self.assertEqual(r.last_usage[1], 4010)
        self.assertFalse(r.refresh())  # unchanged

    def test_a_truncated_file_is_read_again(self):
        r = self.read([reply("m1", 0), reply("m2", 1)])
        write_lines(r.path, [reply("n1", 2, cache_read=7)])
        r.refresh()
        self.assertEqual(r.totals.total, 10 + 7 + 50)

    def test_bad_lines_are_skipped(self):
        r = self.read(['{"usage": not json', "[1, 2]", reply("m1", 0)])
        self.assertEqual(r.last_usage[1], 1010)


class Window(unittest.TestCase):
    SETTING = ("opus[1m]", "model in settings.json")

    def test_attachment_with_a_size(self):
        self.assertEqual(sessions.resolve_window(("claude-opus-5-5[1m]", None), "claude-opus-5-5")[0], 1_000_000)
        self.assertEqual(sessions.resolve_window(("claude-opus-5-5", "Opus 5.5 (1M context)"))[0], 1_000_000)

    def test_setting_covers_a_plain_attachment_of_the_same_family(self):
        tokens, source = sessions.resolve_window(("claude-opus-5-5", None), "claude-opus-5-5", self.SETTING)
        self.assertEqual(tokens, 1_000_000)
        self.assertIn("opus[1m]", source)

    def test_setting_for_another_family_does_not_apply(self):
        self.assertEqual(sessions.resolve_window(None, "claude-haiku-5-5", self.SETTING)[0], 200_000)

    def test_families_that_are_always_1m(self):
        self.assertEqual(sessions.resolve_window(None, "claude-fable-5-1", self.SETTING)[0], 1_000_000)

    def test_usage_above_the_window_raises_it(self):
        tokens, source = sessions.resolve_window(None, "claude-sonnet-5-5", None, observed=250_000)
        self.assertEqual(tokens, 1_000_000)
        self.assertIn("usage above", source)

    def test_attachment_for_another_model_is_stale(self):
        # /model switched from a 1M Opus to Haiku since the attachment was written.
        self.assertEqual(sessions.resolve_window(("claude-opus-5-5[1m]", None), "claude-haiku-5-5")[0], 200_000)

    def test_default(self):
        self.assertEqual(sessions.resolve_window(), (200_000, "default"))


class MonitorScan(TempDir):
    def setUp(self):
        super().setUp()
        self.alive = {}
        self.clock = [0.0]
        self.monitor = sessions.Monitor(self.dir, now=lambda: NOW, alive=lambda pid, start: self.alive.get(pid, True),
                                        clock=lambda: self.clock[0])

    def session(self, pid, sid, status, cwd="B:\\work\\site", since=NOW - timedelta(minutes=3), **extra):
        data = {"pid": pid, "sessionId": sid, "cwd": cwd, "status": status, "statusUpdatedAt": MS(since),
                "name": f"site-{pid}", "kind": "interactive", **extra}
        path = self.dir / "sessions" / f"{pid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def transcript(self, sid, records, folder="B--work-site"):
        path = self.dir / "projects" / folder / f"{sid}.jsonl"
        write_lines(path, records)
        return path

    def test_lists_live_sessions_most_urgent_first(self):
        self.session(1, "a", "busy")
        self.session(2, "b", "idle", since=NOW - timedelta(minutes=30))
        self.session(3, "c", "waiting", waitingFor="permission")
        self.session(4, "d", "idle", since=NOW - timedelta(hours=2))
        found = self.monitor.scan()
        self.assertEqual([s.pid for s in found], [3, 4, 2, 1])
        self.assertEqual(found[0].waiting_for, "permission")

    def test_dead_processes_are_skipped(self):
        self.session(1, "a", "busy")
        self.session(2, "b", "idle")
        self.alive[2] = False
        self.assertEqual([s.pid for s in self.monitor.scan()], [1])

    def test_tokens_from_the_transcript_and_subagents(self):
        self.session(1, "a", "busy")
        path = self.transcript("a", [model_attachment("claude-opus-5-5[1m]"), reply("m1", 0, cache_read=400_000)])
        write_lines(path.parent / "a" / "subagents" / "agent-x.jsonl", [reply("s1", 0, cache_read=100, sidechain=True)])
        write_lines(path.parent / "a" / "subagents" / "workflows" / "wf" / "agent-y.jsonl",
                    [reply("s2", 0, cache_read=200, sidechain=True)])
        s = self.monitor.scan()[0]
        self.assertEqual((s.context, s.window), (400_010, 1_000_000))
        self.assertAlmostEqual(s.context_percent, 40.001)
        self.assertEqual(s.main.total, 400_060)
        self.assertEqual(s.subagents.total, 100 + 200 + 2 * 60)
        self.assertEqual(s.transcript, path)

    def test_named_by_title_not_by_folder(self):
        self.session(1, "a", "idle", nameSource="derived")
        self.assertEqual(self.monitor.scan()[0].name, "site-1")  # nothing better yet
        path = self.transcript("a", [{"type": "ai-title", "aiTitle": "Tray icon question mark"}])
        self.clock[0] += sessions.LOOKUP_RETRY_SECONDS
        self.assertEqual(self.monitor.scan()[0].name, "Tray icon question mark")
        self.session(1, "a", "idle", name="Plan Limits", nameSource="rename")
        self.assertEqual(self.monitor.scan()[0].name, "Plan Limits")
        write_lines(path, [{"type": "custom-title", "customTitle": "Short Names"}], mode="a")
        self.assertEqual(self.monitor.scan()[0].name, "Short Names")

    def test_transcript_found_by_session_id_when_the_folder_name_differs(self):
        self.session(1, "a", "idle")
        self.transcript("a", [reply("m1", 0)], folder="B--work-site-shortened-1234abcd")
        self.assertEqual(self.monitor.scan()[0].context, 1010)

    def test_a_session_without_a_transcript_yet_is_looked_for_again_later(self):
        self.session(1, "a", "busy")
        self.assertIsNone(self.monitor.scan()[0].context)
        self.transcript("a", [reply("m1", 0)])
        self.assertIsNone(self.monitor.scan()[0].context)  # not looked for again yet
        self.clock[0] += sessions.LOOKUP_RETRY_SECONDS
        self.assertEqual(self.monitor.scan()[0].context, 1010)

    def test_quiet_busy_session_is_stalled(self):
        self.session(1, "a", "busy")
        path = self.transcript("a", [reply("m1", 0)])
        quiet = (NOW - timedelta(minutes=7)).timestamp()
        import os
        os.utime(path, (quiet, quiet))
        s = self.monitor.scan()[0]
        self.assertEqual(s.state, sessions.STALLED)
        self.assertEqual(s.since.replace(microsecond=0), NOW - timedelta(minutes=7))

    def test_window_from_the_model_setting(self):
        (self.dir / "settings.json").write_text(json.dumps({"model": "opus[1m]"}), encoding="utf-8")
        self.session(1, "a", "idle")
        self.transcript("a", [reply("m1", 0, cache_read=100_000)])
        s = self.monitor.scan()[0]
        self.assertEqual(s.window, 1_000_000)
        self.assertIn("opus[1m]", s.window_source)

    def test_unreadable_session_file_is_skipped(self):
        (self.dir / "sessions").mkdir(parents=True)
        (self.dir / "sessions" / "9.json").write_text("{half a file", encoding="utf-8")
        self.session(1, "a", "idle")
        self.assertEqual([s.pid for s in self.monitor.scan()], [1])


def make_session(pid, state, status_since, name=None, waiting_for=None, since=None):
    return sessions.Session(
        pid=pid, session_id=f"s{pid}", name=name or f"site-{pid}", title=None, cwd="B:\\work\\site",
        kind="interactive", entrypoint="cli", version=None, state=state, waiting_for=waiting_for,
        since=since or status_since, status_since=status_since, started_at=None, model=None, context=None,
        window=200_000, window_source="default", compacted=False)


class AlertRules(unittest.TestCase):
    def setUp(self):
        self.alerts = sessions.Alerts()

    def step(self, *items, now=NOW):
        return [(kind, s.pid) for kind, s, _ in self.alerts.update(list(items), now)]

    def test_first_update_is_silent(self):
        self.assertEqual(self.step(make_session(1, sessions.NEEDS_YOU, NOW)), [])

    def test_needs_you_alerts_once(self):
        self.step(make_session(1, sessions.RUNNING, NOW))
        self.assertEqual(self.step(make_session(1, sessions.NEEDS_YOU, NOW)), [("attention", 1)])
        self.assertEqual(self.step(make_session(1, sessions.NEEDS_YOU, NOW)), [])

    def test_stalled_alerts(self):
        self.step(make_session(1, sessions.RUNNING, NOW))
        self.assertEqual(self.step(make_session(1, sessions.STALLED, NOW)), [("attention", 1)])

    def test_a_long_turn_finishing_alerts(self):
        start = NOW
        self.step(make_session(1, sessions.YOUR_TURN, start - timedelta(hours=1)))
        self.step(make_session(1, sessions.RUNNING, start))
        self.assertEqual(self.step(make_session(1, sessions.YOUR_TURN, start + timedelta(minutes=3))),
                         [("finished", 1)])

    def test_a_short_turn_stays_silent(self):
        self.step(make_session(1, sessions.YOUR_TURN, NOW - timedelta(hours=1)))
        self.step(make_session(1, sessions.RUNNING, NOW))
        self.assertEqual(self.step(make_session(1, sessions.YOUR_TURN, NOW + timedelta(minutes=1))), [])

    def test_a_permission_prompt_does_not_restart_the_turn(self):
        # Claude Code restamps statusUpdatedAt on each change, so the turn start must be kept.
        self.step(make_session(1, sessions.YOUR_TURN, NOW - timedelta(hours=1)))
        self.step(make_session(1, sessions.RUNNING, NOW))
        self.step(make_session(1, sessions.NEEDS_YOU, NOW + timedelta(minutes=1)))
        self.step(make_session(1, sessions.RUNNING, NOW + timedelta(minutes=2)))
        self.assertEqual(self.step(make_session(1, sessions.YOUR_TURN, NOW + timedelta(minutes=2, seconds=30))),
                         [("finished", 1)])

    def test_a_new_session_waiting_alerts(self):
        self.step(make_session(1, sessions.RUNNING, NOW))
        self.assertEqual(self.step(make_session(1, sessions.RUNNING, NOW), make_session(2, sessions.NEEDS_YOU, NOW)),
                         [("attention", 2)])


class Notification(unittest.TestCase):
    def test_one_change(self):
        s = make_session(1, sessions.NEEDS_YOU, NOW, name="website-83", waiting_for="permission")
        self.assertEqual(sessions.notification([("attention", s, "permission")]),
                         ("Needs you: website-83", "permission\nsite"))

    def test_several_changes_make_one_notification(self):
        a = make_session(1, sessions.NEEDS_YOU, NOW, name="a")
        b = make_session(2, sessions.STALLED, NOW, name="b")
        title, message = sessions.notification([("attention", a, ""), ("attention", b, "")])
        self.assertEqual(title, "2 sessions need you")
        self.assertEqual(message, "a: Needs you\nb: Stalled")
        c = make_session(3, sessions.YOUR_TURN, NOW, name="c")
        self.assertEqual(sessions.notification([("attention", a, ""), ("finished", c, "")])[0], "2 sessions changed")

    def test_nothing(self):
        self.assertIsNone(sessions.notification([]))


class Formatting(unittest.TestCase):
    def test_tokens(self):
        for n, text in ((0, "0"), (999, "999"), (1000, "1.0k"), (48_230, "48.2k"), (512_000, "512k"),
                        (112_400_000, "112M"), (23_000_000, "23.0M"), (2_500_000_000, "2.5B")):
            self.assertEqual(sessions.format_tokens(n), text)

    def test_elapsed(self):
        for seconds, text in ((0, "0s"), (40, "40s"), (60, "1m"), (3700, "1h 1m"), (90_000, "1d 1h")):
            self.assertEqual(sessions.format_elapsed(seconds), text)


if __name__ == "__main__":
    unittest.main()
