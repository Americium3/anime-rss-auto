"""Sidecar subtitles follow their video into the Jellyfin mirror.

Erai-raws Tempal shipped its Chinese track as "<video>.sc.ass" next to the mkv.
The mirror linked only videos, so Jellyfin never saw it; and "sc" is Sardinian
to Jellyfin, so a plain link would still not read as Chinese.
"""
from __future__ import annotations

import os
import unittest

import anime_rss as core
from test_events import MirrorCase

VID = "[Erai-raws] Tempal - Item no Chikara - 01 [1080p][DC3ECFBB]"


class TestSubName(unittest.TestCase):
    def test_fansub_simplified_tag_becomes_titled_chi(self):
        self.assertEqual(core._mirror_sub_name(f"{VID}.sc.ass", VID),
                         f"{VID}.简体中文.chi.ass")

    def test_traditional_and_bilingual_tags(self):
        self.assertEqual(core._mirror_sub_name("a.CHT.srt", "a"), "a.繁體中文.chi.srt")
        self.assertEqual(core._mirror_sub_name("a.chs_jpn.ass", "a"), "a.简日双语.chi.ass")

    def test_tags_jellyfin_already_reads_are_kept(self):
        self.assertEqual(core._mirror_sub_name("a.eng.forced.srt", "a"), "a.eng.forced.srt")
        self.assertEqual(core._mirror_sub_name("a.ass", "a"), "a.ass")


class TestMirrorSubs(MirrorCase):
    def mirror_dir(self):
        return self.dst / "2026.10" / "Overgeared" / "Season 01"

    def test_sidecar_is_linked_with_a_readable_language(self):
        self.episode("2026.10", "Overgeared", f"{VID}.mkv")
        sub = self.episode("2026.10", "Overgeared", f"{VID}.sc.ass")
        core.mirror_sync_pass()
        link = self.mirror_dir() / f"{VID}.简体中文.chi.ass"
        self.assertTrue(link.is_file())
        self.assertTrue(link.samefile(sub))

    def test_subtitle_in_a_per_episode_folder_finds_its_video(self):
        d = self.src / "2026.10" / "Overgeared" / VID
        d.mkdir(parents=True)
        (d / f"{VID}.mkv").write_bytes(b"x")
        (d / f"{VID}.sc.ass").write_bytes(b"s")
        core.mirror_sync_pass()
        self.assertTrue((self.mirror_dir() / f"{VID}.简体中文.chi.ass").is_file())

    def test_subtitle_does_not_announce_an_episode(self):
        self.episode("2026.10", "Overgeared", f"{VID}.mkv")
        self.episode("2026.10", "Overgeared", f"{VID}.sc.ass")
        core.mirror_sync_pass()
        self.assertEqual(len(core.load_events()["events"]), 1)

    def test_orphan_subtitle_without_its_video_is_not_linked(self):
        self.episode("2026.10", "Overgeared", "Other - 02.sc.ass")
        self.episode("2026.10", "Overgeared", f"{VID}.mkv")
        core.mirror_sync_pass()
        names = sorted(p.name for p in self.mirror_dir().iterdir())
        self.assertEqual(names, [f"{VID}.mkv"])

    def test_a_hand_named_link_of_the_same_file_is_not_doubled(self):
        self.episode("2026.10", "Overgeared", f"{VID}.mkv")
        sub = self.episode("2026.10", "Overgeared", f"{VID}.sc.ass")
        core.mirror_sync_pass()
        auto = self.mirror_dir() / f"{VID}.简体中文.chi.ass"
        auto.rename(self.mirror_dir() / f"{VID}.简体中文(AI).chi.ass")
        core.mirror_sync_pass()
        subs = [p for p in self.mirror_dir().iterdir() if p.suffix == ".ass"]
        self.assertEqual(len(subs), 1)
        self.assertTrue(subs[0].samefile(sub))

    def test_subtitle_link_is_pruned_with_its_video(self):
        vid = self.episode("2026.10", "Overgeared", f"{VID}.mkv")
        sub = self.episode("2026.10", "Overgeared", f"{VID}.sc.ass")
        self.episode("2026.10", "Overgeared", "Tempal - 02.mkv")  # keeps the show non-empty
        core.mirror_sync_pass()
        os.remove(vid)
        os.remove(sub)
        core.mirror_prune_orphan_files()
        names = sorted(p.name for p in self.mirror_dir().iterdir())
        self.assertEqual(names, ["Tempal - 02.mkv"])

    def test_prune_keeps_subtitles_whose_video_is_still_there(self):
        self.episode("2026.10", "Overgeared", f"{VID}.mkv")
        self.episode("2026.10", "Overgeared", f"{VID}.sc.ass")
        core.mirror_sync_pass()
        core.mirror_prune_orphan_files()
        self.assertTrue((self.mirror_dir() / f"{VID}.简体中文.chi.ass").is_file())


if __name__ == "__main__":
    unittest.main()
