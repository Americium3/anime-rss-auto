"""The no-mikan flag the panel's cards, slips and tab are all drawn from.

The one thing worth pinning down here is that dismissing a banner must not
clear this. That is not a detail: a dismissed banner used to be the end of the
line for a show mikan has no entry for — the warning went away, the state did
not, and the only control that could fix it went away with the warning. Every
surface that replaced it reads this set, so if it ever starts honouring the
dismiss flag they all silently go back to being unreachable.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import anime_rss as core
import webui


def entry(bgm_id: int, **extra) -> dict:
    return {"bgm_id": bgm_id, "title": f"show {bgm_id}", **extra}


class NoMikanIdsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "unresolved.json"
        self._p = mock.patch.object(core, "UNRESOLVED_PATH", self.path)
        self._p.start()

    def tearDown(self) -> None:
        self._p.stop()
        self._tmp.cleanup()

    def write(self, *entries: dict) -> None:
        self.path.write_text(json.dumps(list(entries)), encoding="utf-8")

    def test_dismissed_entries_still_count(self):
        """The whole point. A dismissed banner is silence, not a resolution."""
        self.write(entry(1, dismissed=True), entry(2))
        self.assertEqual(webui._no_mikan_ids(), {1, 2})

    def test_missing_file_is_empty_not_an_error(self):
        self.assertEqual(webui._no_mikan_ids(), set())

    def test_a_corrupt_file_is_empty_not_an_error(self):
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(webui._no_mikan_ids(), set())

    def test_an_entry_without_an_id_is_skipped(self):
        self.write({"title": "no id here"}, entry(7))
        self.assertEqual(webui._no_mikan_ids(), {7})


class MarkTest(unittest.TestCase):
    def payload(self) -> dict:
        return {
            "groups": {
                "watching": [{"bgm_id": 1}, {"bgm_id": 2}],
                "want": [{"bgm_id": 3}],
                "done": [{"bgm_id": 4}],
            },
            "counts": {"watching": 2, "want": 1, "done": 1},
        }

    def test_marks_across_every_collection_type(self):
        # A film in 想看 and a show in 在看 — the cross-section the tab exists for.
        with mock.patch.object(webui, "_no_mikan_ids", lambda: {2, 3}):
            out = webui._mark_no_mikan(self.payload())
        flat = {s["bgm_id"]: s["no_mikan"] for g in out["groups"].values() for s in g}
        self.assertEqual(flat, {1: False, 2: True, 3: True, 4: False})
        self.assertEqual(out["counts"]["no_mikan"], 2)

    def test_every_show_gets_the_key_either_way(self):
        """The frontend reads s.no_mikan directly; a missing key would render
        the box for nobody and be indistinguishable from 'nothing is unmatched'."""
        with mock.patch.object(webui, "_no_mikan_ids", set):
            out = webui._mark_no_mikan(self.payload())
        self.assertTrue(all("no_mikan" in s for g in out["groups"].values() for s in g))
        self.assertEqual(out["counts"]["no_mikan"], 0)

    def test_re_marking_clears_a_resolved_show(self):
        """Applied outside the collections cache, so the same cached lists have
        to be able to lose a mark — resolving by hand must clear it at once."""
        data = self.payload()
        with mock.patch.object(webui, "_no_mikan_ids", lambda: {2}):
            webui._mark_no_mikan(data)
        with mock.patch.object(webui, "_no_mikan_ids", set):
            out = webui._mark_no_mikan(data)
        self.assertFalse(out["groups"]["watching"][1]["no_mikan"])
        self.assertEqual(out["counts"]["no_mikan"], 0)

    def test_an_empty_payload_does_not_raise(self):
        with mock.patch.object(webui, "_no_mikan_ids", lambda: {1}):
            self.assertEqual(webui._mark_no_mikan({})["counts"]["no_mikan"], 0)


if __name__ == "__main__":
    unittest.main()
