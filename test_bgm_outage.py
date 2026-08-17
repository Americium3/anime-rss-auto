#!/usr/bin/env python3
"""Tests: a bgm outage must never be recorded as a fact about a show.

Run:  python test_bgm_outage.py          (stdlib unittest, no network, no qB)

The daemon's destructive passes — unsubscribe, delete files, mark watched — key off
answers from bgm. The bug these cover is that a failed request used to be written into
the caches in the same shape as a real answer, so one bad minute could be believed
forever (subject_season_cache.json has no expiry) or for a day (episode_span_cache.json).

Every case here asserts the same rule: an answer may be cached, a non-answer may not,
and when we don't know, the pass does nothing.
"""
from __future__ import annotations

import unittest
import unittest.mock as mock
import urllib.error

import anime_rss as core


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.bgm.tv/x", code, "boom", {}, None)


class FakeBgm:
    """Stands in for core.http_get. `mode` decides how bgm behaves this round."""

    def __init__(self, mode: str = "up", payloads: dict | None = None):
        self.mode = mode          # up | down | timeout | ratelimited | missing
        self.payloads = payloads or {}
        self.calls: list[str] = []

    def __call__(self, url: str, *, retries: int = 3, timeout: int = 15) -> bytes:
        self.calls.append(url)
        if self.mode == "down":
            raise core.HttpError(f"GET failed: {url}\n  HTTP 503", 503)
        if self.mode == "timeout":
            raise core.HttpError(f"GET failed: {url}\n  timed out", None)
        if self.mode == "ratelimited":
            raise core.HttpError(f"GET failed: {url}\n  HTTP 429", 429)
        if self.mode == "missing":
            raise core.HttpError(f"GET failed: {url}\n  HTTP 404", 404)
        for frag, body in self.payloads.items():
            if frag in url:
                return body.encode()
        raise AssertionError(f"FakeBgm has no payload for {url}")


SUBJECT_OLD = '{"date": "2025-10-04", "name": "an old show"}'
EPISODES_2 = ('{"data": [{"airdate": "2026-07-05", "ep": 1, "id": 11},'
              ' {"airdate": "2026-07-12", "ep": 2, "id": 12}]}')


class SeasonCacheTest(unittest.TestCase):
    """bgm_subject_season: a cour is cached forever, so a wrong one is forever too."""

    def test_outage_returns_no_answer_and_caches_nothing(self):
        for mode in ("down", "timeout", "ratelimited"):
            with self.subTest(mode=mode):
                cache: dict = {}
                with mock.patch.object(core, "http_get", FakeBgm(mode)):
                    got = core.bgm_subject_season(555, cache)
                self.assertIs(got, core._NO_ANSWER)
                self.assertEqual(cache, {}, "an outage must leave no trace in the cache")

    def test_404_is_an_answer_and_is_cached(self):
        cache: dict = {}
        with mock.patch.object(core, "http_get", FakeBgm("missing")):
            self.assertIsNone(core.bgm_subject_season(555, cache))
        self.assertEqual(cache, {555: None}, "'no such subject' is a fact worth keeping")

    def test_success_is_cached_and_not_refetched(self):
        cache: dict = {}
        fake = FakeBgm("up", {"/v0/subjects/": SUBJECT_OLD})
        with mock.patch.object(core, "http_get", fake):
            self.assertEqual(core.bgm_subject_season(555, cache), "2025.10")
            self.assertEqual(core.bgm_subject_season(555, cache), "2025.10")
        self.assertEqual(len(fake.calls), 1)

    def test_outage_then_recovery_gets_the_real_cour(self):
        """The regression itself: the old code cached None here, and because there is
        no expiry path for None, the show stayed miscategorised for the life of the
        cache file. It must heal on the very next call instead."""
        cache: dict = {}
        with mock.patch.object(core, "http_get", FakeBgm("down")):
            self.assertIs(core.bgm_subject_season(555, cache), core._NO_ANSWER)
        with mock.patch.object(core, "http_get", FakeBgm("up", {"/v0/subjects/": SUBJECT_OLD})):
            self.assertEqual(core.bgm_subject_season(555, cache), "2025.10")


class EpisodeSpanTest(unittest.TestCase):
    """bgm_episode_span: the empty span it returns on failure is read conservatively,
    but it used to be written down and believed for 24h."""

    def test_outage_span_is_not_persisted(self):
        cache: dict = {}
        with mock.patch.object(core, "http_get", FakeBgm("down")):
            span = core.bgm_episode_span(555, cache)
        self.assertEqual(span["count"], 0)
        self.assertTrue(span.get(core._SPAN_NO_ANSWER))
        with mock.patch.object(core, "_atomic_write_json") as w:
            core.save_span_cache(cache)
        self.assertEqual(w.call_args[0][1], {}, "a fault note must never reach disk")

    def test_outage_span_does_not_stick_for_24h(self):
        cache: dict = {}
        with mock.patch.object(core, "http_get", FakeBgm("down")):
            core.bgm_episode_span(555, cache)
        cache[555]["_ts"] -= core._SPAN_FAULT_TTL + 1        # next run, minutes later
        fake = FakeBgm("up", {"/v0/episodes": EPISODES_2})
        with mock.patch.object(core, "http_get", fake):
            span = core.bgm_episode_span(555, cache)
        self.assertEqual(span["last"], "2026-07-12")
        self.assertNotIn(core._SPAN_NO_ANSWER, span)
        self.assertEqual(len(fake.calls), 1, "the blank must have been retried")

    def test_outage_span_is_reused_briefly_so_one_run_does_not_hammer_bgm(self):
        cache: dict = {}
        fake = FakeBgm("down")
        with mock.patch.object(core, "http_get", fake):
            for _ in range(5):
                core.bgm_episode_span(555, cache)
        self.assertEqual(len(fake.calls), 1)

    def test_404_empty_span_is_cached_as_a_fact(self):
        """Eight subjects in the live cache legitimately have no dated episodes, so
        'answered, and the answer is empty' has to stay cacheable."""
        cache: dict = {}
        with mock.patch.object(core, "http_get", FakeBgm("missing")):
            span = core.bgm_episode_span(555, cache)
        self.assertEqual(span["count"], 0)
        self.assertNotIn(core._SPAN_NO_ANSWER, span)
        with mock.patch.object(core, "_atomic_write_json") as w:
            core.save_span_cache(cache)
        self.assertIn(555, w.call_args[0][1])

    def test_failure_direction_is_unchanged(self):
        """Deliberate and load-bearing: an unknown schedule reads as 'not still airing',
        so an old-cour show stays exempt-less and hands-off. Failing toward do-nothing."""
        cache: dict = {}
        with mock.patch.object(core, "http_get", FakeBgm("down")):
            self.assertFalse(core.cour_still_airing(555, "2025.10", cache))
            self.assertFalse(core._old_cour_exempt("2025.10", 555, cache))
            self.assertTrue(core.is_manual_old_show("2025-10-04", 555, False))


class CollectionTypeTest(unittest.TestCase):
    """bgm_collection_type: None means 未收藏, which reconcile answers with rm -rf."""

    def test_outage_is_not_uncollected(self):
        for mode in ("down", "timeout", "ratelimited"):
            with self.subTest(mode=mode):
                with mock.patch.object(core, "http_get", FakeBgm(mode)):
                    self.assertIs(core.bgm_collection_type("u", 555), core._NO_ANSWER)

    def test_404_is_uncollected(self):
        with mock.patch.object(core, "http_get", FakeBgm("missing")):
            self.assertIsNone(core.bgm_collection_type("u", 555))


class SubjectEpisodesTest(unittest.TestCase):
    def test_outage_map_is_not_memoised(self):
        cache: dict = {}
        with mock.patch.object(core, "http_get", FakeBgm("down")):
            self.assertEqual(core.bgm_subject_episodes(555, cache), {})
        self.assertEqual(cache, {})
        with mock.patch.object(core, "http_get", FakeBgm("up", {"/v0/episodes": EPISODES_2})):
            self.assertEqual(core.bgm_subject_episodes(555, cache), {1: 11, 2: 12})


class StubCtx:
    """The slice of SyncContext reconcile_removed touches, with bgm behind FakeBgm."""

    def __init__(self, rules: dict, bgm_ids: dict, watching: list[int]):
        self._rules, self._bgm_ids, self._watching = rules, bgm_ids, watching
        self.season: dict = {}
        self.span: dict = {}
        self._ctype_memo: dict = {}
        self.override_type: dict = {}
        self.user = "u"
        # PR#45 additions reconcile_removed reads: nothing was created this pass,
        # and an empty reverse map means the sibling guard finds no sibling.
        self.created_this_run: set = set()
        self.mikan_bgm_ids: dict = {}

    def rules(self):
        return self._rules

    def feed_paths(self):
        return {}

    def rule_bgm_id_of(self, rdef):
        return self._bgm_ids[rdef["savePath"]]

    # verbatim from SyncContext, so the memo guard is under test too
    collection_type_of = core.SyncContext.collection_type_of
    watching_sibling_of = core.SyncContext.watching_sibling_of

    def is_watching(self, bgm_id):
        # only reached if a test seeds mikan_bgm_ids (sibling-guard scenarios)
        return bgm_id in self._watching

    def collection(self, ctype):
        assert ctype == 3
        return [{"bgm_id": b} for b in self._watching]


class ReconcileUnderOutageTest(unittest.TestCase):
    """End to end on the pass that unsubscribes from mikan and deletes files."""

    RULES = {"an old show": {"savePath": "X:/Anime/2025.10/an old show", "affectedFeeds": []}}

    def _run(self, mode: str, watching: list[int]):
        ctx = StubCtx(self.RULES, {"X:/Anime/2025.10/an old show": 555}, watching)
        payloads = {"/v0/subjects/": SUBJECT_OLD, "/collections/": '{"type": 3}'}
        with mock.patch.object(core, "http_get", FakeBgm(mode, payloads)), \
                mock.patch.object(core, "remove_subscription") as rm:
            core.reconcile_removed("u", "cookie", dry_run=False, purge_dropped=True, ctx=ctx)
        return rm

    def test_outage_deletes_nothing(self):
        """Both signals reconcile needs fail at once during an outage: the cour comes
        back unknown (which reads as 'current', so the old-show guard lets it through)
        and the collection lookup comes back empty (which reads as '未收藏'). The old
        code combined those into "current show, no longer collected" and purged — mikan
        unsubscribed, files deleted. Nothing may be touched now."""
        for mode in ("down", "timeout", "ratelimited"):
            with self.subTest(mode=mode):
                rm = self._run(mode, watching=[])
                rm.assert_not_called()

    def test_the_old_conflation_would_have_deleted_files(self):
        """Proof the cases above aren't vacuous. Treating every failure as an answer —
        `_bgm_said_no` always true — is precisely what the code used to do, and it turns
        the same outage into an unsubscribe plus a file delete on a show the user is
        managing by hand. If this ever stops purging, the scenario has drifted and the
        tests above have stopped guarding anything."""
        ctx = StubCtx(self.RULES, {"X:/Anime/2025.10/an old show": 555}, watching=[])
        with mock.patch.object(core, "http_get", FakeBgm("down")), \
                mock.patch.object(core, "_bgm_said_no", lambda ex: True), \
                mock.patch.object(core, "remove_subscription") as rm:
            core.reconcile_removed("u", "cookie", dry_run=False, purge_dropped=True, ctx=ctx)
        rm.assert_called_once()
        self.assertTrue(rm.call_args.kwargs["delete_files"])

    def test_a_genuinely_dropped_current_show_is_still_purged(self):
        """The other half of the contract: the fix must not just switch teardown off.
        bgm is up, the show is current, and 404 on its collection entry means the user
        really did remove it — so it is unsubscribed and its files go."""
        ctx = StubCtx({"a current show": {"savePath": "X:/Anime/2026.07/a current show",
                                          "affectedFeeds": []}},
                      {"X:/Anime/2026.07/a current show": 777}, watching=[])

        def routed(url, *, retries=3, timeout=15):
            if "/v0/subjects/" in url:
                return b'{"date": "2026-07-05"}'
            raise core.HttpError("GET failed", 404)   # not in the collection

        with mock.patch.object(core, "http_get", routed), \
                mock.patch.object(core, "remove_subscription") as rm:
            core.reconcile_removed("u", "cookie", dry_run=False, purge_dropped=True, ctx=ctx)
        rm.assert_called_once()
        self.assertTrue(rm.call_args.kwargs["delete_files"])

    def test_watching_show_is_kept_while_bgm_is_up(self):
        rm = self._run("up", watching=[555])
        rm.assert_not_called()

    def test_collection_lookup_outage_alone_deletes_nothing(self):
        """The narrower half: bgm is well enough to date the show (so it is known to be
        current and in scope) but the per-subject collection lookup fails. That lookup
        returning None is the sole trigger for the purge branch, so it must not."""
        ctx = StubCtx({"a current show": {"savePath": "X:/Anime/2026.07/a current show",
                                          "affectedFeeds": []}},
                      {"X:/Anime/2026.07/a current show": 777}, watching=[])
        calls = []

        def routed(url, *, retries=3, timeout=15):
            calls.append(url)
            if "/v0/subjects/" in url:
                return b'{"date": "2026-07-05"}'
            raise core.HttpError("GET failed", 503)

        with mock.patch.object(core, "http_get", routed), \
                mock.patch.object(core, "remove_subscription") as rm:
            core.reconcile_removed("u", "cookie", dry_run=False, purge_dropped=True, ctx=ctx)
            rm.assert_not_called()
            self.assertEqual(ctx._ctype_memo, {}, "an outage must not be memoised")
            # ...and the next pass asks again rather than reusing the non-answer
            core.reconcile_removed("u", "cookie", dry_run=False, purge_dropped=True, ctx=ctx)
        self.assertEqual(sum("/collections/" in u for u in calls), 2)


class HttpGetTest(unittest.TestCase):
    def test_status_is_preserved(self):
        with mock.patch.object(core.urllib.request, "urlopen", side_effect=_http_error(503)):
            with self.assertRaises(core.HttpError) as cm:
                core.http_get("https://api.bgm.tv/x", retries=1)
        self.assertEqual(cm.exception.code, 503)

    def test_network_error_has_no_status(self):
        with mock.patch.object(core.urllib.request, "urlopen", side_effect=OSError("reset")):
            with self.assertRaises(core.HttpError) as cm:
                core.http_get("https://api.bgm.tv/x", retries=1)
        self.assertIsNone(cm.exception.code)

    def test_404_is_not_retried_but_503_and_429_are(self):
        for code, expected in ((404, 1), (503, 3), (429, 3)):
            with self.subTest(code=code):
                op = mock.Mock(side_effect=_http_error(code))
                with mock.patch.object(core.urllib.request, "urlopen", op), \
                        mock.patch.object(core.time, "sleep"):
                    with self.assertRaises(core.HttpError):
                        core.http_get("https://api.bgm.tv/x", retries=3)
                self.assertEqual(op.call_count, expected)

    def test_still_a_runtime_error_for_existing_callers(self):
        self.assertTrue(issubclass(core.HttpError, RuntimeError))


if __name__ == "__main__":
    unittest.main(verbosity=2)
