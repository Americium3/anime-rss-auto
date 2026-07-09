# <img src="static/icon.svg" width="28" align="top" alt="logo"> anime-rss-auto

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
| **show resolution** | maps a bgm show to its mikan feed by searching mikan for the show's `name_cn` / `name`, then its bgm aliases (别名 / romaji) when those miss — mikan indexes release/original names, so a display 中文名 that differs from them (e.g. `正后方的神威` vs. mikan's `从后面来的神威先生`) still resolves through the romaji alias. Every candidate is confirmed by matching the mikan page's bgm id, so a loose name never binds the wrong show. A stubborn mismatch that even aliases miss can be pinned in `mikan_overrides.json` (`bgm_id` → mikan `bangumiId`), which is consulted first. Any show that has **already aired** but still resolves to nothing is surfaced as a warning banner on the panel — a persistent state, not a one-shot event, so it clears itself once the show resolves — instead of failing silently | `unresolved_scan_enabled` / `mikan_overrides.json` |
| **prefer-variant** | some groups publish one episode in several variants — by source (Baha / CR / ABEMA / B-Global…) or by subtitle language (简日双语 / 繁日双语, also written JPSC / JPTC / CHS / CHT). Blacklisted sources (ABEMA, B-Global) are folded into every rule's `mustNotContain` so the feed rejects them and they never download; if more than one variant of the same episode does land, only the highest-priority one is kept after download and the rest are deleted (files included). Ranking is lexicographic across dimensions (source first, then language, then revision: Baha ＞ CR, simplified ＞ traditional, original ＞ V2/V3). The language tier is a set of synonym markers, so both CJK (简/繁) and Latin abbreviations (SC/TC/CHS/CHT/GB/BIG5, incl. glued forms like JPSC/JPTC) are recognized while mid-word false hits (disc/watch) are avoided. Revision is the lowest-priority tiebreaker: when the same episode lands as both an original and a re-release (`[V2]` / `04v2`), the original you already have is kept and the later revision is deleted — a lone V2 with no sibling is never touched. Untagged releases are left untouched, and only cours after the cutoff are affected. Run standalone with `python anime_rss.py dedup [--dry-run]` | `prefer_variant_enabled` / `source_blacklist` / `source_priority` / `lang_priority` |
| **no-raw guard** | a fallback subtitle group (not in `group_priority`, so no custom filter) would otherwise get an empty `mustContain` and grab anything in its mikan feed — including cross-posted streaming raws with no Chinese subtitle (e.g. a Netflix dual-audio rip carrying the show's English title). Two lines of defence: new rules for such groups require a Chinese-subtitle marker in the title (`cjk_sub_required`, a single `\|`-OR term so the feed rejects raw releases), and any downloaded torrent whose name matches a raw-platform tag (`hard_reject_tags`, e.g. `NF WEB-DL`) is deleted outright regardless of siblings. Both respect the season cutoff | `cjk_sub_required` / `hard_reject_tags` |
| **ANi grace fuse** | if the top-priority group hasn't published when a show first appears on mikan, wait N hours before locking a lower one (missed items are backfilled from the feed) | `ani_grace_hours` |
| **reconcile** | show moved to 看过/抛弃 → drop the qB rule (files kept); removed from collection entirely → unsubscribe + delete files | `purge_dropped_files` |
| **season cutoff** | shows older than a cour cutoff are never touched — no adds, no deletes. A cross-cour show (半年番/年番) keeps being auto-managed while it is still broadcasting, decided from the authoritative broadcast schedule (bgm per-episode airdates: the show counts as current while its final scheduled episode airdate is today or later; AniList `status` is a fallback). Once it finishes airing it reverts to a manual old show — so a still-running 2-cour/year-long show is not frozen just because its start cour fell behind the cutoff, and a long show that finished seasons ago is never re-touched. Individual shows can also be pinned "current" by bgm id | `skip_before_season` / `pin_current_bgm_ids` |
| **mark-watched** | you pause a finished torrent in qB → that episode is marked watched on bgm (transition-based, never bulk-marks) | `mark_watched_enabled` |
| **autocomplete** | once every main-story episode of a 在看 show is marked watched on bgm (by you, or by mark-watched / jfhook), the whole show is auto-promoted to 看过 — which reconcile then acts on (drop the qB rule, keep mikan + files), firing a panel banner with a one-click "rate & review on bgm" link straight to the show's subject page. Two guards keep it honest: it reads per-episode collection status (not the unreliable `eps` count), and it only fires once the finale has aired (every 本篇 airdate ≤ today), so a still-airing show whose listed episodes you happen to have all watched is never collected early. Respects the season cutoff like every other write pass. Run standalone with `python anime_rss.py autocomplete [--dry-run]` | `autocomplete_watched_enabled` |
| **Jellyfin mirror** | hardlinks new episodes into a `<mirror>\<cour>\<show>\Season 01\` tree (0 extra bytes, seeding untouched) | `jellyfin_mirror_enabled` |
| **Jellyfin autolib** | new cour folder → auto-create a Jellyfin library with a generated cover, newest-first ordering | `jellyfin_autolib_enabled` |
| **Jellyfin prune** | cour deleted from the source library → mirror + Jellyfin library removed; and per-file: a video whose source file no longer exists (e.g. a variant removed by prefer-variant or a subtitle-group switch) has its orphaned mirror hardlink pruned so Jellyfin stops showing the stale version (multiple safety gates: aborts if source root missing/empty, skips a show with zero source videos) | `jellyfin_mirror_delete_enabled` |
| **Jellyfin empty-series self-heal** | a mirror-rebuild race can leave a series looking empty in Jellyfin, so playback fails with "Unable to find a valid media source" → one API call per pass finds series with 0 episodes but video on disk and recursively refreshes them (zero cost for healthy shows, guarded against mid-scan storms) | `jellyfin_heal_empty_enabled` |
| **jfhook** | Jellyfin Webhook plugin → finished an episode → stop seeding it + mark watched on bgm | `jfhook_port` |
| **web UI** | local dashboard: all bgm-marked shows grouped by collection type (在看/想看/看过/搁置/抛弃) with a type filter plus a live title search box, card outlines glow in their collection type's color on hover, card titles are real links to the show's bangumi page (middle-click/keyboard friendly) and follow the UI language (English/romaji titles via AniList in the English UI), every list (except the timetable) split into cour blocks with a full-width color-coded season divider ahead of each block and season-unknown shows sinking to the end — Watching and Plan-to-watch run oldest cour first (a backlog to clear), the Completed / On-hold / Dropped tabs newest cour first; inside a block the Completed tab orders by most recently marked (it reads as a viewing history) while the other tabs order by premiere time (earliest first), live output of a manual sync in the log panel (kept readable after the pass ends; failures toast and auto-open the log), a weekly timetable tab (watching + plan-to-watch shows with air status shown by the time slot's color, a premiere-status filter and a "this season only" toggle that limits the board to the current cour — pinned and still-airing cross-cour shows count as current and stay visible, one column per weekday starting from today, today's column highlighted, airing slots in **your** local timezone, each card also carries its full localized premiere date/time), the watching grid shows each show's weekly update slot (weekday + local time) on the card and has a weekday sub-filter (one chip per weekday, with live counts) to narrow it to a single update day, a 半年番/年番 badge on every multi-cour show's card (classified by the broadcast-schedule span — first-to-last episode airdate — not raw episode count), dark/light theme with follow-system default, premiere banners stay for a week unless dismissed (with a dismiss-all button), "no mikan match" warning banners for already-aired shows the resolver couldn't map to a feed (persist until the show resolves or you dismiss them, with a dismiss-all button), per-show premiere time in **your** local timezone (via AniList), grace countdowns, switch subtitle group (deletes the old group's downloaded files for that show and re-downloads the whole season from the new group, episode for episode — irreversible, guarded by the season cutoff and confirmed with a dialog), per-show "n/m episodes ready to watch" summary, color-coded season badges (8 colors cycling every 2 years), bangumi community rating badge on every card, selected tab persists and is deep-linkable (`?tab=schedule&theme=light&lang=en`), a full-screen branded boot screen with a staged progress bar while the dashboard loads (turns amber and retries every 5 s if the backend is down; `?boothold` freezes it for screenshots), all timestamps localized, offline + qBittorrent-down indicators in the header, phone-friendly layout with proper touch targets, keyboard/screen-reader accessible, reduced-motion aware | `webui.py` |

Each module is independently toggleable in config — take what you need.

## Files

- `anime_rss.py` — everything above except the panel; stdlib only, single file.
  Subcommands: `list`, `plan`, `apply`, `prune`, `sync`, `watch`, `mark`, `autocomplete`, `dedup`, `premiere`, `auth`, `jfhook`.
- `webui.py` + `static/index.html` — FastAPI control panel on `http://127.0.0.1:8767`.
- `run_watch*.bat/vbs`, `run_webui*.bat/vbs` — hidden autostart launchers
  (drop shortcuts to the `.vbs` files into `shell:startup`).
- `mikan_overrides.example.json` — optional `bgm_id → mikan bangumiId` map; copy
  to `mikan_overrides.json` only when a show's "no mikan match" banner persists
  and aliases can't resolve it (find the id in `mikanani.me/Home/Bangumi/<id>`).

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
  Two exceptions still count as "current": a bgm id in `pin_current_bgm_ids`
  (an explicit per-show override), and a cross-cour show still broadcasting (a
  半年番/年番 whose final scheduled episode airdate is today or later, per the bgm
  episode schedule). A long show that has finished airing is read-only again.

## Safety notes

- `config.local.json` holds all secrets and is gitignored; nothing sensitive
  is hardcoded.
- The Jellyfin prune step refuses to run if the source library is missing or
  empty (unmounted-drive protection) and aborts on implausibly large deletions.
- The web UI binds to 127.0.0.1 by default; set `webui_host: "0.0.0.0"` only
  on a trusted LAN (it has no authentication).
