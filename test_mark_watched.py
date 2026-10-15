"""mark_watched_pass fires once per seeding -> stopped transition.

A pass that could not finish the mark must keep that transition pending, or the
next pass sees stopped -> stopped and the episode is never marked on bgm.
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
    "save_path": r"X:\Bangumi\2026.07\A Show", "state": "stoppedUP", "progress": 1,
}
NO_ANSWER = "bgm 未响应，季度无法判定（本轮跳过，下轮重试）"


class StubCtx:
    def __init__(self, rules: dict):
        self._rules = rules
        self.rule_bgmid: dict = {}
        self.season: dict = {}
        self.eps: dict = {}
        self.span: dict = {}

    def rules(self):
        return self._rules


class MarkWatchedRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.states = Path(self._tmp.name) / "seed_states.json"
        self.states.write_text(json.dumps({"h1": "stalledUP"}), encoding="utf-8")
        self.marked: list[int] = []
        self.fail_mark = False
        self._patches = [
            mock.patch.object(core, "SEED_STATES_PATH", self.states),
            mock.patch.object(core, "qb_get_json", lambda _p: [TORRENT]),
            mock.patch.object(core, "bgm_mark_episode_watched", self._mark),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _mark(self, _tok, eid):
        if self.fail_mark:
            raise OSError("bgm timeout")
        self.marked.append(eid)

    def run_pass(self, resolve) -> None:
        with mock.patch.object(core, "resolve_torrent_target", lambda *_a, **_k: resolve):
            core.mark_watched_pass("tok", ctx=StubCtx({"A Show": RULE}))

    def test_bgm_no_answer_is_retried_next_pass(self):
        self.run_pass((None, None, NO_ANSWER))
        self.run_pass((1, 99, "ok"))
        self.assertEqual(self.marked, [99])

    def test_unresolved_bgm_id_is_retried_next_pass(self):
        self.run_pass((None, None, "无法解析 bgm id"))
        self.run_pass((1, 99, "ok"))
        self.assertEqual(self.marked, [99])

    def test_episode_lookup_miss_is_retried_next_pass(self):
        self.run_pass((None, None, "集数 3 在 subject 1 找不到对应集(ep/sort 都无)"))
        self.run_pass((1, 99, "ok"))
        self.assertEqual(self.marked, [99])

    def test_failed_mark_is_retried_next_pass(self):
        self.fail_mark = True
        self.run_pass((1, 99, "ok"))
        self.fail_mark = False
        self.run_pass((1, 99, "ok"))
        self.assertEqual(self.marked, [99])

    def test_successful_mark_fires_only_once(self):
        self.run_pass((1, 99, "ok"))
        self.run_pass((1, 99, "ok"))
        self.assertEqual(self.marked, [99])

    def test_old_cour_is_settled_not_retried(self):
        self.run_pass((None, None, "旧番 2025.10 < 2026.04（手动管理）"))
        self.run_pass((1, 99, "ok"))
        self.assertEqual(self.marked, [])

    def test_no_rule_is_settled_not_retried(self):
        self.run_pass((None, None, "无对应 qB 规则（非自动下载的种子）"))
        self.run_pass((1, 99, "ok"))
        self.assertEqual(self.marked, [])


if __name__ == "__main__":
    unittest.main()
