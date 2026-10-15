"""The full-replace delete in /api/rule/switch must honour the old-season line.

The cour comes from the rule's savePath, the same place the backend's
destructive passes read it from. A rule's tags are not trustworthy for this:
hand-made qB rules and qB 4.x rules have none, and a first tag like 'anime'
sorts after any 'YYYY.MM' string, so judging by the tag would wipe an old
show's folder.
"""
from __future__ import annotations

import unittest
from unittest import mock

import anime_rss as core
import webui

OLD_PATH = "X:\\Bangumi\\2025.10\\Old Show"
NEW_PATH = "X:\\Bangumi\\2026.07\\New Show"


class RuleSwitchDeleteGateTest(unittest.TestCase):
    def switch(self, rdef: dict) -> tuple[dict, list]:
        rdef = {"affectedFeeds": [core.feed_url(100, 1)], **rdef}
        posts = []
        with mock.patch.object(core, "existing_rules", lambda: {"r": rdef}), \
             mock.patch.object(core, "qb_torrents_under",
                               lambda sp: [{"hash": "abc", "save_path": sp}]), \
             mock.patch.object(core, "qb_post", lambda ep, data: posts.append((ep, data))), \
             mock.patch.object(core, "rss_feed_paths", lambda: {}), \
             mock.patch.object(core, "mikan_bangumi_info", lambda mid: {"title": "T"}), \
             mock.patch.object(core, "custom_keyword", lambda *a: ""), \
             mock.patch.object(core, "switch_must_contain", lambda *a: ""), \
             mock.patch.object(core, "restore_must_not_contain", lambda s: s), \
             mock.patch.object(core, "rule_bgm_id", lambda *a: None), \
             mock.patch.object(core, "_old_cour_exempt", lambda *a: False), \
             mock.patch.object(core, "load_manual_imports", lambda: {}), \
             mock.patch.object(core, "mirror_prune_orphan_files", lambda: 0), \
             mock.patch.object(core, "SKIP_BEFORE_SEASON", "2026.04"), \
             mock.patch.object(core, "CONFIG", {}):
            out = webui.api_rule_switch(webui.RuleSwitch(rule_name="r", subgroup=2))
        return out, [d for ep, d in posts if ep == "/api/v2/torrents/delete"]

    def test_old_path_without_torrent_params_keeps_files(self):
        out, deletes = self.switch({"savePath": OLD_PATH})
        self.assertEqual(deletes, [])
        self.assertEqual(out["deleted"], 0)

    def test_old_path_with_empty_tags_keeps_files(self):
        _, deletes = self.switch({"savePath": OLD_PATH, "torrentParams": {"tags": []}})
        self.assertEqual(deletes, [])

    def test_old_path_with_non_cour_tag_keeps_files(self):
        _, deletes = self.switch({"savePath": OLD_PATH, "torrentParams": {"tags": ["anime"]}})
        self.assertEqual(deletes, [])

    def test_old_path_wins_over_a_current_tag(self):
        _, deletes = self.switch({"savePath": OLD_PATH, "torrentParams": {"tags": ["2026.07"]}})
        self.assertEqual(deletes, [])

    def test_no_cour_anywhere_keeps_files(self):
        out, deletes = self.switch({"savePath": "X:\\Downloads\\Show",
                                    "torrentParams": {"tags": ["anime"]}})
        self.assertEqual(deletes, [])
        self.assertTrue(any("kept old files" in n for n in out["notes"]))

    def test_current_path_still_full_replaces(self):
        out, deletes = self.switch({"savePath": NEW_PATH})
        self.assertEqual(deletes, [{"hashes": "abc", "deleteFiles": "true"}])
        self.assertEqual(out["deleted"], 1)

    def test_cour_tag_is_used_when_path_has_none(self):
        _, deletes = self.switch({"savePath": "X:\\Downloads\\Show",
                                  "torrentParams": {"tags": ["2026.07"]}})
        self.assertEqual(len(deletes), 1)


if __name__ == "__main__":
    unittest.main()
