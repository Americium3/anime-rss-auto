#!/usr/bin/env python3
"""Local web control panel for anime-rss-auto.

Serves a single-page dashboard (static/index.html) plus a small JSON API that
aggregates state from bangumi.tv, mikan, qBittorrent and Jellyfin by reusing
anime_rss.py directly. Read-mostly; the only mutating actions are:

  POST /api/sync            run one sync pass in a background thread
  POST /api/grace/expire    end a show's ANi grace period early (lock next pass)
  POST /api/rule/switch     re-point an existing qB rule at another subgroup
  POST /api/unresolved/resolve  bind an unmatched show to a pasted mikan link

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
import urllib.error
import urllib.request
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
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


def _seed_mikan_bgm() -> None:
    """Seed mikan_id -> bgm_id from the persistent resolve cache the sync pipeline
    maintains (mikan_resolve_cache.json), so the FIRST /api/overview after a webui
    restart doesn't refetch ~15 mikan pages via rule_bgm_id (~12s cold). Reverse of
    the bgm_id -> mikan_id entries; positives only. Best-effort."""
    try:
        for skey, v in core.load_resolve_cache().items():
            mid = v.get("mikan_id")
            if mid:
                _mikan_bgm.setdefault(int(mid), int(skey))
    except Exception:  # noqa: BLE001
        pass


_seed_mikan_bgm()


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
              " title{english romaji} status"
              " airingSchedule(perPage:8){nodes{episode airingAt}}"
              " nextAiringEpisode{episode airingAt}"
              " startDate{year month day} endDate{year month day}}}")
# Adjacent string literals concatenate silently, so a seam that loses its space
# fuses two fields into one that doesn't exist — "episodesairingSchedule" once
# made AniList 400 every lookup for three weeks without a peep. Continuation
# lines lead with a space, and this refuses to boot if a seam ever glues two
# field names together again.
assert all(re.search("[{ ]" + _f + "[({ ]", _ANILIST_Q) for _f in
           ("title", "status", "airingSchedule", "nextAiringEpisode",
            "startDate", "endDate")), f"_ANILIST_Q lost a seam space: {_ANILIST_Q}"

# A lookup that found a broadcast time is cached forever — slots don't move. A
# lookup AniList answered with "nothing scheduled" is retried this often, since it
# schedules shows days to weeks ahead of the premiere. (A lookup AniList never
# answered isn't cached at all; see _NO_ANSWER.)
_AIR_MISS_TTL = 6 * 3600


def _load_airing_cache() -> dict:
    try:
        return json.loads(AIRING_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


_airing_write_lock = threading.Lock()


def _save_airing_cache(c: dict) -> None:
    """Merge `c` into what's on disk, newest entry per show wins, then swap the file
    in atomically. Request threads and the fill thread each work on their own copy,
    so a plain overwrite would throw away whichever finished first — and the
    read-merge-write has to be serialised or two writers still lose each other's
    entries between the read and the write."""
    with _airing_write_lock:
        try:
            merged = _load_airing_cache()
            for k, v in c.items():
                old = merged.get(k)
                if not old or v.get("t", 0) >= old.get("t", 0):
                    merged[k] = v
            tmp = AIRING_CACHE_PATH.with_suffix(AIRING_CACHE_PATH.suffix + ".tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, AIRING_CACHE_PATH)
        except Exception:  # noqa: BLE001
            pass


# Consecutive AniList faults, surfaced in /api/overview. A broken query or a rate
# limit degrades every show to "time TBA" identically, so the panel needs a way to
# say "AniList is refusing me" instead of quietly claiming nothing is scheduled.
_anilist_faults = 0
_anilist_fault_kinds: set[str] = set()
_anilist_fault_lock = threading.Lock()


def _anilist_fault(kind: str, detail: str) -> None:
    """Count a fault and log the first of each kind. Systemic breakage hits every
    lookup, so one loud line beats a hundred identical ones in webui.log."""
    global _anilist_faults
    with _anilist_fault_lock:  # request threads and the fill thread both report here
        _anilist_faults += 1
        first = kind not in _anilist_fault_kinds
        _anilist_fault_kinds.add(kind)
    if first:
        print(f"! anilist {kind}: {detail}", flush=True)


def _anilist_ok() -> None:
    """AniList answered — clear the fault state so the panel warning describes now
    and not a blip an hour ago, and so a recurrence gets logged again."""
    global _anilist_faults
    with _anilist_fault_lock:
        _anilist_faults = 0
        _anilist_fault_kinds.clear()


# AniList answers roughly 30 requests a minute. A cold cache has more shows than
# that, so every lookup claims a slot from one pacer shared by the request path and
# the fill thread — otherwise the first poll after a fresh install spends its whole
# budget and gets everything else 429'd.
_ANILIST_GAP = 2.2
_anilist_gate = threading.Lock()
_anilist_next_at = 0.0


def _anilist_slot(block: bool) -> bool:
    """Claim the next request slot. The fill thread blocks until one is due; the
    request path doesn't (block=False), so a cold cache leaves the panel prompt and
    lets the background thread work through the queue instead of stalling a poll.
    The wait happens outside the lock — sleeping while holding it would make the
    non-blocking callers queue up behind the fill thread, which is the whole thing
    this is meant to avoid."""
    global _anilist_next_at
    while True:
        with _anilist_gate:
            now = time.monotonic()
            wait = _anilist_next_at - now
            if wait <= 0:
                _anilist_next_at = now + _ANILIST_GAP
                return True
            if not block:
                return False
        time.sleep(min(wait, 1.0))


def _anilist_defer(seconds: float) -> None:
    """Push the next slot out — used to honour Retry-After when we do trip the limit."""
    global _anilist_next_at
    with _anilist_gate:
        _anilist_next_at = max(_anilist_next_at, time.monotonic() + seconds)


# Returned when AniList didn't answer at all (fault, or we're pacing ourselves).
# Distinct from None, which is AniList answering "no such anime": callers may cache
# a None as a fact, but must never write down a _NO_ANSWER as one.
_NO_ANSWER = object()


def _anilist_media(search: str, block: bool = True):
    """The AniList entry best matching `search`, None if AniList has no such anime,
    or _NO_ANSWER if it never told us.

    A 404 is AniList's "no match" and is routine — bgm's Chinese titles almost never
    resolve. Everything else (rate limit, GraphQL error, network) is a fault: it gets
    reported and returned as _NO_ANSWER rather than posing as a miss, because a miss
    is written into airing_cache.json and believed until _AIR_MISS_TTL is up.
    """
    if not _anilist_slot(block):
        return _NO_ANSWER
    body = json.dumps({"query": _ANILIST_Q, "variables": {"s": search}}).encode()
    req = urllib.request.Request(
        _ANILIST_URL, data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": core.UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as ex:
        if ex.code == 404:  # AniList's "no such anime" — a healthy answer
            _anilist_ok()
            return None
        if ex.code == 429:
            retry = 60.0  # Retry-After is normally a plain seconds count; if it
            with contextlib.suppress(TypeError, ValueError):  # isn't, back off anyway
                retry = float(ex.headers.get("Retry-After") or 60)
            _anilist_defer(retry)
        _anilist_fault(f"HTTP {ex.code}", ex.read().decode("utf-8", "replace")[:300])
        return _NO_ANSWER
    except Exception as ex:  # noqa: BLE001 — network, timeout, malformed body
        _anilist_fault(type(ex).__name__, str(ex)[:300])
        return _NO_ANSWER
    if d.get("errors"):
        _anilist_fault("graphql", json.dumps(d["errors"], ensure_ascii=False)[:300])
        return _NO_ANSWER
    _anilist_ok()
    return (d.get("data") or {}).get("Media")


def _air_needs_lookup(ent: dict | None) -> bool:
    """Whether show_air_info would actually go to AniList for this cache entry:
    never looked up, written before the 'en' field existed, or a miss whose retry
    window has lapsed. Callers use this to decide what to enqueue — a miss must
    not read as "filled in" just because its keys are present with null values."""
    if not ent or "en" not in ent:
        return True
    return ent.get("at") is None and int(time.time()) - ent.get("t", 0) >= _AIR_MISS_TTL


def show_air_info(bgm_id: int, jp: str, cn: str, cache: dict, block: bool = True) -> dict:
    """{'at': unix ts of ep1's broadcast or None, 'en': English/romaji title or None}.

    Cached per bgm_id in airing_cache.json. A known time is kept indefinitely; a
    miss is retried once _AIR_MISS_TTL has passed, in case AniList adds the
    schedule later — or in case it was us that was broken. A lookup AniList never
    answered is not a miss and is not written down at all.
    """
    key = str(bgm_id)
    now = int(time.time())
    ent = cache.get(key)
    if ent and not _air_needs_lookup(ent):
        return ent
    at = en = None
    found = unanswered = False
    for term in (jp, cn):
        if not term:
            continue
        m = _anilist_media(term, block)
        if m is _NO_ANSWER:
            unanswered = True
            continue
        if not m:
            continue
        found = True
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
    if at is None and unanswered and not found:
        # Nothing was learned, so record nothing — writing a miss here would parrot a
        # rate limit back as "this show has no broadcast time" for the whole TTL. If
        # one term did resolve, the answer stands even though the other was throttled:
        # AniList has the show and simply hasn't scheduled it (and we keep its title).
        return ent or {"at": None, "en": None, "t": 0}
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
                if _air_needs_lookup(cache.get(str(bid))):
                    show_air_info(bid, jp, cn, cache)  # blocks on the pacer
                if i % 20 == 19:
                    _save_airing_cache(cache)
            _save_airing_cache(cache)
        finally:
            _title_fill_running = False

    threading.Thread(target=run, daemon=True).start()


# 半年番/年番 classification + still-airing come from the broadcast schedule
# (first->last episode airdate). bgm is authoritative and primary; AniList is a
# fallback only when bgm has no airdates scheduled yet. Spans are cached on disk
# per bgm_id — finished shows keep their airdates forever, so the big completed
# list is a one-time fill, warmed in the background so it never blocks a request.
_SPAN_CACHE_PATH = ROOT / "episode_span_cache.json"


def _load_span_cache() -> dict:
    try:
        raw = json.loads(_SPAN_CACHE_PATH.read_text(encoding="utf-8"))
        return {int(k): v for k, v in raw.items()}
    except Exception:  # noqa: BLE001
        return {}


def _save_span_cache() -> None:
    # Atomic temp+replace: anime_rss.py shares this file (loads it into its per-pass span
    # cache), so a torn half-write here would make its next sync drop the whole span cache
    # and refetch. os.replace is atomic on NTFS, so a concurrent reader never sees a partial.
    # Entries core.bgm_episode_span flagged as "bgm didn't answer" are dropped rather
    # than written: on disk they are indistinguishable from a real empty schedule and
    # would be believed for 24h by both processes. They exist only to stop a re-ask storm.
    try:
        keep = {k: v for k, v in _span_cache.items()
                if not v.get(core._SPAN_NO_ANSWER)}
        tmp = _SPAN_CACHE_PATH.with_suffix(_SPAN_CACHE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, _SPAN_CACHE_PATH)
    except Exception:  # noqa: BLE001
        pass


_span_cache: dict = _load_span_cache()


def _ani_ymd(d: dict | None) -> str | None:
    if d and d.get("year") and d.get("month") and d.get("day"):
        return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"
    return None


# AniList's answer for the handful of shows bgm has no schedule for, kept for the
# process lifetime. Broadcast status barely moves, and without this every poll
# re-asks the same questions — which the pacer would mostly turn down, flipping the
# 半年番/年番 badge on and off between refreshes.
_ani_status: dict[int, dict] = {}


def classify_broadcast(bgm_id: int, jp: str, cn: str, fetch: bool = True) -> tuple[str | None, bool]:
    """(cour_kind, still_airing) from the broadcast schedule. bgm episode airdates
    first; AniList (status / start-end dates) fills in only when bgm has nothing
    scheduled. fetch=False consults just the warm cache — used for the large
    completed list, which is warmed by a background thread instead of inline."""
    sp = core.bgm_episode_span(bgm_id, _span_cache) if fetch else _span_cache.get(bgm_id)
    first, last = (sp or {}).get("first"), (sp or {}).get("last")
    kind = core.cour_kind(first, last)
    airing = core.still_broadcasting(last) if last else False
    if fetch and not last:  # bgm has no schedule yet -> ask AniList
        m = _ani_status.get(bgm_id)
        if not m:
            for term in (jp, cn):  # JP first: AniList 404s on Chinese titles
                if not term:
                    continue
                got = _anilist_media(term, block=False)  # request path — never stall a poll
                if got and got is not _NO_ANSWER:
                    m = _ani_status[bgm_id] = got
                    break
        if m:
            airing = (m.get("status") == "RELEASING") or bool(m.get("nextAiringEpisode"))
            kind = core.cour_kind(_ani_ymd(m.get("startDate")), _ani_ymd(m.get("endDate")))
    return kind, airing


_span_fill_running = False
_span_fill_lock = threading.Lock()


def _start_span_fill(bgm_ids: list[int]) -> None:
    """Warm the episode-span cache for shows not yet known (e.g. the completed list),
    off the request path. bgm calls, so a shorter delay than AniList is fine."""
    global _span_fill_running
    todo = [b for b in bgm_ids if b not in _span_cache]
    with _span_fill_lock:
        if _span_fill_running or not todo:
            return
        _span_fill_running = True

    def run() -> None:
        global _span_fill_running
        try:
            for i, bid in enumerate(todo):
                core.bgm_episode_span(bid, _span_cache)
                time.sleep(0.3)
                if i % 20 == 19:
                    _save_span_cache()
            _save_span_cache()
        finally:
            _span_fill_running = False

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
    out_shows = []
    cur_season = core.current_season()
    for s in shows:
        season = core.season_of(s["date"])
        pinned = s["bgm_id"] in core.PIN_CURRENT_BGM_IDS
        kind, airing = classify_broadcast(s["bgm_id"], s["name"], s["name_cn"])
        is_old = core.is_manual_old_show(s["date"], s["bgm_id"], airing)
        entry = {
            "bgm_id": s["bgm_id"],
            "title": s["name_cn"] or s["name"],
            "title_jp": s["name"],
            "date": s["date"],
            "season": season,
            "pinned": pinned,
            "cour_kind": kind,
            "long_current": airing and bool(season) and season < cur_season,
            # Whether the show still has a weekly slot. airing_at is episode
            # 1's timestamp, so only a still-airing show may have that slot
            # projected forward into an upcoming broadcast.
            "airing": airing,
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
        # Reuse the span cache classify_broadcast just warmed for this show (same
        # /v0/episodes data) instead of a throwaway dict — otherwise every overview
        # re-fetches all 在看 shows' episode schedules from bgm (~3.3s -> ~0s).
        entry["premiere_date"] = core.show_premiere_date(s["bgm_id"], s["date"], _span_cache)
        air = show_air_info(s["bgm_id"], s["name"], s["name_cn"], airing_cache, block=False)
        entry["airing_at"] = air["at"]
        entry["title_en"] = air["en"]
        out_shows.append(entry)
    _save_airing_cache(airing_cache)

    return {
        "season": season_now,
        "grace_hours": core.GRACE_HOURS,
        "qb_ok": qb_ok,
        "anilist_faults": _anilist_faults,
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
    groups: dict[str, list] = {}
    counts: dict[str, int] = {}
    backfill: list[tuple[int, str, str]] = []
    span_backfill: list[int] = []
    cur_season = core.current_season()
    for t in (3, 1, 2, 4, 5):
        try:
            shows = core.bgm_collection_subjects(user, t)
        except Exception:  # noqa: BLE001
            shows = []
        counts[_COLL_TYPES[t]] = len(shows)
        lst = []
        for s in shows:
            cached = airing_cache.get(str(s["bgm_id"])) or {}
            season = core.season_of(s["date"])
            # 在看/想看 classify inline (few, badges must show at once); the big
            # completed/on-hold/dropped lists read the warm cache and are filled
            # in the background so they never stall this request.
            inline = t in (3, 1)
            kind, airing = classify_broadcast(s["bgm_id"], s["name"], s["name_cn"], fetch=inline)
            if not inline and s["bgm_id"] not in _span_cache:
                span_backfill.append(s["bgm_id"])
            e = {
                "bgm_id": s["bgm_id"],
                "type": _COLL_TYPES[t],
                "title": s["name_cn"] or s["name"],
                "title_jp": s["name"],
                "title_en": cached.get("en"),
                "date": s["date"],
                "season": season,
                "pinned": s["bgm_id"] in core.PIN_CURRENT_BGM_IDS,
                "cour_kind": kind,
                "long_current": airing and bool(season) and season < cur_season,
                "airing": airing,   # see /api/overview — gates slot projection
                "image": s.get("image", ""),
                "score": s.get("score"),
                "updated_at": s.get("updated_at"),
                "airing_at": None,
                "premiere_date": None,
            }
            if inline:  # 在看/想看：只对当前/即将播的番查精确开播时间
                # Reuse the span cache classify_broadcast just warmed (0 network) rather
                # than a throwaway dict that re-fetches every inline show's schedule (~7s).
                e["premiere_date"] = core.show_premiere_date(s["bgm_id"], s["date"], _span_cache)
                air = show_air_info(s["bgm_id"], s["name"], s["name_cn"], airing_cache, block=False)
                e["airing_at"] = air["at"]
                e["title_en"] = air["en"]
            if _air_needs_lookup(airing_cache.get(str(s["bgm_id"]))):
                # Either a big-list show, or an inline one the pacer made us skip.
                backfill.append((s["bgm_id"], s["name"], s["name_cn"]))
            lst.append(e)
        groups[_COLL_TYPES[t]] = lst
    _save_airing_cache(airing_cache)
    _save_span_cache()
    _start_title_fill(backfill)
    _start_span_fill(span_backfill)
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


@app.get("/api/events")
def api_events(after_seq: int = 0, limit: int = 200):
    """Automation event log, ascending by seq (see core.add_events).

    Cursor feed for external consumers (the Atrium message centre): pass the
    highest seq already consumed as after_seq. `seq` is the current maximum, so
    a consumer can tell it is caught up and can detect a reset (seq < cursor)
    after events.json is deleted. Unlike /api/notifications this log is
    append-only and has no read state — it is a ledger, not a banner queue.
    """
    limit = max(1, min(int(limit), 1000))
    data = core.load_events()
    fresh = sorted(
        (e for e in data.get("events") or [] if int(e.get("seq") or 0) > after_seq),
        key=lambda e: int(e.get("seq") or 0),
    )
    return {
        "events": fresh[:limit],
        "seq": int(data.get("seq") or 0),
        "hasMore": len(fresh) > limit,
    }


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


@app.get("/api/unresolved")
def api_unresolved():
    """Shows that aired but couldn't be matched to a mikan feed (see
    core.scan_unresolved). Non-dismissed only — the panel shows these as a
    warning banner so a silent resolve miss can't hide anymore."""
    items = [e for e in core.load_unresolved() if not e.get("dismissed")]
    items.sort(key=lambda e: e.get("detected_at", ""), reverse=True)
    return {"unresolved": items}


class UnresolvedDismiss(BaseModel):
    bgm_id: int | None = None  # None = dismiss every unresolved banner


@app.post("/api/unresolved/dismiss")
def api_unresolved_dismiss(body: UnresolvedDismiss):
    items = core.load_unresolved()
    for it in items:
        if body.bgm_id is None or it.get("bgm_id") == body.bgm_id:
            it["dismissed"] = True
    core.save_unresolved(items)
    return {"ok": True}


class UnresolvedResolve(BaseModel):
    bgm_id: int
    url: str


@app.post("/api/unresolved/resolve")
def api_unresolved_resolve(body: UnresolvedResolve):
    """Resolve an unmatched show from a pasted mikan link (see core.manual_resolve):
    a Bangumi link (or an Episode page with a bangumi backlink) subscribes through
    the normal pipeline; a backlink-less Episode page or raw magnet becomes a
    one-shot import that still lands in the show's library folder."""
    url = (body.url or "").strip()
    if not url:
        raise HTTPException(400, {"code": "bad_link"})
    try:
        r = core.manual_resolve(
            str(core.CONFIG.get("bgm_user")), body.bgm_id, url,
            token=core.bgm_token(None), cookie=core.CONFIG.get("mikan_cookie"),
        )
    except ValueError:
        raise HTTPException(400, {"code": "bad_link"})
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, {"code": "resolve_failed", "message": str(ex)})
    return r


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
    #    NEVER touched destructively (see SKIP_BEFORE_SEASON). Exception: a
    #    still-airing 半年番/年番 (or a pinned id) is exempt and full-replaces
    #    just like a current show, matching the backend's destructive passes.
    deleted = 0
    save_path = rdef.get("savePath", "")
    try:
        exempt = (str(season) < core.SKIP_BEFORE_SEASON
                  and core._old_cour_exempt(
                      str(season), core.rule_bgm_id(rdef, {}), _span_cache))
    except Exception:  # noqa: BLE001
        exempt = False
    if save_path and (str(season) >= core.SKIP_BEFORE_SEASON or exempt):
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


# Brand assets (the favicon family + the web manifest) live on disk instead of
# inline, so the browser tab, a pinned taskbar tile and an installed PWA all
# resolve the one canonical mark. Mounted last — a mount shadows every route
# that would otherwise match beneath its prefix.
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


if __name__ == "__main__":
    print(f"=== anime-rss-auto webui on http://{HOST}:{PORT} ===", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
