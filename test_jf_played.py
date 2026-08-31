"""The sweep that backstops jfhook's at-most-once delivery.

jfhook acts on a webhook once and Jellyfin never sends it again, so an episode
watched while the hook could not resolve it is lost with nothing left to retry —
and mark_watched_pass cannot cover for it, because it waits on a seeding ->
stopped transition that a failed hook never produced. These exercise the pull
half against stubs: what it acts on, what it refuses to act on, and — the point
of the whole thing — which failures it is still willing to come back for.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import anime_rss as core

RULE = {"savePath": r"X:\Bangumi\2026.07\A Show"}
TORRENT = {
    "hash": "h1", "name": "[ANi] A Show - 03 [1080P][Baha].mp4",
    "save_path": r"X:\Bangumi\2026.07\A Show", "state": "stalledUP", "progress": 1,
    "content_path": r"X:\Bangumi\2026.07\A Show\[ANi] A Show - 03 [1080P][Baha].mp4",
}
PLAYED = {"Id": "item-1",
          "Path": r"X:\BangumiJF\2026.07\A Show\Season 01\[ANi] A Show - 03 [1080P][Baha].mp4"}
ANCIENT = {"Id": "item-anc",
           "Path": r"X:\BangumiJF\Ancient\Some Old Show\Season 01\ep01.mkv"}


class StubCtx:
    """The slice of SyncContext the sweep passes down to resolve_torrent_target."""

    def __init__(self, rules: dict):
        self._rules = rules
        self.rule_bgmid: dict = {}
        self.season: dict = {}
        self.eps: dict = {}
        self.span: dict = {}

    def rules(self):
        return self._rules


class SweepCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / "jf_played_seen.json"
        self.marked: list[int] = []
        self.stopped: list[str] = []
        self._patches = [
            mock.patch.object(core, "JF_PLAYED_SEEN_PATH", self.ledger),
            mock.patch.object(core, "JELLYFIN_API_KEY", "k"),
            mock.patch.object(core, "_jf_user_id", lambda: "uid"),
            mock.patch.object(core, "qb_get_json", lambda _p: [TORRENT]),
            mock.patch.object(core, "bgm_mark_episode_watched",
                              lambda _t, eid: self.marked.append(eid)),
            mock.patch.object(core, "qb_stop", lambda h: self.stopped.append(h)),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def sweep(self, items, *, resolve, dry_run=False) -> int:
        with mock.patch.object(core, "_jf_req", lambda *_a, **_k: (200, {"Items": items})), \
             mock.patch.object(core, "resolve_torrent_target", lambda *_a, **_k: resolve):
            return core.jellyfin_played_reconcile_pass(
                "tok", ctx=StubCtx({"A Show": RULE}), dry_run=dry_run)

    def seed_ledger(self, *ids: str) -> None:
        self.ledger.write_text(json.dumps({i: "" for i in ids}), encoding="utf-8")

    def read_ledger(self) -> dict:
        return json.loads(self.ledger.read_text(encoding="utf-8"))


class BaselineTest(SweepCase):
    """A first run records the backlog instead of writing all of it to bgm."""

    def test_first_run_marks_nothing_and_records_everything(self):
        n = self.sweep([PLAYED, ANCIENT], resolve=(1, 99, "ok"))
        self.assertEqual(n, 0)
        self.assertEqual(self.marked, [])
        self.assertEqual(set(self.read_ledger()), {"item-1", "item-anc"})

    def test_baseline_leaves_no_file_on_dry_run(self):
        self.sweep([PLAYED], resolve=(1, 99, "ok"), dry_run=True)
        self.assertFalse(self.ledger.exists())


class BackfillTest(SweepCase):
    """The steady state: something played that bgm has no record of."""

    def test_marks_and_records(self):
        self.seed_ledger("other")
        n = self.sweep([PLAYED], resolve=(1, 99, "ok"))
        self.assertEqual((n, self.marked), (1, [99]))
        self.assertIn("item-1", self.read_ledger())

    def test_stops_a_torrent_the_failed_hook_left_seeding(self):
        # The hook stops seeding as it marks; a backfill means it never did, and
        # a torrent left seeding is also why mark_watched_pass never fires here.
        self.seed_ledger("other")
        self.sweep([PLAYED], resolve=(1, 99, "ok"))
        self.assertEqual(self.stopped, ["h1"])

    def test_leaves_an_already_stopped_torrent_alone(self):
        self.seed_ledger("other")
        with mock.patch.object(core, "qb_get_json",
                               lambda _p: [dict(TORRENT, state="stoppedUP")]):
            self.sweep([PLAYED], resolve=(1, 99, "ok"))
        self.assertEqual(self.stopped, [])

    def test_a_recorded_item_is_never_marked_twice(self):
        self.seed_ledger("item-1")
        self.assertEqual(self.sweep([PLAYED], resolve=(1, 99, "ok")), 0)
        self.assertEqual(self.marked, [])

    def test_dry_run_writes_neither_bgm_nor_ledger(self):
        self.seed_ledger("other")
        n = self.sweep([PLAYED], resolve=(1, 99, "ok"), dry_run=True)
        self.assertEqual((n, self.marked), (1, []))
        self.assertEqual(set(self.read_ledger()), {"other"})


class RefusalTest(SweepCase):
    """What the sweep declines to touch, and whether it will come back for it."""

    def test_ancient_is_never_touched(self):
        self.seed_ledger("other")
        self.sweep([ANCIENT], resolve=(1, 99, "ok"))
        self.assertEqual(self.marked, [])
        self.assertIn("item-anc", self.read_ledger())   # settled, not retried

    def test_an_old_cour_is_settled_not_retried(self):
        self.seed_ledger("other")
        self.sweep([PLAYED], resolve=(None, None, "旧番 2025.10 < 2026.04（手动管理）"))
        self.assertEqual(self.marked, [])
        self.assertIn("item-1", self.read_ledger())

    def test_a_torrent_that_is_gone_is_settled(self):
        self.seed_ledger("other")
        with mock.patch.object(core, "qb_get_json", lambda _p: []):
            self.sweep([PLAYED], resolve=(1, 99, "ok"))
        self.assertEqual(self.marked, [])
        self.assertIn("item-1", self.read_ledger())

    def test_bgm_not_answering_is_retried(self):
        self.seed_ledger("other")
        self.sweep([PLAYED], resolve=(None, None, "bgm 未响应，季度无法判定（本轮跳过，下轮重试）"))
        self.assertNotIn("item-1", self.read_ledger())

    def test_an_unrecognised_episode_number_is_retried(self):
        """The whole reason this pass exists: the split-cour bug was fixed hours
        after the episode was watched, and a ledger entry written on the broken
        answer would have made that fix unable to reach it."""
        self.seed_ledger("other")
        self.sweep([PLAYED], resolve=(None, None,
                                      "集数 14 在 subject 633836 找不到对应集(ep/sort 都无)"))
        self.assertNotIn("item-1", self.read_ledger())

    def test_a_failed_bgm_write_is_retried(self):
        self.seed_ledger("other")

        def boom(_t, _eid):
            raise RuntimeError("bgm 500")

        with mock.patch.object(core, "bgm_mark_episode_watched", boom):
            n = self.sweep([PLAYED], resolve=(1, 99, "ok"))
        self.assertEqual(n, 0)
        self.assertNotIn("item-1", self.read_ledger())

    def test_no_bgm_token_does_nothing_at_all(self):
        self.seed_ledger("other")
        with mock.patch.object(core, "_jf_req", lambda *_a, **_k: (200, {"Items": [PLAYED]})):
            n = core.jellyfin_played_reconcile_pass(None, ctx=StubCtx({"A Show": RULE}))
        self.assertEqual((n, self.marked), (0, []))

    def test_jellyfin_being_down_is_not_fatal(self):
        self.seed_ledger("other")

        def boom(*_a, **_k):
            raise OSError("connection refused")

        with mock.patch.object(core, "_jf_req", boom):
            n = core.jellyfin_played_reconcile_pass("tok", ctx=StubCtx({"A Show": RULE}))
        self.assertEqual(n, 0)


class LedgerTrimTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = Path(self._tmp.name) / "jf_played_seen.json"
        self._p = mock.patch.object(core, "JF_PLAYED_SEEN_PATH", self.ledger)
        self._p.start()

    def tearDown(self) -> None:
        self._p.stop()
        self._tmp.cleanup()

    def test_keeps_the_newest_entries(self):
        keep = core.JF_PLAYED_LEDGER_KEEP
        core.save_jf_played_seen({str(i): "" for i in range(keep + 50)})
        out = core.load_jf_played_seen()
        self.assertEqual(len(out), keep)
        self.assertIn(str(keep + 49), out)     # newest survives
        self.assertNotIn("0", out)             # oldest dropped

    def test_a_corrupt_ledger_reads_as_empty(self):
        self.ledger.write_text("{not json", encoding="utf-8")
        self.assertEqual(core.load_jf_played_seen(), {})


if __name__ == "__main__":
    unittest.main()
