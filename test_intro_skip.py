"""Intro Skipper integration: exclusion scope, scan wait, trigger guards.

Every one of these paths writes to a live Jellyfin (plugin configuration, task
runs), so they cannot be exercised against the real server without spending CPU
on the user's library. The API is stubbed and the logic tested on its own.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import anime_rss as core


class ExclusionScopeCase(unittest.TestCase):
    """analyze_excluded_dirs decides what Intro Skipper never looks at."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "BangumiJF"
        self.root.mkdir()
        p = mock.patch.object(core, "JELLYFIN_MIRROR", str(self.root))
        p.start()
        self.addCleanup(p.stop)

    def folders(self, *names: str) -> None:
        for n in names:
            (self.root / n).mkdir()

    def excluded(self, cutoff: str = "2026.07") -> set[str]:
        with mock.patch.object(core, "ANALYZE_SKIP_BEFORE_SEASON", cutoff):
            return {Path(p).name for p in core.analyze_excluded_dirs()}

    def test_only_cours_before_the_cutoff_are_excluded(self):
        self.folders("2025.10", "2026.01", "2026.04", "2026.07")
        self.assertEqual(self.excluded(), {"2025.10", "2026.01", "2026.04"})

    def test_the_cutoff_cour_itself_is_analysed(self):
        self.folders("2026.07")
        self.assertEqual(self.excluded(), set())

    def test_a_future_cour_is_analysed_without_maintenance(self):
        """The blacklist shape is the whole point: next cour needs no edit."""
        self.folders("2026.07", "2026.10", "2027.01")
        self.assertEqual(self.excluded(), set())

    def test_non_cour_folders_are_excluded(self):
        """Ancient is the user's hand-managed archive, not ours to analyse."""
        self.folders("Ancient", "2026.07", "scratch")
        self.assertEqual(self.excluded(), {"Ancient", "scratch"})

    def test_files_at_the_top_level_are_not_listed(self):
        self.folders("2026.04")
        (self.root / "readme.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.excluded(), {"2026.04"})

    def test_a_missing_mirror_yields_nothing(self):
        with mock.patch.object(core, "JELLYFIN_MIRROR", str(self.root / "nope")):
            self.assertEqual(core.analyze_excluded_dirs(), [])

    def test_paths_are_absolute_under_the_mirror(self):
        """PathExclusions matches path roots, so a bare cour name would miss."""
        self.folders("2026.04")
        with mock.patch.object(core, "ANALYZE_SKIP_BEFORE_SEASON", "2026.07"):
            got = core.analyze_excluded_dirs()
        self.assertEqual(got, [str(self.root / "2026.04")])

    def test_cutoff_is_independent_of_the_destructive_one(self):
        """Reusing skip_before_season here would tie 'never delete my old shows'
        to 'never analyse them'. Guard that they are read from separate names."""
        self.folders("2026.04", "2026.07")
        with mock.patch.object(core, "SKIP_BEFORE_SEASON", "2020.01"):
            self.assertEqual(self.excluded("2026.07"), {"2026.04"})


class SyncExclusionsCase(unittest.TestCase):
    """intro_skipper_sync_exclusions writes the plugin config, carefully."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "BangumiJF"
        (self.root / "2026.04").mkdir(parents=True)
        (self.root / "2026.07").mkdir()
        for target, value in (("JELLYFIN_MIRROR", str(self.root)),
                              ("ANALYZE_SKIP_BEFORE_SEASON", "2026.07"),
                              ("INTRO_SKIP_ENABLED", True),
                              ("JELLYFIN_API_KEY", "k")):
            p = mock.patch.object(core, target, value)
            p.start()
            self.addCleanup(p.stop)

    def run_sync(self, current: list[str], **kw) -> tuple[bool, list, dict]:
        """Drive one sync against a fake plugin config; return (ret, posts, conf)."""
        conf = {"PathExclusions": list(current), "MaxParallelism": 2,
                "SeriesExclusions": [], "ProcessPriority": "BelowNormal"}
        posts = []

        def fake(method, path, params=None, body=None, timeout=30):
            if method == "GET":
                return 200, conf
            posts.append((path, body))
            return 204, None

        with mock.patch.object(core, "_jf_req", side_effect=fake):
            ret = core.intro_skipper_sync_exclusions(**kw)
        return ret, posts, conf

    def test_writes_the_excluded_cours_when_empty(self):
        ret, posts, _ = self.run_sync([])
        self.assertTrue(ret)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][1]["PathExclusions"], [str(self.root / "2026.04")])

    def test_is_idempotent(self):
        """The watcher calls this every pass; a POST per pass would be noise."""
        ret, posts, _ = self.run_sync([str(self.root / "2026.04")])
        self.assertFalse(ret)
        self.assertEqual(posts, [])

    def test_separator_and_case_differences_are_not_a_change(self):
        """The plugin folds these itself, so we must not fight it forever."""
        ret, posts, _ = self.run_sync([str(self.root / "2026.04").replace("\\", "/").upper()])
        self.assertFalse(ret)
        self.assertEqual(posts, [])

    def test_entries_outside_the_mirror_are_preserved(self):
        """Those are the user's, added in the plugin's own UI."""
        ret, posts, _ = self.run_sync([r"D:\Movies\Home Video"])
        self.assertTrue(ret)
        self.assertIn(r"D:\Movies\Home Video", posts[0][1]["PathExclusions"])

    def test_a_stale_mirror_entry_is_dropped(self):
        """A cour that crossed the cutoff, or a folder that no longer exists."""
        ret, posts, _ = self.run_sync([str(self.root / "2026.07"),
                                       str(self.root / "2025.10")])
        self.assertTrue(ret)
        self.assertEqual(posts[0][1]["PathExclusions"], [str(self.root / "2026.04")])

    def test_other_plugin_settings_are_carried_through(self):
        """We POST the whole document back, so it had better be the whole one."""
        _, posts, _ = self.run_sync([])
        self.assertEqual(posts[0][1]["MaxParallelism"], 2)
        self.assertEqual(posts[0][1]["ProcessPriority"], "BelowNormal")

    def test_dry_run_writes_nothing(self):
        ret, posts, _ = self.run_sync([], dry_run=True)
        self.assertFalse(ret)
        self.assertEqual(posts, [])

    def test_disabled_does_not_even_read(self):
        with mock.patch.object(core, "INTRO_SKIP_ENABLED", False):
            ret, posts, _ = self.run_sync([])
        self.assertFalse(ret)
        self.assertEqual(posts, [])

    def test_a_read_failure_is_swallowed(self):
        with mock.patch.object(core, "_jf_req", side_effect=OSError("down")):
            self.assertFalse(core.intro_skipper_sync_exclusions())

    def test_a_write_failure_is_swallowed(self):
        def fake(method, path, params=None, body=None, timeout=30):
            if method == "GET":
                return 200, {"PathExclusions": []}
            raise OSError("down")

        with mock.patch.object(core, "_jf_req", side_effect=fake):
            self.assertFalse(core.intro_skipper_sync_exclusions())


class WaitForScanCase(unittest.TestCase):
    """_jf_wait_refresh_idle is the guard against analysing a half-scanned library."""

    def states(self, seq: list[str | None]):
        """Feed a scripted sequence of task States to the poller."""
        it = iter(seq)

        def fake(key):
            nxt = next(it, seq[-1])
            return None if nxt is None else {"State": nxt, "Id": "x"}

        return mock.patch.object(core, "_jf_task_by_key", side_effect=fake)

    def test_waits_out_a_running_scan(self):
        with self.states(["Running", "Running", "Idle"]), \
             mock.patch.object(core.time, "sleep"):
            self.assertTrue(core._jf_wait_refresh_idle(600))

    def test_does_not_return_early_on_the_first_idle_poll(self):
        """The scan is not instant to start: an immediate Idle inside the grace
        window must keep polling, or we trigger before the episode is indexed."""
        seen = []

        def fake(key):
            seen.append(1)
            # Idle, then the scan finally comes up, then it finishes.
            return {"State": ["Idle", "Running", "Running", "Idle"][min(len(seen) - 1, 3)],
                    "Id": "x"}

        with mock.patch.object(core, "_jf_task_by_key", side_effect=fake), \
             mock.patch.object(core.time, "sleep"), \
             mock.patch.object(core, "INTRO_SKIP_START_GRACE", 30):
            self.assertTrue(core._jf_wait_refresh_idle(600))
        # 4 polls, not 1: it did not take the opening Idle at face value.
        self.assertEqual(len(seen), 4)

    def test_gives_up_on_a_scan_that_never_starts(self):
        """A refresh with nothing to do finishes before the first poll."""
        clock = iter([0, 1, 2, 100, 100, 100])
        with self.states(["Idle"]), mock.patch.object(core.time, "sleep"), \
             mock.patch.object(core.time, "time", lambda: next(clock, 100)), \
             mock.patch.object(core, "INTRO_SKIP_START_GRACE", 30):
            self.assertTrue(core._jf_wait_refresh_idle(600))

    def test_a_scan_that_never_ends_times_out(self):
        with self.states(["Running"]), mock.patch.object(core.time, "sleep"):
            self.assertFalse(core._jf_wait_refresh_idle(0))

    def test_a_missing_task_is_not_worth_waiting_for(self):
        with self.states([None]), mock.patch.object(core.time, "sleep"):
            self.assertTrue(core._jf_wait_refresh_idle(600))

    def test_an_api_failure_stops_the_trigger(self):
        """Better to skip this round than to analyse a library mid-scan."""
        with mock.patch.object(core, "_jf_task_by_key", side_effect=OSError("down")), \
             mock.patch.object(core.time, "sleep"):
            self.assertFalse(core._jf_wait_refresh_idle(600))


class TriggerCase(unittest.TestCase):
    """The guards around actually starting the detection task."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "intro_skip_state.json"
        for target, value in (("INTRO_SKIP_STATE_PATH", self.state),
                              ("INTRO_SKIP_ENABLED", True),
                              ("JELLYFIN_API_KEY", "k"),
                              ("INTRO_SKIP_MIN_GAP", 1800)):
            p = mock.patch.object(core, target, value)
            p.start()
            self.addCleanup(p.stop)
        for target in ("intro_skipper_sync_exclusions",):
            p = mock.patch.object(core, target, lambda **kw: False)
            p.start()
            self.addCleanup(p.stop)

    def trigger(self, task_state="Idle", idle=True, **kw):
        posts = []

        def fake_req(method, path, params=None, body=None, timeout=30):
            posts.append((method, path))
            return 204, None

        task = None if task_state is None else {"State": task_state, "Id": "tid"}
        with mock.patch.object(core, "_jf_wait_refresh_idle", lambda t: idle), \
             mock.patch.object(core, "_jf_task_by_key", lambda k: task), \
             mock.patch.object(core, "_jf_req", side_effect=fake_req):
            ret = core.intro_skipper_trigger_analysis(**kw)
        return ret, posts

    def test_triggers_and_stamps_the_ledger(self):
        ret, posts = self.trigger()
        self.assertTrue(ret)
        self.assertEqual(posts, [("POST", "/ScheduledTasks/Running/tid")])
        self.assertGreater(json.loads(self.state.read_text())["last_trigger"], 0)

    def test_throttles_a_second_trigger(self):
        """A busy evening lands several episodes across several passes."""
        self.assertTrue(self.trigger()[0])
        ret, posts = self.trigger()
        self.assertFalse(ret)
        self.assertEqual(posts, [])

    def test_the_throttle_survives_a_restart(self):
        """It is on disk, not in memory: restarting watch must not reset it."""
        self.state.write_text(json.dumps({"last_trigger": core.time.time()}),
                              encoding="utf-8")
        self.assertFalse(self.trigger()[0])

    def test_does_not_stack_on_a_running_task(self):
        ret, posts = self.trigger(task_state="Running")
        self.assertFalse(ret)
        self.assertEqual(posts, [])

    def test_a_missing_task_is_reported_not_raised(self):
        """The plugin may simply not be installed."""
        ret, posts = self.trigger(task_state=None)
        self.assertFalse(ret)
        self.assertEqual(posts, [])

    def test_a_scan_that_never_settles_blocks_the_trigger(self):
        ret, posts = self.trigger(idle=False)
        self.assertFalse(ret)
        self.assertEqual(posts, [])

    def test_dry_run_triggers_nothing(self):
        ret, posts = self.trigger(dry_run=True)
        self.assertFalse(ret)
        self.assertEqual(posts, [])
        self.assertFalse(self.state.exists())

    def test_disabled_triggers_nothing(self):
        with mock.patch.object(core, "INTRO_SKIP_ENABLED", False):
            ret, posts = self.trigger()
        self.assertFalse(ret)
        self.assertEqual(posts, [])

    def test_a_corrupt_ledger_does_not_block_forever(self):
        self.state.write_text("{not json", encoding="utf-8")
        self.assertTrue(self.trigger()[0])

    def test_a_post_failure_leaves_the_ledger_alone(self):
        """Otherwise a failed trigger would throttle the next real one."""
        with mock.patch.object(core, "_jf_wait_refresh_idle", lambda t: True), \
             mock.patch.object(core, "_jf_task_by_key",
                               lambda k: {"State": "Idle", "Id": "tid"}), \
             mock.patch.object(core, "_jf_req", side_effect=OSError("down")):
            self.assertFalse(core.intro_skipper_trigger_analysis())
        self.assertFalse(self.state.exists())


class PendingRetryCase(unittest.TestCase):
    """A trigger the throttle refuses must be delivered later, not dropped.

    The regression this guards: the trigger only fires from the mirror pass, and
    only when that pass linked something. On an evening where several shows
    update inside one throttle window, the second show's episode was silently
    left for the plugin's own midnight sweep.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "intro_skip_state.json"
        for target, value in (("INTRO_SKIP_STATE_PATH", self.state),
                              ("INTRO_SKIP_ENABLED", True),
                              ("JELLYFIN_API_KEY", "k"),
                              ("INTRO_SKIP_MIN_GAP", 1800)):
            p = mock.patch.object(core, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.kicks = []
        p = mock.patch.object(core, "intro_skipper_analyze_async",
                              lambda: self.kicks.append(1))
        p.start()
        self.addCleanup(p.stop)

    def state_now(self) -> dict:
        return json.loads(self.state.read_text(encoding="utf-8"))

    def test_landing_episodes_raises_the_flag(self):
        core.intro_skipper_mark_pending()
        self.assertTrue(self.state_now()["pending"])

    def test_a_delivered_trigger_lowers_it(self):
        core.intro_skipper_mark_pending()
        with mock.patch.object(core, "intro_skipper_sync_exclusions", lambda **k: False), \
             mock.patch.object(core, "_jf_wait_refresh_idle", lambda t: True), \
             mock.patch.object(core, "_jf_task_by_key",
                               lambda k: {"State": "Idle", "Id": "tid"}), \
             mock.patch.object(core, "_jf_req", lambda *a, **k: (204, None)):
            self.assertTrue(core.intro_skipper_trigger_analysis())
        self.assertFalse(self.state_now()["pending"])

    def test_the_second_show_in_the_window_is_not_lost(self):
        """The exact scenario: show A at 21:00, show B at 21:10."""
        core.intro_skipper_mark_pending()
        core._intro_skip_state_update(last_trigger=core.time.time(), pending=False)
        # Show B lands ten minutes later.
        core.intro_skipper_mark_pending()
        with mock.patch.object(core, "intro_skipper_sync_exclusions", lambda **k: False):
            self.assertFalse(core.intro_skipper_trigger_analysis())   # throttled
        self.assertTrue(self.state_now()["pending"], "flag must survive the throttle")
        # A quiet pass inside the window: still too early, nothing kicked.
        core.intro_skipper_retry_pending()
        self.assertEqual(self.kicks, [])
        # A quiet pass after the window: picked back up with no new episode.
        core._intro_skip_state_update(last_trigger=core.time.time() - 1801)
        core.intro_skipper_retry_pending()
        self.assertEqual(len(self.kicks), 1)

    def test_a_scan_that_never_settles_keeps_the_flag(self):
        core.intro_skipper_mark_pending()
        with mock.patch.object(core, "intro_skipper_sync_exclusions", lambda **k: False), \
             mock.patch.object(core, "_jf_wait_refresh_idle", lambda t: False):
            self.assertFalse(core.intro_skipper_trigger_analysis())
        self.assertTrue(self.state_now()["pending"])

    def test_a_busy_task_keeps_the_flag(self):
        core.intro_skipper_mark_pending()
        with mock.patch.object(core, "intro_skipper_sync_exclusions", lambda **k: False), \
             mock.patch.object(core, "_jf_wait_refresh_idle", lambda t: True), \
             mock.patch.object(core, "_jf_task_by_key",
                               lambda k: {"State": "Running", "Id": "tid"}):
            self.assertFalse(core.intro_skipper_trigger_analysis())
        self.assertTrue(self.state_now()["pending"])

    def test_a_failed_post_keeps_the_flag(self):
        core.intro_skipper_mark_pending()
        with mock.patch.object(core, "intro_skipper_sync_exclusions", lambda **k: False), \
             mock.patch.object(core, "_jf_wait_refresh_idle", lambda t: True), \
             mock.patch.object(core, "_jf_task_by_key",
                               lambda k: {"State": "Idle", "Id": "tid"}), \
             mock.patch.object(core, "_jf_req", side_effect=OSError("down")):
            self.assertFalse(core.intro_skipper_trigger_analysis())
        self.assertTrue(self.state_now()["pending"])

    def test_a_quiet_pass_with_nothing_pending_does_nothing(self):
        """Must stay free on the overwhelming majority of passes."""
        core.intro_skipper_retry_pending()
        self.assertEqual(self.kicks, [])

    def test_retry_is_a_no_op_when_disabled(self):
        core.intro_skipper_mark_pending()
        core._intro_skip_state_update(last_trigger=0)
        with mock.patch.object(core, "INTRO_SKIP_ENABLED", False):
            core.intro_skipper_retry_pending()
        self.assertEqual(self.kicks, [])

    def test_marking_is_a_no_op_when_disabled(self):
        with mock.patch.object(core, "INTRO_SKIP_ENABLED", False):
            core.intro_skipper_mark_pending()
        self.assertFalse(self.state.exists())

    def test_the_two_writers_do_not_clobber_each_other(self):
        """mark_pending runs on the sync thread, the stamp on the worker."""
        core._intro_skip_state_update(last_trigger=12345.0, pending=False)
        core.intro_skipper_mark_pending()
        got = self.state_now()
        self.assertEqual(got["last_trigger"], 12345.0)
        self.assertTrue(got["pending"])

    def test_concurrent_updates_all_survive(self):
        import threading
        core._intro_skip_state_update(last_trigger=1.0, pending=False)
        threads = [threading.Thread(target=core.intro_skipper_mark_pending)
                   for _ in range(20)]
        threads += [threading.Thread(
            target=core._intro_skip_state_update, kwargs={"last_trigger": 2.0})
            for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = self.state_now()          # readable JSON, not a torn write
        self.assertIn("last_trigger", got)
        self.assertIn("pending", got)

    def test_a_corrupt_ledger_reads_as_empty(self):
        self.state.write_text("{not json", encoding="utf-8")
        self.assertEqual(core._intro_skip_state_read(), {})
        core.intro_skipper_mark_pending()
        self.assertTrue(self.state_now()["pending"])


class AsyncKickCase(unittest.TestCase):
    """The sync pass must never block on the scan wait."""

    def test_only_one_flight_at_a_time(self):
        import threading
        release = threading.Event()
        started = threading.Semaphore(0)
        calls = []

        def slow(**kw):
            calls.append(1)
            started.release()
            release.wait(5)
            return True

        with mock.patch.object(core, "INTRO_SKIP_ENABLED", True), \
             mock.patch.object(core, "JELLYFIN_API_KEY", "k"), \
             mock.patch.object(core, "intro_skipper_trigger_analysis", slow):
            core.intro_skipper_analyze_async()
            started.acquire(timeout=5)
            core.intro_skipper_analyze_async()   # must not queue behind the first
            release.set()
        self.assertEqual(len(calls), 1)

    def test_disabled_starts_no_thread(self):
        calls = []
        with mock.patch.object(core, "INTRO_SKIP_ENABLED", False), \
             mock.patch.object(core, "intro_skipper_trigger_analysis",
                               lambda **kw: calls.append(1)):
            core.intro_skipper_analyze_async()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
