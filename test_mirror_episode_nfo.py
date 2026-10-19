"""Mirror fixes for releases Jellyfin misfiles: bit-depth tags and extras folders.

Jellyfin read "HEVC-10bit" as episode 10, so all of Kono Suba 3 Bonus Stage
([01], [02], a PV and two disc menus from DBD-Raws) showed up as episode 10. The
menus and PV sit in "menu" / "PV" folders, which an exact-case check missed, so
they were listed as Season 01 episodes as well.
"""
from __future__ import annotations

import os
import unittest

import anime_rss as core
from test_events import MirrorCase

SHOW = "Kono Suba 3 Bonus Stage"
REL = "[DBD-Raws][Kono Suba 3 Bonus Stage][01-02][HEVC-10bit][MKV]"


def dbd(tag: str) -> str:
    return f"[DBD-Raws][Kono Subarashii Sekai ni Shukufuku o! 3 Bonus Stage]{tag}" \
           "[1080P][BDRip][HEVC-10bit][FLAC].mkv"


class TestMirrorSeason(unittest.TestCase):
    def test_extras_folders_match_in_any_case(self):
        self.assertEqual(core._mirror_season(("rel", "menu")), "Season 00")
        self.assertEqual(core._mirror_season(("rel", "PV")), "Season 00")
        self.assertEqual(core._mirror_season(("SPS",)), "Season 00")
        self.assertEqual(core._mirror_season(("特典映像",)), "Season 00")

    def test_release_folder_alone_is_season_one(self):
        self.assertEqual(core._mirror_season((REL,)), "Season 01")
        self.assertEqual(core._mirror_season(()), "Season 01")


class TestEpisodeNfo(MirrorCase):
    def show(self, season: str = "Season 01"):
        return self.dst / "2025.04" / SHOW / season

    def put(self, sub: str, name: str):
        d = self.src / "2025.04" / SHOW / REL
        if sub:
            d = d / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(b"x")
        return d / name

    def nfo(self, name: str, season: str = "Season 01") -> str:
        return (self.show(season) / name).with_suffix(".nfo").read_text(encoding="utf-8")

    def test_bit_depth_release_gets_its_number_pinned(self):
        self.put("", dbd("[01]"))
        self.put("", dbd("[02]"))
        core.mirror_sync_pass()
        self.assertIn("<season>1</season>", self.nfo(dbd("[01]")))
        self.assertIn("<episode>1</episode>", self.nfo(dbd("[01]")))
        self.assertIn("<episode>2</episode>", self.nfo(dbd("[02]")))

    def test_extras_go_to_season_zero(self):
        self.put("", dbd("[01]"))
        self.put("menu", dbd("[menu][01]"))
        self.put("PV", dbd("[PV]"))
        core.mirror_sync_pass()
        self.assertEqual(sorted(p.name for p in self.show().glob("*.mkv")), [dbd("[01]")])
        self.assertEqual(sorted(p.name for p in self.show("Season 00").glob("*.mkv")),
                         sorted([dbd("[menu][01]"), dbd("[PV]")]))
        self.assertIn("<season>0</season>", self.nfo(dbd("[menu][01]"), "Season 00"))
        # No number in the name: nothing to pin, Jellyfin keeps its own guess.
        self.assertFalse((self.show("Season 00") / dbd("[PV]")).with_suffix(".nfo").exists())

    def test_names_jellyfin_reads_fine_get_no_nfo(self):
        name = "[ANi] Show - 04 [1080P][Baha][WEB-DL][AAC AVC][CHT].mp4"
        self.episode("2026.10", "Show", name)
        core.mirror_sync_pass()
        d = self.dst / "2026.10" / "Show" / "Season 01"
        self.assertEqual([p.name for p in d.iterdir()], [name])

    def test_existing_link_is_backfilled_and_hand_nfo_kept(self):
        self.put("", dbd("[01]"))
        self.put("", dbd("[02]"))
        core.mirror_sync_pass()
        (self.show() / dbd("[01]")).with_suffix(".nfo").unlink()
        hand = (self.show() / dbd("[02]")).with_suffix(".nfo")
        hand.write_text("<episodedetails><episode>7</episode></episodedetails>",
                        encoding="utf-8")
        core.mirror_sync_pass()
        self.assertIn("<episode>1</episode>", self.nfo(dbd("[01]")))
        self.assertIn("<episode>7</episode>", hand.read_text(encoding="utf-8"))

    def test_misplaced_extras_link_is_pruned(self):
        self.put("", dbd("[01]"))
        menu = self.put("menu", dbd("[menu][01]"))
        # What the exact-case check left behind: the menu also linked into Season 01.
        self.show().mkdir(parents=True)
        os.link(menu, self.show() / dbd("[menu][01]"))
        core.mirror_sync_pass()
        core.mirror_prune_orphan_files()
        self.assertEqual(sorted(p.name for p in self.show().iterdir()),
                         sorted([dbd("[01]"), dbd("[01]")[:-4] + ".nfo"]))
        self.assertTrue((self.show("Season 00") / dbd("[menu][01]")).samefile(menu))

    def test_pruned_episode_takes_its_nfo_but_not_a_hand_one(self):
        ep1 = self.put("", dbd("[01]"))
        self.put("", dbd("[02]"))
        core.mirror_sync_pass()
        hand = self.show() / "tvshow.nfo"
        hand.write_text("<tvshow/>", encoding="utf-8")
        os.remove(ep1)
        core.mirror_prune_orphan_files()
        self.assertEqual(sorted(p.name for p in self.show().iterdir()),
                         sorted([dbd("[02]"), dbd("[02]")[:-4] + ".nfo", "tvshow.nfo"]))

    def test_mirror_unlink_drops_the_nfo(self):
        self.put("", dbd("[01]"))
        core.mirror_sync_pass()
        core.mirror_unlink(str(self.src / "2025.04" / SHOW), {dbd("[01]")})
        self.assertEqual(list(self.show().iterdir()), [])


if __name__ == "__main__":
    unittest.main()
