#!/usr/bin/env python3
"""Tests: manual resolve (panel pastes a mikan link on an unmatched banner).

Run:  python test_manual_resolve.py       (stdlib unittest, no network, no qB)

The feature has three load-bearing seams, and every case asserts one of them:

  * a pasted link is classified strictly — a stray URL must 400 at the door,
    never reach qB;
  * the override file survives a merge — resolve_show re-reads it every pass,
    and a "_comment" key copied from the example must not nuke the map;
  * a one-shot import stays claimable — resolve_torrent_target must map the
    ruleless torrent back to its bgm subject via the ledger, and a subject with
    exactly one main episode must resolve without an episode number in the name.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

import anime_rss as core


EP_HASH = "7f51a83efd475b11476a80a112807b6761af425b"

EPISODE_HTML_ONESHOT = f"""
<html><head><title>[冷番补完字幕组][缎带英雄][THE RIBBON HERO][2026][1080p] - Mikan Project</title></head>
<body>
<p class="episode-title">[冷番补完字幕组][缎带英雄][THE RIBBON HERO][2026][1080p] [4.5GB]</p>
<a href="/Download/20260813/{EP_HASH}.torrent">下载种子</a>
<a href="magnet:?xt=urn:btih:{EP_HASH}&amp;tr=http%3a%2f%2ft.example%2fannounce">磁力</a>
<a href="/Home/MyBangumi">我的番组</a>
</body></html>
"""

EPISODE_HTML_LINKED = EPISODE_HTML_ONESHOT.replace(
    '<a href="/Home/MyBangumi">', '<a href="/Home/Bangumi/3644">缎带</a><a href="/Home/MyBangumi">'
)


class TestParseManualUrl(unittest.TestCase):
    def test_episode_url(self):
        kind, val = core.parse_manual_url(
            f"https://mikanani.me/Home/Episode/{EP_HASH}"
        )
        self.assertEqual((kind, val), ("episode", EP_HASH))

    def test_episode_url_uppercase_hash_normalised(self):
        kind, val = core.parse_manual_url(
            f"https://mikanani.me/Home/Episode/{EP_HASH.upper()}"
        )
        self.assertEqual((kind, val), ("episode", EP_HASH))

    def test_bangumi_url(self):
        kind, val = core.parse_manual_url("https://mikanani.me/Home/Bangumi/3644#456")
        self.assertEqual((kind, val), ("bangumi", "3644"))

    def test_magnet(self):
        kind, val = core.parse_manual_url(f"magnet:?xt=urn:btih:{EP_HASH}&tr=x")
        self.assertEqual(kind, "magnet")
        self.assertTrue(val.startswith("magnet:"))

    def test_garbage_rejected(self):
        for bad in ("https://netflix.com/title/1", "hello", "",
                    "https://mikanani.me/Home/Search?searchstr=x"):
            with self.assertRaises(ValueError):
                core.parse_manual_url(bad)


class TestMikanEpisodeInfo(unittest.TestCase):
    def test_oneshot_page(self):
        with mock.patch.object(core, "http_get",
                               return_value=EPISODE_HTML_ONESHOT.encode()):
            info = core.mikan_episode_info(EP_HASH)
        self.assertIsNone(info["mikan_id"])
        # &amp; in the page must come back as a working & in the magnet
        self.assertIn(f"btih:{EP_HASH}&tr=", info["magnet"])
        self.assertIn("RIBBON HERO", info["title"])
        self.assertNotIn("[4.5GB]", info["title"])

    def test_linked_page(self):
        with mock.patch.object(core, "http_get",
                               return_value=EPISODE_HTML_LINKED.encode()):
            info = core.mikan_episode_info(EP_HASH)
        self.assertEqual(info["mikan_id"], 3644)

    def test_magnet_rebuilt_when_markup_changes(self):
        with mock.patch.object(core, "http_get", return_value=b"<html></html>"):
            info = core.mikan_episode_info(EP_HASH)
        self.assertEqual(info["magnet"], f"magnet:?xt=urn:btih:{EP_HASH}")


class OverrideFileCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "mikan_overrides.json"
        self._patch = mock.patch.object(core, "MIKAN_OVERRIDES_PATH", self.path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()


class TestSaveMikanOverride(OverrideFileCase):
    def test_creates_and_merges(self):
        core.save_mikan_override(643828, 3644)
        core.save_mikan_override(111, 222)
        self.assertEqual(core.load_mikan_overrides(), {643828: 3644, 111: 222})

    def test_preserves_comment_key(self):
        self.path.write_text(json.dumps({"_comment": "hands off", "1": 2}),
                             encoding="utf-8")
        core.save_mikan_override(3, 4)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["_comment"], "hands off")
        self.assertEqual(core.load_mikan_overrides(), {1: 2, 3: 4})

    def test_load_tolerates_comment_key(self):
        # Before the "_" filter, a copied example's _comment made int() raise and
        # the whole map silently came back {} — every override vanished.
        self.path.write_text(json.dumps({"_comment": "x", "627136": 4042}),
                             encoding="utf-8")
        self.assertEqual(core.load_mikan_overrides(), {627136: 4042})


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "manual_imports.json"
        self._patch = mock.patch.object(core, "MANUAL_IMPORTS_PATH", self.path)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()


class TestResolveTorrentTargetLedger(LedgerCase):
    def torrent(self, name="[冷番补完字幕组][缎带英雄][2026][1080p].mkv"):
        return {"hash": EP_HASH, "name": name,
                "save_path": "X:/Bangumi/2026.07/缎带英雄"}

    def test_ruleless_unknown_torrent_still_skipped(self):
        bgm, eid, why = core.resolve_torrent_target(self.torrent(), {}, {}, {}, {})
        self.assertIsNone(bgm)
        self.assertIn("非自动下载", why)

    def test_ledger_claims_oneshot_and_single_episode_resolves(self):
        core.save_manual_imports({EP_HASH: {"bgm_id": 643828, "name": "RIBBON"}})
        ep_cache = {643828: {1: 999001}}       # pre-warmed: 1 main episode
        season_cache = {643828: "2026.07"}     # pre-warmed: no network
        bgm, eid, why = core.resolve_torrent_target(
            self.torrent(), {}, {}, season_cache, ep_cache)
        self.assertEqual((bgm, eid, why), (643828, 999001, "ok"))

    def test_multi_episode_subject_still_needs_a_number(self):
        core.save_manual_imports({EP_HASH: {"bgm_id": 643828, "name": "RIBBON"}})
        ep_cache = {643828: {1: 999001, 2: 999002}}
        season_cache = {643828: "2026.07"}
        bgm, eid, why = core.resolve_torrent_target(
            self.torrent(), {}, {}, season_cache, ep_cache)
        self.assertIsNone(bgm)
        self.assertIn("集数解析失败", why)

    def test_numbered_name_resolves_normally_via_ledger(self):
        core.save_manual_imports({EP_HASH: {"bgm_id": 643828, "name": "RIBBON"}})
        ep_cache = {643828: {1: 999001, 2: 999002}}
        season_cache = {643828: "2026.07"}
        t = self.torrent(name="[Group] Ribbon Hero - 02 [1080p].mkv")
        bgm, eid, why = core.resolve_torrent_target(
            t, {}, {}, season_cache, ep_cache)
        self.assertEqual((bgm, eid, why), (643828, 999002, "ok"))


class TestManualResolveOrchestration(unittest.TestCase):
    """manual_resolve wiring with every seam mocked: which legs fire for which
    link kinds, and what state each leg leaves behind."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            mock.patch.object(core, "MIKAN_OVERRIDES_PATH", root / "ovr.json"),
            mock.patch.object(core, "MANUAL_IMPORTS_PATH", root / "imp.json"),
            mock.patch.object(core, "UNRESOLVED_PATH", root / "unres.json"),
            mock.patch.object(core, "EVENTS_PATH", root / "events.json"),
        ]
        for p in self._patches:
            p.start()
        (root / "unres.json").write_text(json.dumps(
            [{"bgm_id": 643828, "title": "缎带英雄", "dismissed": False}]
        ), encoding="utf-8")
        self.subject = json.dumps({
            "id": 643828, "name": "THE RIBBON HERO", "name_cn": "缎带英雄",
            "date": "2026-08-08",
        }).encode()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def test_oneshot_episode_imports_but_never_subscribes(self):
        added = {}
        with mock.patch.object(core, "http_get", side_effect=[
                self.subject, EPISODE_HTML_ONESHOT.encode()]), \
             mock.patch.object(core, "bgm_english_alias", return_value="Ribbon Hero"), \
             mock.patch.object(core, "qb_add_magnet",
                               side_effect=lambda m, sp, se: added.update(
                                   magnet=m, save=sp, season=se)), \
             mock.patch.object(core, "apply_entries") as apl, \
             mock.patch.object(core, "bgm_collection_type", return_value=3):
            r = core.manual_resolve(
                "942942", 643828,
                f"https://mikanani.me/Home/Episode/{EP_HASH}", token="tk")
        self.assertTrue(r["ok"] and r["imported"] and not r["subscribed"])
        apl.assert_not_called()
        self.assertIn("2026.07", added["save"])
        self.assertIn("Ribbon Hero", added["save"])
        self.assertEqual(core.load_manual_imports()[EP_HASH]["bgm_id"], 643828)
        self.assertEqual(core.load_unresolved(), [])           # banner cleared
        self.assertEqual([e["kind"] for e in core.load_events()["events"]],
                         ["show.imported"])

    def test_linked_episode_subscribes_and_imports(self):
        with mock.patch.object(core, "http_get", side_effect=[
                self.subject, EPISODE_HTML_LINKED.encode()]), \
             mock.patch.object(core, "bgm_english_alias", return_value=""), \
             mock.patch.object(core, "mikan_bangumi_info", return_value={
                 "bgm_id": 643828, "subgroups": [583], "title": "Mikan - 缎带"}), \
             mock.patch.object(core, "existing_rules",
                               side_effect=[{}, {"缎带英雄": {}}]), \
             mock.patch.object(core, "pick_subgroup", return_value=583), \
             mock.patch.object(core, "qb_add_magnet"), \
             mock.patch.object(core, "apply_entries") as apl, \
             mock.patch.object(core, "bgm_collection_type", return_value=1), \
             mock.patch.object(core, "bgm_set_collection_type") as promo:
            r = core.manual_resolve(
                "942942", 643828,
                f"https://mikanani.me/Home/Episode/{EP_HASH}", token="tk")
        self.assertTrue(r["ok"] and r["imported"] and r["subscribed"])
        apl.assert_called_once()
        entry = apl.call_args[0][0][0]
        self.assertEqual(entry["mikan_id"], 3644)
        self.assertEqual(entry["bgm_id"], 643828)
        self.assertEqual(core.load_mikan_overrides(), {643828: 3644})
        promo.assert_called_once_with("tk", 643828, 3)         # 想看 -> 在看

    def test_bangumi_link_subscribes_without_import(self):
        with mock.patch.object(core, "http_get", return_value=self.subject), \
             mock.patch.object(core, "bgm_english_alias", return_value=""), \
             mock.patch.object(core, "mikan_bangumi_info", return_value={
                 "bgm_id": 999, "subgroups": [583], "title": "Mikan - 缎带"}), \
             mock.patch.object(core, "existing_rules",
                               side_effect=[{}, {"缎带英雄": {}}]), \
             mock.patch.object(core, "qb_add_magnet") as qadd, \
             mock.patch.object(core, "apply_entries"), \
             mock.patch.object(core, "bgm_collection_type", return_value=3):
            r = core.manual_resolve(
                "942942", 643828, "https://mikanani.me/Home/Bangumi/3644")
        self.assertTrue(r["ok"] and r["subscribed"] and not r["imported"])
        qadd.assert_not_called()
        # backlink names ANOTHER subject -> we obey the paste, but say so
        self.assertTrue(any("999" in n for n in r["notes"]))

    def test_bad_link_raises_before_any_network(self):
        with mock.patch.object(core, "http_get") as hg:
            with self.assertRaises(ValueError):
                core.manual_resolve("942942", 643828, "https://netflix.com/x")
        hg.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
