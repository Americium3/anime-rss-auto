import anime_rss as c


SAKURATO = [
    "[桜都字幕组] 飙马野郎 JOJO的奇妙冒险 / Steel Ball Run： JoJo no Kimyou na Bouken [03][1080P][简繁内封]",
    "[桜都字幕组] 飙马野郎 JOJO的奇妙冒险 / Steel Ball Run： JoJo no Kimyou na Bouken [03][1080P][繁体内嵌]",
    "[桜都字幕组] 飙马野郎 JOJO的奇妙冒险 / Steel Ball Run： JoJo no Kimyou na Bouken [03][1080P][简体内嵌]",
    "[桜都字幕组] 飙马野郎 JOJO的奇妙冒险 / Steel Ball Run： JoJo no Kimyou na Bouken [02][1080P][简繁内封]",
    "[桜都字幕组] 飙马野郎 JOJO的奇妙冒险 / Steel Ball Run： JoJo no Kimyou na Bouken [02][1080P][繁体内嵌]",
    "[桜都字幕组] 飙马野郎 JOJO的奇妙冒险 / Steel Ball Run： JoJo no Kimyou na Bouken [02][1080P][简体内嵌]",
]


def test_soft_subbed_simplified_tag_wins_one_per_episode():
    assert c.pick_one_per_episode_tag(SAKURATO) == "简繁内封"


def test_no_single_tag_falls_back_to_none():
    titles = [
        "[X] Show - 01 [1080p][简日双语]", "[X] Show - 01 [1080p][简日双语][v2]",
        "[X] Show - 02 [1080p][简日双语]",
    ]
    assert c.pick_one_per_episode_tag(titles) is None


def test_hard_subbed_only_group_still_gets_one_tag():
    titles = [t for t in SAKURATO if "简体内嵌" in t]
    assert c.pick_one_per_episode_tag(titles) == "简体内嵌"


def test_configured_filter_beats_the_live_feed(monkeypatch):
    monkeypatch.setitem(c.GROUP_FILTER, 370, "LoliHouse")
    monkeypatch.setattr(c, "http_get", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert c.switch_must_contain(1, 370) == "LoliHouse"


def test_unreachable_feed_keeps_the_generic_whitelist(monkeypatch):
    def boom(*a, **k):
        raise OSError("down")
    monkeypatch.setattr(c, "http_get", boom)
    monkeypatch.setitem(c.GROUP_FILTER, 99999, "")
    assert c.switch_must_contain(1, 99999) == c.CJK_SUB_REQUIRED
