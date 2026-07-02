# anime-rss-auto

**English** | [简体中文](README.zh-CN.md)

Fully automated seasonal anime pipeline for Windows:

```
bangumi.tv (watching list)
     │  poll every 5 min
     ▼
mikanani.me (resolve show → RSS feed of ONE subtitle group)
     │
     ▼
qBittorrent (RSS auto-download rules, your naming conventions)
     │  hardlink mirror
     ▼
Jellyfin (per-season libraries auto-created, covers, ordering)
     │  webhook: watched an episode
     ▼
stop seeding that episode + mark it watched on bangumi.tv
```

Mark a show as **watching** on bangumi.tv and everything else — subscription,
download, Jellyfin library, watched-state sync back — happens on its own. Or
just mark it **plan-to-watch**: it is promoted to watching automatically the
day it premieres (advance-release / 先行版 dumps are filtered out).

## Features

| Module | What it does | Toggle |
|---|---|---|
| **sync / watch** | bgm 在看 list → mikan feed + qB rule (savePath `<library>\<YYYY.MM>\<name>`, season tag) | core |
| **premiere watch** | a 想看 (plan-to-watch) show is auto-promoted to 在看 the day it premieres (gated by the bgm first-episode airdate; optional `premiere_times.json` override), firing a panel banner. Advance-release (先行版) items are rejected by name (先行/予告), size (> 2 GB), and pre-air publish date | `premiere_watch_enabled` |
| **subgroup priority** | picks one subtitle group per show by your ranked list; never downloads duplicates | `group_priority` |
| **ANi grace fuse** | if the top-priority group hasn't published when a show first appears on mikan, wait N hours before locking a lower one (missed items are backfilled from the feed) | `ani_grace_hours` |
| **reconcile** | show moved to 看过/抛弃 → drop the qB rule (files kept); removed from collection entirely → unsubscribe + delete files | `purge_dropped_files` |
| **season cutoff** | shows older than a cour cutoff are never touched — no adds, no deletes | `skip_before_season` |
| **mark-watched** | you pause a finished torrent in qB → that episode is marked watched on bgm (transition-based, never bulk-marks) | `mark_watched_enabled` |
| **Jellyfin mirror** | hardlinks new episodes into a `<mirror>\<cour>\<show>\Season 01\` tree (0 extra bytes, seeding untouched) | `jellyfin_mirror_enabled` |
| **Jellyfin autolib** | new cour folder → auto-create a Jellyfin library with a generated cover, newest-first ordering | `jellyfin_autolib_enabled` |
| **Jellyfin prune** | cour deleted from the source library → mirror + Jellyfin library removed (multiple safety gates) | `jellyfin_mirror_delete_enabled` |
| **Jellyfin empty-series self-heal** | a mirror-rebuild race can leave a series looking empty in Jellyfin, so playback fails with "Unable to find a valid media source" → one API call per pass finds series with 0 episodes but video on disk and recursively refreshes them (zero cost for healthy shows, guarded against mid-scan storms) | `jellyfin_heal_empty_enabled` |
| **jfhook** | Jellyfin Webhook plugin → finished an episode → stop seeding it + mark watched on bgm | `jfhook_port` |
| **web UI** | local dashboard: all bgm-marked shows grouped by collection type (在看/想看/看过/搁置/抛弃) with a type filter, per-show premiere time in **your** local timezone (via AniList), premiere banners, grace countdowns, switch subtitle group, torrent progress, manual sync, logs | `webui.py` |

Each module is independently toggleable in config — take what you need.

## Files

- `anime_rss.py` — everything above except the panel; stdlib only, single file.
  Subcommands: `list`, `plan`, `apply`, `prune`, `sync`, `watch`, `mark`, `premiere`, `auth`, `jfhook`.
- `webui.py` + `static/index.html` — FastAPI control panel on `http://127.0.0.1:8767`.
- `run_watch*.bat/vbs`, `run_webui*.bat/vbs` — hidden autostart launchers
  (drop shortcuts to the `.vbs` files into `shell:startup`).

## Setup

1. Requirements: Windows, Python 3.11+, qBittorrent with Web UI (localhost,
   passwordless), and optionally Jellyfin + the Webhook plugin.
   The panel needs `pip install fastapi uvicorn`.
2. Copy `config.example.json` → `config.local.json`, fill in your values
   (bgm user id, mikan cookie, Jellyfin API key, paths).
3. One-shot: `set PYTHONUTF8=1 && python anime_rss.py sync`
   Daemon: `python anime_rss.py watch` (sync every 5 min + jfhook listener).
4. Panel: `python webui.py` → open http://127.0.0.1:8767.

bgm token: either a 365-day personal token (`bgm_access_token`) or OAuth with
auto-refresh — create an app at https://bgm.tv/dev/app, fill
`bgm_client_id`/`bgm_client_secret`, run `python anime_rss.py auth` once.

## Conventions this automates

- qB save path `<bangumi_library>\<YYYY.MM>\<English show name>`, tag `<YYYY.MM>`.
- RSS feeds nest under a `<YYYY.MM>` folder, which is created explicitly before
  subscribing — qBittorrent 5.x's `addFeed` does not auto-create parent folders
  (a missing season folder makes it 409, leaving a rule with no feed behind).
- One subtitle group per show — the mikan RSS URL itself is group-scoped.
- Cours: 01 / 04 / 07 / 10; a cour string sorts lexicographically (`2026.04 < 2026.07`).
- Destructive actions (deleting files/rules) only ever apply to shows from
  `skip_before_season` onward; older shows are strictly read-only to the tool.

## Safety notes

- `config.local.json` holds all secrets and is gitignored; nothing sensitive
  is hardcoded.
- The Jellyfin prune step refuses to run if the source library is missing or
  empty (unmounted-drive protection) and aborts on implausibly large deletions.
- The web UI binds to 127.0.0.1 by default; set `webui_host: "0.0.0.0"` only
  on a trusted LAN (it has no authentication).
