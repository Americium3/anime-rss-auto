#!/usr/bin/env python3
"""Tests: parse_episode reads the episode number from the layouts on disk.

Run:  python test_parse_episode.py      (stdlib unittest, no network)

The Chinese web-rip layout ("【10月】<title> 01【完全无修版】") used to return
None, so a show downloaded that way read as "nothing on disk" on the panel and
could not be marked watched from qB or Jellyfin.
"""
import unittest

import anime_rss as core


class ParseEpisode(unittest.TestCase):
    def test_known_layouts(self):
        cases = {
            "[ANi] Show - 04 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4": 4,
            "[Nekomoe] Engage Kiss [01][1080p].mkv": 1,
            "Cyberpunk.Edgerunners.S01E06.1080p.mkv": 6,
            "【10月】与暗黑召唤师秘密恋爱中 01【完全无修版】.mp4": 1,
            "【10月】与暗黑召唤师秘密恋爱中 12【TV版】.mp4": 12,
            "某番 第03集.mp4": 3,
            "某番 第3话 [1080P].mkv": 3,
        }
        for name, ep in cases.items():
            with self.subTest(name=name):
                self.assertEqual(core.parse_episode(name), ep)

    def test_no_number_stays_none(self):
        for name in (
            "缎带骑士.The.Ribbon.Hero.2026.1080p.NF.WEB-DL.x264.DDP5.1-CoolFansSub.mkv",
            "【10月】标题【简体】.mp4",          # the cour prefix is not an episode
            "Show 1080【简繁】.mp4",            # a resolution is not an episode
        ):
            with self.subTest(name=name):
                self.assertIsNone(core.parse_episode(name))


if __name__ == "__main__":
    unittest.main()
