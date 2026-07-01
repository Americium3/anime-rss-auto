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
import io
import json
import os
import re
import threading
import traceback
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
                "image": img.get("common") or img.get("medium") or "",
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
    for gid, name in re.findall(
        r'href="/Home/PublishGroup/(\d+)"[^>]*>([^<]+)</a>', html_txt
    ):
        _group_names.setdefault(int(gid), name.strip())
    ids = sorted({int(x) for x in re.findall(r"subgroupid=(\d+)", html_txt)})
    return [{"id": i, "name": _group_names.get(i, f"Group {i}")} for i in ids]


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

    try:
        torrents = core.qb_get_json("/api/v2/torrents/info")
    except Exception:  # noqa: BLE001
        torrents = []

    # rule name -> bgm id (mikan page fetches are cached across polls)
    rule_of: dict[int, tuple[str, dict]] = {}
    for rname, rdef in rules.items():
        bid = core.rule_bgm_id(rdef, _mikan_bgm)
        if bid:
            rule_of[bid] = (rname, rdef)

    out_shows = []
    for s in shows:
        season = core.season_of(s["date"])
        is_old = core.is_manual_old_show(s["date"])
        entry = {
            "bgm_id": s["bgm_id"],
            "title": s["name_cn"] or s["name"],
            "title_jp": s["name"],
            "date": s["date"],
            "season": season,
            "image": s["image"],
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
                "save_path": save_path,
                "mikan_id": mid,
                "subgroup": gid,
                "subgroup_name": _group_names.get(gid, f"Group {gid}") if gid else None,
            }
            entry["status"] = "subscribed"
            norm = save_path.replace("\\", "/").rstrip("/").lower()
            for t in torrents:
                sp = (t.get("save_path") or "").replace("\\", "/").rstrip("/").lower()
                if sp == norm:
                    entry["torrents"].append({
                        "name": t.get("name", ""),
                        "progress": round(float(t.get("progress", 0)), 4),
                        "state": t.get("state", ""),
                        "size": t.get("size", 0),
                        "added_on": t.get("added_on", 0),
                    })
            entry["torrents"].sort(key=lambda x: x["added_on"], reverse=True)
        g = grace.get(str(s["bgm_id"]))
        if g is not None and entry["status"] != "subscribed":
            entry["status"] = "grace"
            entry["grace"] = {
                "first_seen": g,
                "expires": g + core.GRACE_HOURS * 3600,
            }
        out_shows.append(entry)

    jf_libs = []
    try:
        _, vfs = core._jf_req("GET", "/Library/VirtualFolders")
        names = [v["Name"] for v in (vfs or [])]
        cours = sorted((n for n in names if core._COUR_DIR_RE.match(n)), reverse=True)
        jf_libs = cours + sorted(n for n in names if not core._COUR_DIR_RE.match(n))
    except Exception:  # noqa: BLE001
        pass

    return {
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "season": season_now,
        "grace_hours": core.GRACE_HOURS,
        "preferred_group": _group_names.get(core.PREFERRED_GID),
        "group_priority": [
            {"id": gid, "name": core.GROUP_NAME[gid]} for gid in core.PRIORITY_IDS
        ],
        "last_sync": last_sync_time(),
        "sync_running": _sync_running,
        "jellyfin": {"url": core.JELLYFIN_URL, "libraries": jf_libs},
        "shows": out_shows,
    }


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
        raise HTTPException(502, f"mikan fetch failed: {ex}")


# --- mutating actions ------------------------------------------------------ #
_sync_running = False
_sync_buf: io.StringIO | None = None


@app.post("/api/sync")
def api_sync():
    global _sync_running, _sync_buf
    if _sync_running:
        return {"started": False, "code": "already_running", "reason": "sync already running"}
    _sync_running = True
    _sync_buf = io.StringIO()

    def run():
        global _sync_running
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
        finally:
            _sync_running = False

    threading.Thread(target=run, daemon=True).start()
    return {"started": True}


@app.get("/api/sync/status")
def api_sync_status():
    return {
        "running": _sync_running,
        "output": _sync_buf.getvalue() if _sync_buf else "",
    }


class GraceExpire(BaseModel):
    bgm_id: int


@app.post("/api/grace/expire")
def api_grace_expire(body: GraceExpire):
    grace = core.load_grace()
    key = str(body.bgm_id)
    if key not in grace:
        raise HTTPException(404, "show is not in a grace period")
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
        raise HTTPException(404, f"no qB rule named {body.rule_name!r}")
    mid, old_gid = rule_subgroup(rdef)
    if not mid:
        raise HTTPException(400, "rule has no mikan feed to switch")
    if old_gid == body.subgroup:
        return {"ok": True, "code": "switched",
                "group": _group_names.get(body.subgroup, str(body.subgroup)),
                "note": "already on that subgroup"}

    notes = []
    old_feed = core.feed_url(mid, old_gid)
    new_feed = core.feed_url(mid, body.subgroup)
    season = (rdef.get("torrentParams", {}).get("tags") or [core.current_season()])[0]

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
    try:
        core.qb_post("/api/v2/rss/setRule",
                     {"ruleName": body.rule_name, "ruleDef": json.dumps(rdef)})
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(502, f"setRule failed: {ex}")

    # 3) move the mikan subscription (best effort)
    cookie = core.CONFIG.get("mikan_cookie")
    if cookie:
        for fn, gid in ((core.mikan_unsubscribe, old_gid), (core.mikan_subscribe, body.subgroup)):
            try:
                fn(cookie, mid, gid)
            except Exception as ex:  # noqa: BLE001
                notes.append(f"mikan {fn.__name__}: {ex}")

    grp = _group_names.get(body.subgroup, str(body.subgroup))
    return {"ok": True, "code": "switched", "group": grp, "notes": notes,
            "note": f"rule now follows {grp}"}


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


if __name__ == "__main__":
    print(f"=== anime-rss-auto webui on http://{HOST}:{PORT} ===", flush=True)
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
