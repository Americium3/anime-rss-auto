#!/usr/bin/env python3
"""Seasonal bangumi.tv -> mikan -> qBittorrent RSS automation.

Workflow (two stages, review in between):

  1) plan : read your bangumi.tv "在看" anime list, resolve each show to a
            mikan bangumiId + preferred subtitle-group subgroupid, and write
            an editable plan.json. Shows that already have a qB rule (same
            mikan feed or same folder) are skipped.
  2) apply: read plan.json (after you eyeball / fix the English folder names)
            and create the RSS feed + auto-download rule in qBittorrent,
            matching your conventions:
              savePath = X:\\Bangumi\\<YYYY.MM>\\<English name>
              tags     = [<YYYY.MM>]   (no category)

Also: `list` just prints the 在看 list.

qBittorrent Web UI is assumed reachable, passwordless, at localhost:8080.
"""
from __future__ import annotations

import argparse
import base64
import html
import http.server
import json
import os
import re
import sys
import threading
import time
import datetime
import traceback
import urllib.parse
import urllib.request
from pathlib import Path

try:  # Win11 Chinese locale -> force utf-8 stdout；行缓冲让 watch.log 即时落盘
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
BGM_API = "https://api.bgm.tv"
MIKAN = "https://mikanani.me"
UA = "anime-rss-auto/0.1 (personal qbit rss helper)"

PLAN_PATH = Path(__file__).with_name("plan.json")
CONFIG_PATH = Path(__file__).with_name("config.local.json")
SEED_STATES_PATH = Path(__file__).with_name("seed_states.json")
BGM_TOKEN_PATH = Path(__file__).with_name("bgm_token.json")
BGM_AUTHORIZE = "https://bgm.tv/oauth/authorize"
BGM_OAUTH_TOKEN = "https://bgm.tv/oauth/access_token"
ILLEGAL_WIN = re.compile(r'[<>:"/\\|?*]')


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


CONFIG = load_config()

# qBittorrent Web UI (assumed passwordless via host whitelist).
QB = str(CONFIG.get("qb_url", "http://localhost:8080")).rstrip("/")

# Root folder qB downloads into: <bangumi_library>\<YYYY.MM>\<show>\
BANGUMI_LIBRARY = str(CONFIG.get("bangumi_library", r"X:\Bangumi"))

# Preferred subtitle groups, highest priority first, as
# [mikan subgroupid, display name, default mustContain filter].
# Override with config "group_priority": [[583, "ANi", ""], ...].
GROUP_PRIORITY = [tuple(g) for g in CONFIG.get("group_priority", [
    (583,  "ANi",        ""),
    (370,  "LoliHouse",  "LoliHouse"),
    (615,  "桜都/Sakurato", ""),
    (1231, "北宇治",      ""),
    (203,  "Skymoon/天月", ""),
])]
PRIORITY_IDS = [g[0] for g in GROUP_PRIORITY]
GROUP_NAME = {g[0]: g[1] for g in GROUP_PRIORITY}
GROUP_FILTER = {g[0]: g[2] for g in GROUP_PRIORITY}

# Shows from a cour BEFORE this one are left entirely to manual handling: the
# script never adds, removes, unsubscribes, or deletes files for them. The
# cutoff is a cour string "YYYY.MM" and comparison is lexicographic (months are
# zero-padded, so "2026.01" < "2026.04" < "2026.10" and "2025.10" < "2026.01").
SKIP_BEFORE_SEASON = str(CONFIG.get("skip_before_season", "2026.04"))

# Calendar month -> the cour (season) month it belongs to.
_COUR_MONTH = {1: 1, 2: 1, 3: 1, 4: 4, 5: 4, 6: 4,
               7: 7, 8: 7, 9: 7, 10: 10, 11: 10, 12: 10}

# --- 首选组宽限期（ANi 保险丝）------------------------------------------- #
# 一部新番在 mikan 上首次解析成功时，如果首选组（GROUP_PRIORITY[0]，即 ANi）
# 还没出现在可用字幕组里，先不锁定规则，等 ani_grace_hours 小时；期间 ANi
# 出现 -> 立刻锁 ANi；到点还没来 -> 按原优先级锁次选组。经验依据：ANi 更新
# 及时，开播几小时内不见就大概率不做这番了。宽限期内漏掉的剧集不会丢——
# mikan 的 per-subgroup RSS 是全量的，规则一建 qB 会把旧条目补抓回来。
# ani_grace_hours <= 0 关闭此机制（回到旧行为：首轮见谁锁谁）。
PREFERRED_GID = PRIORITY_IDS[0]
GRACE_HOURS = float(CONFIG.get("ani_grace_hours", 3))
GRACE_PATH = Path(__file__).with_name("group_grace.json")


def load_grace() -> dict[str, float]:
    """bgm_id(str) -> first-seen-on-mikan unix timestamp, for shows in grace."""
    if GRACE_PATH.exists():
        try:
            return json.loads(GRACE_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_grace(d: dict[str, float]) -> None:
    GRACE_PATH.write_text(json.dumps(d, indent=0), encoding="utf-8")

# --- Jellyfin 硬链接镜像：让新下载的番自动进 Jellyfin 库 -------------------- #
# qB 把剧集下到 X:\Bangumi\<cour>\<show>\；Jellyfin 库看的是 X:\BangumiJF 的
# 硬链接季度镜像（剧名\Season 01\）。每轮 sync 末尾增量建链：只新建、从不删除，
# 不碰原文件/做种。镜像是无害操作（只 os.link），故用独立的
# MIRROR_SKIP_BEFORE_SEASON（默认 "" = 镜像所有季度，含旧番），与破坏性的
# SKIP_BEFORE_SEASON(2026.04) 解耦——旧番照样自动镜像进 Jellyfin，但 RSS
# 删规则/删文件等破坏性逻辑仍只对 SKIP_BEFORE_SEASON 之后的番生效。
JELLYFIN_MIRROR  = str(CONFIG.get("jellyfin_mirror", r"X:\BangumiJF"))
MIRROR_SKIP_BEFORE_SEASON = str(CONFIG.get("mirror_skip_before_season", ""))
JELLYFIN_URL     = str(CONFIG.get("jellyfin_url", "http://localhost:8096")).rstrip("/")
JELLYFIN_API_KEY = str(CONFIG.get("jellyfin_api_key", ""))  # secret: config only
MIRROR_VIDEO_EXT = (".mkv", ".mp4")
MIRROR_SPECIAL_DIRS = {"SPs", "Specials", "SP", "Extras", "Scans", "CDs", "Menu"}
_COUR_DIR_RE = re.compile(r"^\d{4}\.\d{2}$")


def year_of(date_str: str) -> int | None:
    m = re.match(r"\s*(\d{4})", date_str or "")
    return int(m.group(1)) if m else None


def season_of(date_str: str) -> str | None:
    """Map an air date ('YYYY-MM-DD') to its cour string 'YYYY.MM'."""
    m = re.match(r"\s*(\d{4})\D+(\d{1,2})", date_str or "")
    if not m:
        return None
    return f"{int(m.group(1))}.{_COUR_MONTH[int(m.group(2))]:02d}"


def is_manual_old_show(date_str: str) -> bool:
    """True for shows from a cour before SKIP_BEFORE_SEASON -> user's by hand.

    Unknown/unparseable dates are treated as NOT old (current) so brand-new
    shows whose bgm date is still missing are not accidentally ignored.
    """
    s = season_of(date_str)
    return s is not None and s < SKIP_BEFORE_SEASON


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def http_get(url: str, *, retries: int = 3, timeout: int = 15) -> bytes:
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url}\n  {last}")


def qb_post(path: str, data: dict) -> str:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        f"{QB}{path}", data=body, headers={"User-Agent": UA}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8", "replace")


def qb_get_json(path: str):
    return json.loads(http_get(f"{QB}{path}").decode("utf-8", "replace"))


def mikan_cookie(args) -> str | None:
    return getattr(args, "mikan_cookie", None) or CONFIG.get("mikan_cookie")


def _bgm_oauth_creds() -> tuple[str | None, str | None, str]:
    return (
        CONFIG.get("bgm_client_id"),
        CONFIG.get("bgm_client_secret"),
        CONFIG.get("bgm_redirect_uri", "http://localhost"),
    )


def _bgm_oauth_post(data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        BGM_OAUTH_TOKEN,
        data=body,
        headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _save_bgm_oauth_token(tok: dict) -> None:
    exp = tok.get("expires_in")
    data = {
        "access_token": tok["access_token"],
        "refresh_token": tok.get("refresh_token"),
        "expires_at": int(time.time()) + int(exp) if exp else None,
    }
    BGM_TOKEN_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def bgm_token(args=None) -> str | None:
    """Return a usable bgm access token.

    Priority: explicit --bgm-token > OAuth (auto-refreshing) > 365-day personal
    token in config. The OAuth path keeps a refresh_token in bgm_token.json and
    silently renews the short-lived access_token when <1 day remains, so a daily
    watch run never lets it lapse — no yearly manual reissue.
    """
    explicit = getattr(args, "bgm_token", None) if args is not None else None
    if explicit:
        return explicit
    cid, sec, uri = _bgm_oauth_creds()
    if BGM_TOKEN_PATH.exists() and cid and sec:
        try:
            d = json.loads(BGM_TOKEN_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            d = {}
        at, rt, exp = d.get("access_token"), d.get("refresh_token"), d.get("expires_at")
        if at and (exp is None or exp - time.time() > 86400):
            return at
        if rt:
            try:
                tok = _bgm_oauth_post({
                    "grant_type": "refresh_token", "client_id": cid,
                    "client_secret": sec, "refresh_token": rt, "redirect_uri": uri,
                })
                if tok.get("access_token"):
                    _save_bgm_oauth_token(tok)
                    print("# bgm token 已自动续期")
                    return tok["access_token"]
                print(f"# bgm token 续期返回异常：{tok}")
            except Exception as ex:  # noqa: BLE001
                print(f"# bgm token 续期失败，回退：{ex}")
        if at:
            return at  # stale-ish but try it before giving up
    return CONFIG.get("bgm_access_token")


def mikan_post(path: str, payload: dict, cookie: str) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{MIKAN}{path}",
        data=body,
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": f".AspNetCore.Identity.Application={cookie}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def mikan_subscribe(cookie: str, bangumi_id: int, subgroup: int) -> dict:
    return mikan_post(
        "/Home/SubscribeBangumi",
        {"BangumiID": int(bangumi_id), "SubtitleGroupID": int(subgroup)},
        cookie,
    )


def mikan_unsubscribe(cookie: str, bangumi_id: int, subgroup: int | None = None) -> dict:
    return mikan_post(
        "/Home/UnsubscribeBangumi",
        {
            "BangumiID": int(bangumi_id),
            "SubtitleGroupID": int(subgroup) if subgroup else None,
        },
        cookie,
    )


# --------------------------------------------------------------------------- #
# bangumi.tv
# --------------------------------------------------------------------------- #
def bgm_watching(user: str) -> list[dict]:
    """Return currently-watching (type=3) anime (subject_type=2)."""
    out, offset = [], 0
    while True:
        url = (
            f"{BGM_API}/v0/users/{urllib.parse.quote(user)}/collections"
            f"?subject_type=2&type=3&limit=50&offset={offset}"
        )
        d = json.loads(http_get(url).decode("utf-8", "replace"))
        data = d.get("data", [])
        for x in data:
            s = x.get("subject", {})
            out.append(
                {
                    "bgm_id": x.get("subject_id"),
                    "name": s.get("name", ""),
                    "name_cn": s.get("name_cn", ""),
                    "date": s.get("date", ""),
                }
            )
        offset += len(data)
        if offset >= d.get("total", 0) or not data:
            break
    return out


def bgm_collection_type(user: str, subject_id: int) -> int | None:
    """Collection status of one subject for a user.

    bgm type codes: 1=想看 2=看过 3=在看 4=搁置 5=抛弃. None = not collected.
    """
    url = (
        f"{BGM_API}/v0/users/{urllib.parse.quote(user)}"
        f"/collections/{subject_id}"
    )
    try:
        d = json.loads(http_get(url, retries=2).decode("utf-8", "replace"))
        return d.get("type")
    except Exception:  # noqa: BLE001
        return None


def bgm_subject_season(subject_id: int, cache: dict[int, str | None]) -> str | None:
    """Cour string 'YYYY.MM' for a bgm subject's air date (cached, None if unknown)."""
    if subject_id in cache:
        return cache[subject_id]
    try:
        d = json.loads(
            http_get(f"{BGM_API}/v0/subjects/{subject_id}", retries=2).decode("utf-8", "replace")
        )
        cache[subject_id] = season_of(d.get("date", ""))
    except Exception:  # noqa: BLE001
        cache[subject_id] = None
    return cache[subject_id]


def _int_key(v) -> int | None:
    """Integral episode key, or None for missing/fractional (e.g. 5.5 specials)."""
    if v is None:
        return None
    try:
        n = int(v)
        return n if float(v) == n else None
    except (TypeError, ValueError):
        return None


def bgm_subject_episodes(subject_id: int, cache: dict[int, dict[int, int]]) -> dict[int, int]:
    """Map a subject's main-story episode numbers -> bgm episode_id.

    A single number can come at us two ways depending on the fansub's habit:
      - per-season number (bgm 'ep', 1-based)  -> e.g. Re:Zero S4 "- 05"
      - whole-series running number (bgm 'sort') -> e.g. 芙莉蓮二期 "- 33"
    So we key by BOTH. 'sort' is filled first and 'ep' overrides, so when a number
    is a valid per-season ep it wins; numbers that only exist as a running 'sort'
    (continuation seasons) still resolve. type=0 is 本篇, so SP/OP/ED never
    collide with a numeric episode. Cached per subject.
    """
    if subject_id in cache:
        return cache[subject_id]
    out: dict[int, int] = {}
    try:
        d = json.loads(
            http_get(
                f"{BGM_API}/v0/episodes?subject_id={subject_id}&type=0&limit=100",
                retries=2,
            ).decode("utf-8", "replace")
        )
        data = d.get("data", [])
        for e in data:                       # sort first (lower priority)
            eid, n = e.get("id"), _int_key(e.get("sort"))
            if eid is not None and n is not None:
                out[n] = int(eid)
        for e in data:                       # ep overrides sort on collision
            eid, n = e.get("id"), _int_key(e.get("ep"))
            if eid is not None and n is not None:
                out[n] = int(eid)
    except Exception:  # noqa: BLE001
        pass
    cache[subject_id] = out
    return out


def bgm_mark_episode_watched(token: str, episode_id: int) -> None:
    """PUT a single episode's collection status to 看过 (type 2). 204 on success."""
    body = json.dumps({"type": 2}).encode()
    req = urllib.request.Request(
        f"{BGM_API}/v0/users/-/collections/-/episodes/{episode_id}",
        data=body,
        method="PUT",
        headers={
            "User-Agent": UA,
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        if r.status not in (200, 202, 204):
            raise RuntimeError(f"bgm PUT episode {episode_id} -> HTTP {r.status}")


_EP_RES = {360, 480, 540, 576, 720, 1080, 1440, 2160}


def parse_episode(name: str) -> int | None:
    """Best-effort episode number from a torrent/file name. None if unsure.

    Handles the conventions seen under X:\\Bangumi:
      [ANi] ... - 04 [1080P]...     ->  4
      [Nekomoe] Engage Kiss [01]... ->  1
      Cyberpunk...S01E06...         ->  6
    Numbers that look like resolutions (1080, 720, ...) are never returned.
    """
    base = name.rsplit(".", 1)[0]
    m = re.search(r"[Ss]\d{1,2}[Ee](\d{1,3})", base)
    if m:
        return int(m.group(1))
    # " - 04 " / " - 04[" / " - 04v2" : the dominant ANi/LoliHouse/Lilith layout
    m = re.search(r"[-–]\s*(\d{1,3})(?:v\d+)?\s*(?:\[|\(|$)", base)
    if m:
        n = int(m.group(1))
        if n not in _EP_RES:
            return n
    # "[04]" bracketed number (Nekomoe/VCB); exclude resolutions / large junk
    for mm in re.finditer(r"\[(\d{1,3})\]", base):
        n = int(mm.group(1))
        if 0 < n < 200 and n not in _EP_RES:
            return n
    return None


def bgm_english_alias(subject_id: int) -> str:
    """Best-effort English/romaji title from subject infobox (别名)."""
    try:
        d = json.loads(
            http_get(f"{BGM_API}/v0/subjects/{subject_id}").decode("utf-8", "replace")
        )
    except Exception:  # noqa: BLE001
        return ""
    for box in d.get("infobox", []):
        if box.get("key") in ("别名", "英文名", "罗马字"):
            v = box.get("value")
            cands = []
            if isinstance(v, list):
                cands = [i.get("v", "") for i in v if isinstance(i, dict)]
            elif isinstance(v, str):
                cands = [v]
            for c in cands:
                if re.search(r"[A-Za-z]", c) and not re.search(r"[一-鿿]", c):
                    return c.strip()
    return ""


# --------------------------------------------------------------------------- #
# mikan
# --------------------------------------------------------------------------- #
def mikan_search_candidates(query: str) -> list[int]:
    if not query.strip():
        return []
    url = f"{MIKAN}/Home/Search?searchstr={urllib.parse.quote(query)}"
    html_txt = http_get(url).decode("utf-8", "replace")
    ids = re.findall(r"/Home/Bangumi/(\d+)", html_txt)
    seen, out = set(), []
    for i in ids:
        n = int(i)
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def mikan_bangumi_info(bangumi_id: int) -> dict:
    """Return {'bgm_id', 'subgroups', 'title'} for a mikan bangumi page."""
    html_txt = http_get(f"{MIKAN}/Home/Bangumi/{bangumi_id}").decode("utf-8", "replace")
    m = re.search(r"bgm\.tv/subject/(\d+)", html_txt)
    bgm_id = int(m.group(1)) if m else None
    subs = sorted({int(s) for s in re.findall(r"subgroupid=(\d+)", html_txt)})
    t = re.search(r"<title>(.*?)</title>", html_txt, re.S)
    title = html.unescape(t.group(1).strip()) if t else f"Mikan Project - {bangumi_id}"
    return {"bgm_id": bgm_id, "subgroups": subs, "title": title}


def pick_subgroup(available: list[int]) -> int | None:
    for gid in PRIORITY_IDS:
        if gid in available:
            return gid
    return available[0] if available else None


def resolve_show(show: dict) -> dict:
    """Map a bgm show -> mikan bangumiId + subgroupid. Adds resolution keys."""
    bgm_id = show["bgm_id"]
    queries = [q for q in (show["name_cn"], show["name"]) if q]
    candidates: list[int] = []
    for q in queries:
        for c in mikan_search_candidates(q):
            if c not in candidates:
                candidates.append(c)
        if candidates:
            break  # name_cn usually enough

    matched_mikan = None
    matched_subs: list[int] = []
    matched_title = ""
    for bid in candidates[:8]:
        try:
            info = mikan_bangumi_info(bid)
        except Exception:  # noqa: BLE001
            continue
        if info["bgm_id"] == bgm_id:
            matched_mikan, matched_subs, matched_title = bid, info["subgroups"], info["title"]
            break

    confidence = "high"
    if matched_mikan is None and candidates:
        # fall back: first search candidate, confirm via its own subgroups
        try:
            info = mikan_bangumi_info(candidates[0])
            matched_mikan = candidates[0]
            matched_subs, matched_title = info["subgroups"], info["title"]
        except Exception:  # noqa: BLE001
            pass
        confidence = "low (name match, bgm id NOT confirmed)"

    subgroup = pick_subgroup(matched_subs)
    show = dict(show)
    show.update(
        {
            "mikan_id": matched_mikan,
            "mikan_title": matched_title,
            "subgroup": subgroup,
            "subgroup_name": GROUP_NAME.get(subgroup, f"subgroup {subgroup}")
            if subgroup
            else None,
            "available_subgroups": matched_subs,
            "confidence": confidence if matched_mikan else "UNRESOLVED",
        }
    )
    return show


# --------------------------------------------------------------------------- #
# qBittorrent helpers
# --------------------------------------------------------------------------- #
def current_season(today: datetime.date | None = None) -> str:
    d = today or datetime.date.today()
    return f"{d.year}.{_COUR_MONTH[d.month]:02d}"


def feed_url(mikan_id: int, subgroup: int) -> str:
    return f"{MIKAN}/RSS/Bangumi?bangumiId={mikan_id}&subgroupid={subgroup}"


def existing_rules() -> dict:
    return qb_get_json("/api/v2/rss/rules")


def rss_feed_paths() -> dict:
    """Map feed URL -> its item path in the qB RSS tree."""
    tree = qb_get_json("/api/v2/rss/items?withData=false")
    out: dict[str, str] = {}

    def walk(node: dict, prefix: str = "") -> None:
        for k, v in node.items():
            if isinstance(v, dict) and "url" in v:
                out[v["url"]] = prefix + k
            elif isinstance(v, dict):
                walk(v, prefix + k + "\\")

    walk(tree)
    return out


def clean_name(name: str) -> str:
    return ILLEGAL_WIN.sub("", name).strip().rstrip(".")


def make_rule_def(name: str, season: str, feed: str, must_contain: str) -> dict:
    save_bs = f"{BANGUMI_LIBRARY}\\{season}\\{name}"
    save_fs = save_bs.replace("\\", "/")
    return {
        "enabled": True,
        "mustContain": must_contain,
        "mustNotContain": "",
        "useRegex": False,
        "episodeFilter": "",
        "smartFilter": False,
        "previouslyMatchedEpisodes": [],
        "affectedFeeds": [feed],
        "ignoreDays": 0,
        "addPaused": None,
        "assignedCategory": "",
        "savePath": save_bs,
        "priority": 0,
        "torrentContentLayout": None,
        "torrentParams": {
            "category": "",
            "tags": [season],
            "save_path": save_fs,
            "use_auto_tmm": False,
            "operating_mode": "AutoManaged",
            "download_limit": -1,
            "upload_limit": -1,
            "ratio_limit": -2,
            "seeding_time_limit": -2,
            "inactive_seeding_time_limit": -2,
            "share_limit_action": "Default",
            "skip_checking": False,
            "download_path": "",
        },
    }


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_list(args):
    shows = bgm_watching(args.user)
    print(f"在看动画 ({len(shows)}):")
    for s in shows:
        print(f"  [{s['bgm_id']}] {s['name_cn'] or s['name']}  /  {s['name']}  ({s['date']})")


def build_plan(user: str, season: str, *, verbose: bool = True) -> list[dict]:
    shows = bgm_watching(user)
    if verbose:
        print(f"# season = {season}\n# 在看动画: {len(shows)}")

    rules = existing_rules()
    existing_feeds, existing_names = set(), set()
    for rname, rdef in rules.items():
        existing_names.add(rname.strip().lower())
        for f in rdef.get("affectedFeeds", []):
            m = re.search(r"bangumiId=(\d+)", f)
            if m:
                existing_feeds.add(int(m.group(1)))

    plan = []
    for s in shows:
        if is_manual_old_show(s["date"]):
            sea = season_of(s["date"])
            flag = f"skip (旧番 {sea} < {SKIP_BEFORE_SEASON}, 手动管理)"
            plan.append({
                "include": False,
                "flag": flag,
                "name": clean_name(s["name_cn"] or s["name"]),
                "season": season,
                "bgm_id": s["bgm_id"],
                "bgm_name": s["name"],
                "bgm_name_cn": s["name_cn"],
                "mikan_id": None,
                "subgroup": None,
                "subgroup_name": None,
                "available_subgroups": [],
                "mustContain": "",
                "confidence": "skipped-old",
                "feed": None,
                "feed_path": None,
            })
            if verbose:
                print(f"  - {s['name_cn'] or s['name']!r:40} bgm={s['bgm_id']:<7} {flag}")
            continue
        resolved = resolve_show(s)
        mid = resolved["mikan_id"]
        flag = ""
        if mid is not None and mid in existing_feeds:
            flag = "skip (rule exists for this mikan feed)"
        if resolved["confidence"] == "UNRESOLVED":
            flag = "UNRESOLVED (no mikan match)"

        # ANi 保险丝：解析成功但首选组缺席 -> 进入/继续宽限期，先不锁定。
        gkey = str(s["bgm_id"])
        if not flag and GRACE_HOURS > 0 and resolved["subgroup"] is not None:
            if resolved["subgroup"] != PREFERRED_GID:
                grace = load_grace()
                now = time.time()
                first = grace.get(gkey)
                if first is None:
                    grace[gkey] = first = now
                    save_grace(grace)
                left_h = (first + GRACE_HOURS * 3600 - now) / 3600
                if left_h > 0:
                    flag = (f"wait ({GROUP_NAME.get(PREFERRED_GID)} grace, "
                            f"{left_h:.1f}h left, best now: {resolved['subgroup_name']})")
                else:  # 到点 ANi 仍未出现 -> 放行锁次选组，清掉状态
                    grace.pop(gkey, None)
                    save_grace(grace)
            else:  # 首选组到位（含宽限期内赶到）-> 清状态，正常锁定
                grace = load_grace()
                if grace.pop(gkey, None) is not None:
                    save_grace(grace)

        eng = bgm_english_alias(s["bgm_id"]) if not flag.startswith("skip") else ""
        proposed = clean_name(eng or s["name_cn"] or s["name"])
        if proposed.strip().lower() in existing_names and not flag:
            flag = "skip (folder/rule name already exists)"

        entry = {
            "include": not bool(flag),
            "flag": flag,
            "name": proposed,            # <-- EDIT this to your English folder name
            "season": season,
            "bgm_id": s["bgm_id"],
            "bgm_name": s["name"],
            "bgm_name_cn": s["name_cn"],
            "mikan_id": mid,
            "subgroup": resolved["subgroup"],
            "subgroup_name": resolved["subgroup_name"],
            "available_subgroups": resolved["available_subgroups"],
            "mustContain": GROUP_FILTER.get(resolved["subgroup"], ""),
            "confidence": resolved["confidence"],
            "feed": feed_url(mid, resolved["subgroup"])
            if mid and resolved["subgroup"]
            else None,
            # qB RSS tree nests by backslash; "/" would create a flat item.
            "feed_path": f"{season}\\{resolved['mikan_title']}"
            if resolved.get("mikan_title")
            else None,
        }
        plan.append(entry)
        if verbose:
            tag = "+" if entry["include"] else "-"
            print(
                f"  {tag} {proposed!r:40} bgm={s['bgm_id']:<7} "
                f"mikan={mid} grp={entry['subgroup_name']} "
                f"[{resolved['confidence']}] {flag}"
            )
    # 宽限状态兜底清理：已不在在看列表的番不再计时。
    if GRACE_HOURS > 0:
        grace = load_grace()
        stale = [k for k in grace if k not in {str(s["bgm_id"]) for s in shows}]
        if stale:
            for k in stale:
                grace.pop(k, None)
            save_grace(grace)
    return plan


def cmd_plan(args):
    season = args.season or current_season()
    plan = build_plan(args.user, season)
    PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    n_inc = sum(1 for e in plan if e["include"])
    print(f"\n# wrote {PLAN_PATH}  ({n_inc} to add, {len(plan) - n_inc} skipped)")
    print("# Review/edit 'name' and 'subgroup'/'mustContain', then run: apply")


def apply_entries(to_add: list[dict], cookie: str | None, dry_run: bool) -> None:
    print(f"applying {len(to_add)} rules...")
    for e in to_add:
        name = e["name"]
        season = e["season"]
        feed = feed_url(e["mikan_id"], e["subgroup"])  # rebuild from possibly-edited ids
        feed_path = e.get("feed_path") or f"{season}\\Mikan Project - {name}"
        if dry_run:
            rd = make_rule_def(name, season, feed, e.get("mustContain", ""))
            print(f"  [dry] feed  {feed_path}")
            print(f"  [dry] rule  {name} -> {rd['savePath']}  ({feed})")
            print(f"  [dry] mikan subscribe {e['mikan_id']}/{e['subgroup']}")
            continue
        try:
            qb_post("/api/v2/rss/addFeed", {"url": feed, "path": feed_path})
        except Exception as ex:  # noqa: BLE001
            print(f"  ! addFeed {name}: {ex} (continuing; rule may still bind)")
        rule_def = make_rule_def(name, season, feed, e.get("mustContain", ""))
        try:
            qb_post(
                "/api/v2/rss/setRule",
                {"ruleName": name, "ruleDef": json.dumps(rule_def)},
            )
            print(f"  ok  {name} -> {rule_def['savePath']}")
        except Exception as ex:  # noqa: BLE001
            print(f"  ! setRule {name}: {ex}")
        if cookie:
            try:
                r = mikan_subscribe(cookie, e["mikan_id"], e["subgroup"])
                ok = r.get("success") if isinstance(r, dict) else r
                print(f"     mikan subscribe {e['mikan_id']}/{e['subgroup']}: {ok}")
            except Exception as ex:  # noqa: BLE001
                print(f"     ! mikan subscribe failed (cookie expired?): {ex}")


def cmd_apply(args):
    if not PLAN_PATH.exists():
        sys.exit("no plan.json — run `plan` first")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    to_add = [e for e in plan if e.get("include") and e.get("feed") and e.get("name")]
    if not to_add:
        print("nothing to add (no included entries with a feed)")
        return
    apply_entries(to_add, mikan_cookie(args), args.dry_run)


def rule_bgm_id(rdef: dict, cache: dict[int, int | None]) -> int | None:
    for f in rdef.get("affectedFeeds", []):
        m = re.search(r"bangumiId=(\d+)", f)
        if not m:
            continue
        mid = int(m.group(1))
        if mid not in cache:
            try:
                cache[mid] = mikan_bangumi_info(mid)["bgm_id"]
            except Exception:  # noqa: BLE001
                cache[mid] = None
        if cache[mid]:
            return cache[mid]
    return None


def qb_torrents_under(save_path_bs: str) -> list[dict]:
    """qB torrents saved under the given X:\\... folder."""
    norm = save_path_bs.replace("\\", "/").rstrip("/").lower()
    out = []
    for t in qb_get_json("/api/v2/torrents/info"):
        sp = (t.get("save_path") or "").replace("\\", "/").rstrip("/").lower()
        cp = (t.get("content_path") or "").replace("\\", "/").lower()
        if sp == norm or cp.startswith(norm + "/"):
            out.append(t)
    return out


def remove_empty_dir(save_path: str, *, retries: int = 15, delay: float = 1.0) -> None:
    """Remove a show's folder once it is empty.

    qB deletes files asynchronously, so right after a delete the folder may
    still hold files; retry until it is empty (or we give up). Logs the result
    so unattended runs leave a trace in sync.log.
    """
    if not save_path:
        return
    p = Path(save_path)
    for _ in range(retries):
        if not p.is_dir():
            return  # gone already (e.g. qB removed it with the files)
        try:
            if not any(p.iterdir()):
                p.rmdir()
                print(f"     removed empty folder {save_path}")
                return
        except Exception as ex:  # noqa: BLE001
            print(f"     ! could not remove folder {save_path}: {ex}")
            return
        time.sleep(delay)
    print(f"     ! folder still not empty after {retries}s, left in place: {save_path}")


def remove_subscription(
    rname, rdef, feed_paths, cookie, *, delete_files: bool, unsubscribe_mikan: bool = True
) -> None:
    """Tear down one show's qB rule + feed; optionally mikan sub and local files."""
    if delete_files:
        save_path = rdef.get("savePath", "")
        torrents = qb_torrents_under(save_path) if save_path else []
        if torrents:
            hashes = "|".join(t["hash"] for t in torrents)
            try:
                qb_post(
                    "/api/v2/torrents/delete",
                    {"hashes": hashes, "deleteFiles": "true"},
                )
                print(f"     deleted {len(torrents)} torrent(s) + files under {save_path}")
            except Exception as ex:  # noqa: BLE001
                print(f"     ! torrent delete failed: {ex}")
        remove_empty_dir(save_path)
    try:
        qb_post("/api/v2/rss/removeRule", {"ruleName": rname})
    except Exception as ex:  # noqa: BLE001
        print(f"  ! removeRule {rname}: {ex}")
    for f in rdef.get("affectedFeeds", []):
        path = feed_paths.get(f)
        if path:
            try:
                qb_post("/api/v2/rss/removeItem", {"path": path})
            except Exception as ex:  # noqa: BLE001
                print(f"  ! removeItem {path}: {ex}")
        if cookie and unsubscribe_mikan:
            mm = re.search(r"bangumiId=(\d+)&subgroupid=(\d+)", f)
            if mm:
                try:
                    mikan_unsubscribe(cookie, int(mm.group(1)), int(mm.group(2)))
                except Exception as ex:  # noqa: BLE001
                    print(f"     ! mikan unsubscribe failed: {ex}")
    print(f"  ok removed {rname}")


def reconcile_removed(
    user: str, cookie: str | None, dry_run: bool, purge_dropped: bool
) -> None:
    """Tear down rules whose show left 在看.

    - 看过 (type 2): remove qB rule+feed only. KEEP mikan sub + local files.
    - 抛弃 (type 5): remove qB rule+feed only. KEEP mikan sub + local files.
    - 未收藏 / 取消收藏 (type none): unsubscribe mikan + DELETE files (if purge_dropped).
    - 想看 / 搁置 (type 1 / 4): conservatively KEEP everything (might resume).
    """
    rules = existing_rules()
    feed_paths = rss_feed_paths()
    print(f"# checking {len(rules)} rules against bgm status (user {user})")

    rule_only, purge = [], []
    cache: dict[int, int | None] = {}
    season_cache: dict[int, str | None] = {}
    for rname, rdef in rules.items():
        bgm_id = rule_bgm_id(rdef, cache)
        if not bgm_id:
            print(f"  ?  {rname}: could not resolve bgm id (skip)")
            continue
        sea = bgm_subject_season(bgm_id, season_cache)
        if sea is not None and sea < SKIP_BEFORE_SEASON:
            print(f"     {rname}: 旧番 {sea} -> 跳过（手动管理，不增不删不删文件）")
            continue
        ctype = bgm_collection_type(user, bgm_id)
        if ctype == 3:
            print(f"     {rname}: 在看 -> keep")
        elif ctype in (2, 5):
            label = "看过" if ctype == 2 else "抛弃"
            rule_only.append((rname, rdef))
            print(f"  -  {rname}: {label} -> 删 qB 规则，保留 mikan 订阅 + 本地文件")
        elif ctype is None:
            purge.append((rname, rdef))
            act = "unsubscribe mikan + DELETE files" if purge_dropped else "unsubscribe mikan (files kept)"
            print(f"  X  {rname}: 未收藏 -> {act}")
        else:  # 1 想看, 4 搁置
            label = {1: "想看", 4: "搁置"}.get(ctype, str(ctype))
            print(f"     {rname}: {label} -> keep (may resume)")

    if not rule_only and not purge:
        print("\n# nothing to reconcile")
        return
    if dry_run:
        print(
            f"\n# [dry-run] rule_only(keep mikan+files)={len(rule_only)} "
            f"purge(未收藏)={len(purge)} (delete files={purge_dropped})"
        )
        return
    for rname, rdef in rule_only:
        remove_subscription(
            rname, rdef, feed_paths, cookie, delete_files=False, unsubscribe_mikan=False
        )
    for rname, rdef in purge:
        remove_subscription(rname, rdef, feed_paths, cookie, delete_files=purge_dropped)


def cmd_prune(args):
    reconcile_removed(
        args.user, mikan_cookie(args), args.dry_run, purge_dropped=args.purge_files
    )


# --------------------------------------------------------------------------- #
# mark-watched: a torrent the user pauses (seeding -> stoppedUP) => 看过 on bgm
# --------------------------------------------------------------------------- #
# qB upload states that count as "actively seeding" (i.e. NOT user-paused).
_SEEDING_STATES = {
    "uploading", "stalledUP", "forcedUP", "queuedUP", "checkingUP", "moving",
}
# Completed-and-stopped states across qB versions (5.x: stopped*, 4.x: paused*).
_STOPPED_UP_STATES = {"stoppedUP", "pausedUP"}


def load_seed_states() -> dict[str, str]:
    if SEED_STATES_PATH.exists():
        try:
            return json.loads(SEED_STATES_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_seed_states(states: dict[str, str]) -> None:
    SEED_STATES_PATH.write_text(
        json.dumps(states, ensure_ascii=False, indent=0), encoding="utf-8"
    )


def rules_by_savepath(rules: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rdef in rules.values():
        sp = (rdef.get("savePath") or "").replace("\\", "/").rstrip("/").lower()
        if sp:
            out[sp] = rdef
    return out


def resolve_torrent_target(
    t: dict,
    rule_by_path: dict[str, dict],
    mikan_cache: dict[int, int | None],
    season_cache: dict[int, str | None],
    ep_cache: dict[int, dict[int, int]],
) -> tuple[int | None, int | None, str]:
    """For a paused torrent, find (bgm_subject_id, episode_id, reason).

    reason is a short human string explaining a skip (or 'ok'). Either both ids
    are present (reason 'ok') or both are None.
    """
    sp = (t.get("save_path") or "").replace("\\", "/").rstrip("/").lower()
    rdef = rule_by_path.get(sp)
    if rdef is None:
        return None, None, "无对应 qB 规则（非自动下载的种子）"
    bgm_id = rule_bgm_id(rdef, mikan_cache)
    if not bgm_id:
        return None, None, "无法解析 bgm id"
    sea = bgm_subject_season(bgm_id, season_cache)
    if sea is not None and sea < SKIP_BEFORE_SEASON:
        return None, None, f"旧番 {sea} < {SKIP_BEFORE_SEASON}（手动管理）"
    ep = parse_episode(t.get("name", ""))
    if ep is None:
        return None, None, "集数解析失败"
    eps = bgm_subject_episodes(bgm_id, ep_cache)
    eid = eps.get(ep)
    if not eid:
        return None, None, f"集数 {ep} 在 subject {bgm_id} 找不到对应集(ep/sort 都无)"
    return bgm_id, eid, "ok"


def mark_watched_pass(token: str | None, *, dry_run: bool = False) -> None:
    """Detect torrents the user just paused and mark that episode 看过 on bgm.

    Strictly transition-based: a torrent only fires when it moves from an
    actively-seeding state to stoppedUP/pausedUP *between two passes*. The very
    first pass (no prior state file) only records a baseline and marks nothing,
    so the many torrents already sitting at stoppedUP are never bulk-marked.
    """
    if not token:
        print("# mark-watched: 未配置 bgm_access_token，跳过")
        return
    try:
        torrents = qb_get_json("/api/v2/torrents/info")
    except Exception as ex:  # noqa: BLE001
        print(f"# mark-watched: 读取 qB 种子失败，跳过：{ex}")
        return
    states_new = {t["hash"]: t.get("state") for t in torrents}
    states_old = load_seed_states()

    if not states_old and not dry_run:
        save_seed_states(states_new)
        print(f"# mark-watched: 首轮建立基线（{len(states_new)} 个种子），本轮不标记")
        return

    rule_by_path = rules_by_savepath(existing_rules())
    mikan_cache: dict[int, int | None] = {}
    season_cache: dict[int, str | None] = {}
    ep_cache: dict[int, dict[int, int]] = {}

    marked = 0
    for t in torrents:
        h, st = t["hash"], t.get("state")
        prev = states_old.get(h)
        if dry_run:
            # Report on everything currently stopped+complete, ignore transition.
            fired = st in _STOPPED_UP_STATES and t.get("progress", 0) >= 1
        else:
            # Real run: only the seeding -> stopped transition fires.
            fired = (
                st in _STOPPED_UP_STATES
                and prev in _SEEDING_STATES
                and t.get("progress", 0) >= 1
            )
        if not fired:
            continue
        bgm_id, eid, reason = resolve_torrent_target(
            t, rule_by_path, mikan_cache, season_cache, ep_cache
        )
        nm = t.get("name", "")[:55]
        if reason != "ok":
            print(f"   [mark] 跳过（{reason}）: {nm}")
            continue
        ep = parse_episode(t.get("name", ""))
        if dry_run:
            print(f"   [mark][dry] 会标记 ep{ep} 看过 (subject {bgm_id}): {nm}")
            marked += 1
            continue
        try:
            bgm_mark_episode_watched(token, eid)
            marked += 1
            print(f"   [mark] ✓ ep{ep} 看过 (subject {bgm_id}): {nm}")
        except Exception as ex:  # noqa: BLE001
            print(f"   [mark] ! 标记失败 ep{ep} (subject {bgm_id}): {ex}")

    if dry_run:
        print(f"# mark-watched [dry-run]: 命中 {marked} 个已暂停种子（未写入 bgm，未更新基线）")
    else:
        save_seed_states(states_new)
        print(f"# mark-watched: 标记 {marked} 集看过")


# --------------------------------------------------------------------------- #
# jfhook: Jellyfin「看完一集」-> ① 停该集做种  ② bgm 标该集看过
# --------------------------------------------------------------------------- #
# 与 mark-watched 互补、反向：mark 由「用户在 qB 手动暂停」驱动，jfhook 由
# 「Jellyfin 播完/勾选看过」驱动。两者经同一 resolve_torrent_target 收口，
# 因此旧番(< SKIP_BEFORE_SEASON)与 Ancient 在两条链路里都不会被动到。
JFHOOK_PORT_DEFAULT = int(CONFIG.get("jfhook_port", 8766))


def qb_stop(hashes: str) -> None:
    """停止做种：qB 5.x 用 torrents/stop，4.x 回退 torrents/pause。"""
    last = None
    for path in ("/api/v2/torrents/stop", "/api/v2/torrents/pause"):
        try:
            qb_post(path, {"hashes": hashes})
            return
        except Exception as ex:  # noqa: BLE001
            last = ex
    raise RuntimeError(f"stop/pause 均失败：{last}")


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("true", "1", "yes")


def _jf_is_watched_event(p: dict) -> bool:
    """判断这是不是一个「看完一集」事件。

    - PlaybackStop 且 PlayedToCompletion 为真（默认看到 >=90% 算完成）
    - UserDataSaved 且 SaveReason=TogglePlayed 且 Played 为真（在 Jellyfin 里手动勾选看过）
    """
    nt = (p.get("NotificationType") or "").strip()
    if nt == "PlaybackStop":
        return _truthy(p.get("PlayedToCompletion"))
    if nt == "UserDataSaved":
        return (p.get("SaveReason") or "").strip() == "TogglePlayed" and _truthy(p.get("Played"))
    return False


def _jf_path_is_protected(file_path: str) -> bool:
    """Ancient 路径硬闸：路径里出现 Ancient 这一层就整体放行不碰（最优先）。"""
    parts = [seg.strip().lower() for seg in file_path.replace("\\", "/").split("/")]
    return "ancient" in parts


def _jf_find_torrent(file_path: str, torrents: list[dict]) -> dict | None:
    """按文件名把 Jellyfin 播放的那一集对应到 qB 里的种子（单集单种）。

    锚点是文件名：JF 镜像与源是硬链接、文件名一致，单文件种子的 content_path/name
    也就是该文件名。大小写不敏感，扩展名可有可无。
    """
    target = os.path.basename(file_path.replace("\\", "/")).strip().lower()
    if not target:
        return None
    tstem = target.rsplit(".", 1)[0]
    for t in torrents:
        cand = os.path.basename((t.get("content_path") or "").replace("\\", "/")).strip().lower()
        if cand and (cand == target or cand.rsplit(".", 1)[0] == tstem):
            return t
    for t in torrents:
        nm = (t.get("name") or "").strip().lower()
        if nm and (nm == target or nm.rsplit(".", 1)[0] == tstem):
            return t
    return None


def _jf_item_path(item_id: str) -> str:
    """用 ItemId 反查该项的物理路径——webhook 模板没给 Path 时的兜底。"""
    if not (item_id and JELLYFIN_API_KEY):
        return ""
    try:
        _, dto = _jf_req("GET", f"/Items/{item_id}", params={"fields": "Path"})
        if isinstance(dto, dict) and dto.get("Path"):
            return dto["Path"]
    except Exception:  # noqa: BLE001
        pass
    try:
        uid = _jf_user_id()
        if uid:
            _, dto = _jf_req("GET", f"/Users/{uid}/Items/{item_id}")
            if isinstance(dto, dict) and dto.get("Path"):
                return dto["Path"]
    except Exception:  # noqa: BLE001
        pass
    return ""


def handle_jellyfin_event(payload: dict, token_provider, *, dry_run: bool = False) -> None:
    """处理一个 Jellyfin webhook 事件：命中「看完一集」就停做种 + 标 bgm 看过。

    全程 best-effort：任何一步失败都只打日志、绝不抛出（不能拖垮监听）。
    """
    if not _jf_is_watched_event(payload):
        return
    item_type = (payload.get("ItemType") or "").strip()
    if item_type and item_type != "Episode":
        return  # 只处理剧集，电影/合集等忽略
    file_path = payload.get("Path") or ""
    if not file_path:  # 模板没给 Path -> 用 ItemId 反查
        file_path = _jf_item_path(str(payload.get("ItemId") or "").strip())
    label = payload.get("SeriesName") or os.path.basename(file_path.replace("\\", "/")) or "?"
    if not file_path:
        print(f"   [jfhook] 事件无文件路径（且 ItemId 反查失败），跳过: {label}")
        return
    if _jf_path_is_protected(file_path):
        print(f"   [jfhook] 跳过（Ancient 保护）: {label}")
        return
    try:
        torrents = qb_get_json("/api/v2/torrents/info")
    except Exception as ex:  # noqa: BLE001
        print(f"   [jfhook] 读取 qB 失败，跳过：{ex}")
        return
    t = _jf_find_torrent(file_path, torrents)
    if t is None:
        print(f"   [jfhook] 未找到对应种子（可能已删/非自动下载）: {os.path.basename(file_path)}")
        return
    # 经 resolve_torrent_target 收口：它内置「无规则 / 无 bgm id / 旧番 < cutoff /
    # 集数解析失败」全部判为非 ok。只有 ok 时我们才停做种 + 标看过，足够保守。
    bgm_id, eid, reason = resolve_torrent_target(
        t, rules_by_savepath(existing_rules()), {}, {}, {}
    )
    nm = t.get("name", "")[:55]
    if reason != "ok":
        print(f"   [jfhook] 跳过（{reason}）: {nm}")
        return
    ep = parse_episode(t.get("name", ""))
    if dry_run:
        print(f"   [jfhook][dry] 会停做种 + 标 ep{ep} 看过 (subject {bgm_id}): {nm}")
        return
    try:
        qb_stop(t["hash"])
        print(f"   [jfhook] ✓ 已停做种 ep{ep}: {nm}")
    except Exception as ex:  # noqa: BLE001
        print(f"   [jfhook] ! 停做种失败 ep{ep}: {ex}")
    token = token_provider() if callable(token_provider) else token_provider
    if not token:
        print("   [jfhook] 未配置 bgm token，跳过标记")
        return
    try:
        bgm_mark_episode_watched(token, eid)
        print(f"   [jfhook] ✓ ep{ep} 看过 (subject {bgm_id}): {nm}")
    except Exception as ex:  # noqa: BLE001
        print(f"   [jfhook] ! 标记失败 ep{ep} (subject {bgm_id}): {ex}")


def run_jfhook_server(port: int, token_provider) -> http.server.ThreadingHTTPServer:
    """起一个常驻 HTTP 监听，接 Jellyfin Webhook 插件 POST 来的事件。

    立刻 200 应答（不让 Jellyfin 等），再同步处理事件（事件稀疏，无需排队）。
    GET / 作健康检查。
    """
    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静音默认访问日志
            pass

        def _ack(self, code: int = 200):
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            self._ack(200)

        def do_POST(self):
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n > 0 else b""
                self._ack(200)
            except Exception:  # noqa: BLE001
                try:
                    self._ack(400)
                except Exception:  # noqa: BLE001
                    pass
                return
            try:
                payload = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else {}
            except Exception as ex:  # noqa: BLE001
                print(f"   [jfhook] JSON 解析失败：{ex}")
                return
            try:
                handle_jellyfin_event(payload, token_provider)
            except Exception:  # noqa: BLE001
                print("   [jfhook] 处理事件出错：")
                traceback.print_exc()

    return http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)


def _jellyfin_refresh() -> None:
    """触发 Jellyfin 全库扫描，让新硬链接立刻可见（失败不影响 sync）。"""
    if not JELLYFIN_API_KEY:
        return
    try:
        req = urllib.request.Request(
            f"{JELLYFIN_URL}/Library/Refresh", data=b"",
            headers={"X-Emby-Token": JELLYFIN_API_KEY}, method="POST",
        )
        urllib.request.urlopen(req, timeout=15).read()
        print("# mirror: 已触发 Jellyfin 库扫描")
    except Exception as ex:  # noqa: BLE001
        print(f"# mirror: 触发扫描失败（不影响）：{ex}")


def mirror_sync_pass() -> int:
    """把 X:\\Bangumi\\<cour>\\<show>\\… 的新剧集硬链接到 X:\\BangumiJF\\<cour>\\<show>\\Season NN\\。

    只新建硬链接、从不删除（同 NTFS 盘，~0 额外占空间，不碰原文件/做种）。
    处理 >= MIRROR_SKIP_BEFORE_SEASON 的季度文件夹（默认 "" = 含旧番全镜像），
    只跳过 Ancient 等非 YYYY.MM 目录。幂等：已存在的跳过。
    返回本轮新建链接数；>0 时触发一次 Jellyfin 扫描。
    """
    src_root = Path(BANGUMI_LIBRARY)
    dst_root = Path(JELLYFIN_MIRROR)
    if not src_root.exists():
        return 0
    linked = 0
    for cour_dir in src_root.iterdir():
        if not cour_dir.is_dir():
            continue
        cour = cour_dir.name
        if not _COUR_DIR_RE.match(cour) or cour < MIRROR_SKIP_BEFORE_SEASON:
            continue  # 非 YYYY.MM（如 Ancient）：不碰；旧番默认仍镜像（MIRROR_SKIP_BEFORE_SEASON 默认 ""）
        for show_dir in cour_dir.iterdir():
            if not show_dir.is_dir():
                continue
            for f in show_dir.rglob("*"):
                if not f.is_file() or f.suffix.lower() not in MIRROR_VIDEO_EXT:
                    continue
                parents = f.relative_to(show_dir).parts[:-1]
                season = "Season 00" if any(p in MIRROR_SPECIAL_DIRS for p in parents) else "Season 01"
                link = dst_root / cour / show_dir.name / season / f.name
                if link.exists():
                    continue
                try:
                    link.parent.mkdir(parents=True, exist_ok=True)
                    os.link(str(f), str(link))  # NTFS 硬链接
                    linked += 1
                except OSError as ex:
                    print(f"# mirror: 链接失败 {link}：{ex}")
    if linked:
        print(f"# mirror: 新建 {linked} 个硬链接 -> Jellyfin")
        _jellyfin_refresh()
    return linked


# --- Jellyfin 自动建库：让库镜像 X:\BangumiJF 的季度文件夹 -------------------- #
# 本地新增一个季度（如 2026.07）-> mirror_sync_pass 先把它硬链接进 BangumiJF，
# 然后这里检测到「有文件夹却没对应 Jellyfin 库」，自动建库 + 季节封面 + 倒序重排 +
# 触发扫描。只新建、从不删库（本地删了也不动 Jellyfin，符合破坏性操作要谨慎原则）。

def _jf_req(method: str, path: str, params: dict | None = None,
            body: dict | None = None, timeout: int = 30):
    """Jellyfin REST 调用（X-Emby-Token）。返回 (status, json_or_None)。"""
    url = f"{JELLYFIN_URL}{path}"
    if params:
        url += ("&" if "?" in path else "?") + urllib.parse.urlencode(params, doseq=True)
    data = None
    headers = {"X-Emby-Token": JELLYFIN_API_KEY}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
        return r.status, (json.loads(raw) if raw.strip() else None)


def _jf_user_id() -> str | None:
    """取一个用户 id（优先管理员）用于设置 My Media 库顺序。"""
    try:
        _, users = _jf_req("GET", "/Users")
    except Exception:  # noqa: BLE001
        return None
    if not users:
        return None
    for u in users:
        if (u.get("Policy") or {}).get("IsAdministrator"):
            return u["Id"]
    return users[0]["Id"]


def _jf_make_cover_png(name: str) -> bytes | None:
    """复用 jellyfin-setup/season_covers.py 生成季节封面 PNG（缺模块/字体则返回 None）。"""
    tools = str(CONFIG.get("jellyfin_tools_dir", r"X:\Github\jellyfin-setup"))
    try:
        if tools not in sys.path:
            sys.path.insert(0, tools)
        import io
        import season_covers  # noqa: PLC0415
        buf = io.BytesIO()
        season_covers.make_cover(name).save(buf, "PNG")
        return buf.getvalue()
    except Exception as ex:  # noqa: BLE001
        print(f"# jellyfin: 封面生成跳过（{name}）：{ex}")
        return None


def _jf_upload_primary(item_id: str, png_bytes: bytes) -> None:
    """给库（CollectionFolder）上传 Primary 封面（body = base64 PNG）。"""
    req = urllib.request.Request(
        f"{JELLYFIN_URL}/Items/{item_id}/Images/Primary",
        data=base64.b64encode(png_bytes), method="POST",
        headers={"X-Emby-Token": JELLYFIN_API_KEY, "Content-Type": "image/png"},
    )
    urllib.request.urlopen(req, timeout=30).read()


def _jf_reorder_views() -> None:
    """My Media 库顺序：季度倒序（新番最前）+ Ancient 压底，写 UserConfiguration.OrderedViews。"""
    uid = _jf_user_id()
    if not uid:
        return
    try:
        _, vfs = _jf_req("GET", "/Library/VirtualFolders")
        idmap = {v["Name"]: v["ItemId"] for v in (vfs or [])}
        seasons = sorted((n for n in idmap if n.lower() != "ancient"), reverse=True)
        ordered = seasons + (["Ancient"] if "Ancient" in idmap else [])
        _, user = _jf_req("GET", f"/Users/{uid}")
        conf = user["Configuration"]
        conf["OrderedViews"] = [idmap[n] for n in ordered]
        _jf_req("POST", f"/Users/{uid}/Configuration", body=conf)
    except Exception as ex:  # noqa: BLE001
        print(f"# jellyfin: 重排库顺序失败（不影响）：{ex}")


def jellyfin_ensure_libraries() -> int:
    """让 Jellyfin 库镜像 X:\\BangumiJF 顶层文件夹：缺哪个季度库就建哪个。

    每个季度文件夹 -> 一个单路径 tvshows 库；新建后上季节封面、倒序重排、触发扫描。
    只新建、从不删库。返回本轮新建库数（无新增则 0，几乎零开销）。
    """
    if not JELLYFIN_API_KEY:
        return 0
    dst_root = Path(JELLYFIN_MIRROR)
    if not dst_root.exists():
        return 0
    try:
        _, vfs = _jf_req("GET", "/Library/VirtualFolders")
    except Exception as ex:  # noqa: BLE001
        print(f"# jellyfin: 取库列表失败（不影响）：{ex}")
        return 0
    have = {v["Name"] for v in (vfs or [])}
    folders = sorted(d.name for d in dst_root.iterdir() if d.is_dir())
    missing = [n for n in folders if n not in have]
    if not missing:
        return 0
    created: list[str] = []
    for name in missing:
        path = str(dst_root / name)
        params = {"name": name, "collectionType": "tvshows",
                  "paths": path, "refreshLibrary": "false"}
        body = {"LibraryOptions": {"PathInfos": [{"Path": path}],
                                   "EnableRealtimeMonitor": True}}
        try:
            _jf_req("POST", "/Library/VirtualFolders", params=params, body=body)
            created.append(name)
            print(f"# jellyfin: 新建分类库 {name}")
        except Exception as ex:  # noqa: BLE001
            print(f"# jellyfin: 建库失败 {name}：{ex}")
    if not created:
        return 0
    # 上封面（best-effort：拿新库 ItemId 再逐个上传）
    try:
        _, vfs2 = _jf_req("GET", "/Library/VirtualFolders")
        idmap = {v["Name"]: v["ItemId"] for v in (vfs2 or [])}
        for name in created:
            png = _jf_make_cover_png(name)
            if png and name in idmap:
                try:
                    _jf_upload_primary(idmap[name], png)
                    print(f"# jellyfin: 已上封面 {name}")
                except Exception as ex:  # noqa: BLE001
                    print(f"# jellyfin: 上封面失败 {name}：{ex}")
    except Exception as ex:  # noqa: BLE001
        print(f"# jellyfin: 封面阶段出错（不影响）：{ex}")
    _jf_reorder_views()
    _jellyfin_refresh()
    print(f"# jellyfin: 自动新增 {len(created)} 个分类库：{', '.join(created)}")
    return len(created)


def jellyfin_prune_deleted() -> int:
    """让 Jellyfin/BangumiJF 完全镜像 X:\\Bangumi：源里删掉的季度 -> 删 BangumiJF 硬链接 + Jellyfin 库。

    以 `X:\\Bangumi`（用户实际操作的源）为真相，对**所有季度**（含旧番/Ancient）生效。
    **只删 BangumiJF 镜像（硬链接）与库定义，绝不碰 X:\\Bangumi 源文件/做种。**
    多重安全闸：源不存在/为空（疑似盘未挂载）一律中止；一次要删超过半数也中止。
    返回删除的季度数。
    """
    src_root = Path(BANGUMI_LIBRARY)   # X:\Bangumi 真相源
    dst_root = Path(JELLYFIN_MIRROR)   # X:\BangumiJF 镜像
    # —— 安全闸 1/2：源不可用绝不删 ——
    if not src_root.exists():
        print("# jellyfin-prune: 源 X:\\Bangumi 不存在，跳过删除（安全）")
        return 0
    src = {d.name for d in src_root.iterdir() if d.is_dir()}
    if not src:
        print("# jellyfin-prune: 源 X:\\Bangumi 为空，跳过删除（安全，疑似盘未挂载）")
        return 0
    if not dst_root.exists():
        return 0
    mirror_folders = {d.name for d in dst_root.iterdir() if d.is_dir()}
    orphans = sorted(mirror_folders - src)
    if not orphans:
        return 0
    # —— 安全闸 3：一次删太多疑似异常，中止 ——
    if len(orphans) > max(3, len(mirror_folders) // 2):
        print(f"# jellyfin-prune: 异常！将删 {len(orphans)}/{len(mirror_folders)} 个季度，"
              f"疑似源异常，中止（{', '.join(orphans)}）")
        return 0
    import shutil
    try:
        _, vfs = _jf_req("GET", "/Library/VirtualFolders")
    except Exception as ex:  # noqa: BLE001
        print(f"# jellyfin-prune: 取库失败，跳过：{ex}")
        return 0
    libmap = {v["Name"]: v["ItemId"] for v in (vfs or [])}
    # 前缀带尾分隔符，按目录边界比较（避免 "Bangumi" 误判为 "BangumiJF" 的前缀）
    dst_prefix = str(dst_root).rstrip("\\").lower() + "\\"
    src_prefix = str(src_root).rstrip("\\").lower() + "\\"
    deleted = 0
    for name in orphans:
        # 1) 删 Jellyfin 库（只删库定义）
        if name in libmap:
            try:
                _jf_req("DELETE", "/Library/VirtualFolders",
                        params={"name": name, "refreshLibrary": "false"})
                print(f"# jellyfin-prune: 删除 Jellyfin 库 {name}")
            except Exception as ex:  # noqa: BLE001
                print(f"# jellyfin-prune: 删库失败 {name}：{ex}")
        # 2) 删 BangumiJF 硬链接文件夹（双重断言：必须在 BangumiJF 下、绝不在源下）
        target = dst_root / name
        tpath = str(target).rstrip("\\").lower() + "\\"
        if not tpath.startswith(dst_prefix) or tpath.startswith(src_prefix):
            print(f"# jellyfin-prune: 路径安全检查未过，跳过 {target}")
            continue
        try:
            shutil.rmtree(target)
            print(f"# jellyfin-prune: 删除镜像硬链接 {target}")
            deleted += 1
        except Exception as ex:  # noqa: BLE001
            print(f"# jellyfin-prune: 删镜像失败 {target}：{ex}")
    if deleted:
        _jf_reorder_views()
        _jellyfin_refresh()
        print(f"# jellyfin-prune: 联动删除 {deleted} 个季度：{', '.join(orphans)}")
    return deleted


def run_sync_once(user, cookie, season, purge, token=None):
    """One pass: add new 在看, reconcile removed, mark paused eps. Shared by `sync`/`watch`."""
    print(f"=== sync @ {datetime.datetime.now():%Y-%m-%d %H:%M:%S} (user {user}) ===")
    plan = build_plan(user, season)
    to_add = [e for e in plan if e["include"] and e.get("feed") and e.get("name")]
    if to_add:
        apply_entries(to_add, cookie, dry_run=False)
    else:
        print("# no new shows to add")
    reconcile_removed(user, cookie, dry_run=False, purge_dropped=purge)
    if CONFIG.get("mark_watched_enabled", True):
        try:
            mark_watched_pass(token)
        except Exception:  # noqa: BLE001
            print("!!! mark-watched 出错（不影响本轮 sync）：")
            traceback.print_exc()
    if CONFIG.get("jellyfin_mirror_enabled", True):
        try:
            mirror_sync_pass()
        except Exception:  # noqa: BLE001
            print("!!! mirror-sync 出错（不影响本轮 sync）：")
            traceback.print_exc()
    if CONFIG.get("jellyfin_mirror_delete_enabled", True):
        try:
            jellyfin_prune_deleted()
        except Exception:  # noqa: BLE001
            print("!!! jellyfin-prune 出错（不影响本轮 sync）：")
            traceback.print_exc()
    if CONFIG.get("jellyfin_autolib_enabled", True):
        try:
            jellyfin_ensure_libraries()
        except Exception:  # noqa: BLE001
            print("!!! jellyfin-autolib 出错（不影响本轮 sync）：")
            traceback.print_exc()
    print("=== sync done ===")


def cmd_sync(args):
    """One-shot autonomous sync: add new 在看, reconcile removed. For schedulers."""
    season = args.season or current_season()
    purge = args.purge_files or bool(CONFIG.get("purge_dropped_files"))
    run_sync_once(args.user, mikan_cookie(args), season, purge, bgm_token(args))


def cmd_mark(args):
    """Standalone mark-watched pass. --dry-run reports resolution without writing."""
    mark_watched_pass(bgm_token(args), dry_run=args.dry_run)


def cmd_auth(args):
    """One-time OAuth setup so the bgm token auto-renews (no yearly reissue).

    Step 1: `auth`           -> prints the authorize URL.
    Step 2: `auth --code X`  -> exchanges the callback code for tokens.
    """
    cid, sec, uri = _bgm_oauth_creds()
    if not cid or not sec:
        sys.exit(
            "请先在 config.local.json 填 bgm_client_id / bgm_client_secret\n"
            "（到 https://bgm.tv/dev/app 创建应用，回调地址填 http://localhost）"
        )
    if args.code:
        tok = _bgm_oauth_post({
            "grant_type": "authorization_code", "client_id": cid,
            "client_secret": sec, "code": args.code, "redirect_uri": uri,
        })
        if not tok.get("access_token"):
            sys.exit(f"换取 token 失败：{tok}")
        _save_bgm_oauth_token(tok)
        print(f"✓ 已保存到 {BGM_TOKEN_PATH.name}，refresh_token 就位，以后自动续期。")
        print(f"  access_token 有效期 {tok.get('expires_in')}s（过期前脚本会自动换新）")
        return
    url = f"{BGM_AUTHORIZE}?" + urllib.parse.urlencode(
        {"client_id": cid, "response_type": "code", "redirect_uri": uri}
    )
    print("1) 浏览器打开并授权：")
    print("   " + url)
    print(f"2) 授权后会跳到 {uri}/?code=XXXX （页面打不开没关系，看地址栏的 code）")
    print("3) 运行： PYTHONUTF8=1 python anime_rss.py auth --code XXXX")


def cmd_jfhook(args):
    """独立常驻：只起 Jellyfin webhook 监听（不跑 sync），便于单独调试。"""
    port = args.port if args.port is not None else JFHOOK_PORT_DEFAULT
    httpd = run_jfhook_server(port, lambda: bgm_token(args))
    print(f"=== jfhook 独立监听 :{port}（Ctrl-C 退出）===", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("=== jfhook 退出 ===", flush=True)


def cmd_watch(args):
    """Long-running daemon: run a sync pass every --interval seconds.

    A single failing pass (network blip, etc.) never kills the daemon —
    it is logged and the loop waits for the next tick. Re-reads the
    cookie and recomputes the season each pass so a cour rollover and a
    refreshed mikan cookie are picked up without a restart.
    """
    interval = args.interval or int(CONFIG.get("watch_interval_seconds", 300))
    user = args.user
    purge = args.purge_files or bool(CONFIG.get("purge_dropped_files"))
    print(f"=== watch 启动 @ {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
          f"：每 {interval}s 跑一次 sync（Ctrl-C 退出）===", flush=True)
    # 搭车起 jfhook 监听线程：Jellyfin 看完一集 -> 停做种 + 标看过。
    # port<=0 关闭。token 每次事件即时取（bgm_token 会自动续期/读文件）。
    hook_port = (args.jfhook_port if args.jfhook_port is not None
                 else JFHOOK_PORT_DEFAULT)
    if hook_port and hook_port > 0:
        try:
            httpd = run_jfhook_server(hook_port, lambda: bgm_token(args))
            threading.Thread(target=httpd.serve_forever, name="jfhook",
                             daemon=True).start()
            print(f"=== jfhook 监听 :{hook_port}（Jellyfin 看完→停做种+标看过）===",
                  flush=True)
        except Exception as ex:  # noqa: BLE001
            print(f"!!! jfhook 监听启动失败（不影响 sync）：{ex}", flush=True)
    while True:
        try:
            run_sync_once(
                user, mikan_cookie(args), args.season or current_season(),
                purge, bgm_token(args),
            )
        except KeyboardInterrupt:
            print("=== watch 收到中断，退出 ===", flush=True)
            return
        except Exception:
            print("!!! 本轮 sync 出错，跳过，等下一轮：", flush=True)
            traceback.print_exc()
        sys.stdout.flush()
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("=== watch 收到中断，退出 ===", flush=True)
            return


def main():
    default_user = CONFIG.get("bgm_user")

    def add_user(sp):
        sp.add_argument("--user", default=default_user, required=not default_user,
                        help="bangumi.tv username or id (default: config.local.json)")

    def add_cookie(sp):
        sp.add_argument("--mikan-cookie", default=None,
                        help=".AspNetCore.Identity.Application cookie (default: config)")

    def add_token(sp):
        sp.add_argument("--bgm-token", default=None, dest="bgm_token",
                        help="bangumi.tv access token (default: config bgm_access_token)")

    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="print bangumi 在看 list")
    add_user(pl)
    pl.set_defaults(func=cmd_list)

    pp = sub.add_parser("plan", help="resolve shows -> write editable plan.json")
    add_user(pp)
    pp.add_argument("--season", help="YYYY.MM cour (default: auto from today)")
    pp.set_defaults(func=cmd_plan)

    pa = sub.add_parser("apply", help="create qB feeds+rules from plan.json")
    add_cookie(pa)
    pa.add_argument("--dry-run", action="store_true")
    pa.set_defaults(func=cmd_apply)

    pr = sub.add_parser("prune", help="tear down rules whose show left 在看")
    add_user(pr)
    add_cookie(pr)
    pr.add_argument("--dry-run", action="store_true")
    pr.add_argument("--purge-files", action="store_true",
                    help="also DELETE local files for 抛弃/未收藏 shows")
    pr.set_defaults(func=cmd_prune)

    ps = sub.add_parser("sync", help="one-shot: add new + reconcile removed (for schedulers)")
    add_user(ps)
    add_cookie(ps)
    add_token(ps)
    ps.add_argument("--season", help="YYYY.MM cour (default: auto from today)")
    ps.add_argument("--purge-files", action="store_true",
                    help="also DELETE local files for 抛弃/未收藏 shows")
    ps.set_defaults(func=cmd_sync)

    pm = sub.add_parser("mark", help="mark paused-torrent episodes 看过 on bgm (one pass)")
    add_token(pm)
    pm.add_argument("--dry-run", action="store_true",
                    help="report resolution for all stopped torrents; no bgm write, no baseline update")
    pm.set_defaults(func=cmd_mark)

    pau = sub.add_parser("auth", help="one-time OAuth setup so bgm token auto-renews")
    pau.add_argument("--code", default=None, help="callback ?code= from the authorize redirect")
    pau.set_defaults(func=cmd_auth)

    pw = sub.add_parser("watch", help="long-running daemon: sync every --interval seconds")
    add_user(pw)
    add_cookie(pw)
    add_token(pw)
    pw.add_argument("--season", help="YYYY.MM cour (default: auto each pass)")
    pw.add_argument("--interval", type=int, default=None,
                    help="seconds between passes (default: config watch_interval_seconds or 300)")
    pw.add_argument("--jfhook-port", type=int, default=None, dest="jfhook_port",
                    help="Jellyfin webhook 监听端口；0 关闭（default: config jfhook_port 或 8766）")
    pw.add_argument("--purge-files", action="store_true",
                    help="also DELETE local files for 抛弃/未收藏 shows")
    pw.set_defaults(func=cmd_watch)

    pj = sub.add_parser("jfhook", help="standalone: serve Jellyfin webhook (停做种+标看过)")
    add_token(pj)
    pj.add_argument("--port", type=int, default=None,
                    help="listen port (default: config jfhook_port 或 8766)")
    pj.set_defaults(func=cmd_jfhook)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
