#!/usr/bin/env python3
"""Tests: the automation event log must never lose or replay an episode.

Run:  python test_events.py             (stdlib unittest, no network, no qB)

events.json is consumed by an external process (the Atrium message centre) that
tracks its position with a seq cursor and reads the file directly whenever this
service is down. That makes three properties load-bearing, and every case here
asserts one of them:

  * seq is monotonic and survives a restart — a reused number silently costs the
    consumer every event that shared it;
  * a write is atomic — a reader that catches a half-written file must still see
    the previous good state, never a truncated one;
  * an episode produces exactly one event — the hardlink is the ledger entry, so
    re-running the mirror pass must not re-announce what is already in the library.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

import anime_rss as core


class EventLogCase(unittest.TestCase):
    """Base: each test gets a private events.json under a temp dir."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.events_path = self.root / "events.json"
        self._patch = mock.patch.object(core, "EVENTS_PATH", self.events_path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()

    def kinds(self) -> list[str]:
        return [e["kind"] for e in core.load_events()["events"]]

    def seqs(self) -> list[int]:
        return [e["seq"] for e in core.load_events()["events"]]


class TestSeq(EventLogCase):
    def test_empty_when_absent(self):
        data = core.load_events()
        self.assertEqual(data["seq"], 0)
        self.assertEqual(data["events"], [])

    def test_seq_is_monotonic_within_a_batch(self):
        core.add_events([{"kind": "episode.landed", "params": {"ep": n}} for n in (1, 2, 3)])
        self.assertEqual(self.seqs(), [1, 2, 3])

    def test_seq_continues_across_a_restart(self):
        core.add_events([{"kind": "episode.landed", "params": {}}] * 2)
        # A fresh process only knows what the file says — no in-memory counter.
        core.add_event("show.subscribed", {"title": "x"})
        self.assertEqual(self.seqs(), [1, 2, 3])

    def test_never_reissues_a_seq_when_the_header_is_stale(self):
        """A hand-edited or half-written header must not cause a duplicate seq.

        Reusing a number is worse than skipping one: the consumer's cursor is
        already past it, so every event sharing that seq is dropped forever.
        """
        core.add_events([{"kind": "episode.landed", "params": {}}] * 3)
        raw = json.loads(self.events_path.read_text(encoding="utf-8"))
        raw["seq"] = 1                       # header regressed, events intact
        self.events_path.write_text(json.dumps(raw), encoding="utf-8")
        core.add_event("episode.landed", {})
        self.assertEqual(self.seqs(), [1, 2, 3, 4])

    def test_corrupt_file_reads_as_empty(self):
        self.events_path.write_text("{ this is not json", encoding="utf-8")
        self.assertEqual(core.load_events()["events"], [])

    def test_add_events_with_no_items_touches_nothing(self):
        self.assertEqual(core.add_events([]), 0)
        self.assertFalse(self.events_path.exists())

    def test_explicit_zero_ts_is_not_rewritten_to_now(self):
        core.add_events([{"kind": "episode.landed", "params": {}, "ts": 0}])
        self.assertEqual(core.load_events()["events"][0]["ts"], 0)


class TestWrite(EventLogCase):
    def test_write_is_atomic_and_leaves_no_temp_file(self):
        core.add_event("episode.landed", {"show": "s"})
        self.assertTrue(self.events_path.exists())
        leftovers = [p.name for p in self.root.iterdir() if p.name != "events.json"]
        self.assertEqual(leftovers, [], f"temp file left behind: {leftovers}")

    def test_reader_sees_previous_state_when_a_write_dies_midway(self):
        core.add_event("episode.landed", {"show": "first"})
        before = core.load_events()
        with mock.patch.object(core.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                core.add_event("episode.landed", {"show": "second"})
        self.assertEqual(core.load_events(), before)

    def test_non_ascii_survives_the_round_trip(self):
        core.add_event("episode.landed", {"show": "攻壳机动队"})
        self.assertEqual(core.load_events()["events"][0]["params"]["show"], "攻壳机动队")


class TestPruning(EventLogCase):
    def test_keeps_everything_below_the_floor(self):
        core.add_events([{"kind": "episode.landed", "params": {}}] * 10)
        self.assertEqual(len(core.load_events()["events"]), 10)

    def test_old_events_survive_while_under_the_floor(self):
        ancient = int(time.time()) - 400 * 86400
        core.add_events([{"kind": "episode.landed", "params": {}, "ts": ancient}] * 5)
        self.assertEqual(len(core.load_events()["events"]), 5)

    def test_above_the_floor_the_age_cutoff_applies(self):
        ancient = int(time.time()) - 400 * 86400
        old = [{"kind": "episode.landed", "params": {"old": True}, "ts": ancient}] * 40
        fresh = [{"kind": "episode.landed", "params": {}}] * core.EVENTS_MIN_KEEP
        core.add_events(old + fresh)
        kept = core.load_events()["events"]
        self.assertEqual(len(kept), core.EVENTS_MIN_KEEP)
        self.assertFalse(any(e["params"].get("old") for e in kept))

    def test_pruning_does_not_rewind_seq(self):
        ancient = int(time.time()) - 400 * 86400
        core.add_events([{"kind": "episode.landed", "params": {}, "ts": ancient}] * 40
                        + [{"kind": "episode.landed", "params": {}}] * core.EVENTS_MIN_KEEP)
        data = core.load_events()
        self.assertEqual(data["seq"], core.EVENTS_MIN_KEEP + 40)
        core.add_event("episode.landed", {})
        self.assertEqual(core.load_events()["seq"], core.EVENTS_MIN_KEEP + 41)


class MirrorCase(EventLogCase):
    """Drives the real mirror pass over a temp library + mirror tree."""

    def setUp(self) -> None:
        super().setUp()
        self.src = self.root / "Bangumi"
        self.dst = self.root / "BangumiJF"
        self.src.mkdir()
        self.dst.mkdir()
        for target, value in (("BANGUMI_LIBRARY", str(self.src)),
                              ("JELLYFIN_MIRROR", str(self.dst)),
                              ("MIRROR_SKIP_BEFORE_SEASON", "")):
            p = mock.patch.object(core, target, value)
            p.start()
            self.addCleanup(p.stop)
        # Both of these talk to a real Jellyfin. The analysis trigger is only
        # rate-limited by an on-disk stamp, so without this stub a run outside
        # the throttle window would start a real scan on the developer's server.
        for target in ("_jellyfin_refresh", "intro_skipper_analyze_async"):
            p = mock.patch.object(core, target, lambda: None)
            p.start()
            self.addCleanup(p.stop)

    def episode(self, cour: str, show: str, name: str) -> Path:
        d = self.src / cour / show
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_bytes(b"x")
        return f


class TestEpisodeLanded(MirrorCase):
    def test_one_event_per_landed_episode(self):
        self.episode("2026.07", "Ghost in the Shell",
                     "[LoliHouse] The Ghost in the Shell - 04 [WebRip 1080p].mkv")
        core.mirror_sync_pass()
        events = [e for e in core.load_events()["events"]
                  if e["kind"] == "episode.landed" and not e["params"].get("backfill")]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["params"]["ep"], 4)
        self.assertEqual(events[0]["params"]["cour"], "2026.07")
        self.assertEqual(events[0]["params"]["show"], "Ghost in the Shell")
        self.assertEqual(events[0]["params"]["season"], "Season 01")

    def test_rerunning_the_pass_announces_nothing_twice(self):
        self.episode("2026.07", "Show", "[ANi] Show - 01 [1080P].mkv")
        core.mirror_sync_pass()
        first = self.seqs()
        core.mirror_sync_pass()
        core.mirror_sync_pass()
        self.assertEqual(self.seqs(), first)

    def test_unparseable_episode_number_still_produces_an_event(self):
        """BD batches and movie rips have no episode number. The message is the
        point; the number is a nicety. Dropping the event would break the
        'every episode lands in the ledger' contract."""
        self.episode("2026.07", "Some Movie", "Some.Movie.BDRip.mkv")
        core.mirror_sync_pass()
        events = [e for e in core.load_events()["events"] if e["kind"] == "episode.landed"]
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]["params"]["ep"])

    def test_specials_are_tagged_season_00(self):
        d = self.src / "2026.07" / "Show" / "SPs"
        d.mkdir(parents=True)
        (d / "[ANi] Show - OVA [1080P].mkv").write_bytes(b"x")
        core.mirror_sync_pass()
        ev = [e for e in core.load_events()["events"] if e["kind"] == "episode.landed"][0]
        self.assertEqual(ev["params"]["season"], "Season 00")

    def test_a_failed_ledger_write_rolls_the_hardlink_back(self):
        """The link and the ledger entry must live or die together.

        A link left behind without its entry is skipped by every later pass
        (link.exists()), so that episode would never be announced at all.
        Rolling the link back leaves the next pass free to retry.
        """
        core.mirror_sync_pass()          # open the ledger first
        self.episode("2026.07", "Show", "[ANi] Show - 01 [1080P].mkv")
        link = self.dst / "2026.07" / "Show" / "Season 01" / "[ANi] Show - 01 [1080P].mkv"
        with mock.patch.object(core, "add_event", side_effect=OSError("disk full")):
            self.assertEqual(core.mirror_sync_pass(), 0)
        self.assertFalse(link.exists(), "hardlink survived a failed ledger write")
        self.assertEqual(core.load_events()["events"], [])
        # Next pass retries cleanly and announces exactly once.
        self.assertEqual(core.mirror_sync_pass(), 1)
        self.assertEqual(len(core.load_events()["events"]), 1)
        self.assertTrue(link.exists())

    def test_non_video_files_are_ignored(self):
        d = self.src / "2026.07" / "Show"
        d.mkdir(parents=True)
        (d / "[ANi] Show - 01 [1080P].ass").write_bytes(b"x")
        core.mirror_sync_pass()
        self.assertEqual(self.kinds(), [])

    def test_ancient_folders_never_produce_events(self):
        """Ancient/ holds the user's hand-managed back catalogue; the mirror pass
        skips it, so it must stay out of the ledger too."""
        d = self.src / "Ancient" / "Old Show"
        d.mkdir(parents=True)
        (d / "[Group] Old Show - 01 [1080p].mkv").write_bytes(b"x")
        core.mirror_sync_pass()
        self.assertEqual(self.kinds(), [])


class TestBackfill(MirrorCase):
    def _mirrored(self, cour: str, show: str, name: str, age_s: float) -> None:
        d = self.dst / cour / show / "Season 01"
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_bytes(b"x")
        stamp = time.time() - age_s
        import os
        os.utime(f, (stamp, stamp))

    def test_recent_mirrored_episodes_are_backfilled(self):
        self._mirrored("2026.07", "Show", "[ANi] Show - 04 [1080P].mkv", 3600)
        core.mirror_sync_pass()
        events = core.load_events()["events"]
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["params"]["backfill"])
        self.assertEqual(events[0]["params"]["ep"], 4)

    def test_backfill_uses_the_file_mtime_not_now(self):
        self._mirrored("2026.07", "Show", "[ANi] Show - 04 [1080P].mkv", 3600)
        core.mirror_sync_pass()
        ts = core.load_events()["events"][0]["ts"]
        self.assertAlmostEqual(ts, time.time() - 3600, delta=60)

    def test_episodes_older_than_the_window_are_not_backfilled(self):
        self._mirrored("2026.07", "Show", "[ANi] Show - 01 [1080P].mkv", 10 * 86400)
        core.mirror_sync_pass()
        self.assertEqual(core.load_events()["events"], [])

    def test_backfill_runs_only_once(self):
        self._mirrored("2026.07", "Show", "[ANi] Show - 04 [1080P].mkv", 3600)
        core.mirror_sync_pass()
        after_first = self.seqs()
        self._mirrored("2026.07", "Show", "[ANi] Show - 05 [1080P].mkv", 60)
        core.mirror_sync_pass()
        core.mirror_sync_pass()
        self.assertEqual(self.seqs(), after_first)

    def test_backfill_and_its_marker_land_in_one_write(self):
        """Two writes would mean a crash in between replays the whole history
        on the next pass — which is exactly how a duplicated ledger happens."""
        self._mirrored("2026.07", "Show", "[ANi] Show - 04 [1080P].mkv", 3600)
        with mock.patch.object(core, "save_events", wraps=core.save_events) as spy:
            core.mirror_sync_pass()
        self.assertEqual(spy.call_count, 1)
        self.assertTrue(core.load_events()["backfilled"])

    def test_an_empty_backfill_still_marks_the_ledger_as_opened(self):
        """Otherwise every later pass re-scans the mirror looking for history."""
        core.mirror_sync_pass()
        self.assertTrue(self.events_path.exists())
        self.assertTrue(core.load_events()["backfilled"])
        self.assertEqual(core.load_events()["events"], [])

    def test_an_earlier_event_does_not_suppress_the_backfill(self):
        """apply_entries runs before the mirror pass, so a show.subscribed can
        create events.json first. Keying "already backfilled" off the file's
        existence would silently skip the whole first-run history."""
        self._mirrored("2026.07", "Show", "[ANi] Show - 04 [1080P].mkv", 3600)
        core.add_event("show.subscribed", {"title": "Other", "bgm_id": 1, "group": "ANi"})
        core.mirror_sync_pass()
        kinds = [e["kind"] for e in core.load_events()["events"]]
        self.assertEqual(kinds, ["show.subscribed", "episode.landed"])

    def test_backfill_is_capped(self):
        for n in range(core.EVENTS_BACKFILL_CAP + 20):
            self._mirrored("2026.07", "Show", f"[ANi] Show - {n:03d} [1080P].mkv", 60 + n)
        core.mirror_sync_pass()
        self.assertEqual(len(core.load_events()["events"]), core.EVENTS_BACKFILL_CAP)

    def test_backfill_keeps_the_newest_when_capped(self):
        for n in range(core.EVENTS_BACKFILL_CAP + 5):
            # larger n == older file
            self._mirrored("2026.07", "Show", f"[ANi] Show - {n:03d} [1080P].mkv", 60 + n * 60)
        core.mirror_sync_pass()
        eps = {e["params"]["ep"] for e in core.load_events()["events"]}
        self.assertIn(0, eps)                              # newest kept
        self.assertNotIn(core.EVENTS_BACKFILL_CAP + 4, eps)  # oldest dropped

    def test_backfill_is_ordered_oldest_first(self):
        for n in range(5):
            self._mirrored("2026.07", "Show", f"[ANi] Show - {n:02d} [1080P].mkv", 60 + n * 600)
        core.mirror_sync_pass()
        stamps = [e["ts"] for e in core.load_events()["events"]]
        self.assertEqual(stamps, sorted(stamps))


class TestSubscribed(EventLogCase):
    def test_a_new_rule_produces_one_subscribed_event(self):
        entry = {"name": "Show", "season": "2026.07", "mikan_id": 3940,
                 "subgroup": 583, "subgroup_name": "ANi", "bgm_id": 623854,
                 "mustContain": "", "feed_path": "2026.07\\Mikan Project - Show"}
        with mock.patch.object(core, "qb_ensure_rss_folder", lambda p: None), \
             mock.patch.object(core, "qb_post", lambda *a, **k: None), \
             mock.patch.object(core, "rss_feed_paths", lambda: set()):
            core.apply_entries([entry], cookie=None, dry_run=False)
        events = core.load_events()["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "show.subscribed")
        self.assertEqual(events[0]["params"],
                         {"title": "Show", "bgm_id": 623854, "group": "ANi"})

    def test_a_failed_setrule_produces_no_event(self):
        entry = {"name": "Show", "season": "2026.07", "mikan_id": 3940,
                 "subgroup": 583, "subgroup_name": "ANi", "bgm_id": 1,
                 "mustContain": "", "feed_path": "2026.07\\Mikan Project - Show"}

        def boom(path, *a, **k):
            if "setRule" in path:
                raise RuntimeError("qB said no")

        with mock.patch.object(core, "qb_ensure_rss_folder", lambda p: None), \
             mock.patch.object(core, "qb_post", boom), \
             mock.patch.object(core, "rss_feed_paths", lambda: set()):
            core.apply_entries([entry], cookie=None, dry_run=False)
        self.assertEqual(core.load_events()["events"], [])

    def test_dry_run_produces_no_event(self):
        entry = {"name": "Show", "season": "2026.07", "mikan_id": 3940,
                 "subgroup": 583, "subgroup_name": "ANi", "bgm_id": 1,
                 "mustContain": "", "feed_path": "2026.07\\Mikan Project - Show"}
        core.apply_entries([entry], cookie=None, dry_run=True)
        self.assertEqual(core.load_events()["events"], [])


class TestFeedQuery(EventLogCase):
    """The /api/events cursor contract, exercised through the real endpoint."""

    def setUp(self) -> None:
        super().setUp()
        from fastapi.testclient import TestClient
        import webui
        self.client = TestClient(webui.app)

    def test_empty_log(self):
        r = self.client.get("/api/events").json()
        self.assertEqual(r, {"events": [], "seq": 0, "hasMore": False})

    def test_returns_ascending_by_seq(self):
        core.add_events([{"kind": "episode.landed", "params": {"n": n}} for n in range(5)])
        got = self.client.get("/api/events").json()["events"]
        self.assertEqual([e["seq"] for e in got], [1, 2, 3, 4, 5])

    def test_after_seq_skips_what_the_consumer_has(self):
        core.add_events([{"kind": "episode.landed", "params": {}}] * 5)
        got = self.client.get("/api/events?after_seq=3").json()
        self.assertEqual([e["seq"] for e in got["events"]], [4, 5])
        self.assertEqual(got["seq"], 5)
        self.assertFalse(got["hasMore"])

    def test_has_more_is_true_only_when_a_page_is_short(self):
        core.add_events([{"kind": "episode.landed", "params": {}}] * 5)
        page = self.client.get("/api/events?limit=2").json()
        self.assertEqual([e["seq"] for e in page["events"]], [1, 2])
        self.assertTrue(page["hasMore"])
        rest = self.client.get("/api/events?after_seq=2&limit=99").json()
        self.assertEqual([e["seq"] for e in rest["events"]], [3, 4, 5])
        self.assertFalse(rest["hasMore"])

    def test_paging_forward_walks_the_whole_log_exactly_once(self):
        core.add_events([{"kind": "episode.landed", "params": {}}] * 12)
        seen, cursor = [], 0
        for _ in range(10):
            page = self.client.get(f"/api/events?after_seq={cursor}&limit=5").json()
            seen.extend(e["seq"] for e in page["events"])
            if not page["events"]:
                break
            cursor = page["events"][-1]["seq"]
            if not page["hasMore"]:
                break
        self.assertEqual(seen, list(range(1, 13)))

    def test_cursor_beyond_the_head_returns_nothing(self):
        core.add_events([{"kind": "episode.landed", "params": {}}] * 3)
        r = self.client.get("/api/events?after_seq=99").json()
        self.assertEqual(r["events"], [])
        self.assertEqual(r["seq"], 3)

    def test_seq_reported_after_a_reset_lets_a_consumer_notice(self):
        """Deleting events.json restarts numbering; the head seq going backwards
        is the consumer's only signal to drop its cursor and resync."""
        core.add_events([{"kind": "episode.landed", "params": {}}] * 7)
        self.assertEqual(self.client.get("/api/events").json()["seq"], 7)
        self.events_path.unlink()
        core.add_event("episode.landed", {})
        self.assertEqual(self.client.get("/api/events").json()["seq"], 1)

    def test_limit_is_clamped(self):
        core.add_events([{"kind": "episode.landed", "params": {}}] * 3)
        self.assertEqual(len(self.client.get("/api/events?limit=0").json()["events"]), 1)
        self.assertEqual(len(self.client.get("/api/events?limit=99999").json()["events"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
