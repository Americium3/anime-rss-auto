#!/usr/bin/env python3
"""Tests: a rule whose mikan entry moved, and a show that asked for raws.

Run:  python test_mikan_move.py       (stdlib unittest, no network, no qB)

Two silent failures, both live, both of which the pipeline reported as success:

  * Re:Zero 4th season 奪還編 (bgm 633836) was bound to mikan 3951 「丧失篇」 — the
    other half of the cour — because 奪還編 had no mikan entry of its own yet. The
    rule built from that guess then vouched for the guess: the resolve fast path
    accepts any mapping a live feed carries, so it never expired and never got
    searched again. mikan opened 4052, episode 14 appeared only there, 3951
    stopped at 11, and the download side went quiet with nothing in any log.

  * 攻壳机动队 THE GHOST IN THE SHELL is subscribed to mikan's raw bucket
    (subgroup 202, 「生肉/不明字幕」) because that is the release the user wants.
    The raw defenses treated every episode of it as a cross-posted accident and
    deleted it, download after download, for seven weeks.

  * Those deletes then raced the mirror. qB unlinks asynchronously, so three of
    the eight files were hardlinked into the Jellyfin library in the same pass
    that deleted them; the source went empty, which is the one state
    mirror_prune_orphan_files refuses to act on, and Jellyfin showed episodes 1,
    3 and 7 twice for good.

The numbers below are the live ones, so a change that happens to work on
invented data still has to work on the cases that motivated it.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
import unittest.mock as mock
from pathlib import Path

import anime_rss as core

HEAD_MIKAN = 3951    # Mikan Project - Re：从零开始的异世界生活 第四季 丧失篇, eps 1-11
TAIL_MIKAN = 4052    # Mikan Project - Re：从零开始的异世界生活 第四季 夺还篇, eps 12-14
TAIL_BGM = 633836
HEAD_BGM = 547888
ANI = 583

GITS_MIKAN = 4029
RAW = 202            # mikan's global 「生肉/不明字幕」 bucket

SHOW = {"bgm_id": TAIL_BGM, "name": "Re:ゼロから始める異世界生活 4th season 奪還編",
        "name_cn": "Re：从零开始的异世界生活 第四季 夺还篇", "date": "2026-08-12"}


def _rule(mikan_id: int, subgroup: int, **kw) -> dict:
    rdef = {
        "affectedFeeds": [core.feed_url(mikan_id, subgroup)],
        "savePath": r"X:\Bangumi\2026.07\Re：从零开始的异世界生活 第四季 夺还篇",
        "previouslyMatchedEpisodes": ["[ANi]  Re：从零开始的异世界生活 第四季 - 12"],
        "torrentParams": {"tags": ["2026.07"]},
    }
    rdef.update(kw)
    return rdef


class FakeCtx:
    """Only the four seams reconcile_mikan_moves actually touches."""

    def __init__(self, rules, resolve_disk, watching, fresh, rule_bgmid):
        self._rules, self.resolve_disk = rules, resolve_disk
        self._watching, self._fresh = watching, fresh
        self.rule_bgmid = rule_bgmid   # mikan id -> the bgm subject its page names
        self.resolved_with_force = []
        self.invalidated = False

    def rules(self):
        return self._rules

    def collection(self, _kind):
        return self._watching

    def resolve(self, show, *, force_search=False):
        self.resolved_with_force.append((show["bgm_id"], force_search))
        return self._fresh

    def invalidate_rules(self):
        self.invalidated = True


def _ctx(*, confidence="low (name match, bgm id NOT confirmed)",
         rules=None, watching=None, fresh=None, also_resolved=None):
    disk = {str(TAIL_BGM): {"mikan_id": HEAD_MIKAN, "confidence": confidence}}
    disk.update(also_resolved or {})
    return FakeCtx(
        rules if rules is not None else {"Re：从零开始的异世界生活 第四季 夺还篇":
                                         _rule(HEAD_MIKAN, ANI)},
        disk,
        watching if watching is not None else [SHOW],
        fresh if fresh is not None else {
            "mikan_id": TAIL_MIKAN, "available_subgroups": [370, 583, 615],
            "mikan_title": "Mikan Project - Re：从零开始的异世界生活 第四季 夺还篇"},
        {HEAD_MIKAN: HEAD_BGM},
    )


class MikanMoveCase(unittest.TestCase):
    def setUp(self):
        self.calls: list[tuple[str, dict]] = []

    def run_pass(self, ctx, **kw):
        def qb_post(path, payload):
            self.calls.append((path, payload))
            return {}

        with mock.patch.object(core, "qb_post", qb_post), \
             mock.patch.object(core, "rss_feed_paths",
                               return_value={core.feed_url(HEAD_MIKAN, ANI):
                                             "2026.07\\Mikan Project - 丧失篇"}), \
             mock.patch.object(core, "add_event"):
            return core.reconcile_mikan_moves(ctx, **kw)

    def rule_written(self) -> dict:
        for path, payload in self.calls:
            if path.endswith("/setRule"):
                return json.loads(payload["ruleDef"])
        self.fail("no setRule call")

    def test_the_episode_that_was_lost(self):
        """The rule follows 奪還編 to 4052, which is where episode 14 is."""
        ctx = _ctx()
        self.assertEqual(self.run_pass(ctx), 1)
        self.assertEqual(self.rule_written()["affectedFeeds"],
                         [core.feed_url(TAIL_MIKAN, ANI)])

    def test_the_fansub_and_the_folder_do_not_move_with_it(self):
        ctx = _ctx()
        self.run_pass(ctx)
        moved = self.rule_written()
        self.assertTrue(all(core.rule_subgroup_id({"affectedFeeds": [f]}) == ANI
                            for f in moved["affectedFeeds"]))
        self.assertEqual(moved["savePath"], _rule(HEAD_MIKAN, ANI)["savePath"])

    def test_downloaded_episodes_are_not_fetched_again(self):
        """previouslyMatchedEpisodes survives: 12 and 13 are already on disk."""
        ctx = _ctx()
        self.run_pass(ctx)
        self.assertEqual(self.rule_written()["previouslyMatchedEpisodes"],
                         _rule(HEAD_MIKAN, ANI)["previouslyMatchedEpisodes"])

    def test_the_new_feed_is_added_before_the_rule_points_at_it(self):
        ctx = _ctx()
        self.run_pass(ctx)
        order = [p for p, _ in self.calls]
        self.assertLess(order.index("/api/v2/rss/addFeed"),
                        order.index("/api/v2/rss/setRule"))

    def test_a_rule_is_never_stranded_on_a_feed_that_would_not_add(self):
        ctx = _ctx()

        def qb_post(path, payload):
            self.calls.append((path, payload))
            if path.endswith("/addFeed"):
                raise RuntimeError("mikan down")
            return {}

        with mock.patch.object(core, "qb_post", qb_post), \
             mock.patch.object(core, "rss_feed_paths", return_value={}), \
             mock.patch.object(core, "add_event"):
            self.assertEqual(core.reconcile_mikan_moves(ctx), 0)
        self.assertNotIn("/api/v2/rss/setRule", [p for p, _ in self.calls])

    def test_the_abandoned_feed_is_dropped(self):
        ctx = _ctx()
        self.run_pass(ctx)
        removed = [p for path, p in self.calls if path.endswith("/removeItem")]
        self.assertEqual(removed, [{"path": "2026.07\\Mikan Project - 丧失篇"}])

    def test_a_feed_another_rule_still_uses_is_kept(self):
        rules = {"Re：从零开始的异世界生活 第四季 夺还篇": _rule(HEAD_MIKAN, ANI),
                 "Re：从零开始的异世界生活 第四季 丧失篇": _rule(HEAD_MIKAN, ANI)}
        self.run_pass(_ctx(rules=rules))
        self.assertNotIn("/api/v2/rss/removeItem", [p for p, _ in self.calls])

    def test_a_confirmed_mapping_is_never_re_searched(self):
        """A mikan page that names the subject is authoritative — and free."""
        ctx = _ctx(confidence="high")
        self.assertEqual(self.run_pass(ctx), 0)
        self.assertEqual(ctx.resolved_with_force, [])

    def test_a_feed_two_watched_halves_claim_is_left_alone(self):
        """Whose rule is it? Moving it would unsubscribe the other half."""
        ctx = _ctx(
            watching=[SHOW, {"bgm_id": HEAD_BGM, "name": "", "name_cn": "丧失篇",
                             "date": "2026-04-08"}],
            also_resolved={str(HEAD_BGM): {"mikan_id": HEAD_MIKAN, "confidence": "high"}},
        )
        self.assertEqual(self.run_pass(ctx), 0)

    def test_it_declines_when_the_new_entry_lacks_the_current_fansub(self):
        ctx = _ctx(fresh={"mikan_id": TAIL_MIKAN, "available_subgroups": [370],
                          "mikan_title": "x"})
        self.assertEqual(self.run_pass(ctx), 0)

    def test_it_declines_when_the_search_lands_right_back(self):
        ctx = _ctx(fresh={"mikan_id": HEAD_MIKAN, "available_subgroups": [ANI],
                          "mikan_title": "x"})
        self.assertEqual(self.run_pass(ctx), 0)

    def test_a_show_with_no_rule_is_not_searched(self):
        self.assertEqual(self.run_pass(_ctx(rules={})), 0)

    def test_dry_run_writes_nothing(self):
        ctx = _ctx()
        self.assertEqual(self.run_pass(ctx, dry_run=True), 1)
        self.assertEqual(self.calls, [])


class AfterTheMoveCase(unittest.TestCase):
    """Once the rule sits on 4052, ANi's numbering must still reach bangumi.

    This is the shape the move leaves behind, and the only shape a show resolved
    after mikan split the cour ever has: the rule's one feed names 奪還編, so
    head_id IS the later half and no sibling can supply the offset. 丧失篇 ran 11
    episodes (sort 67-77), 奪還編 runs 8 (sort 78-85), and ANi calls 奪還編's third
    「第四季 - 14」 — a number in neither subject under either of its numberings.
    """

    HEAD_EPS = {n: 5000 + n for n in range(1, 12)} | {s: 5000 + s - 66 for s in range(67, 78)}
    TAIL_EPS = {n: 9000 + n for n in range(1, 9)} | {s: 9000 + s - 77 for s in range(78, 86)}
    SPANS = {HEAD_BGM: {"first": "2026-04-08", "last": "2026-06-17", "count": 11},
             TAIL_BGM: {"first": "2026-08-12", "last": "2026-09-30", "count": 8}}
    RULE = {"affectedFeeds": [core.feed_url(TAIL_MIKAN, ANI)]}

    def resolve(self, ep, *, prequel=HEAD_BGM, spans=None, onto=None):
        eps = {HEAD_BGM: self.HEAD_EPS, TAIL_BGM: self.TAIL_EPS}
        spans = self.SPANS if spans is None else spans
        # The resolve cache maps 奪還編 onto its own entry now — itself, and so
        # useless as a sibling. That is what forces the 前传 route.
        onto = {TAIL_MIKAN: [TAIL_BGM]} if onto is None else onto
        with mock.patch.object(core, "_subjects_resolved_onto",
                               side_effect=lambda mid: onto.get(mid, [])),              mock.patch.object(core, "bgm_subject_prequel", return_value=prequel),              mock.patch.object(core, "bgm_subject_episodes",
                               side_effect=lambda sid, _c: eps.get(sid, {})),              mock.patch.object(core, "bgm_episode_span",
                               side_effect=lambda sid, _c: spans.get(sid, {})):
            return core.split_cour_continuation(self.RULE, TAIL_BGM, ep, {}, {})

    def test_the_episode_that_started_all_this(self):
        """ANi 「第四季 - 14」 is 奪還編 episode 3."""
        self.assertEqual(self.resolve(14), (TAIL_BGM, self.TAIL_EPS[3]))

    def test_it_keeps_counting_past_the_seam(self):
        self.assertEqual(self.resolve(12), (TAIL_BGM, self.TAIL_EPS[1]))
        self.assertEqual(self.resolve(19), (TAIL_BGM, self.TAIL_EPS[8]))

    def test_a_number_this_subject_already_owns_is_left_alone(self):
        """3 is 奪還編's own episode 3; resolve_torrent_target never gets here."""
        self.assertIsNone(self.resolve(3))

    def test_a_number_past_both_halves_is_declined(self):
        self.assertIsNone(self.resolve(20))

    def test_it_declines_without_a_prequel(self):
        self.assertIsNone(self.resolve(14, prequel=None))

    def test_it_declines_when_the_halves_are_not_consecutive(self):
        """A prequel that ran after this one is a sequel mislabelled, not an offset."""
        spans = {**self.SPANS, HEAD_BGM: {"first": "2027-01-01", "last": "2027-03-01"}}
        self.assertIsNone(self.resolve(14, spans=spans))

    def test_it_declines_when_a_span_is_unknown(self):
        self.assertIsNone(self.resolve(14, spans={TAIL_BGM: self.SPANS[TAIL_BGM]}))

    def test_a_sibling_on_the_feed_still_wins(self):
        """The original path is untouched: 3951 carrying both halves still works."""
        eps = {HEAD_BGM: self.HEAD_EPS, TAIL_BGM: self.TAIL_EPS}
        rule = {"affectedFeeds": [core.feed_url(HEAD_MIKAN, ANI)]}
        with mock.patch.object(core, "_subjects_resolved_onto", return_value=[TAIL_BGM]),              mock.patch.object(core, "bgm_subject_prequel") as prequel,              mock.patch.object(core, "bgm_subject_episodes",
                               side_effect=lambda sid, _c: eps.get(sid, {})),              mock.patch.object(core, "bgm_episode_span",
                               side_effect=lambda sid, _c: self.SPANS.get(sid, {})):
            self.assertEqual(core.split_cour_continuation(rule, HEAD_BGM, 14, {}, {}),
                             (TAIL_BGM, self.TAIL_EPS[3]))
        prequel.assert_not_called()


class GuessCase(unittest.TestCase):
    def test_only_the_unconfirmed_tier_counts_as_a_guess(self):
        self.assertTrue(core._is_guess("low (name match, bgm id NOT confirmed)"))
        for confident in ("high", "override", "rule", "", None):
            self.assertFalse(core._is_guess(confident), confident)


class SelfCertifyingGuessCase(unittest.TestCase):
    """The fast path must not accept a feed the mapping itself put there."""

    def ctx(self, confidence, *, age_sec):
        ctx = core.SyncContext.__new__(core.SyncContext)
        ctx.user, ctx.dirty = "942942", False
        ctx.resolve_memo, ctx.collections = {}, {}
        ctx.mikan_bgm_ids, ctx.rule_bgmid, ctx.span = {}, {}, {}
        ctx._rules = {"r": _rule(HEAD_MIKAN, ANI)}
        ctx._feed_paths = ctx._feed_by_mikan = None
        ctx.resolve_disk = {str(TAIL_BGM): {
            "mikan_id": HEAD_MIKAN, "available_subgroups": [ANI],
            "mikan_title": "丧失篇", "confidence": confidence,
            "resolved_at": time.time() - age_sec}}
        return ctx

    def resolve(self, ctx):
        fresh = dict(SHOW, mikan_id=TAIL_MIKAN, mikan_title="夺还篇", subgroup=ANI,
                     subgroup_name="ANi", available_subgroups=[ANI], confidence="high")
        with mock.patch.object(core, "resolve_show", return_value=fresh) as search, \
             mock.patch.object(core, "load_mikan_overrides", return_value={}), \
             mock.patch.object(core, "load_grace", return_value={}):
            return ctx.resolve(SHOW), search

    def test_a_stale_guess_is_searched_again_even_though_a_rule_carries_it(self):
        ctx = self.ctx("low (name match, bgm id NOT confirmed)", age_sec=core.RESOLVE_TTL + 1)
        got, search = self.resolve(ctx)
        search.assert_called_once()
        self.assertEqual(got["mikan_id"], TAIL_MIKAN)

    def test_a_fresh_guess_still_rides_the_ttl(self):
        """Re-searching is for the 24h tick, not for every pass."""
        ctx = self.ctx("low (name match, bgm id NOT confirmed)", age_sec=60)
        got, search = self.resolve(ctx)
        search.assert_not_called()
        self.assertEqual(got["mikan_id"], HEAD_MIKAN)

    def test_a_confirmed_mapping_stays_free_forever(self):
        ctx = self.ctx("high", age_sec=core.RESOLVE_TTL * 10)
        got, search = self.resolve(ctx)
        search.assert_not_called()
        self.assertEqual(got["mikan_id"], HEAD_MIKAN)


class RawBucketCase(unittest.TestCase):
    """Subscribing to 「生肉/不明字幕」 is a request, not an accident."""

    def test_a_rule_on_the_raw_bucket_is_recognised(self):
        self.assertTrue(core.rule_wants_raw(_rule(GITS_MIKAN, RAW)))
        self.assertFalse(core.rule_wants_raw(_rule(GITS_MIKAN, 370)))
        self.assertFalse(core.rule_wants_raw({"affectedFeeds": []}))
        self.assertFalse(core.rule_wants_raw(None))

    def test_the_asked_for_release_is_not_deleted(self):
        save = r"X:\Bangumi\2026.07\攻壳机动队 THE GHOST IN THE SHELL"
        torrent = {
            "hash": "abc", "save_path": save, "content_path": save + r"\x.mkv",
            "name": "THE.GHOST.IN.THE.SHELL.S01E01.1080p.AMZN.WEB-DL.DUAL.DDP2.0.H.264-VARYG.mkv",
        }
        self.assertTrue(core._hard_reject(torrent["name"]))  # it IS a raw
        rules = {"攻壳机动队 THE GHOST IN THE SHELL":
                 dict(_rule(GITS_MIKAN, RAW), savePath=save)}
        with mock.patch.object(core, "qb_get_json", return_value=[torrent]), \
             mock.patch.object(core, "existing_rules", return_value=rules), \
             mock.patch.object(core, "qb_post") as post:
            self.assertEqual(core.reject_hard_variants(), 0)
        post.assert_not_called()

    def test_a_raw_cross_posted_into_a_subbed_feed_still_goes(self):
        save = r"X:\Bangumi\2026.07\Some Show"
        torrent = {"hash": "abc", "save_path": save, "content_path": save + r"\x.mkv",
                   "name": "Some.Show.S01E01.1080p.NF.WEB-DL.DDP5.1.H.264-GROUP.mkv"}
        rules = {"Some Show": dict(_rule(1234, 370), savePath=save)}
        with mock.patch.object(core, "qb_get_json", return_value=[torrent]), \
             mock.patch.object(core, "existing_rules", return_value=rules), \
             mock.patch.object(core, "mirror_unlink", return_value=0), \
             mock.patch.object(core, "qb_post") as post:
            self.assertEqual(core.reject_hard_variants(), 1)
        post.assert_called_once()

    def test_the_feed_filter_does_not_demand_subtitles_of_a_raw_group(self):
        entry = {"subgroup": RAW}
        self.assertEqual(
            core.GROUP_FILTER.get(entry["subgroup"])
            or ("" if entry["subgroup"] in core.RAW_SUBGROUP_IDS else core.CJK_SUB_REQUIRED),
            "")

    def test_the_whitelist_backfill_skips_a_raw_rule(self):
        rules = {"攻壳机动队 THE GHOST IN THE SHELL": dict(
            _rule(GITS_MIKAN, RAW),
            savePath=r"X:\Bangumi\2026.07\攻壳机动队 THE GHOST IN THE SHELL",
            mustContain="简|繁")}
        with mock.patch.object(core, "existing_rules", return_value=rules), \
             mock.patch.object(core, "qb_post") as post:
            self.assertEqual(core.reconcile_rule_cjk_whitelist(), 0)
        post.assert_not_called()


class MirrorRaceCase(unittest.TestCase):
    """A file the pipeline deleted must not survive as the library's only copy."""

    RAW_FILE = ("THE.GHOST.IN.THE.SHELL.S01E01.1080p.AMZN.WEB-DL"
                ".DUAL.DDP2.0.H.264-VARYG.mkv")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.src = root / "Bangumi" / "2026.07" / "Show"
        self.dst = root / "BangumiJF" / "2026.07" / "Show" / "Season 01"
        self.src.mkdir(parents=True)
        self.dst.mkdir(parents=True)
        self.patches = [
            mock.patch.object(core, "BANGUMI_LIBRARY", str(root / "Bangumi")),
            mock.patch.object(core, "JELLYFIN_MIRROR", str(root / "BangumiJF")),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(self.tmp.cleanup)
        for p in self.patches:
            self.addCleanup(p.stop)
        core._HARD_REJECTED_NAMES.clear()
        self.addCleanup(core._HARD_REJECTED_NAMES.clear)

    def test_the_mirror_copy_goes_with_the_source(self):
        (self.dst / self.RAW_FILE).write_bytes(b"x")
        self.assertEqual(core.mirror_unlink(str(self.src), {self.RAW_FILE}), 1)
        self.assertFalse((self.dst / self.RAW_FILE).exists())

    def test_it_only_unlinks_what_it_was_given(self):
        (self.dst / self.RAW_FILE).write_bytes(b"x")
        (self.dst / "keep.mkv").write_bytes(b"x")
        core.mirror_unlink(str(self.src), {self.RAW_FILE})
        self.assertTrue((self.dst / "keep.mkv").exists())

    def test_the_source_is_never_touched(self):
        (self.src / self.RAW_FILE).write_bytes(b"x")
        core.mirror_unlink(str(self.src), {self.RAW_FILE})
        self.assertTrue((self.src / self.RAW_FILE).exists())

    def test_a_torrent_outside_the_library_has_no_mirror_twin(self):
        self.assertEqual(core.mirror_unlink(r"D:\elsewhere", {self.RAW_FILE}), 0)

    def test_a_file_deleted_this_pass_is_not_mirrored_on_the_way_out(self):
        """The delete is in flight; the file is still on disk. Do not link it."""
        (self.src / self.RAW_FILE).write_bytes(b"x")
        core._HARD_REJECTED_NAMES.add(self.RAW_FILE)
        with mock.patch.object(core, "_events_backfill_first_run"), \
             mock.patch.object(core, "add_event"), \
             mock.patch.object(core, "_jellyfin_refresh"), \
             mock.patch.object(core, "intro_skipper_analyze_async"):
            self.assertEqual(core.mirror_sync_pass(), 0)
        self.assertFalse((self.dst / self.RAW_FILE).exists())

    def test_an_ordinary_episode_still_gets_mirrored(self):
        (self.src / "[ANi] Show - 01 [1080P][Baha][CHT].mp4").write_bytes(b"x")
        with mock.patch.object(core, "_events_backfill_first_run"), \
             mock.patch.object(core, "add_event"), \
             mock.patch.object(core, "_jellyfin_refresh"), \
             mock.patch.object(core, "intro_skipper_analyze_async"):
            self.assertEqual(core.mirror_sync_pass(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
