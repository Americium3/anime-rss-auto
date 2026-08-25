"""A fansub's season-wide numbering, mapped onto a cour bgm split in two.

The case this exists for is real and was silent: ANi ships
"Re：從零開始的異世界生活 第四季 - 13", bgm has the first eleven episodes of that
season under subject 547888 and the rest under a *separate* subject 633836 whose
own numbering restarts at 1 (its whole-series `sort` calls the same episode 79).
So "13" matches nothing anywhere — not the first subject, which stopped at 11,
and not the second under either of its two numberings — and jfhook logged one
skip line and dropped it. Two episodes never reached bangumi that way.

The numbers below are the live ones, so a change to the rule that happens to
still work on invented data has to still work on the case that motivated it.
"""
from __future__ import annotations

import unittest
from unittest import mock

import anime_rss as core

RULE = {"affectedFeeds": ["https://mikanani.me/RSS/Bangumi?bangumiId=3951&subgroupid=583"]}

HEAD = 547888   # 第四季, episodes 1-11 (sort 67-77), aired 2026-04-08 .. 2026-06-17
TAIL = 633836   # 第四季 奪還編, episodes 1-8 (sort 78-85), from 2026-08-12

# bgm_subject_episodes keys by BOTH the per-season number and the whole-series
# sort, which is exactly why neither of them can answer for "13".
HEAD_EPS = {n: 5000 + n for n in range(1, 12)} | {s: 5000 + s - 66 for s in range(67, 78)}
TAIL_EPS = {n: 9000 + n for n in range(1, 9)} | {s: 9000 + s - 77 for s in range(78, 86)}

SPANS = {
    HEAD: {"first": "2026-04-08", "last": "2026-06-17", "count": 11},
    TAIL: {"first": "2026-08-12", "last": "2026-09-30", "count": 8},
}


def _patched(resolved=(TAIL,), eps=None, spans=None):
    """Stub out the three lookups the rule makes, and nothing else."""
    eps = eps or {HEAD: HEAD_EPS, TAIL: TAIL_EPS}
    spans = SPANS if spans is None else spans
    return (
        mock.patch.object(core, "_subjects_resolved_onto", return_value=list(resolved)),
        mock.patch.object(core, "bgm_subject_episodes", side_effect=lambda sid, _c: eps.get(sid, {})),
        mock.patch.object(core, "bgm_episode_span", side_effect=lambda sid, _c: spans.get(sid, {})),
    )


class SplitCourCase(unittest.TestCase):
    def call(self, ep, **kw):
        a, b, c = _patched(**kw)
        with a, b, c:
            return core.split_cour_continuation(RULE, HEAD, ep, {}, {})

    def test_the_two_episodes_that_were_lost(self):
        """ANi 12 and 13 are the first two episodes of the second subject."""
        self.assertEqual(self.call(12), (TAIL, TAIL_EPS[1]))
        self.assertEqual(self.call(13), (TAIL, TAIL_EPS[2]))

    def test_it_keeps_counting_past_the_seam(self):
        self.assertEqual(self.call(19), (TAIL, TAIL_EPS[8]))

    def test_a_number_beyond_both_halves_is_declined(self):
        self.assertIsNone(self.call(40))

    def test_a_whole_series_number_is_taken_as_is(self):
        """Some groups number straight through the whole show rather than the
        season (LoliHouse writes 79 where ANi writes 13). That number is the
        tail subject's own `sort`, so it needs no offset — and it is
        unambiguous, because no such number exists in the earlier subject."""
        self.assertEqual(self.call(79), (TAIL, TAIL_EPS[79]))

    def test_a_number_both_halves_claim_is_declined(self):
        """Episode 5 exists in both subjects. The rule must not answer for it
        even if something calls it directly."""
        self.assertIsNone(self.call(5))

    def test_it_declines_when_the_halves_are_not_consecutive(self):
        """Same feed, but the second subject aired first — so this is not a
        continuation and the offset would be inventing an answer."""
        flipped = {HEAD: SPANS[HEAD], TAIL: {"first": "2026-01-01", "last": "2026-03-01"}}
        self.assertIsNone(self.call(13, spans=flipped))

    def test_it_declines_when_a_span_is_unknown(self):
        """bgm not answering must read as 'cannot tell', never as 'go ahead'."""
        blank = {HEAD: {"first": None, "last": None}, TAIL: SPANS[TAIL]}
        self.assertIsNone(self.call(13, spans=blank))

    def test_it_declines_when_no_sibling_subject_is_known(self):
        self.assertIsNone(self.call(13, resolved=()))

    def test_it_declines_when_the_only_sibling_is_itself(self):
        self.assertIsNone(self.call(13, resolved=(HEAD,)))

    def test_a_rule_with_no_mikan_feed_is_declined(self):
        a, b, c = _patched()
        with a, b, c:
            self.assertIsNone(core.split_cour_continuation({"affectedFeeds": []}, HEAD, 13, {}, {}))
            self.assertIsNone(core.split_cour_continuation(None, HEAD, 13, {}, {}))

    def test_no_episode_number_is_declined(self):
        a, b, c = _patched()
        with a, b, c:
            self.assertIsNone(core.split_cour_continuation(RULE, HEAD, None, {}, {}))


class ResolveTargetCase(unittest.TestCase):
    """The rule has to be reached from resolve_torrent_target, not merely exist."""

    TORRENT = {"name": "[ANi] Re：從零開始的異世界生活 第四季 - 13 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4",
               "save_path": "X:\\Bangumi\\2026.07\\ReZero", "hash": "abc"}

    def test_the_lost_episode_now_resolves(self):
        a, b, c = _patched()
        with a, b, c, \
             mock.patch.object(core, "rule_bgm_id", return_value=HEAD), \
             mock.patch.object(core, "bgm_subject_season", return_value="2026.07"):
            bgm_id, eid, reason = core.resolve_torrent_target(
                self.TORRENT, {"x:/bangumi/2026.07/rezero": RULE}, {}, {}, {}, {})
        self.assertEqual(reason, "ok")
        self.assertEqual(bgm_id, TAIL)
        self.assertEqual(eid, TAIL_EPS[2])

    def test_an_unresolvable_number_still_reports_why(self):
        a, b, c = _patched(resolved=())
        with a, b, c, \
             mock.patch.object(core, "rule_bgm_id", return_value=HEAD), \
             mock.patch.object(core, "bgm_subject_season", return_value="2026.07"):
            bgm_id, eid, reason = core.resolve_torrent_target(
                self.TORRENT, {"x:/bangumi/2026.07/rezero": RULE}, {}, {}, {}, {})
        self.assertIsNone(bgm_id)
        self.assertIn("找不到对应集", reason)


if __name__ == "__main__":
    unittest.main()
