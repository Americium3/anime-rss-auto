import json

import anime_rss as c

FEED = "https://mikanani.me/RSS/Bangumi?bangumiId=3887&subgroupid=203"
SAVE = r"X:\Bangumi\2026.10\Steel Ball Run"
H_SIMP, H_TRAD, H_SOFT = "1" * 40, "2" * 40, "3" * 40


def _title(tag, ep="01"):
    return f"[桜都字幕组] JOJO的奇妙冒险：飙马野郎 [{ep}][1080P][{tag}]"


def _art(tag, h, read=True, ep="01"):
    return {"title": _title(tag, ep), "link": f"https://mikanani.me/Home/Episode/{h}",
            "isRead": read}


def _rule(mc="简繁内封", mnc="先行|ABEMA|B-Global|BGlobal"):
    return {"affectedFeeds": [FEED], "savePath": SAVE, "mustContain": mc,
            "mustNotContain": mnc, "useRegex": False,
            "torrentParams": {"tags": ["2026.10"], "save_path": SAVE.replace("\\", "/")}}


def _store(monkeypatch, tmp_path, data=None):
    p = tmp_path / "custom_keywords.json"
    if data is not None:
        p.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(c, "CUSTOM_KEYWORDS_PATH", p)
    return p


def test_qb_filter_semantics():
    t = _title("简繁内封")
    assert c.qb_filter_match("简繁内封", t)
    assert c.qb_filter_match("1080p 内封", t)           # space = AND, case-insensitive
    assert not c.qb_filter_match("720p 内封", t)
    assert c.qb_filter_match("繁体内嵌|简繁内封", t)    # | = OR
    assert c.qb_filter_match("JOJO*飙马", t)            # wildcard
    assert c.qb_filter_match("", t)


def test_brackets_become_separators():
    assert c.normalize_keyword("[1080P][简繁内封]") == "1080P 简繁内封"
    assert c.normalize_keyword(" 繁体内嵌 | [CR] ") == "繁体内嵌|CR"
    assert c.normalize_keyword("  ") == ""


def test_store_is_per_show_per_group(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path)
    assert c.set_custom_keyword(7, 203, "[简繁内封]") == "简繁内封"
    c.set_custom_keyword(7, 583, "CR")
    assert c.custom_keyword(7, 203) == "简繁内封"
    assert c.custom_keyword(7, 583) == "CR"
    assert c.custom_keyword(7, 1) == ""
    c.set_custom_keyword(7, 203, "")
    assert c.custom_keyword(7, 203) == ""
    c.set_custom_keyword(7, 583, "")
    assert c.load_custom_keywords() == {}


def test_keyword_drops_only_conflicting_exclusions():
    mnc = "先行|ABEMA|B-Global|BGlobal"
    assert c.drop_keyword_conflicts(mnc, "ABEMA") == "先行|B-Global|BGlobal"
    assert c.drop_keyword_conflicts(mnc, "简繁内封") == mnc
    assert c.drop_keyword_conflicts(mnc, "") == mnc


def test_make_rule_def_puts_keyword_over_auto_filter():
    rd = c.make_rule_def("X", "2026.10", FEED, c.CJK_SUB_REQUIRED, keyword="ABEMA")
    assert rd["mustContain"] == "ABEMA"
    assert "ABEMA" not in rd["mustNotContain"].split("|")
    assert "先行" in rd["mustNotContain"].split("|")


def test_cjk_pass_leaves_a_keyword_that_looks_like_the_whitelist(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path, {"7": {"203": "简"}})
    rules = {"R": _rule(mc="简")}
    writes = []
    monkeypatch.setattr(c, "existing_rules", lambda: rules)
    monkeypatch.setattr(c, "rule_bgm_id", lambda rdef, cache: 7)
    monkeypatch.setattr(c, "qb_post", lambda path, data: writes.append(data))
    assert c.reconcile_rule_cjk_whitelist() == 0
    assert not writes


def test_blacklist_pass_does_not_re_add_what_the_keyword_asks_for(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path, {"7": {"203": "ABEMA"}})
    rules = {"R": _rule(mc="ABEMA", mnc="先行|B-Global|BGlobal")}
    writes = []
    monkeypatch.setattr(c, "existing_rules", lambda: rules)
    monkeypatch.setattr(c, "rule_bgm_id", lambda rdef, cache: 7)
    monkeypatch.setattr(c, "qb_post", lambda path, data: writes.append(data))
    assert c.reconcile_rule_blacklist() == 0
    assert not writes


def test_reconcile_writes_keyword_onto_rule(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path, {"7": {"203": "繁体内嵌"}})
    rules = {"R": _rule()}
    writes = []
    monkeypatch.setattr(c, "existing_rules", lambda: rules)
    monkeypatch.setattr(c, "rule_bgm_id", lambda rdef, cache: 7)
    monkeypatch.setattr(c, "qb_post", lambda path, data: writes.append(json.loads(data["ruleDef"])))
    assert c.reconcile_custom_keywords() == 1
    assert writes[0]["mustContain"] == "繁体内嵌"
    rules["R"] = writes[0]
    assert c.reconcile_custom_keywords() == 0          # idempotent


def test_keyword_for_another_group_is_dormant(monkeypatch, tmp_path):
    _store(monkeypatch, tmp_path, {"7": {"583": "CR"}})
    rules = {"R": _rule()}
    monkeypatch.setattr(c, "existing_rules", lambda: rules)
    monkeypatch.setattr(c, "rule_bgm_id", lambda rdef, cache: 7)
    monkeypatch.setattr(c, "qb_post", lambda *a: (_ for _ in ()).throw(AssertionError("wrote")))
    assert c.reconcile_custom_keywords() == 0


def _dedup_env(monkeypatch, tmp_path, torrents, articles, kw="繁体内嵌"):
    _store(monkeypatch, tmp_path, {"7": {"203": kw}})
    rules = {"R": _rule(mc=kw)}
    deleted = []

    def qb_get_json(path):
        if path.startswith("/api/v2/rss/items"):
            return {"2026.10": {"JOJO": {"url": FEED, "articles": articles}}}
        return torrents

    monkeypatch.setattr(c, "existing_rules", lambda: rules)
    monkeypatch.setattr(c, "rule_bgm_id", lambda rdef, cache: 7)
    monkeypatch.setattr(c, "qb_get_json", qb_get_json)
    monkeypatch.setattr(c, "qb_post", lambda path, data: deleted.append(data["hashes"]))
    return deleted


def _tor(h, name, progress=1.0):
    return {"hash": h, "name": name, "progress": progress,
            "save_path": SAVE.replace("\\", "/"), "content_path": ""}


def test_dedup_keeps_the_keyword_version_over_a_better_ranked_one(monkeypatch, tmp_path):
    # 简 outranks 繁 in LANG_PRIORITY; the keyword asks for 繁 and must win.
    torrents = [_tor(H_SIMP, _title("简体内嵌") + ".mkv"), _tor(H_TRAD, _title("繁体内嵌") + ".mkv")]
    arts = [_art("简体内嵌", H_SIMP), _art("繁体内嵌", H_TRAD)]
    deleted = _dedup_env(monkeypatch, tmp_path, torrents, arts)
    assert c.prefer_variant_dedup() == 1
    assert deleted == [H_SIMP]


def test_dedup_recognises_the_keyword_version_by_hash_when_the_file_is_named_differently(monkeypatch, tmp_path):
    torrents = [_tor(H_SIMP, "[Sakurato] SBR - 01 [CHS].mkv"), _tor(H_TRAD, "[Sakurato] SBR - 01 [CHT].mkv")]
    arts = [_art("简体内嵌", H_SIMP), _art("繁体内嵌", H_TRAD)]
    deleted = _dedup_env(monkeypatch, tmp_path, torrents, arts)
    c.prefer_variant_dedup()
    assert deleted == [H_SIMP]


def test_dedup_waits_for_the_keyword_version_to_finish(monkeypatch, tmp_path):
    torrents = [_tor(H_SIMP, _title("简体内嵌") + ".mkv"), _tor(H_TRAD, _title("繁体内嵌") + ".mkv", 0.4)]
    arts = [_art("简体内嵌", H_SIMP), _art("繁体内嵌", H_TRAD)]
    deleted = _dedup_env(monkeypatch, tmp_path, torrents, arts)
    assert c.prefer_variant_dedup() == 0
    assert not deleted


def test_hard_reject_spares_a_keyword_hit(monkeypatch, tmp_path):
    name = "[X] SBR - 01 [NF WEB-DL 1080p].mkv"
    torrents = [_tor(H_SOFT, name)]
    arts = [{"title": "[X] SBR - 01 [NF WEB-DL]", "link": f"https://mikanani.me/Home/Episode/{H_SOFT}"}]
    deleted = _dedup_env(monkeypatch, tmp_path, torrents, arts, kw="NF WEB-DL")
    monkeypatch.setattr(c, "load_manual_imports", lambda: {})
    monkeypatch.setattr(c, "mirror_unlink", lambda *a: 0)
    assert c.reject_hard_variants() == 0
    assert not deleted


def test_backfill_re_adds_a_read_article_missing_from_qb(monkeypatch, tmp_path):
    arts = [_art("繁体内嵌", H_TRAD, read=True), _art("繁体内嵌", "4" * 40, read=False, ep="02"),
            _art("简体内嵌", H_SIMP, read=True)]
    added = []
    monkeypatch.setattr(c, "qb_get_json", lambda path: (
        {"2026.10": {"JOJO": {"url": FEED, "articles": arts}}}
        if path.startswith("/api/v2/rss/items") else []))
    monkeypatch.setattr(c, "qb_add_magnet", lambda m, sp, season: added.append(m))
    got = c.keyword_backfill(_rule(mc="繁体内嵌"), "繁体内嵌")
    # The unread ep 02 is qB's own job once the rule changes; only the read ep 01 is ours.
    assert added == [f"magnet:?xt=urn:btih:{H_TRAD}"]
    assert len(got) == 1
