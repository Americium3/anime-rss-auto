#!/usr/bin/env python3
"""Local web control panel for anime-rss-auto.

Serves a single-page dashboard (static/index.html) plus a small JSON API that
aggregates state from bangumi.tv, mikan, qBittorrent and Jellyfin by reusing
anime_rss.py directly. Read-mostly; the only mutating actions are:

  POST /api/sync            run one sync pass in a background thread
  POST /api/grace/expire    end a show's ANi grace period early (lock next pass)
  POST /api/rule/switch     re-point an existing qB rule at another subgroup

Run:  python webui.py          (default http://127.0.0.1:8767)
Config keys (config.local.json): webui_port, webui_host.
"""
from __future__ import annotations

import contextlib
import datetime
import html
import io
import json
import os
import re
import threading
import time
import traceback
import urllib.request
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

import anime_rss as core

ROOT = Path(__file__).parent
HOST = str(core.CONFIG.get("webui_host", "127.0.0.1"))
PORT = int(os.environ.get("PORT") or core.CONFIG.get("webui_port", 8767))
WATCH_LOG = ROOT / "watch.log"

app = FastAPI(title="anime-rss-auto control panel", docs_url=None, redoc_url=None)

# Caches that survive between polls (mikan pages are slow-ish to fetch).
_mikan_bgm: dict[int, int | None] = {}          # mikan_id -> bgm_id
_group_names: dict[int, str] = dict(core.GROUP_NAME)  # subgroup id -> display name
_scanned_mids: set[int] = set()                 # mikan ids whose page we already parsed for group names


# --------------------------------------------------------------------------- #
# AniList airing time + English titles (self-maintaining; bgm has neither)
# --------------------------------------------------------------------------- #
# bgm 只有放送"日期"没有"时间"，故精确到分钟的开播时刻从 AniList 取——它给的是
# 绝对 unix 时间戳（ep 放送时刻），前端按浏览器本地时区渲染。同一个查询顺带取
# 英文/罗马字名（title{english romaji}），供英文界面显示番名。按标题搜、结果落
# airing_cache.json；在看/想看同步查，其余类型由后台线程慢速补全；搜不到就回退。
AIRING_CACHE_PATH = ROOT / "airing_cache.json"
_ANILIST_URL = "https://graphql.anilist.co"
_ANILIST_Q = ("query($s:String){Media(search:$s,type:ANIME){"
              "title{english romaji}"
              "airingSchedule(perPage:8){nodes{episode airingAt}}"
              "nextAiringEpisode{episode airingAt} startDate{year month day}}}")


def _load_airing_cache() -> dict:
    try:
        return json.loads(AIRING_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _save_airing_cache(c: dict) -> None:
    try:
        AIRING_CACHE_PATH.write_text(json.dumps(c, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _anilist_media(search: str) -> dict | None:
    body = json.dumps({"query": _ANILIST_Q, "variables": {"s": search}}).encode()
    req = urllib.request.Request(
        _ANILIST_URL, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": core.UA},
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return (d.get("data") or {}).get("Media")


def show_air_info(bgm_id: int, jp: str, cn: str, cache: dict) -> dict:
    """{'at': unix ts of ep1's broadcast or None, 'en': English/romaji title or None}.

    Cached per bgm_id in airing_cache.json. A known time is kept indefinitely; a
    miss (None) is retried after a day in case AniList adds the schedule later.
    Entries written before the 'en' field existed are refreshed once.
    """
    key = str(bgm_id)
    now = int(time.time())
    ent = cache.get(key)
    if ent and "en" in ent and (ent.get("at") is not None or now - ent.get("t", 0) < 86400):
        return ent
    at = en = None
    for term in (jp, cn):
        if not term:
            continue
        try:
            m = _anilist_media(term)
        except Exception:  # noqa: BLE001
            m = None
        if not m:
            continue
        title = m.get("title") or {}
        en = en or title.get("english") or title.get("romaji")
        nodes = (m.get("airingSchedule") or {}).get("nodes") or []
        ep1 = next((n for n in nodes if n.get("episode") == 1), None)
        if ep1 and ep1.get("airingAt"):
            at = int(ep1["airingAt"]); break
        nx = m.get("nextAiringEpisode")
        if nx and nx.get("airingAt"):
            at = int(nx["airingAt"]); break
        sd = m.get("startDate") or {}
        if sd.get("year") and sd.get("month") and sd.get("day"):  # JST midnight fallback
            dt = (datetime.datetime(sd["year"], sd["month"], sd["day"],
                                    tzinfo=datetime.timezone.utc)
                  - datetime.timedelta(hours=9))
            at = int(dt.timestamp()); break
    ent = {"at": at, "en": en, "t": now}
    cache[key] = ent
    return ent


# English titles for finished/on-hold/dropped shows are filled lazily in the
# background — doing hundreds of AniList lookups inline would stall the panel.
_title_fill_running = False
_title_fill_lock = threading.Lock()


def _start_title_fill(items: list[tuple[int, str, str]]) -> None:
    global _title_fill_running
    with _title_fill_lock:
        if _title_fill_running or not items:
            return
        _title_fill_running = True

    def run() -> None:
        global _title_fill_running
        try:
            cache = _load_airing_cache()
            for i, (bid, jp, cn) in enumerate(items):
                if "en" not in (cache.get(str(bid)) or {}):
                    show_air_info(bid, jp, cn, cache)
                    time.sleep(0.8)  # stay far under AniList's rate limit
                if i % 20 == 19:
                    _save_airing_cache(cache)
            _save_airing_cache(cache)
        finally:
            _title_fill_running = False

    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------- #
# Aggregation helpers
# --------------------------------------------------------------------------- #
def bgm_watching_rich(user: str) -> list[dict]:
    """Like core.bgm_watching but keeps the cover image URL."""
    out, offset = [], 0
    while True:
        url = (
            f"{core.BGM_API}/v0/users/{user}/collections"
            f"?subject_type=2&type=3&limit=50&offset={offset}"
        )
        d = json.loads(core.http_get(url).decode("utf-8", "replace"))
        data = d.get("data", [])
        for x in data:
            s = x.get("subject", {})
            img = s.get("images") or {}
            out.append({
                "bgm_id": x.get("subject_id"),
                "name": s.get("name", ""),
                "name_cn": s.get("name_cn", ""),
                "date": s.get("date", ""),
                "eps": s.get("eps") or None,
                "image": img.get("common") or img.get("medium") or "",
                "score": s.get("score") or None,
            })
        offset += len(data)
        if offset >= d.get("total", 0) or not data:
            break
    return out


def rule_subgroup(rdef: dict) -> tuple[int | None, int | None]:
    """(mikan_id, subgroup_id) parsed from a rule's first mikan feed URL."""
    for f in rdef.get("affectedFeeds", []):
        m = re.search(r"bangumiId=(\d+)&subgroupid=(\d+)", f)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None, None


def last_sync_time() -> str | None:
    """Timestamp of the newest '=== sync @ ...' line in watch.log."""
    try:
        raw = WATCH_LOG.read_bytes()[-20000:].decode("utf-8", "replace")
        stamps = re.findall(r"=== sync @ (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", raw)
        return stamps[-1] if stamps else None
    except Exception:  # noqa: BLE001
        return None


def mikan_subgroups_named(mikan_id: int) -> list[dict]:
    """[{id, name}] for every subtitle group on a mikan bangumi page."""
    html_txt = core.http_get(f"{core.MIKAN}/Home/Bangumi/{mikan_id}").decode("utf-8", "replace")
    # Each group renders as <div class="subgroup-text" id="{subgroupid}"> whose
    # inner text is the display name — either plain text (raw / unnamed groups
    # like "生肉/不明字幕") or a /Home/PublishGroup/<pubid> link. The PublishGroup
    # id is NOT the subgroupid, so the name must be keyed by the block's id (the
    # real subgroupid); the old link-only regex both mismatched those and missed
    # link-less groups entirely.
    for gid, inner in re.findall(
        r'<div class="subgroup-text" id="(\d+)">(.*?)<a[^>]*class="mikan-rss"',
        html_txt, re.S,
    ):
        name = html.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        if name:
            _group_names.setdefault(int(gid), name)
    ids = sorted({int(x) for x in re.findall(r"subgroupid=(\d+)", html_txt)})
    # name=None for unknown groups: the label language is the frontend's call.
    return [{"id": i, "name": _group_names.get(i)} for i in ids]


def ensure_group_name(mikan_id: int | None, gid: int | None) -> str | None:
    """Resolve gid -> display name, fetching the mikan page once if the cache
    misses. Non-priority groups (e.g. #202) aren't seeded at startup, so the
    overview would otherwise show a bare id until the user opens the dropdown."""
    if not gid:
        return None
    if gid not in _group_names and mikan_id and mikan_id not in _scanned_mids:
        try:
            mikan_subgroups_named(mikan_id)  # fills _group_names as a side effect
        except Exception:  # noqa: BLE001 — network/parse failure degrades to "#id"
            pass
        finally:
            # Mark scanned regardless so a permanently-nameless group isn't
            # re-fetched on every single poll.
            _scanned_mids.add(mikan_id)
    return _group_names.get(gid)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
@app.get("/api/overview")
def api_overview():
    user = str(core.CONFIG.get("bgm_user"))
    season_now = core.current_season()
    shows = bgm_watching_rich(user)
    rules = core.existing_rules()
    grace = core.load_grace()

    qb_ok = True
    try:
        torrents = core.qb_get_json("/api/v2/torrents/info")
    except Exception:  # noqa: BLE001
        torrents = []
        qb_ok = False  # let the panel distinguish "qB down" from "empty feed"

    # rule name -> bgm id (mikan page fetches are cached across polls)
    rule_of: dict[int, tuple[str, dict]] = {}
    for rname, rdef in rules.items():
        bid = core.rule_bgm_id(rdef, _mikan_bgm)
        if bid:
            rule_of[bid] = (rname, rdef)

    airing_cache = _load_airing_cache()
    prem_cache: dict[int, str | None] = {}
    out_shows = []
    for s in shows:
        season = core.season_of(s["date"])
        eps = s.get("eps")
        pinned = s["bgm_id"] in core.PIN_CURRENT_BGM_IDS
        is_old = core.is_manual_old_show(s["date"], s["bgm_id"], eps)
        entry = {
            "bgm_id": s["bgm_id"],
            "title": s["name_cn"] or s["name"],
            "title_jp": s["name"],
            "date": s["date"],
            "season": season,
            "pinned": pinned,
            "cour_kind": core.cour_kind(eps),
            "long_current": core.long_still_airing(season, eps),
            "image": s["image"],
            "score": s.get("score"),
            "status": "unresolved",
            "rule": None,
            "grace": None,
            "torrents": [],
        }
        if is_old:
            entry["status"] = "manual"
        hit = rule_of.get(s["bgm_id"])
        if hit:
            rname, rdef = hit
            mid, gid = rule_subgroup(rdef)
            save_path = rdef.get("savePath", "")
            entry["rule"] = {
                "name": rname,
                "mikan_id": mid,
                "subgroup": gid,
                "subgroup_name": ensure_group_name(mid, gid),
            }
            entry["status"] = "subscribed"
            # The panel only shows an n/m-ready summary — ship progress alone.
            norm = save_path.replace("\\", "/").rstrip("/").lower()
            for t in torrents:
                sp = (t.get("save_path") or "").replace("\\", "/").rstrip("/").lower()
                if sp == norm:
                    entry["torrents"].append(
                        {"progress": round(float(t.get("progress", 0)), 4)})
        g = grace.get(str(s["bgm_id"]))
        if g is not None and entry["status"] != "subscribed":
            entry["status"] = "grace"
            entry["grace"] = {"expires": g + core.GRACE_HOURS * 3600}
        entry["premiere_date"] = core.show_premiere_date(s["bgm_id"], s["date"], prem_cache)
        air = show_air_info(s["bgm_id"], s["name"], s["name_cn"], airing_cache)
        entry["airing_at"] = air["at"]
        entry["title_en"] = air["en"]
        out_shows.append(entry)
    _save_airing_cache(airing_cache)

    return {
        "season": season_now,
        "grace_hours": core.GRACE_HOURS,
        "qb_ok": qb_ok,
        "group_priority": [
            {"id": gid, "name": core.GROUP_NAME[gid]} for gid in core.PRIORITY_IDS
        ],
        "last_sync": last_sync_time(),
        "sync_running": _sync_running,
        "shows": out_shows,
    }


_collections_cache: dict = {"data": None, "ts": 0.0}
# bgm collection type -> stable key used by the frontend filter.
_COLL_TYPES = {3: "watching", 1: "want", 2: "done", 4: "onhold", 5: "dropped"}


@app.get("/api/collections")
def api_collections():
    """All anime the user has marked on bangumi, grouped by collection type.

    Basic fields for every show; 在看/想看 additionally get a precise airing time
    (AniList) + bgm premiere date so the panel can show local-timezone premieres.
    Cached ~2 min — one poll fans out to 5 bgm list calls + a few cached AniList hits.
    """
    now = time.time()
    if _collections_cache["data"] and now - _collections_cache["ts"] < 120:
        return _collections_cache["data"]
    user = str(core.CONFIG.get("bgm_user"))
    airing_cache = _load_airing_cache()
    prem_cache: dict[int, str | None] = {}
    groups: dict[str, list] = {}
    counts: dict[str, int] = {}
    backfill: list[tuple[int, str, str]] = []
    for t in (3, 1, 2, 4, 5):
        try:
            shows = core.bgm_collection_subjects(user, t)
        except Exception:  # noqa: BLE001
            shows = []
        counts[_COLL_TYPES[t]] = len(shows)
        lst = []
        for s in shows:
            cached = airing_cache.get(str(s["bgm_id"])) or {}
            e = {
                "bgm_id": s["bgm_id"],
                "type": _COLL_TYPES[t],
                "title": s["name_cn"] or s["name"],
                "title_jp": s["name"],
                "title_en": cached.get("en"),
                "date": s["date"],
                "season": core.season_of(s["date"]),
                "pinned": s["bgm_id"] in core.PIN_CURRENT_BGM_IDS,
                "cour_kind": core.cour_kind(s.get("eps")),
                "long_current": core.long_still_airing(core.season_of(s["date"]), s.get("eps")),
                "image": s.get("image", ""),
                "score": s.get("score"),
                "updated_at": s.get("updated_at"),
                "airing_at": None,
                "premiere_date": None,
            }
            if t in (3, 1):  # 在看/想看：只对当前/即将播的番查精确开播时间
                e["premiere_date"] = core.show_premiere_date(s["bgm_id"], s["date"], prem_cache)
                air = show_air_info(s["bgm_id"], s["name"], s["name_cn"], airing_cache)
                e["airing_at"] = air["at"]
                e["title_en"] = air["en"]
            elif "en" not in cached:
                backfill.append((s["bgm_id"], s["name"], s["name_cn"]))
            lst.append(e)
        groups[_COLL_TYPES[t]] = lst
    _save_airing_cache(airing_cache)
    _start_title_fill(backfill)
    out = {"groups": groups, "counts": counts}
    _collections_cache["data"] = out
    _collections_cache["ts"] = now
    return out


@app.get("/api/logs")
def api_logs(lines: int = 120):
    try:
        raw = WATCH_LOG.read_bytes()[-200000:].decode("utf-8", "replace")
        return {"lines": raw.splitlines()[-max(10, min(lines, 1000)):]}
    except Exception as ex:  # noqa: BLE001
        return {"lines": [f"(cannot read watch.log: {ex})"]}


@app.get("/api/subgroups/{mikan_id}")
def api_subgroups(mikan_id: int):
    try:
        return {"subgroups": mikan_subgroups_named(mikan_id)}
    except Exception as ex:  # noqa: BLE001
        # {"code": ...} details render localized in the panel (errText).
        raise HTTPException(502, {"code": "mikan_fetch_failed", "message": str(ex)})


@app.get("/api/notifications")
def api_notifications():
    """Premiere notifications (newest first) written by core.premiere_watch_pass."""
    items = list(reversed(core.load_notifications()))
    return {"notifications": items}


class NotifyRead(BaseModel):
    bgm_id: int | None = None  # None = mark every notification read


@app.post("/api/notifications/read")
def api_notifications_read(body: NotifyRead):
    items = core.load_notifications()
    for it in items:
        if body.bgm_id is None or it.get("bgm_id") == body.bgm_id:
            it["read"] = True
    core.save_notifications(items)
    return {"ok": True}


# --- mutating actions ------------------------------------------------------ #
_sync_running = False
_sync_error = False
_sync_buf: io.StringIO | None = None


@app.post("/api/sync")
def api_sync():
    global _sync_running, _sync_error, _sync_buf
    if _sync_running:
        return {"started": False, "code": "already_running", "reason": "sync already running"}
    _sync_running = True
    _sync_error = False
    _sync_buf = io.StringIO()

    def run():
        global _sync_running, _sync_error
        try:
            with contextlib.redirect_stdout(_sync_buf):
                core.run_sync_once(
                    str(core.CONFIG.get("bgm_user")),
                    core.CONFIG.get("mikan_cookie"),
                    core.current_season(),
                    bool(core.CONFIG.get("purge_dropped_files")),
                    core.bgm_token(None),
                )
        except Exception:  # noqa: BLE001
            traceback.print_exc(file=_sync_buf)
            _sync_error = True  # the panel toasts a failure and opens the log
        finally:
            _sync_running = False

    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


@app.get("/api/sync/status")
def api_sync_status():
    return {
        "running": _sync_running,
        "ok": not _sync_error,
        "output": _sync_buf.getvalue() if _sync_buf else "",
    }


class GraceExpire(BaseModel):
    bgm_id: int


@app.post("/api/grace/expire")
def api_grace_expire(body: GraceExpire):
    grace = core.load_grace()
    key = str(body.bgm_id)
    if key not in grace:
        raise HTTPException(404, {"code": "not_in_grace"})
    grace[key] = 0.0  # expired -> next sync pass locks the best available group
    core.save_grace(grace)
    return {"ok": True, "code": "grace_ended",
            "note": "grace ended; next sync pass (<=5 min) locks the best available group"}


class RuleSwitch(BaseModel):
    rule_name: str
    subgroup: int


@app.post("/api/rule/switch")
def api_rule_switch(body: RuleSwitch):
    """Re-point an existing rule at another subtitle group (feed + filter + mikan sub)."""
    rules = core.existing_rules()
    rdef = rules.get(body.rule_name)
    if not rdef:
        raise HTTPException(404, {"code": "no_rule", "message": body.rule_name})
    mid, old_gid = rule_subgroup(rdef)
    if not mid:
        raise HTTPException(400, {"code": "no_mikan_feed"})
    if old_gid == body.subgroup:
        return {"ok": True, "code": "switched",
                "group": _group_names.get(body.subgroup, str(body.subgroup)),
                "note": "already on that subgroup"}

    notes = []
    old_feed = core.feed_url(mid, old_gid)
    new_feed = core.feed_url(mid, body.subgroup)
    season = (rdef.get("torrentParams", {}).get("tags") or [core.current_season()])[0]

    # 0) full-replace: nuke the OLD group's downloaded files so the NEW group
    #    replaces it episode for episode. mikan's per-subgroup RSS is full, so
    #    the swapped feed re-grabs the whole season into the emptied folder.
    #    Guarded by the season cutoff — pre-cutoff shows are hand-managed and are
    #    NEVER touched destructively (see SKIP_BEFORE_SEASON).
    deleted = 0
    save_path = rdef.get("savePath", "")
    if save_path and str(season) >= core.SKIP_BEFORE_SEASON:
        try:
            victims = core.qb_torrents_under(save_path)
            if victims:
                core.qb_post(
                    "/api/v2/torrents/delete",
                    {"hashes": "|".join(t["hash"] for t in victims),
                     "deleteFiles": "true"},
                )
                deleted = len(victims)
        except Exception as ex:  # noqa: BLE001
            notes.append(f"delete old files: {ex}")
    elif save_path:
        notes.append(f"kept old files (旧番 {season} < {core.SKIP_BEFORE_SEASON})")

    # 1) swap RSS feed items (remove old first: same tree path)
    feed_paths = core.rss_feed_paths()
    old_path = feed_paths.get(old_feed)
    if old_path:
        try:
            core.qb_post("/api/v2/rss/removeItem", {"path": old_path})
        except Exception as ex:  # noqa: BLE001
            notes.append(f"removeItem: {ex}")
    try:
        title = core.mikan_bangumi_info(mid)["title"]
    except Exception:  # noqa: BLE001
        title = f"Mikan Project - {mid}"
    try:
        core.qb_post("/api/v2/rss/addFeed",
                     {"url": new_feed, "path": old_path or f"{season}\\{title}"})
    except Exception as ex:  # noqa: BLE001
        notes.append(f"addFeed: {ex}")

    # 2) rewrite the rule
    rdef["affectedFeeds"] = [new_feed]
    rdef["mustContain"] = core.GROUP_FILTER.get(body.subgroup, "")
    # We just deleted the whole folder, so let qB re-match every episode of the
    # new feed instead of skipping ones it "already grabbed" under the old group.
    rdef["previouslyMatchedEpisodes"] = []
    try:
        core.qb_post("/api/v2/rss/setRule",
                     {"ruleName": body.rule_name, "ruleDef": json.dumps(rdef)})
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, {"code": "setrule_failed", "message": str(ex)})

    # 3) move the mikan subscription (best effort)
    cookie = core.CONFIG.get("mikan_cookie")
    if cookie:
        for fn, gid in ((core.mikan_unsubscribe, old_gid), (core.mikan_subscribe, body.subgroup)):
            try:
                fn(cookie, mid, gid)
            except Exception as ex:  # noqa: BLE001
                notes.append(f"mikan {fn.__name__}: {ex}")

    # 4) prune the Jellyfin mirror right away so the old group's hardlinks don't
    #    linger until the next watch cycle (源已在 step 0 删过 -> 现在把镜像对齐).
    try:
        core.mirror_prune_orphan_files()
    except Exception as ex:  # noqa: BLE001
        notes.append(f"mirror-prune: {ex}")

    grp = _group_names.get(body.subgroup, str(body.subgroup))
    return {"ok": True, "code": "switched", "group": grp, "notes": notes,
            "deleted": deleted, "note": f"rule now follows {grp}"}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


if __name__ == "__main__":
    print(f"=== anime-rss-auto webui on http://{HOST}:{PORT} ===", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
