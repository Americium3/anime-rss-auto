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
     ▲
     └─ untick "played" and both are undone
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
| **show resolution** | maps a bgm show to its mikan feed by searching mikan for the show's `name_cn` / `name`, then its bgm aliases (别名 / romaji) when those miss — mikan indexes release/original names, so a display 中文名 that differs from them (e.g. `正后方的神威` vs. mikan's `从后面来的神威先生`) still resolves through the romaji alias. Each query is retried tilde-stripped when the exact string finds nothing — mikan's search returns zero results for any query containing `～`, which would otherwise sink every `main ～subtitle～` style title. Every candidate is confirmed by matching the mikan page's bgm id, so a loose name never binds the wrong show. A stubborn mismatch that even aliases miss can be pinned in `mikan_overrides.json` (`bgm_id` → mikan `bangumiId`), which is consulted first. Any show that has **already aired** but still resolves to nothing is surfaced as a warning banner on the panel — a persistent state, not a one-shot event, so it clears itself once the show resolves — instead of failing silently | `unresolved_scan_enabled` / `mikan_overrides.json` |
| **manual resolve** | the "no mikan match" banner carries a paste box: drop a mikan link in and the show is bound by hand, no `mikan_overrides.json` editing. A `Bangumi` page link — or an `Episode` page link whose page names its bangumi — persists the override and subscribes through the normal pipeline (feed + rule + mikan sub, same folder conventions); an `Episode` link is also downloaded itself immediately (the exact release you picked, even if the rule's group filter would never match it). An `Episode` page with **no** bangumi backlink (one-shot releases: 冷番补完-style movies/specials that belong to no mikan bangumi and can never match RSS) or a raw magnet URI becomes a one-shot import: the magnet is handed straight to qB under the show's library folder, and `manual_imports.json` records `infohash → bgm subject` so mark-watched / jfhook / autocomplete claim the ruleless torrent exactly as if a rule had downloaded it (a subject with a single main episode resolves without an episode number in the file name — movie treatment). A 想看 show is promoted to 在看 on success, and the banner clears immediately. Also scriptable: `python anime_rss.py resolve --bgm-id N --url <link>` | always on (it's the banner's second button) |
| **prefer-variant** | some groups publish one episode in several variants — by source (Baha / CR / ABEMA / B-Global…) or by subtitle language (简日双语 / 繁日双语, also written JPSC / JPTC / CHS / CHT). Blacklisted sources (ABEMA, B-Global) are folded into every rule's `mustNotContain` so the feed rejects them and they never download; if more than one variant of the same episode does land, only the highest-priority one is kept after download and the rest are deleted (files included). Ranking is lexicographic across dimensions (source first, then language, then revision: Baha ＞ CR, simplified ＞ traditional, original ＞ V2/V3). The language tier is a set of synonym markers, so both CJK (简/繁) and Latin abbreviations (SC/TC/CHS/CHT/GB/BIG5, incl. glued forms like JPSC/JPTC) are recognized while mid-word false hits (disc/watch) are avoided. Revision is the lowest-priority tiebreaker: when the same episode lands as both an original and a re-release (`[V2]` / `04v2`), the original you already have is kept and the later revision is deleted — a lone V2 with no sibling is never touched. Untagged releases are left untouched, and only cours after the cutoff are affected. Run standalone with `python anime_rss.py dedup [--dry-run]` | `prefer_variant_enabled` / `source_blacklist` / `source_priority` / `lang_priority` |
| **no-raw guard** | a fallback subtitle group (not in `group_priority`, so no custom filter) would otherwise get an empty `mustContain` and grab anything in its mikan feed — including cross-posted streaming raws with no Chinese subtitle (e.g. a Netflix dual-audio rip carrying the show's English title). Two lines of defence: new rules for such groups require a Chinese-subtitle marker in the title (`cjk_sub_required`, a single `\|`-OR term so the feed rejects raw releases — `CR` and `Baha` are whitelisted too, since Crunchyroll and Bahamut 動畫瘋 rips carry official Chinese subs but tag the title with the platform, not the language, so groups that only mirror those platforms aren't blocked wholesale; the untranslated ABEMA/B-Global rips stay excluded via `source_blacklist`), and any downloaded torrent whose name matches a raw-platform tag (`hard_reject_tags`, e.g. `NF WEB-DL`) is deleted outright regardless of siblings. When the whitelist grows, existing fallback rules are upgraded in place on the next pass (old default strings are recognized by prefix, custom per-group filters are never touched). Both respect the season cutoff | `cjk_sub_required` / `hard_reject_tags` |
| **ANi grace fuse** | if the top-priority group hasn't published when a show first appears on mikan, wait N hours before locking a lower one (missed items are backfilled from the feed) | `ani_grace_hours` |
| **reconcile** | show moved to 看过/抛弃 → drop the qB rule (files kept); removed from collection entirely → unsubscribe + delete files. Two guards against false teardowns: a rule created earlier in the same pass is never a teardown candidate, and a split-cour show (one mikan entry shared by several bgm subjects) keeps its rule while **any** of those subjects is still 在看 — the mikan page may still identify the entry by the finished half, which reads as 看过 | `purge_dropped_files` |
| **season cutoff** | shows older than a cour cutoff are never touched — no adds, no deletes. A cross-cour show (半年番/年番) keeps being auto-managed while it is still broadcasting, decided from the authoritative broadcast schedule (bgm per-episode airdates: the show counts as current while its final scheduled episode airdate is today or later; AniList `status` is a fallback). Once it finishes airing it reverts to a manual old show — so a still-running 2-cour/year-long show is not frozen just because its start cour fell behind the cutoff, and a long show that finished seasons ago is never re-touched. Individual shows can also be pinned "current" by bgm id | `skip_before_season` / `pin_current_bgm_ids` |
| **split-cour continuation** | a fansub numbers a season straight through while bgm splits that season into two subjects, the second restarting at episode 1 — so ANi's `第四季 - 13` matches nothing: not the first subject, which stopped at 11, and not the second, whose own numbering calls it 2 and whose whole-series `sort` calls it 79. The episode reaches neither qB's stop nor bangumi, and the only trace is one skip line. When a number fits neither numbering of the subject the mikan page names, the episode is retried against the other subject that resolved onto the same mikan entry, offset by however many main episodes the first one had. Applied only when all of: both subjects resolved onto the same feed; the number is not already valid in the earlier subject (they overlap — both have an episode 5); it becomes valid in the later one once offset, at 1 or above; and the earlier subject finished broadcasting before the later one began — which also settles direction, so a mikan page naming the later half makes this decline rather than guess | always on |
| **mark-watched** | you pause a finished torrent in qB → that episode is marked watched on bgm (transition-based, never bulk-marks) | `mark_watched_enabled` |
| **autocomplete** | once every main-story episode of a 在看 show is marked watched on bgm (by you, or by mark-watched / jfhook), the whole show is auto-promoted to 看过 — which reconcile then acts on (drop the qB rule, keep mikan + files), firing a panel banner with a one-click "rate & review on bgm" link straight to the show's subject page. Two guards keep it honest: it reads per-episode collection status (not the unreliable `eps` count), and it only fires once the finale has aired (every 本篇 airdate ≤ today), so a still-airing show whose listed episodes you happen to have all watched is never collected early. Respects the season cutoff like every other write pass. Run standalone with `python anime_rss.py autocomplete [--dry-run]` | `autocomplete_watched_enabled` |
| **Jellyfin mirror** | hardlinks new episodes into a `<mirror>\<cour>\<show>\Season 01\` tree (0 extra bytes, seeding untouched) | `jellyfin_mirror_enabled` |
| **Jellyfin autolib** | new cour folder → auto-create a Jellyfin library with a generated cover, newest-first ordering | `jellyfin_autolib_enabled` |
| **Jellyfin prune** | cour deleted from the source library → mirror + Jellyfin library removed; and per-file: a video whose source file no longer exists (e.g. a variant removed by prefer-variant or a subtitle-group switch) has its orphaned mirror hardlink pruned so Jellyfin stops showing the stale version (multiple safety gates: aborts if source root missing/empty, skips a show with zero source videos) | `jellyfin_mirror_delete_enabled` |
| **Jellyfin empty-series self-heal** | a mirror-rebuild race can leave a series looking empty in Jellyfin, so playback fails with "Unable to find a valid media source" → one API call per pass finds series with 0 episodes but video on disk and recursively refreshes them (zero cost for healthy shows, guarded against mid-scan storms) | `jellyfin_heal_empty_enabled` |
| **Intro Skipper trigger** | an episode that lands at 21:00 would otherwise have no skip button until the plugin's next nightly sweep, so hardlinking a new file into the mirror kicks the Intro Skipper detection task instead. Three things keep that from becoming a CPU tax: it first waits for the library scan the mirror just started to reach `Idle` (triggering mid-scan would fingerprint a library that does not contain the new episode yet — a long run that finds nothing), it never fires while the task is already running, and it is throttled to one trigger per `intro_skip_min_gap_minutes`. The wait runs off-thread, so the rest of the pass is not held up. Scope is bounded by `analyze_skip_before_season`: every mirror folder older than that cour — plus non-cour folders like `Ancient` — is written into the plugin's own `PathExclusions`, which is a **blacklist** of path roots, so a cour created later is analysed by default and needs no maintenance. The list is maintained from the same place that auto-creates the cour libraries, and any entry you added yourself outside the mirror is left alone. Run standalone with `python anime_rss.py analyze [--dry-run]` | `intro_skip_analyze_enabled` / `analyze_skip_before_season` |
| **jfhook** | Jellyfin Webhook plugin → finished an episode → stop seeding it + mark watched on bgm. It undoes itself too: unticking "played" in Jellyfin or Findroid resumes seeding that episode and puts its bgm episode status back to 未收藏 (type 0), so a misclick is one more click to fix rather than two manual repairs. Both directions are classified from the same event (`UserDataSaved` + `SaveReason=TogglePlayed`, on `Played` true/false — an explicit false only, since the webhook template renders inapplicable fields as `""` and "absent" must not read as "unticked"), and a `PlaybackStop` that merely fell short of completion is someone stopping halfway, never an undo. The undo path resolves through the **same** `resolve_torrent_target` collar as the forward one, so the `Ancient` hard gate and the `skip_before_season` red line are inherited rather than reimplemented, and each leg is independent — a qBittorrent failure still lets the bgm write through. Undo is the gentler direction (resuming a torrent costs upload traffic; a bgm mark can be re-applied), so it runs unthrottled by default; marking a whole **series** played cascades one webhook per episode, and `jfhook_reverse_rate_limit_per_min` puts a per-minute ceiling on the undo direction only if you want one. Debug with `python anime_rss.py jfhook --dry-run` | `jfhook_port` / `jfhook_reverse_enabled` / `jfhook_reverse_rate_limit_per_min` |
| **event ledger** | every episode that lands in the library appends one entry to an append-only `events.json` (monotonic `seq`, atomic write, retention `max(newest 500, last 30 days)`), as does every new show subscription — deduped per (show, group) against the entries still on file, and skipped entirely when the same pass's premiere banner already announced the auto-subscription, so one premiere reads as one card, not three. Served at `GET /api/events?after_seq=…&limit=…` — ascending by seq, with `hasMore` — so an external message centre can follow it with a cursor and miss nothing, including across its own downtime (the file can be read directly too). The entry is written when the episode is hardlinked into the library, so "landed" means it is actually there to watch rather than merely queued, and that pass is idempotent, so each episode is announced exactly once. A first run backfills the previous 48 hours from the mirror, so the ledger is not empty on day one | always on |
| **web UI** | local dashboard, built as one piece of furniture: a Victorian writing desk in a study. The centre of the page is the desk's writing surface — green skiver leather let into the timber, a binder's double gilt rule tooled around it, leather blotter corners, the binding fold of the register lying open down the middle, and the ghost of the last cover you opened printed faintly into the leather. A bookcase cornice runs across the top of the window at every width. Every show stands on that surface as a bound volume on its own brass-bracketed shelf — the poster is the cover face of a three-faced box whose spine carries a head cap, sewn bands, a stamped label panel and sometimes a tooled medallion, with cloth, foil and furniture chosen from the show's own id so no two volumes are bound alike; hovering squares a volume up and lifts it off its plank, clicking opens that show's full card in the middle of the screen. Each cour break is a leather shelf-edge label in a brass holder with the shelf rail running out of it. Past 1800px the desk grows its two pedestals: the left carries the archive — every finished show standing edge-on as a spine with its title set vertically down it, its tail band tinted by its bangumi rating, re-shelvable by season, score or year (deep-linkable as `?sort=score`) and clickable straight through to the same card; the right carries the duty desk — paired local/Tokyo desk clocks in brass bezels above the next 48 hours of broadcasts (episode 1's AniList slot walked forward a week at a time, and only for shows the backend still reports as airing, so a finished show is never given an imaginary broadcast), a ruled accession ledger of the episodes that most recently landed with today's entries rubber-stamped at their own angles, a machine panel of domed indicator lamps reading qBittorrent, the last sync, the subscription count and how much of the download queue has finished, and a census panel plotting the archive itself — how many volumes are on the shelves, the span of premiere years they cover, their mean bangumi rating, and one brass column per year across that whole span with the gaps drawn as gaps and the tallest year carrying its own figure; clicking a column re-shelves the archive by date and lights that year's spines on the opposite rail. An Emeralite lamp hangs over each pedestal — cased green glass, brass harp and pull chain, an opal mouth, a soft-edged cone and an elliptical pool of light on the timber below — brass gallery-rail uprights run the full height of the room on both sides, a herringbone floor is held at the foot of the window, and the furniture drifts against that structure as you scroll while the lamps and uprights stay mounted. Below 1800px the pedestals are not built, but the desk stays: the cornice, the leather, the gilt tooling and the lamplight are all there down to 380px. Light theme is the same room at a different hour rather than an inversion — pale oak, parchment and daylight through the window, with the lamps simply off. The only two accent colours are the two inks a desk like this holds — the red its margins are ruled in and the blue-black its entries are written in — and the eight cour colours are dyed book cloths rather than a screen palette, measured on their own tint over leather, card and drawer front in both hours. Type is set in two bundled variable faces and no third — EB Garamond for the Latin and Noto Serif SC for the CJK, both SIL OFL, both served from `static/fonts/` rather than named, because the CJK serif this page wants is not on a stock Windows install and the fallback there is SimSun. There is no sans anywhere: the room sets its words in a book face and its figures on a typewriter, and those are its only two voices. The rem the sheet is measured in is grown a tenth to compensate for the Garamond's smaller x-height; nothing is set below 12px, no Chinese below 13px, spine titles are the largest they have been, and a Normal/Large/Larger control in the settings drawer rescales the whole sheet at once (deep-linkable as `?size=l`). All bgm-marked shows are grouped by collection type (在看/想看/看过/搁置/抛弃) with a type filter plus a live title search box, and theme, language, subgroup priority and the daemon log live behind a settings gear rather than in the chrome. Each card title carries a bangumi and a mikan favicon button linking out to that show's pages (mikan deep-links when its feed is known, else falls back to a mikan title search) and follows the UI language (English/romaji titles via AniList in the English UI), every list (except the timetable) split into cour blocks with a full-width color-coded season divider ahead of each block and season-unknown shows sinking to the end — Watching and Plan-to-watch run oldest cour first (a backlog to clear), the Completed / On-hold / Dropped tabs newest cour first; inside a block the Completed tab orders by most recently marked (it reads as a viewing history) while the other tabs order by premiere time (earliest first), live output of a manual sync in the log panel (kept readable after the pass ends; failures toast and auto-open the log), a weekly timetable tab printed as the study's own stationery — a sheet of buff form stock inside a printed double border, a masthead with the cour and your timezone over a heavy-and-hair rule, one ruled day column per weekday with its date at the head, a red margin rule down each column with the times set into it, every show pasted in as a small photographic print at its own angle (squared up when you hover it), today struck with a rubber stamp in the same red the accession ledger is ruled in, the current moment marked by a proofreader's caret in the margin, and a printed foot carrying the week's slot count; watching + plan-to-watch shows, air status shown by the time's colour, a premiere-status filter and a "this season only" toggle that limits the sheet to the current cour — pinned and still-airing cross-cour shows count as current and stay visible, columns run from today, grab any empty part of the sheet to drag it left/right, airing slots in **your** local timezone, each entry also carries its full localized premiere date/time), the watching grid shows each show's weekly update slot (weekday + local time) on the card and has a weekday sub-filter (one chip per weekday, with live counts) to narrow it to a single update day, a 半年番/年番 badge on every multi-cour show's card (classified by the broadcast-schedule span — first-to-last episode airdate — not raw episode count), dark/light theme with follow-system default, premiere banners stay for a week unless dismissed (with a dismiss-all button), "no mikan match" warning banners for already-aired shows the resolver couldn't map to a feed (persist until the show resolves or you dismiss them, with a dismiss-all button; each carries a paste box that resolves the show by hand from a mikan link or magnet — see manual resolve), per-show premiere time in **your** local timezone (via AniList), grace countdowns, switch subtitle group (deletes the old group's downloaded files for that show and re-downloads the whole season from the new group, episode for episode — irreversible, guarded by the season cutoff and confirmed with a dialog), per-show "n/m episodes ready to watch" summary, color-coded season badges (8 colors cycling every 2 years), bangumi community rating badge on every card, tinted red (worst) through green (best) by the score, selected tab persists and is deep-linkable (`?tab=schedule&theme=light&lang=en`), a full-screen opening sequence while the dashboard loads — a shelf plank draws itself, bound volumes rise onto it whole — cover, spine and board arriving as one object, never a cover first and a book afterwards — the wordmark settles glyph by glyph, and the curtain wipes up once the desk and the shelves both have their data, so the whole room is standing when it lifts rather than the archive filling in as a second wave afterwards (it will not hold more than four seconds for the archive) (no fake progress bar; the volumes turn amber and it retries every 5 s if the backend is down; `?boothold` freezes it for screenshots), all timestamps localized, offline / qBittorrent-down / AniList-down indicators in the header (airing times come from AniList, so when it stops answering the timetable says so rather than quietly filing every show under "time TBA"), phone-friendly layout with proper touch targets, keyboard/screen-reader accessible, subtle motion throughout (staggered card waterfalls and an opening choreography as the boot screen lifts, an ambient drifting background glow, a pulsing “now” line marking the current time in today's timetable column, theme cross-fades, and hover/press micro-interactions — entry animations replay only on user actions, never on background polls), reduced-motion aware. **Everything on the desk is worked rather than read.** The centrepiece is *the sounding*: bangumi's per-episode airdates, qBittorrent's per-episode torrents and your own per-episode watched marks are drawn as three tracks on one scale (`GET /api/progress`), with the broadcast as the datum and the reading taken *down* from it — being five episodes behind is a depth of five and the needle hangs that far below the surface, because a still-running show has no total for a bar to fill. Each episode is its own hoverable cell reading out its state (not aired / aired / downloading / downloaded / seeding / watched) and its airdate, and hovering one walks the needle back to it like a transport, swimming home when you leave. Scale: at most 26 episodes are drawn one cell per episode; past that the newest 12 keep full width and the head shares the rest with a 2px floor, because a long-runner's tail is where every decision still lives and a scrollbar would hide it. Depth is square-rooted against a full cour, so 1 behind is plainly not level while 12 and 20 are both simply deep. Hovering the instrument releases drift chevrons pointing where the needle is about to go — up toward the line when episodes are downloaded and waiting, up but in the waiting colour when you are behind with nothing in hand, down and away when you are level but the next broadcast is going to pull ahead. The same reading is taken in one line per show on a *Progress* panel on the right rail, deepest first. Beyond that: a cross-highlight bus (point at a show anywhere — shelf, archive spine, timetable slip, 48-hour list, rail — and every other appearance of it lights at once), a three-state sort control on the grid and the archive (press for ascending, again for descending, again to restore the tab's own order) with FLIP-animated reordering, clickable cour plates and season badges that narrow the grid to that cour, a hover peek that pulls a volume half off the shelf with its sounding, ready count, next broadcast and subgroup without opening anything, a subgroup picker that replaced the bare dropdown and states each candidate's name, id, where the automation ranks it and a standing warning that choosing one destroys files, a timetable whose now-line reads out the clock and the wait until the next slot, whose day columns light under the pointer and whose heads narrow the shelf to that weekday, plus a time-of-day band filter, and keyboard navigation (←/→ walk the tabs, 1–9 jump, `/` searches, Esc closes one layer at a time). Each panel draws inside its own boundary: one that throws prints a bilingual notice on **its own card** and logs the stack while every other panel still draws, instead of taking the rest of the room down silently with it | `webui.py` |

Each module is independently toggleable in config — take what you need.

## Files

- `anime_rss.py` — everything above except the panel; stdlib only, single file.
  Subcommands: `list`, `plan`, `apply`, `prune`, `sync`, `watch`, `mark`, `autocomplete`, `resolve`, `dedup`, `premiere`, `analyze`, `auth`, `jfhook`.
- `webui.py` + `static/index.html` — FastAPI control panel on `http://127.0.0.1:8767`.
  The panel is one self-contained document: its CSS and JS are inline, so there are no
  separate asset files that could go out of step with each other. `webui.py` serves it
  with `Cache-Control: no-cache` (revalidate, do not guess a freshness lifetime) and
  rewrites every `/static/…` reference to carry that file's mtime, since changing the URL
  is the only cache bust that works on a copy a browser already holds. The shell also
  carries its build stamp on `<html data-build>`; a tab left open across an edit notices
  (`GET /api/version`) and offers a reload, which is the one case a version string in an
  asset URL cannot fix — the stale document is what would have to ask for the new one.
- `static/_probe.html`, `static/_states.html` — the validation bench (see below). They live
  under `static/` because the assertions have to run same-origin with the panel.
- `scripts/` — `probe.ps1` (assertions), `shot.ps1` (screenshots), `states.ps1` (screenshots
  with hover/focus painted in), `bench_server.py` and `_bench.ps1` (shared plumbing).
  Screenshots land in `scripts/shots/`, which is gitignored.
- `static/fonts/` — the two bundled variable faces the panel is set in (EB Garamond, Noto Serif SC), both SIL OFL; see `LICENSE.txt` there.
- `static/brand/` — the app's mark in every format anything asks for. Generated, never hand-edited: the marks for all five local services are drawn by one script, kept at `icons/gen.py` in the Atrium repo.
- `run_watch*.bat/vbs`, `run_webui*.bat/vbs` — hidden autostart launchers
  (drop shortcuts to the `.vbs` files into `shell:startup`).
- `mikan_overrides.example.json` — optional `bgm_id → mikan bangumiId` map,
  consulted first by the resolver. The panel's manual-resolve paste box writes
  this file for you; hand-edit it only for scripted/offline use (find the id in
  `mikanani.me/Home/Bangumi/<id>`).
- `manual_imports.json` — one-shot import ledger (auto-created, gitignored):
  `infohash → bgm subject` for panel-imported torrents that have no qB rule to
  answer for them, so mark-watched / jfhook can still claim them.
- `events.json` — the automation ledger (auto-created, gitignored). Append-only,
  so deleting it loses history rather than rebuilding it; consumers detect the
  restarted `seq` and resync from what remains.
- Runtime caches (auto-created, gitignored, safe to delete — rebuilt next pass):
  `mikan_resolve_cache.json` (bgm_id → mikan resolution), `episode_span_cache.json`
  (per-episode airdate schedule, shared with the panel), `subject_season_cache.json`
  (immutable air cour), `heal_pending.json` (a flag for the Jellyfin empty-series scan),
  `intro_skip_state.json` (when the Intro Skipper task was last triggered — losing it
  costs one extra trigger, nothing more).

## Validating the panel

Three reusable scripts, all driven by a fixture dataset held inside `index.html` rather
than by the live panel:

```powershell
pwsh scripts\probe.ps1                      # assertions: en+zh x dark+light, plus a fault run
pwsh scripts\probe.ps1 -Lang zh -Theme dark # one combination
pwsh scripts\shot.ps1                       # screenshots, entry animations frozen
pwsh scripts\shot.ps1 -Tab schedule -Width 1400
pwsh scripts\states.ps1                     # screenshots with hover/focus states painted in
pwsh scripts\states.ps1 -Scene gauge        # shelf | gauge | long | timetable | picker | all
```

`probe.ps1` serves `static/` from a throwaway port, opens `_probe.html` in headless Chrome,
and reads back the assertions it wrote. The probe drives the panel with real pointer and
keyboard events inside a same-origin iframe and checks that state actually changed — the
needle moves to the episode under the pointer, a sort key returns to the default order on
its third press, hovering one appearance of a show lights the others, the console stays
silent. **It never opens the live panel.** The frame is loaded with `?fixture`, which
replaces `fetch` with a canned dataset and *refuses* every non-GET, because each mutating
route here does something irreversible (deleting a show's files, ending a grace window,
writing a bangumi collection) and a test run must be unable to reach one by accident.

The last pass runs with `?fault=upcoming`, which makes one panel throw on purpose, and
asserts that it prints a notice on its own card and that every panel after it still draws.

`states.ps1` exists because a plain screenshot cannot show a hover effect: nothing is under
the pointer, so every `:hover` rule is inert. It dispatches real events for the script-driven
states and rewrites the page's own `:hover` / `:focus` rules onto attributes for the rest,
so a hover rule added later is covered without anyone maintaining a list.

Both screenshot scripts pass `--force-prefers-reduced-motion`. Without it headless captures
the first frame of an entry animation that will never advance, so every card sits at opacity
0 and the shot looks like a page that failed to render.

## Performance

A steady-state 5-minute pass completes in **well under a second** (~108s → ~0.8s on the
reference setup). Every network-heavy pass shares one per-run context, so each show is
resolved / spanned / typed at most once; mikan resolutions persist across runs, and a show
that already has a qB rule needs **zero** mikan calls (its `bangumiId` is read straight from
the rule's feed). A short-lived negative cache stops re-searching 想看 movies/specials mikan
will never index. The Jellyfin empty-series `/Items` scan and the library-folder check are
gated to run only when the mirror changed or every N passes, not every pass. Knobs:
`resolve_ttl_seconds` (default 24h — resolved-identity refresh; a ruled show never refetches),
`resolve_negative_ttl_seconds` (default 30 min — how long an unindexed show is skipped before
re-search; set `0` to disable and re-search every pass), `heal_backstop_passes` (default 12 —
the quiet-pass backstop period for the Jellyfin scans). Shows in ANi grace or with a manual
override always refetch their subgroup roster, so a late higher-priority group is never missed.

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
- A show premiering within `cour_rollover_days` (default 14) of the next cour's
  first day is filed under that next cour, not the calendar quarter its date falls
  in — an end-of-September premiere is an autumn show that got a head start, and
  all but one of its episodes air in the autumn cour. Set to 0 for plain calendar
  quarters. `python test_season.py` covers the rule. The `skip_before_season`
  cutoff below deliberately does NOT see this rollover: it compares the plain
  calendar cour, so which old shows are hands-off never changes with a display
  rule.
- Destructive actions (deleting files/rules) only ever apply to shows from
  `skip_before_season` onward; older shows are strictly read-only to the tool.
  Two exceptions still count as "current": a bgm id in `pin_current_bgm_ids`
  (an explicit per-show override), and a cross-cour show still broadcasting (a
  半年番/年番 whose final scheduled episode airdate is today or later, per the bgm
  episode schedule). A long show that has finished airing is read-only again.
- Every one of those judgements is an answer from bgm, so the tool separates "bgm
  answered, and the answer is no" from "bgm did not answer". A 404 is an answer and is
  cached; a 5xx, a timeout or a rate limit is not, and is never written to a cache.
  When a pass cannot get an answer it skips the show and retries next round — an
  unreachable bgm is not evidence about a show.

## Safety notes

- `config.local.json` holds all secrets and is gitignored; nothing sensitive
  is hardcoded.
- No cache entry is allowed to record a failed lookup as a fact. This matters most
  for `subject_season_cache.json`, which has no expiry (an air cour is immutable): a
  cour wrongly recorded as unknown reads as "current", which is what makes an old
  hands-off show eligible for teardown. `python test_bgm_outage.py` covers the
  behaviour under a simulated outage.
- The Jellyfin prune step refuses to run if the source library is missing or
  empty (unmounted-drive protection) and aborts on implausibly large deletions.
- The web UI binds to 127.0.0.1 by default; set `webui_host: "0.0.0.0"` only
  on a trusted LAN (it has no authentication).
