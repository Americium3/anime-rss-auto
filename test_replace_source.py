import anime_rss as c


def test_drop_manual_imports_keeps_new_and_other_shows(monkeypatch):
    imports = {
        "a" * 40: {"bgm_id": 7, "name": "old"},
        "b" * 40: {"bgm_id": 7, "name": "new"},
        "c" * 40: {"bgm_id": 8, "name": "other show"},
    }
    deleted, saved = [], {}
    monkeypatch.setattr(c, "load_manual_imports", lambda: dict(imports))
    monkeypatch.setattr(c, "save_manual_imports", lambda d: saved.update(d))
    monkeypatch.setattr(c, "qb_get_json", lambda path: [
        {"hash": path.rsplit("=", 1)[1], "name": "old.mkv", "save_path": "X:/s", "content_path": "X:/s/old.mkv"}])
    monkeypatch.setattr(c, "qb_post", lambda path, data: deleted.append(data["hashes"]))
    monkeypatch.setattr(c, "mirror_unlink", lambda *a: 0)
    monkeypatch.setattr(c, "add_event", lambda *a, **k: None)
    notes = []
    assert c.drop_manual_imports(7, "b" * 40, notes) == 1
    assert deleted == ["a" * 40]
    assert set(saved) == {"b" * 40, "c" * 40}


def test_drop_manual_imports_keeps_record_when_qb_fails(monkeypatch):
    imports = {"a" * 40: {"bgm_id": 7, "name": "old"}}
    saved = []
    monkeypatch.setattr(c, "load_manual_imports", lambda: dict(imports))
    monkeypatch.setattr(c, "save_manual_imports", lambda d: saved.append(d))
    def boom(path):
        raise RuntimeError("qB down")
    monkeypatch.setattr(c, "qb_get_json", boom)
    notes = []
    assert c.drop_manual_imports(7, None, notes) == 0
    assert not saved and any("could not remove" in n for n in notes)
