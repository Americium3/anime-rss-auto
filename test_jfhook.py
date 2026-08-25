"""jfhook in both directions: watched -> stop+mark, unwatched -> resume+unmark.

The undo direction cannot be exercised against the real setup — it would resume
the user's actual torrents and rewrite their actual bgm history — so the two
payloads below are the ones captured off a live Jellyfin 10.11.11 with Webhook
21.0.0.0 (an episode ticked and unticked, template rendering every field), and
everything downstream of them is driven against stubs.
"""
from __future__ import annotations

import unittest
from unittest import mock

import anime_rss as core

# Captured verbatim. Note Played is a capitalised *string* and PlayedToCompletion
# arrives as "" rather than going missing, which is what _falsy has to survive.
WATCHED = {
    "NotificationType": "UserDataSaved", "ItemType": "Episode",
    "ItemId": "2c0d61a7-93f1-a789-57bc-cedd56eea531", "SeriesName": "Future Diary",
    "EpisodeNumber": "1", "PlayedToCompletion": "", "Played": "True",
    "SaveReason": "TogglePlayed",
}
UNWATCHED = dict(WATCHED, Played="False")
# Also captured verbatim, from watch.log: what an episode watched to the end in
# Findroid actually delivers. The hook used to read SaveReason and drop this.
FINISHED = dict(WATCHED, SaveReason="PlaybackFinished", SeriesName="Grand Blue Dreaming")
# The saves that ride along once Jellyfin has flipped the flag mid-session.
PROGRESS_PLAYED = dict(WATCHED, SaveReason="PlaybackProgress")
PROGRESS_UNPLAYED = dict(WATCHED, SaveReason="PlaybackProgress", Played="False")


class EventClassificationCase(unittest.TestCase):
    """Which payloads mean 'watched', which mean 'undo', and which mean neither."""

    def test_the_captured_tick_reads_as_watched(self):
        self.assertTrue(core._jf_is_watched_event(WATCHED))
        self.assertFalse(core._jf_is_unwatched_event(WATCHED))

    def test_the_captured_untick_reads_as_undo(self):
        self.assertTrue(core._jf_is_unwatched_event(UNWATCHED))
        self.assertFalse(core._jf_is_watched_event(UNWATCHED))

    def test_case_does_not_matter(self):
        for v in ("False", "false", "FALSE"):
            self.assertTrue(core._jf_is_unwatched_event(dict(WATCHED, Played=v)), v)
        for v in ("True", "true", "TRUE"):
            self.assertTrue(core._jf_is_watched_event(dict(WATCHED, Played=v)), v)

    def test_an_empty_played_is_not_an_undo(self):
        """Absent-rendered-as-empty must not read as 'the user unticked it'."""
        self.assertFalse(core._jf_is_unwatched_event(dict(WATCHED, Played="")))
        self.assertFalse(core._jf_is_unwatched_event(
            {k: v for k, v in WATCHED.items() if k != "Played"}))

    def test_a_different_save_reason_is_ignored(self):
        """UserDataSaved also fires for playback position, favourites, etc."""
        for reason in ("PlaybackProgress", "UpdateUserRating", ""):
            p = dict(UNWATCHED, SaveReason=reason)
            self.assertFalse(core._jf_is_unwatched_event(p), reason)

    def test_an_incomplete_playback_stop_is_not_an_undo(self):
        """Stopping halfway means 'I stopped', not 'undo what you recorded'."""
        p = {"NotificationType": "PlaybackStop", "ItemType": "Episode",
             "PlayedToCompletion": "False", "Played": "False", "SaveReason": ""}
        self.assertFalse(core._jf_is_unwatched_event(p))
        self.assertFalse(core._jf_is_watched_event(p))

    def test_a_completed_playback_stop_is_still_watched(self):
        p = {"NotificationType": "PlaybackStop", "ItemType": "Episode",
             "PlayedToCompletion": "True"}
        self.assertTrue(core._jf_is_watched_event(p))
        self.assertFalse(core._jf_is_unwatched_event(p))

    def test_watching_an_episode_to_the_end_reads_as_watched(self):
        """The regression: PlaybackFinished + Played=True is the ordinary case.

        Recognising only TogglePlayed meant the hook worked when the checkbox
        was ticked by hand and silently did nothing when an episode was simply
        watched, which is how eight episodes went missing over two days.
        """
        self.assertTrue(core._jf_is_watched_event(FINISHED))
        self.assertFalse(core._jf_is_unwatched_event(FINISHED))

    def test_the_played_flag_is_believed_whatever_the_save_reason(self):
        for reason in ("PlaybackFinished", "PlaybackProgress", "TogglePlayed", ""):
            self.assertTrue(core._jf_is_watched_event(dict(WATCHED, SaveReason=reason)), reason)

    def test_a_mid_play_save_is_still_not_watched(self):
        """Played=False during playback stays neutral in both directions."""
        self.assertFalse(core._jf_is_watched_event(PROGRESS_UNPLAYED))
        self.assertFalse(core._jf_is_unwatched_event(PROGRESS_UNPLAYED))

    def test_only_a_deliberate_untick_undoes_anything(self):
        """Widening the watched side must not widen the undo side with it."""
        for reason in ("PlaybackProgress", "PlaybackFinished", "PlaybackStart", ""):
            self.assertFalse(core._jf_is_unwatched_event(dict(UNWATCHED, SaveReason=reason)), reason)

    def test_no_payload_is_ever_both(self):
        for played in ("True", "False", "", "yes", "no", "1", "0", "garbage"):
            p = dict(WATCHED, Played=played)
            self.assertFalse(core._jf_is_watched_event(p)
                             and core._jf_is_unwatched_event(p), played)


class QbStartCase(unittest.TestCase):
    """qb_start mirrors qb_stop's 5.x-then-4.x endpoint fallback."""

    def drive(self, failing: set[str]):
        seen = []

        def fake_post(path, data):
            seen.append(path)
            if path in failing:
                raise RuntimeError("no such endpoint")

        with mock.patch.object(core, "qb_post", side_effect=fake_post):
            core.qb_start("abc")
        return seen

    def test_prefers_the_modern_endpoint(self):
        self.assertEqual(self.drive(set()), ["/api/v2/torrents/start"])

    def test_falls_back_to_resume_on_older_qbittorrent(self):
        self.assertEqual(self.drive({"/api/v2/torrents/start"}),
                         ["/api/v2/torrents/start", "/api/v2/torrents/resume"])

    def test_raises_only_when_both_fail(self):
        with mock.patch.object(core, "qb_post", side_effect=RuntimeError("down")):
            with self.assertRaises(RuntimeError):
                core.qb_start("abc")


class BgmUnmarkCase(unittest.TestCase):
    def test_unmark_puts_type_zero(self):
        with mock.patch.object(core, "_bgm_put_episode_type") as put:
            core.bgm_unmark_episode_watched("tok", 42)
        put.assert_called_once_with("tok", 42, 0)

    def test_mark_still_puts_type_two(self):
        with mock.patch.object(core, "_bgm_put_episode_type") as put:
            core.bgm_mark_episode_watched("tok", 42)
        put.assert_called_once_with("tok", 42, 2)


class HandlerRoutingCase(unittest.TestCase):
    """The handler picks a direction and inherits every existing guard."""

    def setUp(self) -> None:
        self.torrent = {"hash": "H", "name": "[ANi] Show - 03 [1080P].mp4",
                        "content_path": r"X:\Bangumi\2026.07\Show\ep03.mp4"}
        self.calls: list[tuple] = []
        for name in ("qb_stop", "qb_start"):
            p = mock.patch.object(core, name,
                                  lambda h, n=name: self.calls.append((n, h)))
            p.start()
            self.addCleanup(p.stop)
        for name in ("bgm_mark_episode_watched", "bgm_unmark_episode_watched"):
            p = mock.patch.object(core, name,
                                  lambda t, e, n=name: self.calls.append((n, e)))
            p.start()
            self.addCleanup(p.stop)
        for name, val in (("existing_rules", lambda: {}),
                          ("rules_by_savepath", lambda r: {}),
                          ("qb_get_json", lambda p: [self.torrent]),
                          ("_jf_find_torrent", lambda p, t: self.torrent)):
            p = mock.patch.object(core, name, val)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(core, "resolve_torrent_target",
                              lambda *a, **k: (900, 77, "ok"))
        p.start()
        self.addCleanup(p.stop)
        for target, value in (("JFHOOK_REVERSE_ENABLED", True),
                              ("JFHOOK_REVERSE_RATE_LIMIT", 0)):
            p = mock.patch.object(core, target, value)
            p.start()
            self.addCleanup(p.stop)
        core._JF_REVERSE_HITS.clear()
        core._JF_APPLIED.clear()
        core._JF_NOISE.clear()

    def run_event(self, payload, path=r"X:\BangumiJF\2026.07\Show\Season 01\ep03.mp4", **kw):
        core.handle_jellyfin_event(dict(payload, Path=path), "tok", **kw)
        return self.calls

    def test_watched_stops_seeding_and_marks(self):
        self.assertEqual(self.run_event(WATCHED),
                         [("qb_stop", "H"), ("bgm_mark_episode_watched", 77)])

    def test_undo_resumes_seeding_and_unmarks(self):
        self.assertEqual(self.run_event(UNWATCHED),
                         [("qb_start", "H"), ("bgm_unmark_episode_watched", 77)])

    def test_ancient_is_untouched_in_both_directions(self):
        """The hard gate is inherited, not reimplemented on the undo path."""
        anc = r"X:\BangumiJF\Ancient\Show\Season 01\ep03.mp4"
        self.run_event(UNWATCHED, path=anc)
        self.run_event(WATCHED, path=anc)
        self.assertEqual(self.calls, [])

    def test_an_old_cour_is_untouched_in_both_directions(self):
        """resolve_torrent_target is the single red line for both."""
        with mock.patch.object(core, "resolve_torrent_target",
                               lambda *a, **k: (None, None, "旧番 2025.10（手动管理）")):
            self.run_event(UNWATCHED)
            self.run_event(WATCHED)
        self.assertEqual(self.calls, [])

    def test_a_missing_torrent_does_nothing(self):
        with mock.patch.object(core, "_jf_find_torrent", lambda p, t: None):
            self.run_event(UNWATCHED)
        self.assertEqual(self.calls, [])

    def test_a_non_episode_is_ignored(self):
        self.run_event(dict(UNWATCHED, ItemType="Movie"))
        self.assertEqual(self.calls, [])

    def test_undo_can_be_switched_off_without_affecting_forward(self):
        with mock.patch.object(core, "JFHOOK_REVERSE_ENABLED", False):
            self.run_event(UNWATCHED)
            self.assertEqual(self.calls, [])
            self.run_event(WATCHED)
        self.assertEqual(self.calls, [("qb_stop", "H"),
                                      ("bgm_mark_episode_watched", 77)])

    def test_dry_run_touches_nothing(self):
        self.run_event(UNWATCHED, dry_run=True)
        self.run_event(WATCHED, dry_run=True)
        self.assertEqual(self.calls, [])

    def test_a_seeding_failure_does_not_stop_the_bgm_write(self):
        """Best-effort: one leg failing must not swallow the other."""
        with mock.patch.object(core, "qb_start", side_effect=RuntimeError("down")):
            self.run_event(UNWATCHED)
        self.assertEqual(self.calls, [("bgm_unmark_episode_watched", 77)])

    def test_a_bgm_failure_is_swallowed(self):
        with mock.patch.object(core, "bgm_unmark_episode_watched",
                               side_effect=RuntimeError("down")):
            self.run_event(UNWATCHED)
        self.assertEqual(self.calls, [("qb_start", "H")])

    def test_no_token_still_resumes_seeding(self):
        core.handle_jellyfin_event(
            dict(UNWATCHED, Path=r"X:\BangumiJF\2026.07\Show\Season 01\ep03.mp4"), "")
        self.assertEqual(self.calls, [("qb_start", "H")])

    def test_an_unrelated_event_is_a_no_op(self):
        self.run_event(dict(UNWATCHED, SaveReason="PlaybackProgress"))
        self.assertEqual(self.calls, [])

    def test_finishing_an_episode_stops_seeding_and_marks(self):
        """End to end for the shape that was being dropped."""
        self.assertEqual(self.run_event(FINISHED),
                         [("qb_stop", "H"), ("bgm_mark_episode_watched", 77)])

    def test_the_burst_after_one_episode_acts_once(self):
        """Fourteen Played=True saves for one episode, one stop and one mark."""
        for _ in range(6):
            self.run_event(PROGRESS_PLAYED)
        self.run_event(FINISHED)
        self.assertEqual(self.calls,
                         [("qb_stop", "H"), ("bgm_mark_episode_watched", 77)])

    def test_a_failed_attempt_is_retried_by_the_next_notification(self):
        """Suppression follows success, so a transient failure is not a loss."""
        with mock.patch.object(core, "bgm_mark_episode_watched",
                               side_effect=RuntimeError("bgm down")):
            self.run_event(PROGRESS_PLAYED)
        self.calls.clear()
        self.run_event(FINISHED)
        self.assertEqual(self.calls,
                         [("qb_stop", "H"), ("bgm_mark_episode_watched", 77)])

    def test_unticking_after_watching_is_never_suppressed(self):
        self.run_event(FINISHED)
        self.calls.clear()
        self.run_event(UNWATCHED)
        self.assertEqual(self.calls,
                         [("qb_start", "H"), ("bgm_unmark_episode_watched", 77)])

    def test_two_different_episodes_are_independent(self):
        self.run_event(FINISHED)
        self.calls.clear()
        self.run_event(dict(FINISHED, ItemId="other-item"))
        self.assertEqual(self.calls,
                         [("qb_stop", "H"), ("bgm_mark_episode_watched", 77)])

    def test_a_dry_run_does_not_suppress_the_real_thing(self):
        self.run_event(FINISHED, dry_run=True)
        self.assertEqual(self.run_event(FINISHED),
                         [("qb_stop", "H"), ("bgm_mark_episode_watched", 77)])


class MidPlayNoiseCase(unittest.TestCase):
    """Mid-play heartbeats are thinned in the log, never silenced entirely."""

    def setUp(self) -> None:
        core._JF_NOISE.clear()

    def test_a_mid_play_save_counts_as_noise(self):
        self.assertTrue(core._jf_is_midplay_noise(PROGRESS_UNPLAYED))
        self.assertTrue(core._jf_is_midplay_noise(dict(WATCHED, SaveReason="PlaybackStart",
                                                       Played="False")))

    def test_nothing_that_carries_a_verdict_is_noise(self):
        for p in (WATCHED, UNWATCHED, FINISHED, PROGRESS_PLAYED):
            self.assertFalse(core._jf_is_midplay_noise(p), p.get("SaveReason"))

    def test_an_unrecognised_shape_is_never_silenced(self):
        """Whatever this classifier does not understand stays fully logged."""
        self.assertFalse(core._jf_is_midplay_noise(
            {"NotificationType": "SomethingNew", "Played": "False"}))

    def test_one_line_per_episode_then_quiet(self):
        self.assertTrue(core._jf_noise_is_due(PROGRESS_UNPLAYED))
        for _ in range(20):
            self.assertFalse(core._jf_noise_is_due(PROGRESS_UNPLAYED))
        self.assertTrue(core._jf_noise_is_due(dict(PROGRESS_UNPLAYED, ItemId="other")))


class ReverseRateLimitCase(unittest.TestCase):
    """Marking a Series played cascades one webhook per episode."""

    def setUp(self) -> None:
        core._JF_REVERSE_HITS.clear()
        core._JF_APPLIED.clear()
        core._JF_NOISE.clear()
        self.addCleanup(core._JF_REVERSE_HITS.clear)

    def test_disabled_by_default_never_brakes(self):
        with mock.patch.object(core, "JFHOOK_REVERSE_RATE_LIMIT", 0):
            self.assertFalse(any(core._jf_reverse_rate_limited() for _ in range(500)))

    def test_sheds_past_the_limit(self):
        with mock.patch.object(core, "JFHOOK_REVERSE_RATE_LIMIT", 3):
            got = [core._jf_reverse_rate_limited() for _ in range(5)]
        self.assertEqual(got, [False, False, False, True, True])

    def test_the_window_rolls_forward(self):
        with mock.patch.object(core, "JFHOOK_REVERSE_RATE_LIMIT", 2):
            self.assertFalse(core._jf_reverse_rate_limited())
            self.assertFalse(core._jf_reverse_rate_limited())
            self.assertTrue(core._jf_reverse_rate_limited())
            core._JF_REVERSE_HITS[:] = [t - 61 for t in core._JF_REVERSE_HITS]
            self.assertFalse(core._jf_reverse_rate_limited())

    def test_the_forward_direction_is_never_braked(self):
        """A binge marking many episodes watched must still all be recorded."""
        calls = []
        with mock.patch.object(core, "JFHOOK_REVERSE_RATE_LIMIT", 1), \
             mock.patch.object(core, "qb_stop", lambda h: calls.append(h)), \
             mock.patch.object(core, "bgm_mark_episode_watched", lambda t, e: None), \
             mock.patch.object(core, "existing_rules", lambda: {}), \
             mock.patch.object(core, "rules_by_savepath", lambda r: {}), \
             mock.patch.object(core, "qb_get_json", lambda p: []), \
             mock.patch.object(core, "_jf_find_torrent",
                               lambda p, t: {"hash": "H", "name": "Show - 03.mp4"}), \
             mock.patch.object(core, "resolve_torrent_target",
                               lambda *a, **k: (900, 77, "ok")):
            for n in range(5):
                core.handle_jellyfin_event(
                    dict(WATCHED, ItemId=f"episode-{n}",
                         Path=rf"X:\BangumiJF\2026.07\S\Season 01\e{n}.mp4"), "tok")
        self.assertEqual(len(calls), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
